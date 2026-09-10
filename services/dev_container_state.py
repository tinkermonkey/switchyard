"""
Dev Container State Management

Tracks the state of project development container images:
- unverified: Default state for new projects
- in_progress: dev_environment_setup agent is working
- verified: Docker image built and tested successfully
- blocked: Unable to build working image; repair cycle stops retrying
- changes_needed: Verifier could not confirm a required fix; repair cycle retries
"""

import yaml
import logging
import subprocess
import os
from pathlib import Path
from typing import Dict, Optional
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)

# Label baked into switchyard's base Dockerfile (see repo-root Dockerfile) and
# inherited by every project's <project>-agent:latest image via `FROM
# switchyard-orchestrator:latest`. A project's own docker-compose.yml can
# happen to name one of its own services "agent", which — with no explicit
# `image:` tag — collides with this exact tag under Docker Compose's default
# `<compose-project>-<service>` naming. That silently swaps out the agent
# environment image for something unrelated (see incident: phone-home's own
# "agent" microservice, unrelated to this environment, overwrote
# phone-home-agent:latest and caused every senior_software_engineer container
# launch to boot the wrong entrypoint). Checking for this label — rather than
# just tag existence — catches that class of collision regardless of which
# unrelated image ends up holding the tag.
SWITCHYARD_AGENT_ENV_LABEL = "io.switchyard.agent-environment"

# How long a pending-operation marker is believed before it is treated as
# abandoned. Bounded by what can legitimately keep one alive: the only writer is
# /api/projects/<p>/rebuild-image, whose worker clears it the moment it takes
# the dev_container_build lock and gives up waiting after
# REBUILD_ENDPOINT_LOCK_TIMEOUT_SECONDS (900s), plus slack for a worker thread
# that is merely slow to be scheduled.
#
# Needed because that worker is a daemon thread and is therefore killed
# outright at interpreter shutdown: a SIGTERM or a container restart during the
# lock wait leaves the marker on disk with nobody coming back to clear it, and
# mcp/server.py's get_image_build_status then masks the project's real image
# state with "status": "queued" forever, telling the calling agent a rebuild is
# waiting that will never start (#152 review).
PENDING_OPERATION_MAX_AGE_SECONDS = 1200.0

# How long any read or write of a project's state file waits for that file's own
# lock. Bounded rather than blocking because get_status() is reachable from the
# orchestrator's event loop (validate_task_can_run), and utils.file_lock's own
# guidance is that such call sites must not risk an unbounded wait. The critical
# section is one small YAML read-modify-write, so 10s of contention means
# something is wrong rather than merely busy, and both paths fall back to the
# same behaviour an unreadable/unwritable file already had.
STATE_LOCK_TIMEOUT_SECONDS = 10


class DevContainerStatus(Enum):
    """Status of a project's development container"""
    UNVERIFIED = "unverified"  # Default for new projects
    IN_PROGRESS = "in_progress"  # Setup agent running
    VERIFIED = "verified"  # Image built and tested
    BLOCKED = "blocked"  # Failed to build working image; repair cycle stops retrying
    CHANGES_NEEDED = "changes_needed"  # Verifier could not confirm a required fix
    # (distinct from BLOCKED: repair_cycle's env-rebuild sub-cycle treats this as
    # retryable rather than terminal — see _run_env_rebuild_sub_cycle)


class DevContainerStateManager:
    """Manages development container state for projects"""

    def __init__(self, state_dir: Path = None):
        """Initialize dev container state manager"""
        if state_dir is None:
            # CRITICAL: Use absolute path to orchestrator's state directory
            # This prevents state from being created inside project directories when
            # agents execute with project working directory
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "dev_containers"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"DevContainerStateManager initialized with state_dir: {state_dir}")

    def get_state_file(self, project_name: str) -> Path:
        """Get the state file path for a project"""
        return self.state_dir / f"{project_name}.yaml"

    def get_status(self, project_name: str) -> DevContainerStatus:
        """
        Get the current status of a project's dev container

        Args:
            project_name: Name of the project

        Returns:
            Current DevContainerStatus
        """
        try:
            status_str = self._read_state(project_name).get('status', 'unverified')
            return DevContainerStatus(status_str)
        except Exception as e:
            logger.error(f"Failed to read dev container status for {project_name}: {e}")
            return DevContainerStatus.UNVERIFIED

    def set_status(
        self,
        project_name: str,
        status: DevContainerStatus,
        image_name: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        """
        Set the status of a project's dev container

        Args:
            project_name: Name of the project
            status: New status
            image_name: Docker image name (e.g., "context-studio-agent:latest")
            error_message: Error message if status is BLOCKED
        """
        updates = {
            'status': status.value,
            'updated_at': datetime.now().isoformat(),
        }

        if image_name:
            updates['image_name'] = image_name

        if error_message:
            updates['error_message'] = error_message
        elif status != DevContainerStatus.BLOCKED:
            # Clear error message if status changed from blocked
            updates['error_message'] = None

        if self._merge_state(project_name, updates):
            logger.info(f"Updated dev container status for {project_name}: {status.value}")

    def set_pending_operation(self, project_name: str, operation: str) -> None:
        """
        Record that `operation` has been REQUESTED for this project but has not
        started yet, without touching `status`.

        Exists because an operator-triggered rebuild does not begin when the
        request is accepted: services/observability_server.py's
        /api/projects/<p>/rebuild-image answers {"success": true, "triggered":
        true} immediately and its worker thread then waits up to
        REBUILD_ENDPOINT_LOCK_TIMEOUT_SECONDS for the dev_container_build lock,
        marking IN_PROGRESS only once it has it (a rebuild that never gets the
        lock must not leave the project claiming a build is under way). This
        state file is that request's ONLY feedback channel -- mcp/server.py's
        get_image_build_status reads it directly -- so for the whole wait a
        caller polling it read the project's PRE-EXISTING status, commonly
        'verified', and concluded the rebuild had already finished (#152
        review). This gives the waiting phase its own representation instead of
        leaving the previous status to speak for it.

        Deliberately NOT a DevContainerStatus value: 'queued' is orthogonal to
        the image's actual state (the image really is still verified while a
        rebuild waits), and every `status ==` branch in the codebase would have
        to learn about it.
        """
        self._merge_state(
            project_name,
            {
                'pending_operation': operation,
                'pending_operation_at': datetime.now().isoformat(),
            },
        )

    def clear_pending_operation(self, project_name: str) -> None:
        """
        Clear the marker set by set_pending_operation() -- the requested
        operation has either started (and now owns `status`) or been dropped.
        """
        self._merge_state(project_name, {'pending_operation': None, 'pending_operation_at': None})

    def get_pending_operation(self, project_name: str) -> Optional[Dict[str, str]]:
        """
        The operation requested but not yet started for this project, as
        {'operation': ..., 'requested_at': ...}, or None.

        A marker older than PENDING_OPERATION_MAX_AGE_SECONDS is reported as
        absent: see that constant for why one can be left on disk with nobody
        coming back to clear it.
        """
        state = self._read_state(project_name)

        operation = state.get('pending_operation')
        if not operation:
            return None

        requested_at = state.get('pending_operation_at')
        if self._is_stale_pending_operation(requested_at):
            # Debug, not warning: every /api/projects poll and every
            # get_image_build_status call comes through here.
            logger.debug(
                f"Ignoring abandoned '{operation}' marker for {project_name} "
                f"(requested {requested_at}, older than "
                f"{PENDING_OPERATION_MAX_AGE_SECONDS:.0f}s)"
            )
            return None

        return {'operation': operation, 'requested_at': requested_at}

    @staticmethod
    def _is_stale_pending_operation(requested_at: Optional[str]) -> bool:
        """True if a marker requested at `requested_at` can no longer be live."""
        if not requested_at:
            # No timestamp to age against -- written by an older version, or the
            # file was hand-edited. Treat it as stale rather than as immortal.
            return True
        try:
            age = (datetime.now() - datetime.fromisoformat(requested_at)).total_seconds()
        except Exception:
            return True
        return age > PENDING_OPERATION_MAX_AGE_SECONDS

    def clear_stale_pending_operations(self) -> int:
        """
        Clear every abandoned pending-operation marker on disk, returning how
        many were removed.

        Called from services/observability_server.py's start_observability_server
        alongside the orphaned-lock recovery, for the same reason: that process
        is the only writer of these markers and is a docker-compose singleton,
        so any stale marker present at its startup belongs to its own dead
        predecessor. get_pending_operation() already refuses to report one, but
        only this removes it from the file.
        """
        cleared = 0
        for state_file in sorted(self.state_dir.glob('*.yaml')):
            project_name = state_file.stem
            state = self._read_state(project_name)
            if not state.get('pending_operation'):
                continue
            if not self._is_stale_pending_operation(state.get('pending_operation_at')):
                continue
            logger.warning(
                f"Clearing abandoned '{state['pending_operation']}' marker for "
                f"{project_name} (requested {state.get('pending_operation_at')}) - "
                f"the thread that owned it never came back"
            )
            self.clear_pending_operation(project_name)
            cleared += 1
        return cleared

    def set_last_operation_error(self, project_name: str, message: str) -> None:
        """
        Record that a requested operation could not be carried out, WITHOUT
        touching `status`.

        The channel the rebuild endpoint uses to report a rebuild that never
        started. It deliberately is not a status: lock contention is not a
        verdict on the image, and there is no status value that can honestly
        express it -- BLOCKED is terminal (validate_task_can_run gives the
        terminal statuses no staleness escape), so writing it over a VERIFIED,
        UNVERIFIED or CHANGES_NEEDED project turns a healthy or self-healing
        state into one that refuses every task for that project until a human
        intervenes (#152 review). Being a non-`status` field is also what lets
        this be written without the dev_container_build lock: it cannot clobber
        a live holder's verdict, so it does not need to wait for one.
        """
        self._merge_state(
            project_name,
            {
                'last_operation_error': message,
                'last_operation_error_at': datetime.now().isoformat(),
            },
        )

    def clear_last_operation_error(self, project_name: str) -> None:
        """
        Clear the record set by set_last_operation_error().

        Not the only thing that clears it, and no longer the load-bearing one:
        _merge_state() drops the record on any write carrying a `status`, so
        every set_status() from every path supersedes it (see _merge_state).
        This stays for the rebuild endpoint, which retracts an earlier request's
        drop explicitly the moment it takes the lock rather than leaving it to
        the IN_PROGRESS mark that follows -- the retraction is the point there,
        not a side effect of the next write.
        """
        self._merge_state(
            project_name, {'last_operation_error': None, 'last_operation_error_at': None}
        )

    def get_last_operation_error(self, project_name: str) -> Optional[Dict[str, str]]:
        """
        The last requested operation that could not be carried out, as
        {'error': ..., 'at': ...}, or None.

        Reported only until something writes a `status` for this project, which
        drops the record (see _merge_state) -- so what comes back here always
        post-dates the project's current verdict rather than describing a
        rebuild that has since happened. 'at' is part of the payload, not
        decoration: every consumer (the /api/projects payload, the web UI's
        DevContainerStatus, mcp/server.py's get_image_build_status) shows it
        alongside the message so a reader can date the drop.
        """
        state = self._read_state(project_name)

        message = state.get('last_operation_error')
        if not message:
            return None
        return {'error': message, 'at': state.get('last_operation_error_at')}

    def get_state(self, project_name: str) -> Dict:
        """
        The project's whole state file as a dict, {} if absent or unreadable.

        For callers that want several fields at once (e.g. /api/projects
        building its dev_container payload) without re-reading the file per
        accessor -- and without re-deriving the locked read below.
        """
        return self._read_state(project_name)

    def _read_state(self, project_name: str) -> Dict:
        """
        The project's state file as a dict, {} if absent or unreadable.

        Taken under the state file's own lock so a reader never sees the
        half-written file a concurrent _merge_state() is producing -- the same
        reason PipelineLockManager._read_yaml_lock_only holds its lock across
        the read.
        """
        from utils.file_lock import file_lock

        state_file = self.get_state_file(project_name)

        if not state_file.exists():
            return {}

        try:
            with file_lock(
                self._state_lock_file(state_file),
                timeout=STATE_LOCK_TIMEOUT_SECONDS,
                enforce_timeout=True,
            ):
                if not state_file.exists():  # Check again inside lock
                    return {}
                with open(state_file, 'r') as f:
                    return yaml.safe_load(f) or {}
        except Exception as e:
            logger.error(f"Failed to read dev container state for {project_name}: {e}")
            return {}

    @staticmethod
    def _state_lock_file(state_file: Path) -> Path:
        """
        Guard file serializing every read and write of `state_file`.

        Nothing inside a _read_state()/_merge_state() critical section may call
        back into this class: utils.file_lock is not re-entrant and raises
        ReentrantFileLockError on a nested same-thread acquire, which both
        methods swallow via their `except Exception` and degrade to {} / False
        -- i.e. get_status() would start reporting UNVERIFIED for a VERIFIED
        project rather than raising. No path nests today; the critical sections
        are deliberately kept to the one YAML read-modify-write for that reason.
        """
        return state_file.with_suffix(state_file.suffix + '.lock')

    def _merge_state(self, project_name: str, updates: Dict) -> bool:
        """
        Read-modify-write the project's state file, applying `updates` and
        deleting any key whose new value is None. Every writer of this file goes
        through here -- set_status(), the pending-operation markers and the
        last-operation-error record -- so that a write which owns only some of
        the keys cannot carry the others backwards.

        Held under the state file's own lock for the whole read-modify-write,
        because "every writer is inside the dev_container_build lock" is not
        true of this file and cannot be made true: the pending-operation marker
        is deliberately set from the rebuild endpoint's REQUEST thread, before
        the lock wait even starts, precisely so it is visible to the first poll
        (#152 review). Without this lock that unlocked writer's blind whole-file
        rewrite could land on top of a set_status() the other container made in
        between and silently revert it -- a freshly VERIFIED image reading back
        as UNVERIFIED, which refuses every task for the project. The lock is
        cross-process (fcntl on the shared bind mount), which matters because
        the observability server and the orchestrator are separate containers.

        Any write carrying a 'status' also drops the last-operation-error
        record. That record says a requested operation never ran, and it is the
        one field here with no owner coming back to retract it: its only
        explicit clear is the rebuild endpoint's, which fires only when ANOTHER
        rebuild is requested and gets the lock, and unlike the pending-operation
        marker it has no age-out. So a rebuild dropped on lock contention was
        reported forever -- an orange "rebuild never started" line under a green
        "verified" badge, and a "the rebuild has to be requested again" payload
        to every agent polling get_image_build_status -- on a project some other
        path had since rebuilt and verified (#152 review). A real verdict
        genuinely supersedes it, so writing one retracts it here, where every
        status writer already passes.

        Returns True if the file was written.
        """
        from utils.file_lock import file_lock

        state_file = self.get_state_file(project_name)

        try:
            with file_lock(
                self._state_lock_file(state_file),
                timeout=STATE_LOCK_TIMEOUT_SECONDS,
                enforce_timeout=True,
            ):
                if state_file.exists():
                    try:
                        with open(state_file, 'r') as f:
                            state = yaml.safe_load(f) or {}
                    except Exception as e:
                        logger.warning(f"Failed to read existing state, creating new: {e}")
                        state = {}
                else:
                    state = {}

                for key, value in updates.items():
                    if value is None:
                        state.pop(key, None)
                    else:
                        state[key] = value

                if 'status' in updates:
                    # See the docstring: a verdict retracts the record of an
                    # operation that never produced one.
                    state.pop('last_operation_error', None)
                    state.pop('last_operation_error_at', None)

                with open(state_file, 'w') as f:
                    yaml.dump(state, f, default_flow_style=False)
                return True
        except Exception as e:
            logger.error(f"Failed to save dev container state for {project_name}: {e}")
            return False

    def get_status_updated_at(self, project_name: str) -> Optional[datetime]:
        """
        Get the timestamp of the last status update for a project's dev container.

        Used to detect a status stuck at IN_PROGRESS with no forward progress (e.g. the
        setup/verifier task died without ever resolving to a terminal status) -- see
        validate_task_can_run's staleness check in agents/orchestrator_integration.py.

        Returns:
            The updated_at timestamp (naive datetime, matching set_status's
            datetime.now().isoformat() format), or None if unavailable/unparseable.
        """
        updated_at_str = self._read_state(project_name).get('updated_at')
        if not updated_at_str:
            return None

        try:
            return datetime.fromisoformat(updated_at_str)
        except Exception as e:
            logger.error(f"Failed to read dev container updated_at for {project_name}: {e}")
            return None

    def get_image_name(self, project_name: str) -> Optional[str]:
        """
        Get the Docker image name for a project's dev container

        Args:
            project_name: Name of the project

        Returns:
            Image name (e.g., "context-studio-agent:latest") or None
        """
        return self._read_state(project_name).get('image_name')

    def is_verified(self, project_name: str) -> bool:
        """Check if a project's dev container is verified and ready"""
        return self.get_status(project_name) == DevContainerStatus.VERIFIED

    def is_blocked(self, project_name: str) -> bool:
        """Check if a project's dev container setup is blocked"""
        return self.get_status(project_name) == DevContainerStatus.BLOCKED

    def verify_image_exists(self, project_name: str) -> bool:
        """
        Verify that the Docker image for a project actually exists locally AND
        is genuinely a switchyard-built agent environment (carries
        SWITCHYARD_AGENT_ENV_LABEL) — not just something else that happens to
        hold the same tag.

        A same-named-but-foreign image (e.g. a project's own docker-compose
        service tagged identically via Compose's default naming) fails this
        check even though `docker image inspect` succeeds, since tag
        existence alone can't distinguish "our image" from "an unrelated
        image that overwrote our tag".

        Args:
            project_name: Name of the project

        Returns:
            True if the image exists locally and carries the switchyard
            agent-environment label, False otherwise
        """
        image_name = self.get_image_name(project_name)

        if not image_name:
            logger.debug(f"No image name recorded for {project_name}")
            return False

        try:
            result = subprocess.run(
                ['docker', 'image', 'inspect',
                 '--format', '{{ index .Config.Labels "%s" }}' % SWITCHYARD_AGENT_ENV_LABEL,
                 image_name],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                logger.warning(f"Docker image {image_name} does not exist locally (state may be stale)")
                return False

            if result.stdout.strip() != "true":
                logger.warning(
                    f"Docker image {image_name} exists but is missing the "
                    f"{SWITCHYARD_AGENT_ENV_LABEL} label — it was not built from this "
                    f"project's Dockerfile.agent and has likely overwritten the tag "
                    f"(e.g. an unrelated docker-compose service sharing the same name). "
                    f"Treating as not verified; rebuild required."
                )
                return False

            logger.debug(f"Docker image {image_name} exists locally and is a genuine agent environment")
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout checking if Docker image {image_name} exists")
            return False
        except Exception as e:
            logger.error(f"Error checking if Docker image {image_name} exists: {e}")
            return False

    def verify_and_update_status(self, project_name: str) -> bool:
        """
        Verify that a project's Docker image exists and update status if it doesn't

        This is useful after Docker context switches or system restarts where the
        state file may say "verified" but the image no longer exists.

        Args:
            project_name: Name of the project

        Returns:
            True if image exists (or status is not verified), False if image is missing
        """
        status = self.get_status(project_name)

        # Only verify if status is VERIFIED
        if status != DevContainerStatus.VERIFIED:
            return True  # No verification needed for other states

        # Check if image actually exists
        if self.verify_image_exists(project_name):
            return True  # Image exists, all good

        # Image is missing, or the tag now points at something that isn't a
        # genuine switchyard agent environment (see verify_image_exists) -
        # reset status to UNVERIFIED either way so a rebuild is forced.
        image_name = self.get_image_name(project_name)
        logger.warning(
            f"Project {project_name} marked as verified but image {image_name} is missing "
            f"or is not a genuine agent-environment image. Resetting status to unverified."
        )

        self.set_status(
            project_name,
            DevContainerStatus.UNVERIFIED,
            image_name=image_name,
            error_message=(
                "Image missing, or tag now points at an unrelated image (e.g. overwritten "
                "by another docker build/compose using the same name) - rebuild required"
            )
        )

        return False

    def get_all_statuses(self) -> Dict[str, DevContainerStatus]:
        """
        Get status for all projects

        Returns:
            Dict mapping project names to their dev container status
        """
        statuses = {}

        for state_file in self.state_dir.glob("*.yaml"):
            project_name = state_file.stem
            statuses[project_name] = self.get_status(project_name)

        return statuses


# Global instance
dev_container_state = DevContainerStateManager()

"""
Project Resource Lock Manager

Thin facade over PipelineLockManager providing project-scoped (not
project+board-scoped) exclusive resource locking.

Phase 2 item of the concurrency redesign (issue #53, parent #88, umbrella
#34). Full-API investigation of PipelineLockManager confirmed every existing
call site is board-scoped dispatch gating (project, board) -> single holder.
A project-scoped resource lock (project) -> single holder is structurally
identical: binary, single-holder, "is this thing claimed" -- just keyed by
project alone instead of (project, board). Every fail-closed/retained/
staleness/TTL/steal semantic PipelineLockManager already implements is
already correct for that. So rather than duplicate that logic or invent a
parallel lock/release/staleness implementation, this class reuses
PipelineLockManager completely unchanged: a resource lock for
(project, resource_name) is represented internally as a pipeline lock for
(project, board=<namespaced resource board name>).

Naming convention for the internal `board` value
--------------------------------------------------
    __resource__{resource_name}

Deliberately NOT a colon-delimited form like "__resource__:{resource_name}".
PipelineLockManager.get_all_locks() scans Redis lock keys
(`pipeline_lock:{project}:{board}`) and skips any key whose total colon
count isn't exactly 2 (its own guard against picking up unrelated keys that
happen to share the "pipeline_lock:" prefix). A `board` value containing its
own colon would push a resource lock's Redis key to 3 colons and make it
invisible to that scan -- verified directly in
tests/unit/services/test_project_resource_lock_manager.py
(TestGetAllLocksCompatibility). The colon-free prefix used here keeps the
key at exactly 2 colons, so get_all_locks() discovers resource locks exactly
like it discovers ordinary pipeline locks.

The "__resource__" prefix itself is reserved: no real board name defined in
config/foundations/workflows.yaml starts with it, so a resource lock can
never collide with a real (project, board) pipeline lock today. This is a
convention enforced by code review of workflows.yaml, not a runtime check --
if a real board were ever named with this prefix, it would silently share
the same underlying Redis key/YAML file as a resource lock of the matching
name. Accepted as a known limitation: board names come from static,
reviewed config, not user/agent input, so this is not considered a live
risk worth a runtime cross-check against config/foundations/workflows.yaml
(which would couple this thin facade to config-loading machinery for a
collision with no known trigger).

`resource_name` itself IS validated at runtime (see _resource_board) since,
unlike board names, it may originate from less-trusted call-site data (e.g.
an issue/PR-derived identifier) once follow-up issues wire real callers to
this facade.

Scope note: this class ships the locking mechanism only. It is intentionally
not wired into any call site by this issue -- that is a follow-up (#54).
"""

import logging
from datetime import datetime, timezone
from typing import Optional, Tuple

# TouchResult is imported (and re-exported) so callers of this facade --
# services/project_checkout_lock.py's heartbeat -- can interpret
# touch_resource()'s tri-state return without reaching past the facade into
# PipelineLockManager directly.
from services.pipeline_lock_manager import (
    LOCK_TTL_SECONDS,
    PipelineLockManager,
    PipelineLock,
    PROCESS_OWNER_ID,
    TouchResult,
    get_pipeline_lock_manager,
    owner_process_role,
)

logger = logging.getLogger(__name__)

# Reserved board-name prefix marking a PipelineLockManager (project, board)
# lock as actually a project-scoped *resource* lock rather than a real
# pipeline board. Deliberately contains no ':' -- see module docstring.
RESOURCE_BOARD_PREFIX = "__resource__"

# Comfortably under typical filesystem filename limits (e.g. 255 BYTES on
# ext4) even after the project name, this prefix, and the ".yaml" suffix are
# all concatenated into a single on-disk filename component. Enforced as a
# UTF-8 byte count, not a character count -- ext4's limit is byte-based, and
# resource_name may contain multi-byte characters (see _resource_board).
MAX_RESOURCE_NAME_BYTES = 150

# How long a resource lock owned by a DIFFERENT kind of process (see
# recover_orphaned_resource_locks) may go without its liveness being refreshed
# before startup recovery stops believing its owner is alive.
#
# Derived from the Redis TTL rather than restated, because the number that
# actually matters here is services/project_checkout_lock.py's
# HEARTBEAT_INTERVAL_SECONDS -- itself REDIS_LOCK_TTL_SECONDS / 4 -- which is how
# often every held-with-heartbeat context manager in this codebase calls
# touch_resource() and so resets lock_acquired_at. Three intervals gives a live
# holder two missed ticks of slack before it is declared dead. Expressed against
# LOCK_TTL_SECONDS (already imported here) instead of importing that constant
# directly, because project_checkout_lock imports THIS module -- the import would
# be circular.
FOREIGN_OWNER_LIVENESS_GRACE_SECONDS = LOCK_TTL_SECONDS * 0.75


class InvalidResourceNameError(ValueError):
    """Raised when a resource_name would corrupt the namespaced board value."""


class ProjectResourceLockManager:
    """
    Project-scoped exclusive resource locking.

    A thin facade over PipelineLockManager: every method here delegates
    straight through to the equivalent PipelineLockManager method, passing a
    `board` value namespaced from `resource_name` (see _resource_board), so
    resource locks and ordinary pipeline (board) locks coexist in the same
    underlying Redis/YAML stores without colliding, while every fail-closed/
    retained/staleness/TTL/steal semantic PipelineLockManager already
    implements applies unchanged.
    """

    def __init__(self, lock_manager: Optional[PipelineLockManager] = None):
        """
        Args:
            lock_manager: PipelineLockManager instance to delegate to.
                Optional -- when omitted, defaults to the process-wide
                get_pipeline_lock_manager() singleton so callers share its
                already-warmed Redis connection and on-disk state by default.
                Constructing a fresh PipelineLockManager() opens a new Redis
                connection (including its connect timeout) on every call, so
                pass one explicitly only when a genuinely isolated instance
                is wanted (e.g. tests).
        """
        self._lock_manager = lock_manager if lock_manager is not None else get_pipeline_lock_manager()

    @staticmethod
    def _resource_board(resource_name: str) -> str:
        """
        Map a resource_name to the namespaced `board` value used internally.

        Validates resource_name first -- the resulting `board` value flows
        straight into PipelineLockManager's Redis key (`pipeline_lock:{project}:
        {board}`) and its on-disk YAML lock path (state_dir / f"{project}_{board}
        .yaml", auto-creating parent directories), so an unvalidated resource_name
        is both a path-traversal risk (e.g. "../../../tmp/evil") and a
        colon-count risk (a ':' in resource_name would push the Redis key from 2
        colons to 3, defeating get_all_locks()'s discovery filter -- the exact
        compatibility risk this module's docstring documents guarding against).

        Raises:
            InvalidResourceNameError: if resource_name is not a non-empty str,
                has leading/trailing whitespace, contains a control character
                (including a null byte), contains ':', '/', or '\\', or its
                UTF-8 encoding exceeds MAX_RESOURCE_NAME_BYTES.
        """
        if not isinstance(resource_name, str):
            raise InvalidResourceNameError(
                f"resource_name must be a str, got {type(resource_name).__name__}"
            )
        if not resource_name:
            raise InvalidResourceNameError("resource_name must be non-empty")
        if resource_name != resource_name.strip():
            # Not just cosmetic: _resource_board("db_migration") and
            # _resource_board(" db_migration") would otherwise produce distinct
            # board strings that name the SAME logical resource in every
            # caller's intent, silently defeating mutual exclusion between them.
            raise InvalidResourceNameError(
                f"resource_name {resource_name!r} has leading/trailing whitespace"
            )
        if any(ord(c) < 0x20 or ord(c) == 0x7F for c in resource_name):
            # A control character (e.g. a null byte) survives this method's
            # other checks but can make the on-disk YAML write fail while the
            # Redis write succeeds -- PipelineLockManager treats "at least one
            # store wrote" as success, so the lock would silently exist only in
            # Redis, invisible to get_all_locks()'s YAML glob scan and to
            # restart recovery, and would vanish on a Redis flush/TTL expiry
            # even though nothing released it.
            raise InvalidResourceNameError(
                f"resource_name {resource_name!r} contains a control character"
            )
        resource_name_bytes = len(resource_name.encode("utf-8"))
        if resource_name_bytes > MAX_RESOURCE_NAME_BYTES:
            # An overlong board value can make the YAML lock file's path exceed
            # the filesystem's filename length limit (byte-based, e.g. ext4's
            # 255-byte NAME_MAX -- checked in UTF-8 bytes, not code points, so
            # a multi-byte resource_name can't sneak past a char-count check
            # while still being too many bytes on disk). Path.exists() does
            # not swallow the resulting OSError (verified: ENAMETOOLONG), so
            # an unbounded resource_name can crash the caller instead of
            # failing with this documented exception.
            raise InvalidResourceNameError(
                f"resource_name is {resource_name_bytes} UTF-8 bytes, exceeding "
                f"the {MAX_RESOURCE_NAME_BYTES}-byte limit"
            )
        if any(c in resource_name for c in (":", "/", "\\")):
            raise InvalidResourceNameError(
                f"resource_name {resource_name!r} contains a disallowed character "
                f"(':', '/', '\\') -- these would corrupt the namespaced board "
                f"value's Redis key format or its on-disk lock file path"
            )
        return f"{RESOURCE_BOARD_PREFIX}{resource_name}"

    def acquire_resource(
        self, project: str, resource_name: str, issue_number: int
    ) -> Tuple[bool, str]:
        """
        Attempt to acquire the named project-scoped resource lock.

        Delegates directly to PipelineLockManager.try_acquire_lock() -- see
        its docstring for the full fail-closed/retained/staleness/TTL
        semantics, all of which apply unchanged here.

        Returns:
            (can_execute: bool, reason: str)
        """
        return self._lock_manager.try_acquire_lock(
            project, self._resource_board(resource_name), issue_number
        )

    def release_resource(
        self, project: str, resource_name: str, issue_number: int, force: bool = False
    ) -> bool:
        """
        Release the named project-scoped resource lock.

        Delegates directly to PipelineLockManager.release_lock() -- see its
        docstring for the retained-lock refusal and fail-closed semantics,
        which apply unchanged here.

        Returns:
            True if the lock was released, False if not held by this issue,
            or if it's retained/unknown and force was not set.
        """
        return self._lock_manager.release_lock(
            project, self._resource_board(resource_name), issue_number, force=force
        )

    def _foreign_owner_is_plausibly_alive(self, lock: PipelineLock) -> bool:
        """
        True when `lock` is stamped with a different process KIND's owner id and
        its liveness has been refreshed recently enough that that process is
        plausibly still running -- see FOREIGN_OWNER_LIVENESS_GRACE_SECONDS and
        recover_orphaned_resource_locks().

        An unparseable/missing lock_acquired_at counts as alive: this decides
        whether to DISPOSSESS a holder, so the unknown case must fail closed.
        """
        try:
            acquired_at = datetime.fromisoformat(lock.lock_acquired_at.replace('Z', '+00:00'))
        except Exception:
            return True
        if acquired_at.tzinfo is None:
            acquired_at = acquired_at.replace(tzinfo=timezone.utc)
        age_seconds = (datetime.now(timezone.utc) - acquired_at).total_seconds()
        return age_seconds < FOREIGN_OWNER_LIVENESS_GRACE_SECONDS

    def recover_orphaned_resource_locks(self, resource_name: str) -> int:
        """
        Release the locks for `resource_name`, across all projects, that belong
        to a DEAD incarnation of the calling process. STARTUP ONLY -- see the
        callers in main.py and services/observability_server.py.

        Nothing else frees these: main.py's stale-lock recovery iterates
        configured pipeline BOARDS, and a resource lock lives under the reserved
        board `__resource__{resource_name}` (see RESOURCE_BOARD_PREFIX), so that
        loop never sees one -- and PipelineLockManager's own reclamation waits
        out either its 4-hour staleness heuristic or the 7200s Redis TTL. Until
        this existed, a crash mid-build left the lock live for up to ~2 hours and
        made services/work_execution_state.py's post-restart dev-container
        reconciliation a guaranteed no-op in exactly the case it was written for
        (#152 review).

        Which locks belong to a dead incarnation
        ----------------------------------------
        This used to release EVERY non-retained holder, on the premise that
        "every holder of a resource lock is an in-process operation of the
        orchestrator that took it, so a freshly-started process has no
        legitimate holders by construction". That premise was false, and #152
        WI-7 is what falsified it: docker-compose.yml runs observability-server
        as its OWN container (`python -m services.observability_server`,
        separate from the orchestrator's `python main.py`, sharing the same
        Redis and state mount), and its /api/projects/<p>/rebuild-image handler
        now holds this very lock for the whole duration of an operator-triggered
        `docker build` -- minutes to tens of minutes. An orchestrator restart
        inside that window blanket-released a lock whose holder was very much
        alive, and the theft was silent: touch_resource() returns NOT_HELD
        rather than re-acquiring, and the heartbeat only ticks every
        REDIS_LOCK_TTL_SECONDS/4, so a sub-30-minute build never noticed at all
        (#152 review, findings 1 and 4).

        So a lock is only released when its owner is provably not running:

          - `owner_process` names THIS process's own kind (PROCESS_OWNER_ID's
            role half -- see _derive_process_role). Every such kind is a
            docker-compose singleton, and this method is startup-only, called
            before this process has acquired anything: a lock stamped with my
            own role is therefore my dead predecessor's, whatever its instance
            id says.
          - `owner_process` is absent entirely -- a lock written before the
            stamp existed, i.e. by a process from before this upgrade, which by
            definition is not the one running now. Preserves the pre-stamp
            behaviour for the one deploy in which such a row can still exist.
          - `owner_process` names a DIFFERENT kind (observability-server, an
            admin script) AND its liveness has not been refreshed within
            FOREIGN_OWNER_LIVENESS_GRACE_SECONDS. Every held-with-heartbeat
            context manager in this codebase touches its lock several times
            inside that window, so a foreign owner that has gone quiet for that
            long is dead; one that has not is left strictly alone.

        A retained lock (marked by mark_lock_failed for deliberate human
        recovery) is deliberately NOT released either: release_resource()
        refuses it without force, and force is reserved for
        scripts/release_lock.py's explicit confirmation flow.

        Returns:
            Number of locks released.
        """
        board = self._resource_board(resource_name)
        my_role = owner_process_role(PROCESS_OWNER_ID)
        released = 0
        try:
            locks = self._lock_manager.get_all_locks()
        except Exception as e:
            logger.error(f"Could not enumerate locks to recover '{resource_name}' holders: {e}")
            return 0

        for lock in locks:
            if lock.board != board or lock.lock_status != 'locked':
                continue
            if lock.retained_reason:
                logger.warning(
                    f"Leaving retained '{resource_name}' lock for {lock.project} in place "
                    f"(holder #{lock.locked_by_issue}: {lock.retained_reason}) -- only "
                    f"scripts/release_lock.py's deliberate recovery flow may clear it"
                )
                continue

            owner_role = owner_process_role(lock.owner_process)
            if owner_role is not None and owner_role != my_role:
                if self._foreign_owner_is_plausibly_alive(lock):
                    logger.info(
                        f"Leaving '{resource_name}' lock for {lock.project} in place "
                        f"(holder #{lock.locked_by_issue}, owner {lock.owner_process}) -- "
                        f"it belongs to a different process than this one ({PROCESS_OWNER_ID}) "
                        f"and its liveness was refreshed at {lock.lock_acquired_at}, so that "
                        f"process is still running and the operation it guards is still going"
                    )
                    continue
                logger.warning(
                    f"Releasing '{resource_name}' lock for {lock.project} owned by "
                    f"{lock.owner_process} (holder #{lock.locked_by_issue}) -- a different "
                    f"process than this one ({PROCESS_OWNER_ID}), but its liveness has not "
                    f"been refreshed since {lock.lock_acquired_at}, well past the "
                    f"{FOREIGN_OWNER_LIVENESS_GRACE_SECONDS:.0f}s heartbeat grace, so it is "
                    f"no longer running"
                )
            else:
                logger.warning(
                    f"Releasing orphaned '{resource_name}' lock for {lock.project} "
                    f"(held by #{lock.locked_by_issue} since {lock.lock_acquired_at}, owner "
                    f"{lock.owner_process or 'unknown (pre-stamp)'}) -- its holder did not "
                    f"survive the previous {my_role} process"
                )
            try:
                if self._lock_manager.release_lock(lock.project, board, lock.locked_by_issue):
                    released += 1
                else:
                    logger.error(
                        f"Failed to release orphaned '{resource_name}' lock for {lock.project} "
                        f"(holder #{lock.locked_by_issue})"
                    )
            except Exception as e:
                logger.error(
                    f"Error releasing orphaned '{resource_name}' lock for {lock.project}: {e}",
                    exc_info=True,
                )

        return released

    def get_resource_lock(self, project: str, resource_name: str) -> Optional[PipelineLock]:
        """
        Get current lock state for the named project-scoped resource lock.

        Delegates directly to PipelineLockManager.get_lock().

        Returns:
            PipelineLock if locked, None if unlocked.
        """
        return self._lock_manager.get_lock(project, self._resource_board(resource_name))

    def touch_resource(self, project: str, resource_name: str, issue_number: int) -> TouchResult:
        """
        Refresh an already-held resource lock's liveness markers (TTL AND
        acquired-at timestamp) without changing its holder.

        Delegates directly to PipelineLockManager.touch_lock() -- see its
        docstring for why this exists separately from acquire_resource()'s
        own TTL-only refresh-on-reentry behavior. Used by
        services/project_checkout_lock.py's heartbeat mechanism to keep a
        long-held lock from being mistaken for an abandoned one by the
        staleness heuristic while it's still genuinely alive.

        Returns:
            The TouchResult touch_lock() produced, passed through unchanged:
            REFRESHED (held by issue_number and liveness extended), NOT_HELD
            (confirmed not held by issue_number), or REFRESH_FAILED (the
            stores themselves failed, so liveness was NOT extended and the
            state may be unknown). Only REFRESHED is truthy, so this stays
            drop-in compatible with the bool return this method used to have
            -- see TouchResult for why the distinction was needed.
        """
        return self._lock_manager.touch_lock(
            project, self._resource_board(resource_name), issue_number
        )

    def mark_resource_failed(
        self, project: str, resource_name: str, issue_number: int, reason: str
    ) -> bool:
        """
        Durably mark the resource lock as retained due to a failed attempt.

        Delegates directly to PipelineLockManager.mark_lock_failed() -- see its
        docstring for the full durable-retention semantics, which apply
        unchanged here. Without this, a crashed holder's resource lock could
        only be recovered by the ordinary staleness heuristic rather than held
        for deliberate human recovery.

        Returns:
            True if the lock was found (held by issue_number) and marked,
            False otherwise.
        """
        return self._lock_manager.mark_lock_failed(
            project, self._resource_board(resource_name), issue_number, reason
        )

    def clear_resource_retained_reason(
        self, project: str, resource_name: str, issue_number: int
    ) -> bool:
        """
        Clear a durable retained_reason on the resource lock without releasing it.

        Delegates directly to PipelineLockManager.clear_retained_reason() -- see
        its docstring for the narrow self-heal-retry use case this exists for.
        """
        return self._lock_manager.clear_retained_reason(
            project, self._resource_board(resource_name), issue_number
        )

    def get_resource_retained_reason(
        self, project: str, resource_name: str, issue_number: int
    ) -> Optional[str]:
        """
        Return the retained_reason if issue_number currently holds a
        failed/retained resource lock, else None.

        Delegates directly to PipelineLockManager.get_retained_reason(),
        including its fail-closed behavior on unreadable lock state.
        """
        return self._lock_manager.get_retained_reason(
            project, self._resource_board(resource_name), issue_number
        )

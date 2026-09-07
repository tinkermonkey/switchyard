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
from typing import Optional, Tuple

from services.pipeline_lock_manager import PipelineLockManager, PipelineLock

logger = logging.getLogger(__name__)

# Reserved board-name prefix marking a PipelineLockManager (project, board)
# lock as actually a project-scoped *resource* lock rather than a real
# pipeline board. Deliberately contains no ':' -- see module docstring.
RESOURCE_BOARD_PREFIX = "__resource__"


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
                Optional -- when omitted, a new PipelineLockManager is
                constructed with its own Redis client and state_dir. Callers
                that want to share the process-wide singleton (and its Redis
                connection / on-disk state) must pass
                get_pipeline_lock_manager() explicitly.
        """
        self._lock_manager = lock_manager if lock_manager is not None else PipelineLockManager()

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
            InvalidResourceNameError: if resource_name is empty or contains ':',
                '/', '\\', or a '..' path segment.
        """
        if not resource_name or not resource_name.strip():
            raise InvalidResourceNameError("resource_name must be non-empty")
        if any(c in resource_name for c in (":", "/", "\\")) or ".." in resource_name:
            raise InvalidResourceNameError(
                f"resource_name {resource_name!r} contains a disallowed character "
                f"(':', '/', '\\') or '..' -- these would corrupt the namespaced "
                f"board value's Redis key format or its on-disk lock file path"
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

    def get_resource_lock(self, project: str, resource_name: str) -> Optional[PipelineLock]:
        """
        Get current lock state for the named project-scoped resource lock.

        Delegates directly to PipelineLockManager.get_lock().

        Returns:
            PipelineLock if locked, None if unlocked.
        """
        return self._lock_manager.get_lock(project, self._resource_board(resource_name))

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

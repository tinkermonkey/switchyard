"""
Project Checkout Lock

Serializes operations against a project's SHARED base-clone directory --
ProjectWorkspaceManager.get_project_dir(project_name) with no epic_id -- the
one physical directory that is simultaneously the startup git clone/update
target, the mount/cwd for any agent execution that hasn't been scoped to an
isolated per-epic git worktree, and (historically) the Docker build context
for dev_environment_setup's image build.

Phase 2 item of the concurrency redesign (issue #54, parent #88, umbrella
#34), built on top of the project-scoped resource lock facade (#53,
services/project_resource_lock_manager.py).

Why this is narrower than issue #54's original description
------------------------------------------------------------
#54 was filed against a codebase where get_project_dir(project_name) was
project_workspace.py's ONLY working directory for a project -- every board's
git checkout, every agent's cwd/mount, and the Docker build context all
shared that one path. Between #54 being filed and this fix landing, #119's
epic-worktree isolation work (issues #120-126, #45-52) migrated almost every
git-mutating and agent-execution call site onto an isolated, per-epic git
worktree (ProjectWorkspaceManager.get_or_create_epic_worktree()) instead --
see get_project_dir()'s own docstring. Two operations against DIFFERENT
epics' worktrees don't share a directory at all, so serializing them here
would reintroduce exactly the cross-epic throughput cost that isolation work
was designed to remove, violating this issue's own "no behavior change for
the common case" acceptance criterion.

What's left to protect is genuinely narrower but still real: the shared base
clone itself. See ProjectWorkspaceManager.is_base_clone_dir() -- callers use
that to decide whether a given resolved project_dir needs this lock at all;
this module only implements the lock/wait/release mechanics once that
decision has been made.

Blocking vs failing
--------------------
ProjectResourceLockManager.acquire_resource() (like the
PipelineLockManager.try_acquire_lock() it wraps) is a single, non-blocking
attempt -- see project_monitor.py's board-lock dispatch gate, which reacts to
a failed attempt by simply returning and letting its next 30s poll cycle
retry. There is no equivalent natural retry point here: a failed acquisition
mid-operation (e.g. inside an agent's container run, or auto-commit) isn't
something the caller can just shelve until later -- the guarded operation
either serializes (waits) or the caller has to abort a partially-set-up
execution. Per the acceptance criterion ("two concurrent operations ...
serialize correctly ... instead of racing"), this module polls
acquire_resource() with a sleep between attempts until it succeeds or a
generous bounded timeout elapses, then raises loudly (never silently skips
the guarded operation, and never proceeds unlocked).
"""

import asyncio
import itertools
import logging
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from services.project_resource_lock_manager import ProjectResourceLockManager

logger = logging.getLogger(__name__)

# Reserved resource name for this lock. Distinct from any real board name
# (see RESOURCE_BOARD_PREFIX in project_resource_lock_manager.py) and from
# any other resource this facade may be asked to lock in the future.
RESOURCE_NAME = "project_checkout"

# Generous enough to outlast the longest legitimate holder of this lock -- a
# Docker-executed agent run, hard-timeout up to 1800s for build-type agents
# per config/foundations/agents.yaml -- without waiting forever on a
# genuinely stuck/crashed holder. PipelineLockManager's own staleness/TTL
# recovery (inherited unchanged through the ProjectResourceLockManager
# facade) is what actually reclaims a dead holder's lock; this timeout is
# just this caller's patience for that recovery to take effect.
DEFAULT_TIMEOUT_SECONDS = 1900.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0

# Used to mint a unique holder id for call sites with no real GitHub issue in
# scope (e.g. startup project initialization, which runs before any board is
# polled or any issue/epic exists). Deliberately NOT a single shared
# constant: PipelineLockManager.try_acquire_lock() treats a matching
# issue_number as reentrant ("already_holds_lock") with no other identity
# check, so a shared sentinel would let two genuinely different concurrent
# anonymous callers each be told they already hold the lock and both proceed
# concurrently -- exactly the race this lock exists to close. Counts DOWN
# from -1 so every anonymous holder id is unique and, since real GitHub issue
# numbers are always positive, can never collide with one.
_anonymous_holder_ids = itertools.count(start=-1, step=-1)
_anonymous_holder_ids_guard = threading.Lock()


def next_anonymous_holder_id() -> int:
    """
    Mint a unique negative "issue number" to attribute a lock hold to, for a
    call site with no real GitHub issue in scope. Call this fresh at the
    point of acquisition (not once at import time or cached) so each
    concurrent anonymous acquire attempt gets its own distinct identity.
    """
    with _anonymous_holder_ids_guard:
        return next(_anonymous_holder_ids)


class ProjectCheckoutLockTimeoutError(RuntimeError):
    """Raised when the project_checkout resource lock could not be acquired
    within the configured timeout -- surfaced loudly rather than silently
    skipping (or silently running unlocked) the guarded operation."""


@asynccontextmanager
async def project_checkout_lock_async(
    project: str,
    issue_number: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Async context manager serializing access to `project`'s shared base-clone
    directory for the duration of the `with` block.

    Polls ProjectResourceLockManager.acquire_resource() -- a single
    non-blocking attempt -- with asyncio.sleep() between attempts (never
    blocks the event loop) until acquired or timeout_seconds elapses.
    Releases in a finally block so an exception raised inside the `with` body
    still frees the lock.

    Args:
        project: Project name.
        issue_number: GitHub issue number to attribute this hold to. When no
            real issue is in scope at the call site, pass
            next_anonymous_holder_id() (freshly called, not cached/shared) --
            document why at the call site.
        timeout_seconds / poll_interval_seconds: override only for tests.
        facade: injected ProjectResourceLockManager (e.g. one built on a
            temp-dir PipelineLockManager + mock Redis, matching
            test_project_resource_lock_manager.py's own pattern) -- for
            tests only. Production call sites omit this and get the default
            facade, which shares the process-wide PipelineLockManager
            singleton.

    Raises:
        ProjectCheckoutLockTimeoutError: not acquired within timeout_seconds.
    """
    facade = facade if facade is not None else ProjectResourceLockManager()
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason = facade.acquire_resource(project, RESOURCE_NAME, issue_number)
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise ProjectCheckoutLockTimeoutError(
                f"Could not acquire '{RESOURCE_NAME}' lock for project {project!r} "
                f"(issue #{issue_number}) within {timeout_seconds}s: {reason}"
            )
        logger.info(
            f"'{RESOURCE_NAME}' lock busy for project {project!r} (issue #{issue_number}): "
            f"{reason} -- waiting {poll_interval_seconds}s before retrying"
        )
        await asyncio.sleep(poll_interval_seconds)

    try:
        yield
    finally:
        released = facade.release_resource(project, RESOURCE_NAME, issue_number)
        if not released:
            logger.warning(
                f"'{RESOURCE_NAME}' lock release for project {project!r} "
                f"(issue #{issue_number}) returned False -- lock may already be "
                "released, retained, or held by a different issue number"
            )


@contextmanager
def project_checkout_lock_sync(
    project: str,
    issue_number: int,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Synchronous counterpart of project_checkout_lock_async(), for the one
    call site (ProjectWorkspaceManager.initialize_project(), at startup,
    before any asyncio event loop is guaranteed to be running) that cannot
    await. Uses time.sleep() between polls -- MUST NOT be called from a
    coroutine running on an asyncio event loop, which it would block.

    See project_checkout_lock_async() for the full contract (including the
    `facade` test-injection parameter); identical semantics otherwise.
    """
    facade = facade if facade is not None else ProjectResourceLockManager()
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason = facade.acquire_resource(project, RESOURCE_NAME, issue_number)
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise ProjectCheckoutLockTimeoutError(
                f"Could not acquire '{RESOURCE_NAME}' lock for project {project!r} "
                f"(issue #{issue_number}) within {timeout_seconds}s: {reason}"
            )
        logger.info(
            f"'{RESOURCE_NAME}' lock busy for project {project!r} (issue #{issue_number}): "
            f"{reason} -- waiting {poll_interval_seconds}s before retrying"
        )
        time.sleep(poll_interval_seconds)

    try:
        yield
    finally:
        released = facade.release_resource(project, RESOURCE_NAME, issue_number)
        if not released:
            logger.warning(
                f"'{RESOURCE_NAME}' lock release for project {project!r} "
                f"(issue #{issue_number}) returned False -- lock may already be "
                "released, retained, or held by a different issue number"
            )

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

Why every acquisition gets its own unique holder id, not the caller's real
issue_number
------------------------------------------------------------------------
Found in code review: PipelineLockManager.try_acquire_lock() treats a
MATCHING issue_number as reentrant ("already_holds_lock") with no other
identity check -- correct for its original design (one active dispatch per
issue at a time), wrong for this lock's actual job (mutual exclusion between
independent, possibly-concurrent operations that merely happen to share a
project or an issue number). If two genuinely different concurrent
operations for the SAME real issue number both called acquire_resource()
with that issue number directly (e.g. a Docker agent run and an unrelated
auto-commit/watchdog redispatch both tagged issue #42), each would be told
"already_holds_lock" and both would proceed concurrently against the shared
base clone -- exactly the race this lock exists to close -- and whichever
finished first would release the lock out from under the other still-running
one. So `issue_number` here is ONLY for log attribution; the identity
actually passed to acquire_resource()/release_resource() is always a fresh,
process-unique id minted by _mint_unique_holder_id(), guaranteeing every
acquisition is judged strictly on its own, never treated as a reentrant hold
of some other concurrent caller's lock. The tradeoff: a stuck lock inspected
via get_all_locks()/PipelineLock.locked_by_issue shows this synthetic id, not
a real GitHub issue number -- an operator needs this module's logs (which do
include the real issue_number) for that attribution, not the lock state
itself.
"""

import asyncio
import itertools
import logging
import os
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

# Generous enough to outlast the longest legitimate holder of this lock.
# claude/claude_integration.py wraps ANY Docker-executed agent run against
# the shared base clone in this lock (gated on is_base_clone_dir(), not
# restricted to any particular agent), and config/foundations/agents.yaml's
# per-agent `timeout` ranges up to 10800s (senior_software_engineer) -- found
# in review (#56) after the original 1900s value here was calibrated against
# a stale "1800s for build-type agents" assumption that undercounted the
# real ceiling. 100s of margin over that longest configured agent timeout.
#
# Corrected in review round 3 (of #54): this does NOT outlast PipelineLockManager's
# own staleness/TTL recovery (inherited unchanged through the
# ProjectResourceLockManager facade) -- that takes 7200s (Redis lock key
# TTL) to 14400s (the YAML-fallback 4-hour staleness threshold), both far
# longer than this timeout. A crashed holder's lock is therefore NOT
# reliably recoverable within one call's wait here. The actual design
# intent is fail LOUD and relatively promptly (raising
# ProjectCheckoutLockTimeoutError) so this specific call attempt gives up
# and lets whatever triggers it again (the next board poll, the next
# dispatch, an operator retry) try again later, rather than pinning a
# thread/coroutine for up to 4 hours waiting out the staleness window in
# one blocking call. In a genuine crash, expect this to raise repeatedly
# (roughly every DEFAULT_TIMEOUT_SECONDS) until the underlying lock
# actually becomes recoverable -- noisy, but not stuck, and never silently
# proceeding unlocked.
DEFAULT_TIMEOUT_SECONDS = 10900.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0

# Seeds _mint_unique_holder_id()'s counter so it differs across process
# restarts, not just within one process's lifetime. Found in code review: a
# counter that always started at a fixed value (e.g. -1) would let a lock
# left behind by a crashed process (Redis TTL 7200s, or the non-expiring
# YAML copy) collide with the very first id the NEW process mints -- which,
# combined with config_manager.list_projects()'s deterministic sorted()
# ordering, would deterministically hit the alphabetically-first project on
# every single restart. Combining the pid with a high-resolution timestamp
# makes two process incarnations' id ranges collide only by astronomical
# coincidence, without needing true randomness.
_PROCESS_HOLDER_ID_SEED = -(abs(hash((os.getpid(), time.time_ns()))) % 10**15 + 1)
_unique_holder_ids = itertools.count(start=_PROCESS_HOLDER_ID_SEED, step=-1)
_unique_holder_ids_guard = threading.Lock()


def _mint_unique_holder_id() -> int:
    """
    Mint a holder id guaranteed unique for the lifetime of this process (and,
    via _PROCESS_HOLDER_ID_SEED, vanishingly unlikely to collide with a
    previous process incarnation's abandoned lock either). Always negative,
    so it can never collide with a real GitHub issue number (always
    positive). Called once per acquisition attempt by
    project_checkout_lock_async()/_sync() -- see this module's docstring
    ("Why every acquisition gets its own unique holder id") for why this is
    used for EVERY acquisition, not just ones with no real issue_number in
    scope.
    """
    with _unique_holder_ids_guard:
        return next(_unique_holder_ids)


class ProjectCheckoutLockTimeoutError(RuntimeError):
    """Raised when the project_checkout resource lock could not be acquired
    within the configured timeout -- surfaced loudly rather than silently
    skipping (or silently running unlocked) the guarded operation."""


def _attribution(issue_number: Optional[int]) -> str:
    return f"issue #{issue_number}" if issue_number is not None else "no issue in scope"


def _timeout_error(
    resource_name: str,
    project: str,
    issue_number: Optional[int],
    timeout_seconds: float,
    reason: str,
    error_cls: type = ProjectCheckoutLockTimeoutError,
) -> Exception:
    """
    Shared by every project-scoped resource lock built on this pattern (this
    module's own project_checkout lock, and services/dev_container_build_lock.py's
    dev_container_build lock) -- see this module's docstring for why every
    such lock shares this poll/timeout/release shape and its holder-id
    minting. `error_cls` lets each lock raise its own distinct exception type
    while sharing this message format.
    """
    return error_cls(
        f"Could not acquire '{resource_name}' lock for project {project!r} "
        f"({_attribution(issue_number)}) within {timeout_seconds}s: {reason}"
    )


def _log_busy(resource_name: str, project: str, issue_number: Optional[int], reason: str, poll_interval_seconds: float) -> None:
    logger.info(
        f"'{resource_name}' lock busy for project {project!r} ({_attribution(issue_number)}): "
        f"{reason} -- waiting {poll_interval_seconds}s before retrying"
    )


def _release_and_warn(
    facade: ProjectResourceLockManager, resource_name: str, project: str, holder_id: int, issue_number: Optional[int]
) -> None:
    released = facade.release_resource(project, resource_name, holder_id)
    if not released:
        logger.warning(
            f"'{resource_name}' lock release for project {project!r} "
            f"({_attribution(issue_number)}) returned False -- lock may already be "
            "released or retained"
        )


@asynccontextmanager
async def project_checkout_lock_async(
    project: str,
    issue_number: Optional[int] = None,
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
        issue_number: GitHub issue number to attribute this hold to in LOG
            MESSAGES only -- not used as the lock's holder identity (see this
            module's docstring, "Why every acquisition gets its own unique
            holder id"). Pass None (the default) when no real issue is in
            scope at the call site.
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
    holder_id = _mint_unique_holder_id()
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason = facade.acquire_resource(project, RESOURCE_NAME, holder_id)
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise _timeout_error(RESOURCE_NAME, project, issue_number, timeout_seconds, reason)
        _log_busy(RESOURCE_NAME, project, issue_number, reason, poll_interval_seconds)
        await asyncio.sleep(poll_interval_seconds)

    try:
        yield
    finally:
        _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)


@contextmanager
def project_checkout_lock_sync(
    project: str,
    issue_number: Optional[int] = None,
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
    `facade` test-injection parameter and the `issue_number` log-only
    caveat); identical semantics otherwise.
    """
    facade = facade if facade is not None else ProjectResourceLockManager()
    holder_id = _mint_unique_holder_id()
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason = facade.acquire_resource(project, RESOURCE_NAME, holder_id)
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise _timeout_error(RESOURCE_NAME, project, issue_number, timeout_seconds, reason)
        _log_busy(RESOURCE_NAME, project, issue_number, reason, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    try:
        yield
    finally:
        _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)

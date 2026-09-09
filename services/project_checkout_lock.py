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

Acquisition order relative to services/dev_container_build_lock.py
--------------------------------------------------------------------
The one call site that nests both locks (claude/claude_integration.py's
run_claude_code(), local-execution branch) always acquires
dev_container_build_lock OUTER and this module's lock INNER. Found in code
review as a forward-looking risk (not a live bug -- confirmed no call site
does the reverse today): nothing enforces this ordering mechanically, so a
future call site nesting them in the opposite order could deadlock/mutually
timeout two concurrent operations against each other. If you need both
locks together, acquire dev_container_build_lock first.

Why the heartbeat runs on an OS thread, never an asyncio task (#141)
----------------------------------------------------------------------
The async variant originally ran its heartbeat as a sibling asyncio.Task,
which assumed the guarded operation would periodically yield control back to
the event loop. Neither real caller does: claude/docker_runner.py's
_execute_in_container() monitors the container with a synchronous
`claude_done_event.wait(timeout=...)` loop, and claude/claude_integration.py's
_run_claude_code_locally() reads the subprocess with a synchronous
`for line in iter(process.stdout.readline, '')` loop. Both are awaited from
inside the `async with` block but block the single-threaded event loop for
the operation's ENTIRE duration, so the sibling heartbeat task was never
scheduled until the operation it was supposed to protect had already
finished -- making the heartbeat inert for exactly the multi-hour holds it
exists to protect, and letting PipelineLockManager's 7200s Redis TTL lapse
under a still-live holder (agents.yaml allows agent timeouts up to 10800s).

Of the three options weighed in #141, the heartbeat now runs on a real OS
thread for BOTH variants (the async one just joins it differently on exit).
The alternatives were rejected as both riskier and narrower:

  - Wrapping the guarded operation in asyncio.to_thread() would have to be
    done at every call site, and neither call site can actually be moved off
    the loop wholesale -- run_agent_in_container()/_run_claude_code_locally()
    are async functions with their own awaits (streaming callbacks,
    observability writes) interleaved around the blocking sections.
  - Restructuring both loops to yield periodically fixes only the two call
    sites that exist today, and silently regresses the moment a third
    blocking caller is wrapped in this lock.

A thread-based heartbeat is correct regardless of what the guarded body
does, which is the property the remaining Phase 2 work items need: the
board-dispatch re-acquire path and ProjectWorkspaceManager's
_add_epic_worktree() (a hot path called straight from the event-loop thread)
both get a working heartbeat from this module with no further analysis of
whether their bodies yield.

That property only holds if the heartbeat is also STARTED without waiting on
the event loop. Found in review of this same change: offloading the acquire
(see _acquire_and_start_heartbeat_off_loop below) introduced a suspension
point between "the worker thread acquired the lock" and "the coroutine is
rescheduled and starts the heartbeat" -- and a loop blocked by some OTHER
task's guarded body (the very thing this design accepts as normal) can hold
that gap open for hours, past the 7200s Redis TTL, on a lock that is already
held and not yet being refreshed. So acquisition and heartbeat start happen
in the SAME executor callable, and the context manager below adopts the
already-running thread rather than starting its own.

The corollary rule for everything else in this module's async paths: no
synchronous lock I/O runs on the event-loop thread. Three shapes implement
that, and which one applies depends on what a cancellation delivered mid-call
would cost:

  (a) Cancellable acquisition I/O -- loop.run_in_executor() +
      asyncio.shield() + a done-callback that releases an orphaned success
      (_acquire_and_start_heartbeat_off_loop, the pattern
      services/docker_socket_access_gate.py established for the same
      hazard). Offloading is what lets a cancellation interleave with the
      attempt at all, and a concurrent.futures worker already running cannot
      be interrupted, so the callback is what keeps a post-cancellation
      success from wedging the lock with nobody left to release it.

  (b) An exit-path wait that must complete -- loop.run_in_executor() +
      asyncio.shield() with a SYNCHRONOUS fallback both on cancellation and
      on executor shutdown (_join_heartbeat_thread_async). A done-callback
      can't rescue this one, because the release that must not overtake the
      join runs immediately after it returns; instead, whenever the awaited
      form can't be relied on, the join is finished here and now on the
      calling thread. Thread.join() needs no event loop, so it still
      completes while a cancellation is unwinding or the loop is tearing
      down.

  (c) The final release -- left inline on the loop, the one genuine
      exception. Not because "every await is cancellable" (shape (b)
      disproves that as a blanket argument) but because there is no
      orphan-cleanup path for a SKIPPED release: nothing else in the process
      knows this holder_id, so a release that never happens leaks the lock
      until TTL/staleness recovery (7200s-14400s). It is bounded by
      PipelineLockManager's own Redis socket timeouts.

_default_facade_off_loop() is a plain asyncio.to_thread() with neither
shield nor done-callback, and needs neither: constructing a
ProjectResourceLockManager has no lock side effect to orphan -- it only
connects/pings Redis -- so an abandoned construction leaks nothing.
"""

import asyncio
import functools
import itertools
import logging
import os
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Optional, Tuple

from services.project_resource_lock_manager import ProjectResourceLockManager, TouchResult

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


# How often to refresh the Redis lock key's TTL while legitimately holding a
# lock built on this pattern. CRITICAL, found in code review and confirmed by
# direct source reading of PipelineLockManager.try_acquire_lock(): the Redis
# TTL is fixed at 7200s and is refreshed ONLY as a side effect of a repeat
# acquire_resource() call for the SAME holder_id (the "already_holds_lock"
# transaction branch does `pipe.expire(lock_key, 7200)`) -- it is never
# refreshed proactively. This module's own acquire-then-yield-then-release
# usage calls acquire_resource() exactly ONCE per hold, so a hold that
# outlives 7200s with no heartbeat would have its Redis copy silently expire
# while still legitimately held. Worse: the acquire transaction's own check
# (`if lock_data and lock_data.get('lock_status') == 'locked'`) reads an
# expired key back as an EMPTY dict, which is falsy -- so a second caller's
# acquire attempt at that point succeeds immediately, with no check against
# the still-valid YAML copy at that point in the code path. Comfortably
# under half the 7200s TTL so at least one heartbeat always lands before
# expiry even under scheduling jitter.
HEARTBEAT_INTERVAL_SECONDS = 1800.0

# The Redis lock-key TTL a failing heartbeat is racing (PipelineLockManager's
# own fixed 7200s -- see HEARTBEAT_INTERVAL_SECONDS above), and how long a
# run of consecutive heartbeat failures may go on before it stops being a
# transient blip and starts genuinely threatening that TTL. Found in review
# (#140 item 30): every failed refresh used to log the same WARNING whether
# it was one Redis hiccup or two hours of sustained failure with the TTL
# about to lapse under a still-live holder, so an operator had no signal
# distinguishing the two. Half the TTL leaves at least one more heartbeat
# interval of margin after the escalation fires.
#
# Found in a later review round (#146 WI-1): "a failed refresh" here means
# TouchResult.REFRESH_FAILED, not an exception. touch_lock() catches every
# store failure internally and returns rather than raising, so an escalation
# hung only off `except Exception` around touch_resource() could never fire
# for the sustained-outage case it was written for -- see _heartbeat_worker.
REDIS_LOCK_TTL_SECONDS = 7200.0
HEARTBEAT_FAILURE_ESCALATION_SECONDS = REDIS_LOCK_TTL_SECONDS / 2.0


def _heartbeat_worker(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    heartbeat_interval_seconds: float,
    stop_event: threading.Event,
) -> None:
    """
    Body of the heartbeat thread started by _start_heartbeat_thread(), shared
    verbatim by both the sync and async held-with-heartbeat context managers
    (see this module's docstring, "Why the heartbeat runs on an OS thread,
    never an asyncio task", for why the async variant no longer has its own
    asyncio.Task implementation of this).

    Uses touch_resource(), NOT acquire_resource(): found in a later review
    round that acquire_resource()'s "already_holds_lock" reentry branch
    refreshes ONLY the Redis TTL, never lock_acquired_at -- so the 4-hour
    staleness heuristic would still eventually judge a long-held, actively
    heartbeating lock as abandoned and hand it to a different caller.
    touch_resource() (PipelineLockManager.touch_lock()) resets both.

    Each tick's touch_resource() lands in exactly one of three states, which
    this deliberately does NOT collapse together (see TouchResult, added in
    #146 WI-1 review for precisely this):

      - TouchResult.NOT_HELD -- CONFIRMED not held by this holder_id anymore.
        Logged as an ERROR: the lock was lost (e.g. a heartbeat delayed past
        the Redis TTL under scheduling starvation let a competing caller
        acquire it first) and the guarded operation is very likely now racing
        that competing caller. This cannot safely cancel/interrupt the
        guarded body from here (that would need real task cancellation wired
        through every caller), so it can only surface the condition loudly
        rather than silently continue as if nothing happened.

      - TouchResult.REFRESH_FAILED, or a raised exception -- the stores
        themselves failed, so liveness was NOT extended but nothing is known
        to have taken the lock. Logged and retried at the next interval
        rather than propagated; a RUN of them escalates from WARNING to ERROR
        once it has lasted HEARTBEAT_FAILURE_ESCALATION_SECONDS, at which
        point the lock's TTL really is at risk of lapsing under a still-live
        holder. Before the tri-state return this arrived as a plain False,
        indistinguishable from the case above -- so a sustained Redis outage
        logged an ERROR claiming the lock had been LOST to another holder
        (directly contradicting the ERROR touch_lock() itself logs one line
        earlier) on every tick, while the escalation written for that exact
        outage sat unreachable behind `except Exception`. A later round found
        the same escalation still unreachable for the Redis-writes-fail/
        reads-succeed outage (OOM, MISCONF, READONLY), because touch_lock()
        OR-ed its two write legs and reported a YAML-only write as a full
        refresh -- see its write path for why only the Redis leg extends
        anything that actually expires.

      - TouchResult.REFRESHED -- resets the failure run and its clock.
    """
    consecutive_failures = 0
    last_success_at = time.monotonic()
    while not stop_event.wait(heartbeat_interval_seconds):
        try:
            result = facade.touch_resource(project, resource_name, holder_id)
        except Exception as e:
            consecutive_failures += 1
            _log_heartbeat_failure(
                resource_name, project, consecutive_failures, time.monotonic() - last_success_at, str(e)
            )
            continue
        if result is TouchResult.REFRESH_FAILED:
            consecutive_failures += 1
            _log_heartbeat_failure(
                resource_name,
                project,
                consecutive_failures,
                time.monotonic() - last_success_at,
                "the lock's durable stores could not confirm or extend this holder's liveness",
            )
            continue
        # `not result` rather than an is-NOT_HELD check: TouchResult.__bool__
        # makes only REFRESHED truthy, and a facade that still returns a
        # plain bool (test doubles, and any future implementation of this
        # duck-typed facade) must keep meaning "lost" by False.
        if not result:
            consecutive_failures += 1
            logger.error(
                f"'{resource_name}' lock heartbeat for project {project!r} found "
                "the lock is NO LONGER held by this holder -- it was lost "
                "(e.g. to staleness recovery while a refresh was delayed); the "
                "operation this heartbeat guards may now be racing a different "
                "holder of the same resource"
            )
            continue
        if consecutive_failures:
            logger.info(
                f"'{resource_name}' lock heartbeat for project {project!r} recovered "
                f"after {consecutive_failures} consecutive failed refresh(es)"
            )
        consecutive_failures = 0
        last_success_at = time.monotonic()


def _log_heartbeat_failure(
    resource_name: str,
    project: str,
    consecutive_failures: int,
    seconds_since_success: float,
    detail: str,
) -> None:
    """Log one failed heartbeat refresh, escalating a sustained run of them
    from WARNING to ERROR -- see HEARTBEAT_FAILURE_ESCALATION_SECONDS."""
    prefix = (
        f"'{resource_name}' lock heartbeat refresh failed for project {project!r}: "
        f"{detail} -- {consecutive_failures} consecutive failure(s) over "
        f"{seconds_since_success:.0f}s"
    )
    if seconds_since_success >= HEARTBEAT_FAILURE_ESCALATION_SECONDS:
        logger.error(
            f"{prefix}; the {REDIS_LOCK_TTL_SECONDS:.0f}s Redis lock TTL is now at "
            "real risk of lapsing while this hold is still live, which would let a "
            "second caller acquire the same resource concurrently"
        )
    else:
        logger.warning(f"{prefix} -- will retry at the next heartbeat interval")


def _start_heartbeat_thread(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    heartbeat_interval_seconds: float,
) -> Tuple[threading.Event, threading.Thread]:
    """Start the heartbeat OS thread for one hold; returns its stop event and
    the thread, which the caller must set/join on exit."""
    stop_event = threading.Event()
    heartbeat_thread = threading.Thread(
        target=_heartbeat_worker,
        args=(facade, resource_name, project, holder_id, heartbeat_interval_seconds, stop_event),
        daemon=True,
        name=f"lock-heartbeat-{resource_name}-{project}",
    )
    heartbeat_thread.start()
    return stop_event, heartbeat_thread


async def _join_heartbeat_thread_async(heartbeat_thread: threading.Thread) -> None:
    """
    Wait for a stopped heartbeat thread to actually exit, from an async exit
    path, without blocking the event loop and without letting a cancellation
    abandon a heartbeat's in-flight touch_resource() call (#140 item 34).

    The join itself must have NO timeout -- found in review: a bounded join
    (the original implementation used timeout=5) could return while a
    heartbeat's in-flight touch_resource() call is still running; the outer
    code would then release the lock, and the orphaned call could complete
    AFTER that release and silently re-establish the lock under this
    now-abandoned holder_id, leaking it until the next staleness recovery. An
    unbounded join is safe here specifically because the only thing that can
    still be running after stop_event is set is at most one single
    touch_resource() call already in flight, itself bounded by
    PipelineLockManager's own Redis socket timeouts.

    The async variant previously did `await heartbeat_task`, which reopened
    that same leak through a different door: a bare await re-raises
    CancelledError the instant the enclosing task is cancelled, abandoning
    the in-flight call exactly as a too-short timeout would. Unlike
    _acquire_and_start_heartbeat_off_loop()'s orphan cleanup, a done-callback
    can't fix this one -- the release that must not overtake the join runs
    synchronously in the CALLER's finally, immediately after this returns --
    so on cancellation the join is finished here and now, on this thread. That is a
    plain Thread.join(), which needs no event loop, so it completes even while
    the cancellation is unwinding.

    The submission itself is INSIDE the try, not above it -- found in review:
    BaseEventLoop.run_in_executor() raises its RuntimeErrors synchronously at
    call time (_check_closed()'s "Event loop is closed",
    _check_default_executor()'s "Executor shutdown has been called", and
    ThreadPoolExecutor.submit()'s "cannot schedule new futures after
    shutdown"), never through the awaited future. With the call above the
    try, the shutdown fallback below could never fire for the one scenario it
    names: asyncio.run()'s own teardown calls shutdown_default_executor(), so
    any hold still unwinding after that point skipped the join entirely AND
    replaced the guarded body's real exception with a RuntimeError about
    asyncio internals.
    """
    loop = asyncio.get_running_loop()
    try:
        join_future = loop.run_in_executor(None, heartbeat_thread.join)
        # shield(), matching _acquire_and_start_heartbeat_off_loop()'s
        # reasoning: a cancellation delivered here must not tear down the
        # join itself.
        await asyncio.shield(join_future)
    except asyncio.CancelledError:
        heartbeat_thread.join()
        raise
    except RuntimeError:
        # asyncio's default executor refuses new work once the loop/interpreter
        # is shutting down -- join directly instead of skipping the join.
        heartbeat_thread.join()


@asynccontextmanager
async def _held_with_heartbeat_async(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
    heartbeat: Optional[Tuple[threading.Event, threading.Thread]] = None,
):
    """
    Wraps an already-acquired lock's held duration with a background
    heartbeat that periodically calls touch_resource() with the SAME
    holder_id -- see HEARTBEAT_INTERVAL_SECONDS above for why this is
    necessary, not just defensive, and _heartbeat_worker() for what each tick
    actually does.

    The heartbeat runs on a real OS thread, NOT an asyncio.Task, so it fires
    even while the guarded body blocks the event loop for its whole duration
    (which both real callers do -- see this module's docstring, "Why the
    heartbeat runs on an OS thread, never an asyncio task"). Identical to
    _held_with_heartbeat_sync() apart from how the thread is joined on exit.

    `heartbeat` adopts a thread the ACQUIRING worker thread already started
    (_acquire_and_start_heartbeat_off_loop) instead of starting one here.
    Production callers always pass it: starting the thread here would put an
    event-loop scheduling delay between the acquire succeeding and the first
    refresh being possible, and a loop blocked by another task's guarded body
    can stretch that gap past the Redis TTL on an already-held lock -- see
    the module docstring. Omitting it (tests that exercise the held-duration
    behavior directly) starts the thread here instead.
    """
    stop_event, heartbeat_thread = heartbeat if heartbeat is not None else _start_heartbeat_thread(
        facade, resource_name, project, holder_id, heartbeat_interval_seconds
    )
    try:
        yield
    finally:
        stop_event.set()
        await _join_heartbeat_thread_async(heartbeat_thread)


@contextmanager
def _held_with_heartbeat_sync(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
):
    """Synchronous counterpart of _held_with_heartbeat_async() -- see its
    docstring for the full rationale, identical otherwise."""
    stop_event, heartbeat_thread = _start_heartbeat_thread(
        facade, resource_name, project, holder_id, heartbeat_interval_seconds
    )
    try:
        yield
    finally:
        stop_event.set()
        # No timeout -- see _join_heartbeat_thread_async() for why a bounded
        # join here would risk releasing the lock while a heartbeat's in-flight
        # touch_resource() call is still running, which could then re-leak
        # the lock after release.
        heartbeat_thread.join()


async def _default_facade_off_loop() -> ProjectResourceLockManager:
    """
    Build the default ProjectResourceLockManager off the event loop (#140
    item 7).

    ProjectResourceLockManager() defaults to get_pipeline_lock_manager(),
    whose double-checked-locking guard is held across a full
    PipelineLockManager() construction -- including a Redis connect + .ping()
    with socket_connect_timeout=5. If the event-loop thread reaches that
    guard while a background thread is mid-construction, the loop stalls for
    up to that timeout. Bounded and one-time per process (main.py also warms
    the singleton off the loop before anything concurrent starts), but there
    is no reason for the loop to be the thread that waits.
    """
    return await asyncio.to_thread(ProjectResourceLockManager)


def _acquire_and_start_heartbeat(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    issue_number: Optional[int],
    heartbeat_interval_seconds: float,
) -> Tuple[bool, str, Optional[Tuple[threading.Event, threading.Thread]]]:
    """
    One acquire_resource() attempt plus, on success, the heartbeat thread for
    the hold it just won -- deliberately in ONE callable so that both happen
    on the same worker thread with no event-loop scheduling in between.

    Found in review of #146 WI-1: with the heartbeat started by the awaiting
    coroutine instead, a lock could sit acquired-but-unheartbeated for as long
    as the event loop stayed blocked (another task's guarded body -- the case
    this whole module is designed around -- blocks it for the agent's entire
    runtime, up to 10800s), letting the 7200s Redis TTL lapse under a hold
    that was already granted. See the module docstring.

    Returns (can_execute, reason, heartbeat), where heartbeat is the
    (stop_event, thread) pair to adopt when can_execute is True and None
    otherwise.
    """
    can_execute, reason = facade.acquire_resource(project, resource_name, holder_id)
    if not can_execute:
        return can_execute, reason, None
    try:
        heartbeat = _start_heartbeat_thread(
            facade, resource_name, project, holder_id, heartbeat_interval_seconds
        )
    except BaseException:
        # The lock IS held by the time we get here, but the awaiting
        # coroutine will only ever see the exception -- so nobody downstream
        # knows to release it. Release before propagating rather than leaving
        # a holder nothing will ever clean up.
        logger.error(
            f"'{resource_name}' lock for project {project!r} ({_attribution(issue_number)}) "
            "was acquired but its heartbeat thread could not be started -- releasing "
            "immediately rather than holding it with no liveness refresh"
        )
        _release_and_warn(facade, resource_name, project, holder_id, issue_number)
        raise
    return can_execute, reason, heartbeat


async def _acquire_and_start_heartbeat_off_loop(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    issue_number: Optional[int],
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
) -> Tuple[bool, str, Optional[Tuple[threading.Event, threading.Thread]]]:
    """
    Run one _acquire_and_start_heartbeat() attempt in a worker thread instead
    of on the event loop (#140 item 6).

    acquire_resource() is synchronous I/O -- a Redis transaction plus a YAML
    file read/write on the fallback path -- and the async context managers
    poll it every poll_interval_seconds for up to timeout_seconds, so under
    contention calling it inline stalls the shared event loop on every tick,
    despite those context managers' own "never blocks the event loop" claim.

    Mirrors services/docker_socket_access_gate.py's acquire() exactly,
    including its shield()/done-callback pair, for the same reason: offloading
    the call is what makes a cancellation able to interleave with it at all,
    and a concurrent.futures worker already executing CANNOT be interrupted.
    Cancelling the awaiting task only stops US from watching it -- the attempt
    runs on, and if it succeeds after we've unwound, that holder is never
    released by anyone, wedging this project's resource lock until
    PipelineLockManager's own TTL/staleness recovery eventually reclaims it
    (7200s-14400s). Without shield() the future's own state would be CANCELLED
    by the time the done-callback ran, so the callback could not even observe
    whether the orphaned attempt actually acquired anything.
    """
    loop = asyncio.get_running_loop()
    attempt = loop.run_in_executor(
        None,
        functools.partial(
            _acquire_and_start_heartbeat,
            facade,
            resource_name,
            project,
            holder_id,
            issue_number,
            heartbeat_interval_seconds,
        ),
    )
    try:
        return await asyncio.shield(attempt)
    except asyncio.CancelledError:
        attempt.add_done_callback(
            functools.partial(
                _release_if_orphan_acquired, facade, resource_name, project, holder_id, issue_number
            )
        )
        raise


def _release_if_orphan_acquired(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    issue_number: Optional[int],
    attempt,
) -> None:
    """Done-callback for an acquisition future whose awaiting coroutine was
    cancelled -- see _acquire_and_start_heartbeat_off_loop(). If the orphaned
    attempt went on to actually acquire the lock, nothing else will ever stop
    its heartbeat or release it, so this does both (heartbeat first, so a
    refresh in flight can't re-establish the lock after the release). Runs
    synchronously on the event loop thread (asyncio's own done-callback
    contract) -- a brief, one-off cost only on this rare cancellation
    path."""
    try:
        if attempt.cancelled():
            return
        can_execute, _, heartbeat = attempt.result()
        if can_execute:
            logger.warning(
                f"'{resource_name}' lock acquisition for project {project!r} "
                f"({_attribution(issue_number)}) was cancelled, but its orphaned "
                "background attempt succeeded after the fact -- releasing the phantom "
                "holder immediately instead of leaving it for TTL/staleness recovery "
                "to eventually reclaim."
            )
            if heartbeat is not None:
                stop_event, heartbeat_thread = heartbeat
                stop_event.set()
                heartbeat_thread.join()
            _release_and_warn(facade, resource_name, project, holder_id, issue_number)
    except Exception as cleanup_exc:
        logger.warning(
            f"Failed to auto-release orphaned '{resource_name}' holder for project "
            f"{project!r} ({_attribution(issue_number)}): {cleanup_exc}"
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
    non-blocking attempt, run in a worker thread that also starts this hold's
    heartbeat the moment the attempt succeeds (see
    _acquire_and_start_heartbeat_off_loop) -- with asyncio.sleep() between
    attempts, so the poll genuinely never blocks the event loop, until
    acquired or timeout_seconds elapses. Releases in a finally block so an
    exception raised inside the `with` body still frees the lock.

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
    facade = facade if facade is not None else await _default_facade_off_loop()
    holder_id = _mint_unique_holder_id()
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason, heartbeat = await _acquire_and_start_heartbeat_off_loop(
            facade, RESOURCE_NAME, project, holder_id, issue_number
        )
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise _timeout_error(RESOURCE_NAME, project, issue_number, timeout_seconds, reason)
        _log_busy(RESOURCE_NAME, project, issue_number, reason, poll_interval_seconds)
        await asyncio.sleep(poll_interval_seconds)

    try:
        async with _held_with_heartbeat_async(
            facade, RESOURCE_NAME, project, holder_id, heartbeat=heartbeat
        ):
            yield
    finally:
        # Deliberately synchronous, not offloaded -- shape (c) in this
        # module's docstring. NOT because awaits are cancellable in general
        # (_join_heartbeat_thread_async offloads an exit path safely with
        # shield + a synchronous fallback), but because a SKIPPED release has
        # no orphan-cleanup path: nothing else in the process knows this
        # holder_id, so it would leak until TTL/staleness recovery. Bounded by
        # PipelineLockManager's own Redis socket timeouts.
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
        with _held_with_heartbeat_sync(facade, RESOURCE_NAME, project, holder_id):
            yield
    finally:
        _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)

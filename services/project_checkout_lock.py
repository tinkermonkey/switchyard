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

  (c) The final release -- shape (b) again, with one extra rule
      (_release_and_warn_async). It has shape (b)'s "must complete" problem
      for shape (b)'s reason: there is no orphan-cleanup path for a SKIPPED
      release, because nothing else in the process knows this holder_id, so a
      release that never happens leaks the lock until TTL/staleness recovery
      (7200s-14400s). The extra rule is that its cancellation fallback does
      NOT re-run the release on the calling thread the way the join does --
      see that function.

      This was the module's one deliberate on-loop exception until #153 WI-8,
      justified by "bounded by PipelineLockManager's own Redis socket
      timeouts". That stopped being true when release_lock() gained its
      '<state>.yaml.acquire.lock' guard: the dominant term became
      RELEASE_GUARD_TIMEOUT_SECONDS + RELEASE_GUARD_RETRY_TIMEOUT_SECONDS of
      poll-sleeping on the calling thread, spent under a failure mode
      (Redis down, so every try_acquire_lock() takes that same guard on its
      YAML-fallback path) that this module's own waiters make near-continuous.

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
from typing import Dict, List, Optional, Tuple

from services.pipeline_lock_manager import LOCK_TTL_SECONDS, ReleaseResult
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


# ---------------------------------------------------------------------------
# In-process registry of live resource-lock waits and holds (#140 item 9)
# ---------------------------------------------------------------------------
# services/pipeline_watchdog.py decides a pipeline run is a zombie from "marked
# active, older than zombie_threshold_minutes, and no agent container running",
# with hand-written exemptions for the two in-process executors that
# legitimately sit containerless for a long time (review_cycle_executor.
# active_cycles, human_feedback_loop_executor.active_loops). A dispatch parked
# in the poll loop below is a third such state and had no equivalent signal: it
# waits up to DEFAULT_TIMEOUT_SECONDS (~3h) with no container to find, so past
# 30 minutes the watchdog reaped the run and redispatched the same issue while
# the original coroutine was still waiting -- and that coroutine then acquires
# the lock and launches its OWN container, giving one issue two concurrent
# executions.
#
# This registry is the missing signal, in the same shape as those two
# exemptions: process-local, in memory only, no durable state, and strictly
# bounded by the lifetime of the `with`/`async with` frame that registered it
# (an abandoned or crashed caller unwinds the frame and deregisters itself).
# Registration deliberately spans BOTH the wait AND the hold: the guarded body
# of the dev_container_build lock is an image build, which runs no container
# labelled for the issue either, so a hold is just as invisible to the
# watchdog's container probe as a wait is.
#
# Every activity carries a per-phase BUDGET, and an over-budget one stops
# counting as proof of life (_ResourceLockActivity.is_expired). Without that the
# exemption has no upper bound and a hung guarded body makes its pipeline run
# permanently un-reapable: a wait can only outlive its own timeout_seconds by a
# poll interval, but the hold is bounded solely by the operation inside it, and
# on the non-Docker path that bound does not exist -- claude_integration.
# _run_claude_code_locally() (every requires_docker: false agent) blocks on
# subprocess readline() with no timeout of any kind, so a Claude CLI that stalls
# with no output would keep the run 'active' and its board lock held until the
# orchestrator process restarts. Past the budget the watchdog goes back to
# treating the run as a zombie: double execution is the worse failure, but only
# for as long as somebody is plausibly still working.
_resource_activity_guard = threading.Lock()
_resource_activity: Dict[Tuple[str, int], List["_ResourceLockActivity"]] = {}

# Ceiling on how long a HELD activity still counts as proof of life. Matches
# docker_runner's agent_hard_timeout default -- also the longest timeout any
# agent is configured with in config/foundations/agents.yaml -- because that is
# the longest a guarded body (an agent container run, an image build, a local
# Claude CLI session) can legitimately take. A WAITING activity gets a different
# budget: the caller's own timeout_seconds, which its poll loop already enforces.
HELD_ACTIVITY_MAX_SECONDS = 10800.0

# Slack on a WAITING activity's budget, so a poll that is in flight when the
# deadline passes isn't declared hung a beat before the loop itself raises
# ProjectCheckoutLockTimeoutError and unwinds the frame.
WAITING_ACTIVITY_GRACE_SECONDS = 60.0


class _ResourceLockActivity:
    """One live acquisition attempt (and, once acquired, hold) of a resource lock."""

    __slots__ = (
        "resource_name",
        "project",
        "issue_number",
        "started_at",
        "phase",
        "phase_started_at",
        "budget_seconds",
    )

    def __init__(
        self,
        resource_name: str,
        project: str,
        issue_number: int,
        wait_budget_seconds: float,
    ):
        self.resource_name = resource_name
        self.project = project
        self.issue_number = issue_number
        self.started_at = time.monotonic()
        self.phase = "waiting"
        # Budgeted per PHASE rather than per activity: a wait that legitimately
        # ran most of its timeout and then acquired the lock must start the hold
        # with a fresh allowance, not an already-spent one.
        self.phase_started_at = self.started_at
        self.budget_seconds = wait_budget_seconds + WAITING_ACTIVITY_GRACE_SECONDS

    def mark_held(self) -> None:
        self.phase = "held"
        self.phase_started_at = time.monotonic()
        self.budget_seconds = HELD_ACTIVITY_MAX_SECONDS

    def is_expired(self) -> bool:
        """True once this phase has run longer than it could legitimately need --
        i.e. the frame is still registered but the work inside it is hung."""
        return (time.monotonic() - self.phase_started_at) > self.budget_seconds

    def describe(self) -> str:
        age_minutes = (time.monotonic() - self.started_at) / 60
        return (
            f"'{self.resource_name}' lock {self.phase} for {age_minutes:.1f} minutes"
        )


@contextmanager
def _tracked_resource_activity(
    resource_name: str,
    project: str,
    issue_number: Optional[int],
    wait_budget_seconds: float = DEFAULT_TIMEOUT_SECONDS,
):
    """
    Publish this acquisition to the registry above for its whole lifetime.

    Yields the activity (so the caller can mark_held() once acquired), or None
    when there is no issue to attribute it to -- project_checkout_lock's
    issue_number is optional and log-only, and an activity nothing can key on
    is an activity nothing can look up. Never raises out of registration:
    failing to publish must not take down the guarded operation itself.

    Args:
        wait_budget_seconds: the caller's own acquisition timeout -- how long the
            wait phase can legitimately last before its poll loop gives up. Past
            that (plus WAITING_ACTIVITY_GRACE_SECONDS) the activity stops
            vouching for the issue; see the registry comment above.
    """
    if issue_number is None:
        yield None
        return

    activity = _ResourceLockActivity(
        resource_name, project, issue_number, wait_budget_seconds
    )
    key = (project, issue_number)
    with _resource_activity_guard:
        _resource_activity.setdefault(key, []).append(activity)
    try:
        yield activity
    finally:
        with _resource_activity_guard:
            entries = _resource_activity.get(key)
            if entries is not None:
                try:
                    entries.remove(activity)
                except ValueError:
                    pass
                if not entries:
                    _resource_activity.pop(key, None)


# Public name for the registrar above. services/project_workspace.py publishes
# its own per-epic serializer wait through this same registry (code review on
# #151/WI-6): a thread queued on that plain threading.Lock is exactly as
# invisible to the watchdog's container probe as one queued in the poll loop
# below, and for the same reason -- it is waiting to run work that has no
# container yet. The registry is deliberately generic over resource_name (the
# key is (project, issue_number), and describe_active_resource_lock_activity()
# just reports whichever resource the oldest live activity names), so it needs
# no change to carry a second waiter type.
tracked_resource_activity = _tracked_resource_activity


def describe_active_resource_lock_activity(project: str, issue_number: int) -> Optional[str]:
    """
    Describe the oldest live resource-lock wait/hold for (project, issue_number),
    or None when this process has none.

    A non-None return means some coroutine or thread in THIS process is inside a
    project_checkout / dev_container_build lock context manager for this issue and
    is going to run the guarded operation once it gets the lock. Callers that
    reap or redispatch stalled work (services/pipeline_watchdog.py) must treat
    that as "still legitimately in flight", exactly as they already treat an
    entry in review_cycle_executor.active_cycles.

    An activity past its own budget reports as None even though its frame is
    still registered (see _ResourceLockActivity.is_expired): at that point the
    guarded operation is hung rather than working, and continuing to vouch for it
    would leave its pipeline run un-reapable for the life of the process. It is
    logged at warning on the way past, so the hang itself is visible and not just
    the reaping that follows.

    Returns a description rather than the activity object on purpose: the only
    two things a caller needs are whether anything is live and what to say about
    it in a log line.
    """
    with _resource_activity_guard:
        entries = list(_resource_activity.get((project, issue_number), ()))
    if not entries:
        return None

    live = []
    for activity in entries:
        if activity.is_expired():
            logger.warning(
                f"Resource lock activity for {project} issue #{issue_number} is past its "
                f"budget ({activity.describe()}) -- treating it as hung rather than as work "
                f"in flight. The guarded operation is not going to finish on its own; the "
                f"pipeline run is no longer protected from zombie cleanup."
            )
        else:
            live.append(activity)

    if not live:
        return None
    return min(live, key=lambda a: a.started_at).describe()


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
    if released:
        return
    if released is ReleaseResult.SERIALIZATION_FAILED:
        # Distinct from a refusal, and much worse (found in the WI-8 review
        # round): the release did not complete, so this holder_id's lock is
        # still (at least partly) recorded -- and nothing else in the process
        # knows this holder_id, so there is no orphan-cleanup path for it. It
        # leaks until the Redis TTL or the 4-hour staleness heuristic, blocking
        # every acquisition of this resource for this project meanwhile.
        # Reported at ERROR, and not as "may already be released or retained",
        # which would send an operator looking for a retained lock that does
        # not exist.
        logger.error(
            f"'{resource_name}' lock release for project {project!r} "
            f"({_attribution(issue_number)}) could not be serialized against a "
            f"concurrent acquire/refresh -- it did NOT complete for holder "
            f"{holder_id} and will now leak until TTL/staleness recovery"
        )
        return
    logger.warning(
        f"'{resource_name}' lock release for project {project!r} "
        f"({_attribution(issue_number)}) returned {released} -- lock may already be "
        "released or retained"
    )


def _log_offloaded_release_outcome(
    resource_name: str, project: str, issue_number: Optional[int], future
) -> None:
    """Done-callback for an offloaded _release_and_warn() whose awaiting
    coroutine was cancelled -- see _release_and_warn_async(). The release
    itself keeps running in its worker thread and logs its own outcome; this
    exists only so an exception raised inside it is retrieved and logged
    rather than surfacing as an unretrieved-future warning at GC."""
    try:
        if future.cancelled():
            return
        future.result()
    except Exception as exc:
        logger.warning(
            f"'{resource_name}' lock release for project {project!r} "
            f"({_attribution(issue_number)}) raised after its awaiting coroutine "
            f"was cancelled: {exc}"
        )


async def _release_and_warn_async(
    facade: ProjectResourceLockManager, resource_name: str, project: str, holder_id: int, issue_number: Optional[int]
) -> None:
    """
    _release_and_warn() from an async exit path, without spending its wait on
    the event-loop thread (#153 WI-8 review round) -- shape (b) in this
    module's docstring, with one deliberate difference on cancellation.

    See shape (c) there for why the release stopped being safe to run inline:
    PipelineLockManager.release_lock()'s acquire guard now costs up to
    RELEASE_GUARD_TIMEOUT_SECONDS + RELEASE_GUARD_RETRY_TIMEOUT_SECONDS of
    `time.sleep(0.1)` polling on the calling thread, and every exit of an
    `async with project_checkout_lock_async(...)` runs on the loop that also
    does board polling, dispatch, the board-lock heartbeat sweep and
    progression for every project.

    Same shape and same reasons as _join_heartbeat_thread_async(): shield() so
    a cancellation delivered here cannot tear down the release itself, a
    synchronous fallback because a SKIPPED release has no orphan-cleanup path
    (nothing else in the process knows this holder_id, so it would leak until
    TTL/staleness recovery, 7200s-14400s), and the submission INSIDE the try
    because run_in_executor() raises its RuntimeErrors synchronously at call
    time, never through the future.

    One difference from the join: the cancellation fallback WAITS for the call
    already in flight instead of making the call itself. Thread.join() is
    idempotent; a release is not, and re-running it here would both report a
    second, spurious "may already be released" and spend the guard budget on
    the loop after all. Waiting on the executor's own completion event costs
    the same wall clock as the join's fallback does, on the same rare path,
    and leaves exactly one release_resource() call.
    """
    loop = asyncio.get_running_loop()
    finished = threading.Event()

    def release():
        try:
            _release_and_warn(facade, resource_name, project, holder_id, issue_number)
        finally:
            finished.set()

    try:
        release_future = loop.run_in_executor(None, release)
        await asyncio.shield(release_future)
    except asyncio.CancelledError:
        # Unbounded, exactly as _join_heartbeat_thread_async()'s fallback join
        # is, and bounded in practice by the same thing: release_lock()'s two
        # guard budgets. Returning here while the release is still in flight
        # would let the caller unwind past it.
        finished.wait()
        release_future.add_done_callback(
            functools.partial(
                _log_offloaded_release_outcome, resource_name, project, issue_number
            )
        )
        raise
    except RuntimeError:
        # asyncio's default executor refuses new work once the loop/interpreter
        # is shutting down -- release directly rather than skipping it.
        release()


# The Redis lock-key TTL every constant below is calibrated against.
# IMPORTED from PipelineLockManager rather than restated here -- found in
# review (#146 WI-1): this used to be a literal 7200.0 duplicating a bare
# `7200` spelled out at seven separate expire() call sites in
# pipeline_lock_manager.py, with nothing tying the two modules together. An
# operator shortening the real TTL there (e.g. to speed up stale-lock
# recovery) would have left HEARTBEAT_INTERVAL_SECONDS below racing its own
# key expiry with zero margin, and HEARTBEAT_FAILURE_ESCALATION_SECONDS
# unable to fire before the TTL lapsed -- silently, with the whole test suite
# still green. The relationship between the three is now asserted directly in
# tests/unit/services/test_project_checkout_lock.py.
REDIS_LOCK_TTL_SECONDS = float(LOCK_TTL_SECONDS)

# How often to refresh the Redis lock key's TTL while legitimately holding a
# lock built on this pattern. CRITICAL, found in code review and confirmed by
# direct source reading of PipelineLockManager.try_acquire_lock(): the Redis
# TTL is refreshed ONLY as a side effect of a repeat acquire_resource() call
# for the SAME holder_id (the "already_holds_lock" transaction branch does
# `pipe.expire(lock_key, LOCK_TTL_SECONDS)`) -- it is never refreshed
# proactively. This module's own acquire-then-yield-then-release usage calls
# acquire_resource() exactly ONCE per hold, so a hold that outlives the TTL
# with no heartbeat would have its Redis copy silently expire while still
# legitimately held. Worse: the acquire transaction's own check
# (`if lock_data and lock_data.get('lock_status') == 'locked'`) reads an
# expired key back as an EMPTY dict, which is falsy -- so a second caller's
# acquire attempt at that point succeeds immediately, with no check against
# the still-valid YAML copy at that point in the code path. Comfortably
# under half the TTL so at least one heartbeat always lands before expiry
# even under scheduling jitter.
HEARTBEAT_INTERVAL_SECONDS = REDIS_LOCK_TTL_SECONDS / 4.0

# How long a run of consecutive heartbeat failures may go on before it stops
# being a transient blip and starts genuinely threatening the TTL above.
# Found in review (#140 item 30): every failed refresh used to log the same
# WARNING whether it was one Redis hiccup or two hours of sustained failure
# with the TTL about to lapse under a still-live holder, so an operator had
# no signal distinguishing the two. Half the TTL leaves at least one more
# heartbeat interval of margin after the escalation fires.
#
# Found in a later review round (#146 WI-1): "a failed refresh" here means
# TouchResult.REFRESH_FAILED, not an exception. touch_lock() catches every
# store failure internally and returns rather than raising, so an escalation
# hung only off `except Exception` around touch_resource() could never fire
# for the sustained-outage case it was written for -- see _heartbeat_worker.
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


async def _poll_until_acquired_async(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    issue_number: Optional[int],
    timeout_seconds: float,
    poll_interval_seconds: float,
    error_cls: type = ProjectCheckoutLockTimeoutError,
) -> Optional[Tuple[threading.Event, threading.Thread]]:
    """
    Poll for `resource_name` until acquired or `timeout_seconds` elapses, and
    return the heartbeat handle for the hold it won.

    The acquire/poll/timeout loop shared by every polling context manager built
    on this pattern -- this module's project_checkout_lock_async() and
    services/dev_container_build_lock.py's dev_container_build_lock_async()
    (#140 items 5 and 22: four near-identical copies of these thirteen lines
    across two files, differing only in which sleep primitive they use and
    which exception type they raise, even after both modules already shared
    _timeout_error/_log_busy/_release_and_warn/_held_with_heartbeat_*).

    Deliberately NOT folded together with the sync variant below on some
    "sleep primitive" parameter: the two no longer differ only in that. This
    one acquires through _acquire_and_start_heartbeat_off_loop() -- a worker
    thread that also starts the heartbeat, with the shield()/orphan-release
    contract that goes with it -- and so has a heartbeat handle to return; the
    sync one calls acquire_resource() directly and has none.

    Raises:
        error_cls: not acquired within timeout_seconds. Each lock passes its
            own type -- see _timeout_error().
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason, heartbeat = await _acquire_and_start_heartbeat_off_loop(
            facade, resource_name, project, holder_id, issue_number
        )
        if can_execute:
            return heartbeat
        if time.monotonic() >= deadline:
            raise _timeout_error(
                resource_name, project, issue_number, timeout_seconds, reason,
                error_cls=error_cls,
            )
        _log_busy(resource_name, project, issue_number, reason, poll_interval_seconds)
        await asyncio.sleep(poll_interval_seconds)


def _poll_until_acquired_sync(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    issue_number: Optional[int],
    timeout_seconds: float,
    poll_interval_seconds: float,
    error_cls: type = ProjectCheckoutLockTimeoutError,
) -> None:
    """
    Synchronous counterpart of _poll_until_acquired_async(), for the sync
    context managers. Uses time.sleep() between polls -- MUST NOT be called
    from a coroutine running on an asyncio event loop; see
    project_checkout_lock_sync() for why that is more than a "blocks the loop"
    caveat here.

    Returns nothing: the sync path starts its heartbeat later, inside
    _held_with_heartbeat_sync().

    Raises:
        error_cls: not acquired within timeout_seconds.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason = facade.acquire_resource(project, resource_name, holder_id)
        if can_execute:
            return
        if time.monotonic() >= deadline:
            raise _timeout_error(
                resource_name, project, issue_number, timeout_seconds, reason,
                error_cls=error_cls,
            )
        _log_busy(resource_name, project, issue_number, reason, poll_interval_seconds)
        time.sleep(poll_interval_seconds)


def _stop_heartbeat_and_release(
    facade: ProjectResourceLockManager,
    resource_name: str,
    project: str,
    holder_id: int,
    issue_number: Optional[int],
    heartbeat: Optional[Tuple[threading.Event, threading.Thread]],
) -> None:
    """Stop a hold's heartbeat and then release it, in that order -- see
    _release_if_orphan_acquired(), whose off-loop cleanup this is. Swallows
    and logs its own failures because it usually runs fire-and-forget in a
    worker thread, with no caller left to observe the future."""
    try:
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
    on the event loop thread (asyncio's own done-callback contract), so the
    join-then-release pair it performs is handed to a worker thread rather than
    run there: the release's own wait is bounded by release_lock()'s two guard
    budgets, which is far too long to spend on the loop (#153 WI-8 review
    round; see _release_and_warn_async()). Both halves go in ONE callable
    because the ordering between them is load-bearing -- a heartbeat refresh
    still in flight must not land after the release and re-establish the lock.
    A done-callback cannot await, so if the executor refuses the work (loop
    tearing down) the pair is run here after all rather than skipped."""
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
            cleanup = functools.partial(
                _stop_heartbeat_and_release,
                facade, resource_name, project, holder_id, issue_number, heartbeat,
            )
            try:
                asyncio.get_running_loop().run_in_executor(None, cleanup)
            except RuntimeError:
                cleanup()
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
    # Spans the wait AND the hold -- see the registry's own comment above for
    # why the watchdog needs both halves published, not just the wait.
    #
    # The registry comment's rationale (an image build runs no container for the
    # probe to find) is dev_container_build's, not this lock's: THIS lock's
    # guarded body is claude_integration's `await run_agent_in_container(...)`,
    # which does run a labelled container, so during a healthy run the watchdog's
    # cheaper container probe already exempts it and publishing the hold changes
    # nothing. Publishing the hold matters in exactly one window -- container
    # gone (OOM-kill, docker daemon restart), coroutine still inside this frame
    # -- and in that window it is the difference between the watchdog reaping a
    # run whose coroutine is about to launch its own container and leaving it
    # alone. That is also why the hold is budgeted rather than unconditional: an
    # await that never returns would otherwise suppress zombie cleanup for the
    # life of the process.
    with _tracked_resource_activity(
        RESOURCE_NAME, project, issue_number, wait_budget_seconds=timeout_seconds
    ) as activity:
        heartbeat = await _poll_until_acquired_async(
            facade, RESOURCE_NAME, project, holder_id, issue_number,
            timeout_seconds, poll_interval_seconds,
        )

        if activity is not None:
            activity.mark_held()

        try:
            async with _held_with_heartbeat_async(
                facade, RESOURCE_NAME, project, holder_id, heartbeat=heartbeat
            ):
                yield
        finally:
            # Offloaded, not inline -- shape (c) in this module's docstring.
            # A SKIPPED release has no orphan-cleanup path (nothing else in
            # the process knows this holder_id), so it has to complete; but
            # release_lock()'s acquire guard makes "complete" cost up to two
            # guard budgets of poll-sleeping, which must not be spent on the
            # shared event loop.
            await _release_and_warn_async(
                facade, RESOURCE_NAME, project, holder_id, issue_number
            )


@contextmanager
def project_checkout_lock_sync(
    project: str,
    issue_number: Optional[int] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Synchronous counterpart of project_checkout_lock_async(), for the call
    sites that cannot await: ProjectWorkspaceManager.initialize_project()
    (startup, before any asyncio event loop is guaranteed to be running) and
    ProjectWorkspaceManager.get_or_create_epic_worktree() (a plain sync method
    whose async callers reach it through asyncio.to_thread).

    Uses time.sleep() between polls -- MUST NOT be called from a coroutine
    running on an asyncio event loop. Not merely because it would block the
    loop for its whole wait: every in-process holder of the project_checkout
    lock (claude_integration's `async with project_checkout_lock_async` around
    a container run, auto_commit, finalize_feature_branch_work) releases from a
    coroutine on that same loop, so a poll there starves the holder it is
    waiting for and the wait cannot succeed. get_or_create_epic_worktree()
    detects a running loop and clamps its own timeout to a single non-blocking
    attempt for exactly this reason; a new sync call site reachable from the
    loop needs the same treatment, or (better) an off-loop hop at its caller.

    See project_checkout_lock_async() for the full contract (including the
    `facade` test-injection parameter and the `issue_number` log-only
    caveat); identical semantics otherwise.
    """
    facade = facade if facade is not None else ProjectResourceLockManager()
    holder_id = _mint_unique_holder_id()
    with _tracked_resource_activity(
        RESOURCE_NAME, project, issue_number, wait_budget_seconds=timeout_seconds
    ) as activity:
        _poll_until_acquired_sync(
            facade, RESOURCE_NAME, project, holder_id, issue_number,
            timeout_seconds, poll_interval_seconds,
        )

        if activity is not None:
            activity.mark_held()

        try:
            with _held_with_heartbeat_sync(facade, RESOURCE_NAME, project, holder_id):
                yield
        finally:
            _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)


@asynccontextmanager
async def project_checkout_lock_if_shared_async(
    project: str,
    work_dir,
    issue_number: Optional[int] = None,
    **lock_kwargs,
):
    """
    Async context manager holding the project_checkout lock for the duration of
    the `with` block IFF `work_dir` genuinely IS `project`'s shared base clone,
    and yielding which of the two happened.

    The single choke point for #140 item 4. The guard it replaces --

        if workspace_manager.is_base_clone_dir(project, work_dir):
            async with project_checkout_lock_async(project, issue_number):
                return await do_the_work(...)
        return await do_the_work(...)

    -- was copy-pasted near-identically at three call sites
    (claude/claude_integration.py x2, services/auto_commit.py x1, the last of
    which had already collapsed its duplicated body onto a nullcontext in
    #149 item 23). Every copy is a place a future call site can forget the
    guard, or apply a change to one branch and miss the other. Centralising it
    here also puts the decision next to the lock whose contract explains it.

    Why the decision is `is_base_clone_dir()` and not "always lock": epic
    worktrees share their directory with nothing, so locking them would
    serialize sibling epics for no reason -- see is_base_clone_dir()'s own
    docstring, and note that it fails CLOSED (treats an unresolvable directory
    as the base clone), which is why callers must resolve a real work_dir
    before getting here rather than passing a '.' fallback.

    Args:
        project: Project name.
        work_dir: The directory the guarded operation will actually work in.
        issue_number: log attribution only, never the lock's holder identity --
            see this module's docstring.
        **lock_kwargs: forwarded to project_checkout_lock_async() (timeout_seconds
            / poll_interval_seconds / facade), for tests.

    Yields:
        True inside the hold, False having taken nothing. Callers that behave
        differently in the shared clone (auto_commit re-reads the branch after
        the wait, because another board can have moved HEAD while it waited)
        read this rather than calling is_base_clone_dir() a second time.

    Raises:
        ProjectCheckoutLockTimeoutError: the directory IS the shared base clone
            and the lock was not acquired within its timeout. Callers must not
            fall through to the unlocked path on this -- see the module
            docstring and services/resource_lock_errors.py.
    """
    # Function-local, matching auto_commit.py's own import of it:
    # services/project_workspace.py imports this module, so a module-level
    # import here would close the cycle.
    from services.project_workspace import workspace_manager

    if not workspace_manager.is_base_clone_dir(project, work_dir):
        yield False
        return

    async with project_checkout_lock_async(project, issue_number, **lock_kwargs):
        yield True

"""
Dev Container Build Lock

Serializes operations against a project's dev-container Docker image build --
the `docker build -f /workspace/{project}/Dockerfile.agent -t
{project}-agent:latest /workspace/{project}` that dev_environment_setup's
agent session issues itself (prompts/content/agents/dev_environment_setup/
guidelines.md), and the resulting project-keyed state file this codebase
tracks it with (services/dev_container_state.py, one YAML at
state/dev_containers/{project}.yaml per project).

Phase 2 item of the concurrency redesign (issue #56, parent #88, umbrella
#34), built on top of the project-scoped resource lock facade (#53,
services/project_resource_lock_manager.py) and mirroring the pattern
services/project_checkout_lock.py (#54) established for a different shared
resource (the project's shared base-clone directory). This module is a
deliberately separate resource/module rather than a reuse of that one -- see
#56's own issue text: the dev-container build is a logically distinct
resource from the checkout directory (different failure mode, different
current risk profile -- see below), even though both are guarded by the same
ProjectResourceLockManager facade underneath. What IS reused from that
module, rather than re-derived, is its unique-holder-id minting (see "Why
this reuses project_checkout_lock's holder id minting" below) -- the exact
piece #54's own review called out as a bug class worth not re-deriving from
scratch.

Current risk profile (from #56's own text) -- distinct from #54's resource
------------------------------------------------------------------------
Unlike the primary checkout (confirmed live-racing across boards today),
this resource is safe today only by accident of the Environment Support
board's own lock limiting it to one build at a time. It becomes unsafe the
moment Phase 3a raises that board's concurrency limit above 1, or if two
projects' builds ever interleave in a way that touches shared Docker daemon
state. Separately, and ALREADY live today independent of any board
concurrency limit: scripts/rebuild_project_images.py and
scripts/set_dev_container_verified.py both bypass PipelineLockManager (and
therefore this lock, until wired in below) entirely -- an operator running
either while a pipeline-driven build/verify is in flight can race it right
now. Both admin scripts are wired to acquire this same lock (see their own
call sites) as part of this issue specifically to close that.

Investigation: where does the actual build+verify orchestration run?
----------------------------------------------------------------------
config/foundations/agents.yaml sets `requires_docker: false` for
dev_environment_setup and dev_environment_verifier -- its own inline comment
calls dev_environment_setup the "ONLY agent allowed to run outside Docker,"
but that comment is itself stale: a third agent, pipeline_analysis, also
sets `requires_docker: false` (found in PR #138 review,
/pr-review-toolkit:review-pr -- see #140 items 20/26, closed by #152: that
agent used to acquire THIS lock too, scoped to a hardcoded
project="switchyard" regardless of which project actually ran, via
services/pipeline_run_analysis.py). claude/claude_integration.py's
run_claude_code() routes on the `use_docker` flag alone, not agent identity:
for any of these three agents it does NOT hand off to
docker_runner.run_agent_in_container() (which is what wraps a normal agent's
Claude Code session in its own nested container) -- it calls
_run_claude_code_locally() instead, which runs the Claude Code CLI as a
subprocess of the orchestrator process itself. For dev_environment_setup/
verifier specifically, that subprocess is what issues the actual
`docker build` / `docker inspect` calls (via its own Bash tool, against the
orchestrator's own docker socket mount) -- there is no separate, monitorable
"the build" step in orchestrator-side Python distinct from "run this agent's
Claude Code session". So the real orchestrator-side hook around the
build+verify window is exactly the hook #54 already uses for these agents'
local-execution path: run_claude_code()'s _run_claude_code_locally() call
site. This module's lock is acquired there, gated on AGENT IDENTITY
(BUILD_WINDOW_AGENTS / agent_holds_build_window() below), mirroring the
existing is_base_clone_dir()-gated project_checkout_lock acquisition
immediately above it in the same function.

Why the gate is agent identity and not the `use_docker` flag (#152, item B)
------------------------------------------------------------------------------
It originally was the flag -- "only dev_environment_setup/verifier ever reach
the local-execution branch" -- and that assumption was simply false. The
branch is reached by every caller that hands run_claude_code() a context with
requires_docker/use_docker false, which today is five more callers than the
two this lock exists for:

  - pipeline_analysis, via services/pipeline_run_analysis.py's post-run
    analysis. It queries Elasticsearch and never builds or inspects anything,
    yet it took this lock -- and, because that module hardcoded the project
    name, it took the *switchyard*-scoped one for every project's analysis, so
    an unrelated project's post-run analysis contended head-on with a real
    switchyard dev-container build. Its own timeout was then caught by a bare
    `except Exception` that dropped the analysis output entirely.
  - scripts/analyze_codebase.py (x3 discovery passes),
    scripts/generate_strategy.py and scripts/generate_artifacts.py, which pass
    `use_docker: False` with no agent_config at all. An ad hoc analysis or
    strategy run for a project could block for the full
    DEFAULT_TIMEOUT_SECONDS behind that project's real dev_environment_setup /
    verifier build.

None of those seven call sites builds an image, so none of them has anything
to serialize against; each was paying (up to) a ~1h wait for it. Keying the
gate on the agent whose session genuinely IS the build window fixes all of
them at once, and -- unlike the flag -- a new requires_docker: false agent
now defaults to NOT taking a build lock it has no use for, rather than
silently inheriting one. The corresponding risk (a future agent that DOES
build images and is not added to the set) is why the gate lives here, next to
the lock and its docstring, rather than as an inline literal at the call
site.

Two locks, and which one guards what (#152, item A)
------------------------------------------------------
The state file this lock exists for has TWO locks over it, and they are not
alternatives:

  * THIS lock (dev_container_build, Redis/YAML via
    ProjectResourceLockManager) guards the whole build/verify EXECUTION
    WINDOW -- the minutes-to-an-hour during which a `docker build` runs and
    the resulting verdict is decided. Every caller that owns such a window
    takes it: claude_integration.py's agent-gated wrap around the local
    Claude Code session, each admin script's wrap around its build +
    state-update sequence, and the rebuild endpoint's wrap around its
    IN_PROGRESS -> build -> BLOCKED sequence.
  * The state file's OWN lock (state/dev_containers/<project>.yaml.lock,
    fcntl via utils.file_lock) guards each individual read-modify-write of
    that YAML, inside DevContainerStateManager._merge_state()/_read_state().
    #56's issue text deferred set_status()'s blind read-modify-write as a
    Phase 0 item; #152 closed it here, because "every writer is inside the
    dev_container_build lock" was never true of this file and cannot be made
    true -- the pending-operation marker is deliberately written from the
    rebuild endpoint's REQUEST thread, before the lock wait even starts,
    precisely so the first poll can see it. Without a lock of its own, that
    unlocked writer's whole-file rewrite could land on top of a set_status()
    another container made in between and silently revert it. It is
    cross-process for the same reason: the observability server and the
    orchestrator are separate containers.

Ordering is fixed: build lock OUTERMOST, state-file lock INNERMOST, and the
state-file critical section never calls out -- it does one YAML
read-modify-write and returns. So there is no lock-order cycle to deadlock
on.

The verifier's in-session set_status() does not self-block on either
--------------------------------------------------------------------------
dev_environment_verifier's own prompt (Step 5,
prompts/content/agents/dev_environment_verifier/review_task.md) has its live
Claude Code session run inline Python that imports dev_container_state and
calls set_status() directly, via that session's own Bash tool -- WHILE the
session is running inside the very window this module's lock holds around
the whole local execution (see claude/claude_integration.py). That is why
set_status() must never acquire THIS lock internally: the call would be
serialized behind the session it is part of (the session can't finish
without the call returning; the call can't return until the session, which
holds the lock, finishes), and since every acquisition here mints its own
unique holder id (see below), PipelineLockManager's same-issue-number
reentrancy check does not rescue it -- it is a genuine self-block bounded
only by this module's timeout.

The state file's own lock does NOT reintroduce that: that in-session call
runs in a separate subprocess (the Claude Code CLI's Bash tool), so its
flock is on a different open file description from anything the orchestrator
thread holds, and the orchestrator thread is not holding the state-file lock
across the session anyway -- it holds it only for the microseconds of one
read-modify-write. What the state-file lock DOES do for that call is make it
safe: the in-session write and the orchestrator-side writes bracketing the
session no longer interleave mid-file.

The trap that replaces it: utils.file_lock is not re-entrant and raises
ReentrantFileLockError on a nested same-thread acquire, which both
_merge_state() and _read_state() swallow via `except Exception`, degrading
silently to False / {} -- get_status() would report UNVERIFIED for a VERIFIED
project. Nothing nests today, and the critical sections are kept to the one
YAML read-modify-write so nothing has cause to: anyone needing another field
inside one has to read it off the dict already loaded there rather than
calling back into DevContainerStateManager.

Known, deliberately accepted gaps in coverage
------------------------------------------------
This lock is held only around each *separately dispatched* invocation's own
local Claude Code session (dev_environment_setup's build session,
dev_environment_verifier's verify session -- these are distinct pipeline
stage executions, not one continuous operation) and around each admin
script's own build+state-update sequence. It does NOT cover:

  - services/agent_executor.py's own dev_container_state.set_status() calls
    that bracket the locked session from OUTSIDE it: the IN_PROGRESS mark
    immediately before dev_environment_setup's session starts, and the
    UNVERIFIED reset immediately after a failed session's exception has
    already propagated out of (and therefore released the lock inside)
    claude_integration.py. Both are single, synchronous statements taking
    microseconds, not the multi-minute build itself.
  - agents/dev_environment_verifier_agent.py's own set_status() calls, which
    run in the few lines immediately AFTER `await run_claude_code(...)`
    returns -- i.e. after this lock has already been released for that
    session.

Closing these completely would require this lock to span from before
agent_executor.py's IN_PROGRESS mark to after its failure-path reset --
which lives inside execute_agent(), a ~1000-line method that is the single
shared execution path for every agent this orchestrator runs, not just these
two. Reworking its control flow to hold a lock across that span (without
reindenting the whole method, which would carry its own review/regression
risk to every other agent) was judged out of scope for a surgical fix here.
The residual exposure is narrow -- at most a few Python statements running
immediately adjacent to the locked window, never the actual docker build or
the multi-minute verification session -- and is a state-file bookkeeping
race, not the Docker-daemon-level race #56 exists to close. Since #152 each
of those statements is at least atomic in itself: the state file's own lock
(see "Two locks" above) makes every one of them a serialized
read-modify-write, so what is still unguarded here is WHICH verdict wins, not
whether the file survives two of them landing together.

Bookkeeping writers, and why they get a NON-BLOCKING variant (#152, item A)
-----------------------------------------------------------------------------
#152 turned up three more writers of the state file that own no build window
at all, and are therefore a different shape from every caller above:

  - services/observability_server.py's /api/projects/<project>/rebuild-image
    endpoint. This one IS a build owner -- it calls the same
    scripts/rebuild_project_images.rebuild_project_image() an operator would
    run from a shell -- and gets the ordinary blocking
    dev_container_build_lock_sync() around its whole IN_PROGRESS -> build ->
    BLOCKED sequence, exactly like the two admin scripts. Because
    rebuild_project_image() takes this same lock itself and every acquisition
    mints its own holder id (no reentrancy -- see below), it is called with
    lock_held_by_caller=True from inside that wrap; nesting the two would be a
    genuine self-block, not a reentrant hold. It passes a shorter timeout than
    the default and, on a timeout, records the drop in the state file rather
    than only logging it -- that file is the only feedback channel it has once
    it has answered the request (see its own comment).

  - services/work_execution_state.py's cleanup_stuck_in_progress_states(),
    which reconciles a dev_environment_setup/verifier execution that died
    mid-flight -- five read-then-write points, all reached right after a
    crash/restart.

That last one is an OBSERVER doing single-statement bookkeeping, and giving
it the blocking context manager would have been the wrong tool twice over. It
sits on a path that must not stall: the reconciliation runs during
orchestrator startup (from main.py, and from inside that project's execution
state file lock), and a blocking acquire there would hold that file lock for
the whole wait. So it takes dev_container_build_lock_if_free_sync(): a SINGLE
non-blocking attempt that yields True when the lock was taken and False when
it was not. On False the caller skips its write entirely. That is deliberately
not the "never proceed unlocked" rule being bent -- the guarded write does not
happen at all, which is the safe direction here (a live holder's own status
write wins).

What that skip is NOT is self-healing on its own, and two review rounds on
#152 were both about writers that assumed it was:

  - A busy acquire does not imply a LIVE holder. Right after a restart the
    previous process's lock is routinely still in Redis under its own TTL
    (PipelineLockManager only reclaims it after its 4-hour staleness heuristic
    or the 7200s TTL), and a dead holder writes nothing, ever. That is exactly
    the crash-during-build case the reconciliation exists for, so the skip was
    not occasional there -- it was deterministic. main.py now recovers the
    orphaned locks of THIS process's own dead predecessor at startup, BEFORE
    cleanup_stuck_in_progress_states() runs, so the acquire succeeds (see
    ProjectResourceLockManager.recover_orphaned_resource_locks -- it is
    deliberately not a blanket release, since observability-server runs as its
    own container and legitimately holds this lock across an orchestrator
    restart); and when the acquire still does not succeed, that reconciliation
    leaves its execution record `in_progress` for the next sweep to retry
    rather than consuming it.
  - "Not acquired" is not always contention at all. try_acquire_lock() also
    fails CLOSED on unknown/degraded lock state (both stores unreadable, the
    YAML-fallback acquire guard unavailable) and refuses a retained lock from
    a failed run. In none of those is anyone holding a build window, so
    acquire_failure_is_contention() below classifies the reason and
    _log_skipped() reports a degraded outcome at ERROR with the real reason
    rather than narrating a holder that does not exist.

pipeline/repair_cycle.py's _finalize_unconfirmed_changes_needed() was the
third writer wired to the non-blocking variant, and is now on the BLOCKING one
with a bounded timeout instead. It forces the terminal CHANGES_NEEDED ->
BLOCKED transition when the env rebuild sub-cycle stops driving it, and unlike
the writers above it holds no file lock, is not on a startup path, and its
whole purpose is that CHANGES_NEEDED has no other owner -- so a skipped write
there is a project no scheduler will ever touch again, not a bookkeeping blip.
It also has the one interleaving that makes a busy lock most likely: the
verifier writes CHANGES_NEEDED from INSIDE its own Claude Code session (see
prompts/content/agents/dev_environment_verifier/review_task.md Step 5), i.e.
while this lock is still held for the rest of that session, so the sub-cycle's
30s poll routinely observes CHANGES_NEEDED inside the holder's window. A
bounded wait outlasts that tail; a single non-blocking attempt did not.

Holding the lock is also what makes those writers' check-then-act atomic
rather than merely serialized: each re-reads get_status() INSIDE the lock and
re-decides there, so a status that changed while the caller was deciding
(e.g. an operator rebuild that just succeeded) is seen rather than clobbered.
Locking the write alone would not have fixed that -- the decision was already
stale by the time the write ran.

These do not register with project_checkout_lock's watchdog activity registry
the way the blocking variants do: that registry exists to stop the zombie
reaper redispatching a run parked in a poll loop, and there is no poll loop
here -- the guarded body is a single state-file write, never work the reaper
could mistake for a hung container.

Why this reuses project_checkout_lock's holder id minting
-------------------------------------------------------------
See services/project_checkout_lock.py's own module docstring ("Why every
acquisition gets its own unique holder id, not the caller's real
issue_number") for the full rationale: PipelineLockManager.try_acquire_lock()
treats a matching issue_number as reentrant with no other identity check,
which is wrong for a mutual-exclusion lock guarding independent, possibly
concurrent operations that merely happen to share a project or issue number.
That module's fix -- mint a fresh, process-unique, always-negative holder id
per acquisition via _mint_unique_holder_id(), never the caller's real
issue_number -- applies unchanged to this lock's identical shape (also a
project-scoped mutual-exclusion lock via the same ProjectResourceLockManager
facade). Rather than re-derive that fix (and risk re-deriving the bug it was
found to fix), this module imports and reuses that exact function, so both
locks mint from the same process-wide, process-restart-safe counter.

Acquisition order relative to services/project_checkout_lock.py
-------------------------------------------------------------------
When a call site needs both locks (today, only claude/claude_integration.py's
run_claude_code() local-execution branch does), acquire THIS lock
(dev_container_build) OUTER and project_checkout_lock INNER -- see
project_checkout_lock.py's own module docstring for why a future call site
reversing this order would risk a deadlock/mutual-timeout between two
concurrent operations.
"""

import logging
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from services.project_checkout_lock import (
    _acquire_and_start_heartbeat_off_loop,
    _attribution,
    _default_facade_off_loop,
    acquire_failure_is_contention,
    _held_with_heartbeat_async,
    _held_with_heartbeat_sync,
    _mint_unique_holder_id,
    _poll_until_acquired_async,
    _poll_until_acquired_sync,
    _release_and_warn,
    _release_and_warn_async,
    _tracked_resource_activity,
)
from services.project_resource_lock_manager import ProjectResourceLockManager

logger = logging.getLogger(__name__)

# Reserved resource name for this lock -- distinct from project_checkout
# (services/project_checkout_lock.py) and from any real board name (see
# RESOURCE_BOARD_PREFIX in project_resource_lock_manager.py).
RESOURCE_NAME = "dev_container_build"

# The agents whose local (non-Docker) Claude Code session IS this project's
# dev-container build/verify window, and therefore the only agents
# claude/claude_integration.py wraps in this lock. See the module docstring
# ("Why the gate is agent identity and not the `use_docker` flag") -- keying on
# `use_docker` instead swept in pipeline_analysis and four scripts/ entry
# points that never build anything.
#
# Kept next to the lock rather than inline at the call site so a future agent
# that genuinely does build images has one obvious place to be added; nothing
# derives this from config/foundations/agents.yaml, because no field there
# expresses "this agent's session issues the project's docker build" (both
# requires_docker and tools_enabled: docker_operations are true of agents that
# have nothing to do with this resource).
BUILD_WINDOW_AGENTS = frozenset({"dev_environment_setup", "dev_environment_verifier"})

# Generous enough to outlast the longest legitimate holder of this lock.
# config/foundations/agents.yaml sets timeout: 3600 for BOTH
# dev_environment_setup and dev_environment_verifier (the two agents whose
# local Claude Code sessions this lock wraps -- see module docstring). 100s
# of margin over that, mirroring project_checkout_lock.py's own
# margin-over-the-longest-legitimate-holder reasoning (that module's 1900s
# over a then-1800s ceiling).
#
# As with project_checkout_lock.py: this does NOT outlast
# PipelineLockManager's own staleness/TTL recovery (7200s Redis TTL, up to
# 14400s YAML-fallback staleness), so a genuinely crashed holder's lock is
# not reliably recoverable within one call's wait here. Raising loudly
# (DevContainerBuildLockTimeoutError) and letting the next dispatch/operator
# retry is the intended behavior, not a bug -- see project_checkout_lock.py's
# own "Blocking vs failing" section for the full reasoning, which applies
# unchanged here.
DEFAULT_TIMEOUT_SECONDS = 3700.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0


class DevContainerBuildLockTimeoutError(RuntimeError):
    """Raised when the dev_container_build resource lock could not be
    acquired within the configured timeout -- surfaced loudly rather than
    silently skipping (or silently running unlocked) the guarded build/verify
    operation or admin-script mutation."""


def agent_holds_build_window(agent: Optional[str]) -> bool:
    """
    True when `agent`'s local Claude Code session is this project's
    dev-container build/verify window, and so must be serialized by this lock.

    The gate claude/claude_integration.py's local-execution branch uses. False
    for every other agent (and for a missing/unknown agent name): an agent this
    module has never heard of does not build images, and making it wait out one
    that does is the exact defect #152 item B exists to fix. A new
    image-building agent belongs in BUILD_WINDOW_AGENTS above -- there is no
    safe way to infer membership, so this deliberately does not guess.
    """
    return agent in BUILD_WINDOW_AGENTS


# _poll_until_acquired_{async,sync}()/_release_and_warn()/_held_with_heartbeat_*()
# are shared with services/project_checkout_lock.py (imported above) rather than
# duplicated here -- both modules are the same poll/timeout/release shape over
# the same ProjectResourceLockManager facade, differing only in resource_name
# and exception type (both passed explicitly to the shared helpers below). Found
# in review (#56): keeping two independently-maintained copies risked a fix
# to one (e.g. #54's own holder-id-uniqueness bug, or its later
# DEFAULT_TIMEOUT_SECONDS correction) silently not reaching the other.
#
# #140 items 5 and 22 closed the last of that duplication: the acquire/poll/
# timeout loop itself, which survived as four near-identical copies across the
# two modules (async + sync, here + there) even after the helpers above were
# shared. _timeout_error()/_log_busy() are now reached only through those
# shared loops, so this module no longer imports them directly.


@asynccontextmanager
async def dev_container_build_lock_async(
    project: str,
    issue_number: Optional[int] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Async context manager serializing access to `project`'s dev-container
    build/state resource for the duration of the `with` block.

    Polls ProjectResourceLockManager.acquire_resource() -- a single
    non-blocking attempt, run in a worker thread that also starts this hold's
    heartbeat the moment the attempt succeeds (see
    project_checkout_lock._acquire_and_start_heartbeat_off_loop) -- with
    asyncio.sleep() between attempts, so the poll genuinely never blocks the
    event loop, until acquired or timeout_seconds elapses. Releases in a
    finally block so an exception raised inside the `with` body still frees
    the lock.

    See this module's docstring for exactly what this lock is (and is not)
    held around, and why dev_container_state.set_status() itself is
    deliberately NOT made to acquire this lock internally.

    Args:
        project: Project name.
        issue_number: GitHub issue number to attribute this hold to in LOG
            MESSAGES only -- not used as the lock's holder identity (every
            acquisition mints its own; see project_checkout_lock.py's module
            docstring, reused unchanged here). Pass None (the default) when
            no real issue is in scope at the call site.
        timeout_seconds / poll_interval_seconds: override only for tests.
        facade: injected ProjectResourceLockManager -- for tests only.
            Production call sites omit this and get the default facade,
            which shares the process-wide PipelineLockManager singleton.

    Raises:
        DevContainerBuildLockTimeoutError: not acquired within timeout_seconds.
    """
    facade = facade if facade is not None else await _default_facade_off_loop()
    holder_id = _mint_unique_holder_id()
    # Publishes the wait AND the hold to project_checkout_lock's in-process
    # registry so the zombie watchdog can tell a dispatch that is legitimately
    # blocked here from one that has genuinely died -- see that registry's
    # comment. Matters at least as much for this lock as for project_checkout:
    # the operation it guards is an image build, which runs no container
    # labelled for the issue for the watchdog's probe to find.
    #
    # The hold is budgeted (HELD_ACTIVITY_MAX_SECONDS), not unconditional: the
    # body guarded here is claude_integration._run_claude_code_locally(), whose
    # subprocess readline() loop has no timeout of any kind, so a Claude CLI that
    # stalls with no output would otherwise keep this issue's pipeline run
    # exempt from zombie cleanup until the orchestrator process restarts.
    with _tracked_resource_activity(
        RESOURCE_NAME, project, issue_number, wait_budget_seconds=timeout_seconds
    ) as activity:
        heartbeat = await _poll_until_acquired_async(
            facade, RESOURCE_NAME, project, holder_id, issue_number,
            timeout_seconds, poll_interval_seconds,
            error_cls=DevContainerBuildLockTimeoutError,
        )

        if activity is not None:
            activity.mark_held()

        try:
            async with _held_with_heartbeat_async(
                facade, RESOURCE_NAME, project, holder_id, heartbeat=heartbeat
            ):
                yield
        finally:
            # Offloaded, not inline -- see the same finally in
            # project_checkout_lock_async() for why.
            await _release_and_warn_async(
                facade, RESOURCE_NAME, project, holder_id, issue_number
            )


@contextmanager
def dev_container_build_lock_sync(
    project: str,
    issue_number: Optional[int] = None,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Synchronous counterpart of dev_container_build_lock_async(), for callers
    with no asyncio event loop guaranteed to be running -- namely
    scripts/rebuild_project_images.py and scripts/set_dev_container_verified.py,
    both plain synchronous CLI scripts. Uses time.sleep() between polls --
    MUST NOT be called from a coroutine running on an asyncio event loop,
    which it would block.

    See dev_container_build_lock_async() for the full contract (including the
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
            error_cls=DevContainerBuildLockTimeoutError,
        )

        if activity is not None:
            activity.mark_held()

        try:
            with _held_with_heartbeat_sync(facade, RESOURCE_NAME, project, holder_id):
                yield
        finally:
            _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)


# try_acquire_lock() returns False for reasons that are NOT "a live holder has
# this right now", and treating all of them as contention was a defect found in
# review of #152: every one of these means nobody is inside a build window and
# nobody is going to write a fresher status, so a caller that skips its write on
# one of them is dropping it for good, while the log line narrates a holder that
# does not exist.
#
#   - lock_state_unknown_failing_closed        (both Redis and YAML reads failed)
#   - lock_acquire_serialization_timeout       (acquire guard contended)
#   - lock_acquire_serialization_unavailable   (   "     "    unopenable)
#   - lock_mirror_write_failed                 (granted in Redis, but the
#                                               durable YAML copy did not land)
#   - lock_mirror_write_failed_while_held      (same, on a lock this caller
#                                               ALREADY holds -- see
#                                               refusal_leaves_caller_holding_lock)
#   - lock_write_failed                        (YAML fallback: the lock was not
#                                               recorded in EITHER store)
#   - locked_by_issue_<n>_failed               (retained after a failed run --
#                                               a durable marker, not a holder)
#
# See PipelineLockManager.try_acquire_lock() for each one's own comment.
# acquire_failure_is_contention() moved to services/project_checkout_lock.py
# (#169): project_checkout_lock_if_free_sync() needs the identical
# classification for the identical reason, and the reasons it classifies come
# from the shared ProjectResourceLockManager facade rather than from anything
# specific to this lock. Re-exported here (it is imported at the top of this
# module) so `from services.dev_container_build_lock import
# acquire_failure_is_contention` keeps working.


def _log_skipped(project: str, issue_number: Optional[int], reason: str) -> None:
    if acquire_failure_is_contention(reason):
        logger.warning(
            f"'{RESOURCE_NAME}' lock for project {project!r} ({_attribution(issue_number)}) "
            f"is busy ({reason}) -- skipping this bookkeeping write of the dev container "
            f"state rather than waiting out a build for it or clobbering the holder's own "
            f"status. See services/dev_container_build_lock.py's module docstring "
            f"(\"Bookkeeping writers\")."
        )
        return
    logger.error(
        f"'{RESOURCE_NAME}' lock for project {project!r} ({_attribution(issue_number)}) "
        f"could not be taken ({reason}) -- this is NOT contention: no build/verify holds "
        f"this project's container state and nothing else is going to write a fresher "
        f"status. The bookkeeping write was still skipped (this module never writes "
        f"unlocked), so this project's dev container state may now be stale. See "
        f"services/dev_container_build_lock.py's module docstring (\"Bookkeeping writers\")."
    )


@asynccontextmanager
async def dev_container_build_lock_if_free_async(
    project: str,
    issue_number: Optional[int] = None,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Async context manager making ONE non-blocking attempt at `project`'s
    dev_container_build lock, and yielding whether it got it.

    For bookkeeping writers of dev_container_state that own no build window --
    see this module's docstring ("Bookkeeping writers, and why they get a
    NON-BLOCKING variant") for why waiting would be both harmful and useless
    for them. Yields True inside the hold (release and heartbeat handled
    exactly as dev_container_build_lock_async() does), or False having taken
    nothing, in which case the caller MUST skip its write rather than perform
    it unlocked.

    Never raises DevContainerBuildLockTimeoutError: contention is the ordinary,
    expected outcome here rather than a failure, and manufacturing a lock
    timeout for a caller that deliberately did not wait would misreport it to
    every consumer of services/resource_lock_errors.is_lock_timeout_error().

    Args:
        project: Project name.
        issue_number: log attribution only -- see
            dev_container_build_lock_async().
        facade: injected ProjectResourceLockManager -- for tests only.
    """
    facade = facade if facade is not None else await _default_facade_off_loop()
    holder_id = _mint_unique_holder_id()
    # Off the event loop for the same reason the polling variant is: this is a
    # Redis transaction plus (on the fallback path) a YAML read/write. See
    # project_checkout_lock.py's "no synchronous lock I/O runs on the
    # event-loop thread" rule and _acquire_and_start_heartbeat_off_loop()'s
    # own shield/orphan-release contract.
    can_execute, reason, heartbeat = await _acquire_and_start_heartbeat_off_loop(
        facade, RESOURCE_NAME, project, holder_id, issue_number
    )
    if not can_execute:
        _log_skipped(project, issue_number, reason)
        yield False
        return

    try:
        async with _held_with_heartbeat_async(
            facade, RESOURCE_NAME, project, holder_id, heartbeat=heartbeat
        ):
            yield True
    finally:
        # Offloaded for the same reason dev_container_build_lock_async()'s
        # release is -- see project_checkout_lock._release_and_warn_async().
        await _release_and_warn_async(
            facade, RESOURCE_NAME, project, holder_id, issue_number
        )


@contextmanager
def dev_container_build_lock_if_free_sync(
    project: str,
    issue_number: Optional[int] = None,
    facade: Optional[ProjectResourceLockManager] = None,
):
    """
    Synchronous counterpart of dev_container_build_lock_if_free_async(), for
    callers with no asyncio event loop guaranteed to be running -- namely
    services/work_execution_state.py's post-restart reconciliation, reached
    from main.py's startup path.

    Identical semantics otherwise; see that function for the full contract.
    Unlike dev_container_build_lock_sync() this never sleeps, so it is safe on
    startup and inside another file lock.
    """
    facade = facade if facade is not None else ProjectResourceLockManager()
    holder_id = _mint_unique_holder_id()
    can_execute, reason = facade.acquire_resource(project, RESOURCE_NAME, holder_id)
    if not can_execute:
        _log_skipped(project, issue_number, reason)
        yield False
        return

    try:
        with _held_with_heartbeat_sync(facade, RESOURCE_NAME, project, holder_id):
            yield True
    finally:
        _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)

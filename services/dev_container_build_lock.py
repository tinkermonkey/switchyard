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
/pr-review-toolkit:review-pr -- see #140 for the resulting lock-acquisition
gap: pipeline_analysis unconditionally acquires THIS lock too, scoped to a
hardcoded project="switchyard" regardless of which project actually ran,
via services/pipeline_run_analysis.py). claude/claude_integration.py's
run_claude_code() acts on the `use_docker` flag alone, not agent identity:
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
site. This module's lock is acquired there, gated on the same `use_docker`
flag (see claude/claude_integration.py) that also lets pipeline_analysis
reach it, mirroring the existing is_base_clone_dir()-gated
project_checkout_lock acquisition immediately above it in the same
function.

Why dev_container_state.set_status() itself is deliberately left unlocked
---------------------------------------------------------------------------
#56's own issue text flags set_status()'s blind read-modify-write as
"already flagged as a Phase 0 item (don't re-scope the locking fix itself
here, just confirm it as this issue's acquire-target once the project-level
lock exists)". Investigation confirms locking set_status() itself would be
actively wrong here, not just out of scope: dev_environment_verifier's own
prompt (Step 5, prompts/content/agents/dev_environment_verifier/
review_task.md) instructs its live Claude Code session to run inline Python
that imports dev_container_state and calls set_status() directly, via that
session's own Bash tool -- WHILE the session is running inside the very
window this module's lock holds around the whole local execution (see
claude/claude_integration.py). If set_status() also tried to acquire this
same resource lock internally, that in-session call would be strictly
serialized behind the very session it is part of -- the session can't finish
without that call returning, and that call can't return until the session
(which holds the lock) finishes. Every acquisition here mints its own unique
holder id (see below), so this is NOT rescued by PipelineLockManager's
same-issue-number reentrancy check -- it is a genuine self-block, bounded
only by this module's own timeout, not a legitimate resolution. So
set_status() stays exactly as unlocked as it was before this issue;
protection instead comes from every caller that owns a build/verify
execution window (claude_integration.py's agent-gated wrap, and each admin
script's own wrap around its build+state-update sequence) acquiring this
lock around that whole window before calling set_status() at all.

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
race, not the Docker-daemon-level race #56 exists to close.

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

import asyncio
import logging
import time
from contextlib import asynccontextmanager, contextmanager
from typing import Optional

from services.project_checkout_lock import (
    _acquire_and_start_heartbeat_off_loop,
    _default_facade_off_loop,
    _held_with_heartbeat_async,
    _held_with_heartbeat_sync,
    _log_busy,
    _mint_unique_holder_id,
    _release_and_warn,
    _timeout_error,
)
from services.project_resource_lock_manager import ProjectResourceLockManager

logger = logging.getLogger(__name__)

# Reserved resource name for this lock -- distinct from project_checkout
# (services/project_checkout_lock.py) and from any real board name (see
# RESOURCE_BOARD_PREFIX in project_resource_lock_manager.py).
RESOURCE_NAME = "dev_container_build"

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


# _timeout_error()/_log_busy()/_release_and_warn() are shared with
# services/project_checkout_lock.py (imported above) rather than duplicated
# here -- both modules are the same poll/timeout/release shape over the same
# ProjectResourceLockManager facade, differing only in resource_name and
# exception type (both passed explicitly to the shared helpers below). Found
# in review (#56): keeping two independently-maintained copies risked a fix
# to one (e.g. #54's own holder-id-uniqueness bug, or its later
# DEFAULT_TIMEOUT_SECONDS correction) silently not reaching the other.


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
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason, heartbeat = await _acquire_and_start_heartbeat_off_loop(
            facade, RESOURCE_NAME, project, holder_id, issue_number
        )
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise _timeout_error(RESOURCE_NAME, project, issue_number, timeout_seconds, reason, error_cls=DevContainerBuildLockTimeoutError)
        _log_busy(RESOURCE_NAME, project, issue_number, reason, poll_interval_seconds)
        await asyncio.sleep(poll_interval_seconds)

    try:
        async with _held_with_heartbeat_async(
            facade, RESOURCE_NAME, project, holder_id, heartbeat=heartbeat
        ):
            yield
    finally:
        # Deliberately synchronous, not offloaded -- see the same finally in
        # project_checkout_lock_async() for why.
        _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)


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
    deadline = time.monotonic() + timeout_seconds
    while True:
        can_execute, reason = facade.acquire_resource(project, RESOURCE_NAME, holder_id)
        if can_execute:
            break
        if time.monotonic() >= deadline:
            raise _timeout_error(RESOURCE_NAME, project, issue_number, timeout_seconds, reason, error_cls=DevContainerBuildLockTimeoutError)
        _log_busy(RESOURCE_NAME, project, issue_number, reason, poll_interval_seconds)
        time.sleep(poll_interval_seconds)

    try:
        with _held_with_heartbeat_sync(facade, RESOURCE_NAME, project, holder_id):
            yield
    finally:
        _release_and_warn(facade, RESOURCE_NAME, project, holder_id, issue_number)

"""
Agent Orchestrator Integration Layer

This module provides the integration between the main orchestrator and the agent system,
replacing the legacy agent_stages.py with a proper factory-based approach.
"""

import logging
import uuid
from enum import Enum
from typing import Dict, Any, Optional
from datetime import datetime, timedelta
from pipeline.base import PipelineStage
from agents import AGENT_REGISTRY, get_agent_class
from services.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)


# How long a dev container status may sit at IN_PROGRESS before it's treated as stale
# rather than genuinely still setting up. See the staleness check in
# validate_task_can_run below.
STALE_IN_PROGRESS_MINUTES = 20

# Same idea for CHANGES_NEEDED, which the repair cycle's env-rebuild sub-cycle is
# supposed to own (see the CHANGES_NEEDED branch below). It is the ONLY status
# whose retry has no owner other than that sub-cycle, so any path that stops the
# sub-cycle without completing its terminal CHANGES_NEEDED -> BLOCKED transition
# used to leave the project unschedulable forever, with the operator-facing reason
# still claiming a retry was under way (#152 review). A live sub-cycle never parks
# here for long -- it polls every 30s and resets to UNVERIFIED at the top of its
# next attempt -- so 30 minutes is comfortably above any healthy window while still
# bounding the damage from a sub-cycle that died mid-flight.
STALE_CHANGES_NEEDED_MINUTES = 30


class DevSetupQueueOutcome(Enum):
    """What queue_dev_environment_setup() actually did.

    That function has four ways to return without enqueuing anything -- three
    deferrals plus a failed status write -- and used to report all of them the
    same way it reports success: by returning None. Neither caller could tell
    the two apart, and both assumed the good one (#169 review):

      - the dispatch path below emitted a "Development environment setup has
        been queued ... auto_queued: True" decision event on every pass, so a
        project deferring to somebody else's build produced a stream of
        successful recoveries for work that was never enqueued; and
      - repair_cycle's env-rebuild sub-cycle followed the call with an
        unbounded `while True` poll for a terminal status that, with nothing
        queued, nobody was going to write.

    Deferral is not failure -- it means another run already covers this project
    and the next board poll is the retry point -- so callers need the
    distinction, not just an exception. `queued` is the only question most of
    them ask; the members are separate so the event stream can say WHICH.
    """

    QUEUED = 'queued'
    # A setup is IN_PROGRESS and the mark is still fresh.
    DEFERRED_IN_PROGRESS = 'deferred_in_progress'
    # IN_PROGRESS went stale, but _dev_setup_in_flight_reason() found a live run.
    DEFERRED_RUN_IN_FLIGHT = 'deferred_run_in_flight'
    # The dev_container_build lock is held by a holder confirmed still alive --
    # not merely by a row saying so (see _live_build_lock_reason).
    DEFERRED_BUILD_LOCK_HELD = 'deferred_build_lock_held'
    # The IN_PROGRESS mark could not be written, so nothing was enqueued.
    FAILED_STATUS_WRITE = 'failed_status_write'

    @property
    def queued(self) -> bool:
        """True iff a dev_environment_setup task actually reached the queue."""
        return self is DevSetupQueueOutcome.QUEUED


async def validate_task_can_run(task, logger) -> Dict[str, Any]:
    """
    Validate if a task can run based on agent requirements

    Args:
        task: Task object
        logger: Logger instance

    Returns:
        Dict with 'can_run' (bool), 'reason' (str), 'needs_dev_setup' (bool)
    """
    from config.manager import config_manager
    from services.dev_container_state import dev_container_state, DevContainerStatus

    # Get agent configuration
    agent_config = config_manager.get_project_agent_config(task.project, task.agent)
    requires_dev_container = getattr(agent_config, 'requires_dev_container', False)

    if not requires_dev_container:
        # Agent doesn't need dev container, can always run
        return {'can_run': True, 'reason': 'No dev container required'}

    # Check dev container status.
    #
    # The status and the timestamp both staleness branches below age against come
    # from ONE snapshot (#171). Read separately -- get_status() then
    # get_status_updated_at(), each taking the state file's lock on its own --
    # they could straddle an interleaving write and pair a status from before it
    # with a timestamp from after, which is how a genuinely fresh IN_PROGRESS
    # gets aged out as stuck (and a setup re-queued on top of a live one).
    status, updated_at = dev_container_state.get_status_and_updated_at(task.project)

    if status == DevContainerStatus.VERIFIED:
        return {'can_run': True, 'reason': 'Dev container verified and ready'}
    elif status == DevContainerStatus.IN_PROGRESS:
        # Staleness fallback: IN_PROGRESS has no built-in timeout, so a setup/verifier
        # task that dies without ever reaching a terminal status (crashed, or the
        # orchestrator restarted mid-run) would otherwise defer every task for this
        # project every 30s, forever, with no error ever surfaced. 20 minutes is well
        # above the ~3 minutes setup+verification normally takes, so this only fires
        # once something has genuinely gone stale, not on a slow-but-healthy build.
        if updated_at and (datetime.now() - updated_at) > timedelta(minutes=STALE_IN_PROGRESS_MINUTES):
            logger.log_warning(
                f"Dev container for '{task.project}' has been stuck IN_PROGRESS since "
                f"{updated_at.isoformat()} (over {STALE_IN_PROGRESS_MINUTES} minutes) - "
                f"treating as unverified so setup gets retried instead of deferring forever"
            )
            return {
                'can_run': False,
                'reason': (
                    f"Dev container setup for '{task.project}' appears stuck "
                    f"(in_progress since {updated_at.isoformat()}) - retrying setup"
                ),
                'needs_dev_setup': True,
            }
        return {
            'can_run': False,
            'reason': f"Dev container setup currently in progress for '{task.project}'",
            'needs_dev_setup': False,
            'defer': True,
        }
    elif status == DevContainerStatus.BLOCKED:
        return {
            'can_run': False,
            'reason': f"Dev container setup is blocked for '{task.project}'. Check state/dev_containers/{task.project}.yaml for error details",
            'needs_dev_setup': False
        }
    elif status == DevContainerStatus.CHANGES_NEEDED:
        # Staleness fallback, mirroring the IN_PROGRESS one above. CHANGES_NEEDED is
        # driven by exactly one owner (the repair cycle's env-rebuild sub-cycle), so
        # a sub-cycle that stops without completing its own terminal transition --
        # crashed, or unable to take the dev_container_build lock to write BLOCKED --
        # leaves this status with nobody to move it and every future task for the
        # project refused forever. See STALE_CHANGES_NEEDED_MINUTES.
        if updated_at and (datetime.now() - updated_at) > timedelta(minutes=STALE_CHANGES_NEEDED_MINUTES):
            logger.log_warning(
                f"Dev container for '{task.project}' has been stuck CHANGES_NEEDED since "
                f"{updated_at.isoformat()} (over {STALE_CHANGES_NEEDED_MINUTES} minutes) - "
                f"no repair cycle is driving it, treating as unverified so setup gets retried"
            )
            return {
                'can_run': False,
                'reason': (
                    f"Dev container verification for '{task.project}' has been stuck at "
                    f"changes_needed since {updated_at.isoformat()} with no repair cycle "
                    f"driving it - retrying setup"
                ),
                'needs_dev_setup': True,
            }
        # needs_dev_setup=False (not the UNVERIFIED default) is deliberate: the
        # repair cycle's env-rebuild sub-cycle owns retrying this project and will
        # re-queue setup itself on its next attempt. If this returned
        # needs_dev_setup=True, an unrelated task validated against this project
        # while CHANGES_NEEDED is set (a brief window before the sub-cycle's own
        # reset) could trigger a second, redundant queue_dev_environment_setup()
        # call racing the sub-cycle's own retry.
        return {
            'can_run': False,
            'reason': f"Dev container verification for '{task.project}' could not confirm a required fix; the repair cycle is retrying",
            'needs_dev_setup': False
        }
    else:  # UNVERIFIED
        return {
            'can_run': False,
            'reason': f"Dev container not yet verified for project '{task.project}'",
            'needs_dev_setup': True
        }


def _dev_setup_in_flight_reason(
    project: str, logger, check_build_lock: bool = True
) -> Optional[str]:
    """
    Describe the dev_environment_setup run that is genuinely still alive for
    `project`, or None if none is.

    The IN_PROGRESS staleness window (STALE_IN_PROGRESS_MINUTES) measures the
    wall-clock age of the last status WRITE, and nothing refreshes that
    timestamp while a setup is queued or running: queue_dev_environment_setup()
    stamps IN_PROGRESS at ENQUEUE time, ORCHESTRATOR_WORKERS defaults to 1, and
    dev_environment_setup is configured timeout: 3600 precisely because image
    builds legitimately run long. So the window routinely elapses over a setup
    that is merely slow or still waiting for a worker, and treating that as
    "the run it would defer to is not coming back" queued a duplicate every 20
    minutes -- each one later serializing behind the real build on the
    dev_container_build lock, running a full redundant Claude-driven rebuild,
    flipping the project back to IN_PROGRESS and re-deferring every task for it
    (#152 review).

    These three probes are the liveness signal that age is not. Each is
    independently sufficient, and a probe that RAISES counts as "cannot rule out
    a live run": re-queueing wrongly costs an hour-scale redundant agent run,
    while skipping wrongly costs one 30-second sweep, since every caller of this
    is itself retried.

    Args:
        check_build_lock: run probe 3 (does a LIVE holder own the
            dev_container_build lock?).
            queue_dev_environment_setup() passes False when it is itself holding
            that lock (#171): probe 3 would find its own hold and report a build
            that is not running, turning the stale-IN_PROGRESS recovery into a
            permanent no-op. Nothing is lost by skipping it there -- having
            acquired the lock non-blockingly IS probe 3, and a stronger form of
            it: no other holder existed a moment ago, and none can appear while
            this caller keeps holding it. The caller that could NOT take the
            lock still passes True, because a refused acquire is not by itself
            evidence of a live build -- see _live_build_lock_reason(), which
            probe 3 delegates to and which the refused-acquire caller consults
            for the same reason.
    """
    from task_queue.task_manager import TaskQueue

    # 1. Still sitting in the queue, never dequeued. Automated setup tasks are
    #    always enqueued with issue_number 0 (see below and main.py's startup
    #    queueing), so the agent+project pair identifies them.
    try:
        pending = TaskQueue(use_redis=True).get_pending_tasks(agent='dev_environment_setup')
        for task in pending:
            if task.project == project:
                return f"a dev_environment_setup task ({task.id}) is still queued"
    except Exception as e:
        logger.warning(
            f"Could not check the task queue for a pending dev_environment_setup for "
            f"{project}: {e} - assuming one may be queued rather than risk a duplicate"
        )
        return "the task queue could not be checked"

    # 2. Dequeued and running: an execution record still marked in_progress.
    try:
        from services.work_execution_state import work_execution_tracker
        state = work_execution_tracker.load_state(project, 0)
        for execution in state.get('execution_history', []):
            if (
                execution.get('agent') == 'dev_environment_setup'
                and execution.get('outcome') == 'in_progress'
            ):
                return (
                    f"a dev_environment_setup execution started "
                    f"{execution.get('timestamp')} is still in progress"
                )
    except Exception as e:
        logger.warning(
            f"Could not check execution state for a running dev_environment_setup for "
            f"{project}: {e} - assuming one may be running rather than risk a duplicate"
        )
        return "the execution state could not be checked"

    # 3. Building. The setup session's own `docker build` holds this for the whole
    #    build window, which is the part that outlasts the staleness window most
    #    often -- and a LIVE holder means a build genuinely IS running, whoever
    #    started it. Skipped for a caller that already holds it; see
    #    check_build_lock.
    if not check_build_lock:
        return None

    return _live_build_lock_reason(project, logger)


def _live_build_lock_reason(project: str, logger) -> Optional[str]:
    """
    Describe the LIVE holder of `project`'s dev_container_build lock, or None
    when nothing is holding it -- including when a row says held but its holder
    is gone.

    "There is a lock row" is not "a build is running" (#169 review). A row
    outlives its holder in ways nothing in this process cleans up: a release
    that could not be serialized against a concurrent acquire/refresh does not
    complete, and the holder id it was for is known to nobody, so it leaks until
    the Redis TTL or the 4-hour staleness heuristic (see
    services/project_checkout_lock.py's _release_and_warn). Reading the row alone
    then defers every caller for that whole window to a build that does not
    exist and cannot be started -- and, on queue_dev_environment_setup()'s path,
    reports each deferral as a successful recovery, every 30s board poll, for
    hours.

    Liveness is the same evidence startup recovery uses to decide a lock's owner
    is gone: every holder of this lock heartbeats it (touch_resource(), which
    resets lock_acquired_at) several times inside
    FOREIGN_OWNER_LIVENESS_GRACE_SECONDS, so one that has gone quiet for longer
    is not working. Unknowns fail CLOSED -- an unreadable lock store, an
    unparseable timestamp -- because re-queueing wrongly costs an hour-scale
    redundant agent run while skipping wrongly costs one 30-second sweep.

    Returns:
        A reason string suitable for _dev_setup_in_flight_reason()'s contract
        (why a run cannot be ruled out), or None when the lock is genuinely not
        guarding anything.
    """
    try:
        from services.dev_container_build_lock import RESOURCE_NAME as DEV_CONTAINER_BUILD_RESOURCE
        from services.project_resource_lock_manager import (
            FOREIGN_OWNER_LIVENESS_GRACE_SECONDS,
            ProjectResourceLockManager,
        )
        manager = ProjectResourceLockManager()
        lock = manager.get_resource_lock(project, DEV_CONTAINER_BUILD_RESOURCE)
        if not lock or lock.retained_reason:
            return None
        if not manager.holder_liveness_is_fresh(lock):
            logger.warning(
                f"The dev_container_build lock for {project} says it has been held since "
                f"{lock.lock_acquired_at} (holder #{lock.locked_by_issue}), but nothing "
                f"has refreshed that holder's liveness in over "
                f"{FOREIGN_OWNER_LIVENESS_GRACE_SECONDS:.0f}s -- every holder heartbeats "
                f"this lock several times inside that window while it works, so this row "
                f"is abandoned rather than busy (a release that could not be serialized "
                f"leaks exactly this way). Treating it as unheld instead of deferring to "
                f"a build that is not running; it will be reclaimed by the TTL/staleness "
                f"path"
            )
            return None
        return (
            f"the dev_container_build lock has been held since "
            f"{lock.lock_acquired_at}, so a build is running"
        )
    except Exception as e:
        logger.warning(
            f"Could not check the dev_container_build lock for {project}: {e} - "
            f"assuming a build may be running rather than risk a duplicate"
        )
        return "the dev_container_build lock could not be checked"


async def queue_dev_environment_setup(
    project: str, logger, change_description: str = "", pipeline_run_id: str = None,
    cycle_stack: list = None,
) -> DevSetupQueueOutcome:
    """
    Queue a dev_environment_setup task for a project.

    Idempotent: skips queuing if setup is already IN_PROGRESS and that status is
    fresher than STALE_IN_PROGRESS_MINUTES -- past that window the run it would
    be deferring to is not coming back, and deferring to it means nothing is ever
    queued (see the guard's own comment).
    Sets status to IN_PROGRESS before enqueuing to prevent races, and takes this
    project's dev_container_build lock across both so the check and the mark are
    one critical section rather than two (#171). Also returns without queuing
    when that lock is held by a LIVE holder (#169 review) -- see the acquire
    below for why a loser of that race has to stop rather than carry on
    unserialized.

    Returns:
        DevSetupQueueOutcome -- whether a task was actually enqueued, and if
        not, which of the four no-op paths was taken. Callers MUST branch on
        it rather than on "did this raise": deferring is a normal, frequent
        outcome that raises nothing, and treating it as success is what made
        the dispatch path claim a queue that never happened and made
        repair_cycle poll forever for a status nobody would write (#169
        review). See the enum for the full account.

    Args:
        project: Project name
        logger: Logger instance
        change_description: Optional description of what env changes are needed,
            injected into the task issue body so the setup agent receives specific
            instructions (e.g. from systemic failure analysis).
        pipeline_run_id: If provided, tags the setup (and subsequent verifier) task
            with this ID so their events appear in stall-detection queries.
        cycle_stack: If provided, propagates the caller's cycle stack into the
            queued task context so agent_initialized events carry full hierarchy.
    """
    from services.dev_container_build_lock import (
        acquire_failure_is_contention,
        dev_container_build_lock_attempt_async,
    )
    from services.dev_container_state import dev_container_state, DevContainerStatus

    # The whole check-then-mark runs inside this project's dev_container_build
    # lock (#171). "Mark as in-progress BEFORE queuing to prevent races" below
    # only prevents them if the read that decided to mark and the mark itself
    # are one critical section: two concurrent callers for the same project
    # (an ordinary dispatch validating against it while repair_cycle's
    # env-rebuild sub-cycle re-queues its own attempt, say) both read a
    # non-IN_PROGRESS status, both wrote IN_PROGRESS, and both enqueued -- the
    # duplicate hour-scale rebuild _dev_setup_in_flight_reason()'s docstring
    # describes, arrived at from the other direction.
    #
    # NON-BLOCKING, because a held build window is exactly the case this
    # function must not wait for: dev_environment_setup and
    # dev_environment_verifier hold this lock for their whole session (see
    # agent_holds_build_window), so a bounded wait here would park a dispatch
    # behind the very build it is trying not to duplicate.
    #
    # What a refused acquire means depends on WHY, and the two answers are
    # opposite (#169 review). An earlier version fell back to the previous,
    # unserialized behaviour on any refusal, on the reasoning that this "closes
    # the race whenever the lock is free -- which is precisely the case the race
    # needs, since two racers both find it free and only one can win it". That
    # does not hold, because the loser did not stop: A wins the lock, B is
    # refused, B carries on, reads the status before A's IN_PROGRESS write
    # lands, and both enqueue. The lock changed which of them was serialized,
    # not how many tasks got queued.
    #
    #   - GENUINE CONTENTION, CONFIRMED LIVE: return without queuing. Somebody
    #     is inside this critical section right now -- either the winner of this
    #     exact race, who is queuing on our behalf, or a build/verify session
    #     holding its own window, which is a setup already running. Either way a
    #     second task is the duplicate hour-scale rebuild
    #     _dev_setup_in_flight_reason()'s docstring exists to prevent, and the
    #     next 30s board poll is the retry point if the winner somehow queued
    #     nothing. "Confirmed live" is not redundant: the refusal reason names
    #     the holder recorded in the row, not one observed to be running, and an
    #     abandoned row is indistinguishable from a busy one by that string alone
    #     -- see _live_build_lock_reason().
    #   - DEGRADED / FAIL-CLOSED / RETAINED: fall back and decide unserialized.
    #     Nobody holds a build window in any of these -- a retained lock is a
    #     marker left by a run that already ended -- so skipping would be a NEW
    #     way for a project to never get a setup queued at all.
    #     _dev_setup_in_flight_reason()'s probe 3 deliberately does not treat
    #     either as a live run, and this leaves that judgement where it already
    #     is.
    async with dev_container_build_lock_attempt_async(project) as (serialized, refusal_reason):
        unserialized_because = None
        if not serialized:
            unserialized_because = (
                f"{refusal_reason}: a degraded/fail-closed store, or a lock retained "
                f"after a failed run"
            )
        if not serialized and acquire_failure_is_contention(refusal_reason):
            # The refusal reason names a holder; whether that holder still
            # EXISTS is a separate question (#169 review). An abandoned row --
            # a release that could not be serialized, leaked until its TTL --
            # reads as genuine contention here, and short-circuiting on it
            # blocks every setup this project needs for the rest of that window
            # while reporting each deferral as a successful recovery.
            live_holder = _live_build_lock_reason(project, logger)
            if live_holder:
                logger.info(
                    f"Not queuing dev_environment_setup for {project}: its "
                    f"dev_container_build lock is held right now ({refusal_reason}) and "
                    f"{live_holder}, so either a setup/verify session is already running "
                    f"or another caller won this exact race and is queuing one. Deferring "
                    f"to it rather than queuing a duplicate; the next board poll retries "
                    f"if it did not."
                )
                return DevSetupQueueOutcome.DEFERRED_BUILD_LOCK_HELD
            unserialized_because = (
                f"{refusal_reason}, but that holder is gone -- see the warning above"
            )
        if not serialized:
            logger.warning(
                f"Deciding whether to queue dev_environment_setup for {project} "
                f"WITHOUT its dev_container_build lock -- the acquire failed for a "
                f"reason that is NOT a live holder ({unserialized_because}). Nothing "
                f"is in this critical section, so skipping would drop the setup for "
                f"good; the check-then-mark below runs unserialized instead, exactly "
                f"as it did before this lock existed."
            )

        # Check if setup is already in progress - avoid duplicate queuing.
        #
        # The guard is bounded by the same staleness window validate_task_can_run uses
        # to decide a task NEEDS setup. Without that bound the two disagreed and the
        # disagreement was silent: past STALE_IN_PROGRESS_MINUTES, validation returns
        # needs_dev_setup=True, its caller calls this function, this function sees
        # IN_PROGRESS and returns having queued nothing, and the status is never
        # written -- so the next task repeats it, forever, while emitting a
        # "Recovery successful" decision event each pass (#152 review).
        #
        # Read HERE rather than at the caller: the status that got this call made
        # was read in validate_task_can_run(), before the lock existed, and is
        # stale by construction (#171).
        current_status, updated_at = dev_container_state.get_status_and_updated_at(project)
        if current_status == DevContainerStatus.IN_PROGRESS:
            is_stale = bool(
                updated_at
                and (datetime.now() - updated_at) > timedelta(minutes=STALE_IN_PROGRESS_MINUTES)
            )
            if not is_stale:
                logger.info(f"Dev environment setup already in progress for {project}, skipping duplicate queue")
                return DevSetupQueueOutcome.DEFERRED_IN_PROGRESS
            # Age alone does not mean the run is gone -- nothing refreshes the status
            # timestamp while a setup is queued or building, so the window elapses over
            # healthy slow runs too. Only re-queue once no live run can be found.
            #
            # check_build_lock is skipped when this call is the one holding it:
            # probe 3 would find our own hold and report a build that is not
            # running, turning this recovery into a permanent no-op. Having
            # acquired the lock IS probe 3, more strongly.
            in_flight = _dev_setup_in_flight_reason(
                project, logger, check_build_lock=not serialized
            )
            if in_flight:
                logger.info(
                    f"Dev environment setup for {project} has been IN_PROGRESS since "
                    f"{updated_at.isoformat()} (over {STALE_IN_PROGRESS_MINUTES} minutes), but "
                    f"{in_flight} - skipping duplicate queue"
                )
                return DevSetupQueueOutcome.DEFERRED_RUN_IN_FLIGHT
            logger.warning(
                f"Dev environment setup for {project} has been IN_PROGRESS since "
                f"{updated_at.isoformat()} (over {STALE_IN_PROGRESS_MINUTES} minutes) with no "
                f"queued task, no running execution and no build holding the lock - "
                f"queuing a fresh setup rather than deferring to a run that is not coming back"
            )

        # Mark as in-progress BEFORE queuing to prevent races.
        #
        # And DO NOT queue if the mark did not land (#171 review). set_status()
        # returns False when its own write failed -- most plausibly the state
        # file's cross-process lock timing out at STATE_LOCK_TIMEOUT_SECONDS
        # against the verifier's in-session write or an operator running
        # scripts/rebuild_project_images.py -- and it only logs. Enqueuing
        # anyway is the duplicate this whole critical section exists to
        # prevent, arrived at from a third direction: the file still says
        # UNVERIFIED, so the next 30s board poll re-decides needs_dev_setup,
        # reaches here with the build lock now free, reads UNVERIFIED (so the
        # stale/in-flight probes never run) and queues a SECOND hour-scale
        # rebuild. Returning instead leaves the state file and the queue
        # agreeing -- nothing marked, nothing queued -- and that same next poll
        # retries the whole check-then-mark.
        # (#198) The tag belongs to the dev-container environment, which
        # several projects may share -- never compose it from the project name.
        from services.dev_container_environment import image_tag_for

        marked = dev_container_state.set_status(
            project,
            DevContainerStatus.IN_PROGRESS,
            image_name=image_tag_for(project)
        )
        if not marked:
            logger.error(
                f"Not queuing dev_environment_setup for {project}: its IN_PROGRESS "
                f"mark could not be written (see the dev container state error above). "
                f"Queuing a setup the state file does not record would let the next "
                f"board poll read the unchanged status and queue a second one; the "
                f"next poll retries this whole check-then-mark instead."
            )
            return DevSetupQueueOutcome.FAILED_STATUS_WRITE
        logger.info(f"Set dev container status to IN_PROGRESS for {project}")

        await _enqueue_dev_environment_setup(
            project, logger, change_description, pipeline_run_id, cycle_stack
        )
        return DevSetupQueueOutcome.QUEUED


async def _enqueue_dev_environment_setup(
    project: str,
    logger,
    change_description: str,
    pipeline_run_id: Optional[str],
    cycle_stack: Optional[list],
) -> None:
    """Build and enqueue the dev_environment_setup task, rolling the IN_PROGRESS
    mark back on failure.

    Split out of queue_dev_environment_setup() (#171) only so that function's
    decision -- which now runs inside the dev_container_build lock -- stays
    readable next to the guard it belongs to. Called from inside the `async
    with` on that lock: the enqueue is a single Redis push, and rolling the
    status back has to happen under the same hold that wrote it, or another
    caller could observe the IN_PROGRESS this call is in the middle of
    retracting.

    "Inside the `with`" is not the same as "holding the lock", and on the
    degraded / fail-closed / retained path it is not holding it -- the acquire
    was refused for a reason that is not a live holder, and the caller
    deliberately carries on unserialized rather than drop the setup for good
    (see the acquire's own comment). The rollback is best-effort there, exactly
    as the check-then-mark above it is.
    """
    from task_queue.task_manager import Task, TaskPriority, TaskQueue
    from services.dev_container_state import dev_container_state, DevContainerStatus

    try:
        logger.info(f"Auto-queuing dev_environment_setup task for {project}")

        task_queue = TaskQueue(use_redis=True)

        base_body = 'Auto-triggered: Agent requires dev container but it is not verified'
        has_required_fix = bool(change_description and change_description.strip())
        issue_body = (
            f"{base_body}\n\n"
            "## REQUIRED FIX\n"
            "⚠️ This is what's actually broken — this is not a general audit.\n\n"
            f"{change_description}\n\n"
            "---\n\n"
            "A specific automated check is failing because of this. Locate the exact file(s) "
            "this describes — which may or may not be Dockerfile.agent — and resolve it "
            "directly. See the REQUIRED FIX handling instructions in your guidelines."
            if has_required_fix
            else base_body
        )

        context = {
            'issue': {
                'title': f'Development environment setup for {project}',
                'body': issue_body,
                'number': 0
            },
            # NO 'issue_number' key — project-scoped dispatch, no GitHub issue.
            # See services/agent_executor.py's normalize_issue_scope() (#162).
            'board': 'system',
            'project': project,
            'repository': project,
            'automated_setup': True,
            'auto_triggered': True,
            'skip_workspace_prep': True,  # System task — no feature branch needed
            'use_docker': False  # Run locally in orchestrator environment
        }
        if pipeline_run_id:
            context['pipeline_run_id'] = pipeline_run_id
        if cycle_stack is not None:
            context['cycle_stack'] = cycle_stack

        task = Task(
            id=str(uuid.uuid4()),
            agent="dev_environment_setup",
            project=project,
            priority=TaskPriority.HIGH,
            context=context,
            created_at=datetime.now().isoformat()
        )

        task_queue.enqueue(task)
        logger.info(f"Auto-queued dev_environment_setup task: {task.id}")
    except Exception as e:
        # Roll back status to prevent permanent stuck IN_PROGRESS state
        logger.error(
            f"Failed to enqueue dev_environment_setup for {project}: {e}. "
            f"Rolling back status from IN_PROGRESS to UNVERIFIED to allow retry."
        )
        dev_container_state.set_status(
            project,
            DevContainerStatus.UNVERIFIED,
            error_message=f"Enqueue failed: {e}"
        )
        raise


class AgentStage(PipelineStage):
    """Generic pipeline stage that wraps any agent"""

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None, project_name: Optional[str] = None):
        # Check for custom circuit breaker config
        circuit_breaker = None
        if agent_config and 'agent_config' in agent_config:
            # agent_config['agent_config'] is the AgentConfig object from ConfigManager
            real_config = agent_config['agent_config']
            if hasattr(real_config, 'circuit_breaker_config') and real_config.circuit_breaker_config:
                cb_config = real_config.circuit_breaker_config
                breaker_name = f"{project_name}:{agent_name}" if project_name else agent_name
                if not project_name:
                    logger.warning(
                        f"AgentStage for '{agent_name}' constructed without project_name — "
                        f"custom CircuitBreaker falling back to un-namespaced key."
                    )
                circuit_breaker = CircuitBreaker(
                    name=breaker_name,
                    failure_threshold=cb_config.get('failure_threshold', 3),
                    recovery_timeout=cb_config.get('recovery_timeout', 30),
                    success_threshold=cb_config.get('success_threshold', 2)
                )

        super().__init__(agent_name, circuit_breaker=circuit_breaker, agent_config=agent_config, project_name=project_name)
        self.agent_class = get_agent_class(agent_name)
        if not self.agent_class:
            raise ValueError(f"Unknown agent: {agent_name}")

        self.agent_instance = self.agent_class(agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the wrapped agent"""
        return await self.agent_instance.execute(context)


def create_stage_from_config(stage_config, project_name: str) -> PipelineStage:
    """
    Create a pipeline stage from configuration.

    Determines whether to create a standard AgentStage, RepairCycleStage, or PRReviewStage
    based on the stage_type field.

    Args:
        stage_config: PipelineStage configuration from config manager
        project_name: Project name for loading test configurations

    Returns:
        Instantiated PipelineStage (AgentStage, RepairCycleStage, or PRReviewStage)
    """
    from config.manager import config_manager
    from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig
    from pipeline.pr_review_stage import PRReviewStage
    import logging

    logger = logging.getLogger(__name__)

    # Check if this is a specialized pipeline stage
    if hasattr(stage_config, 'stage_type') and stage_config.stage_type:

        if stage_config.stage_type == 'repair_cycle':
            # Load testing configuration from project
            project_config = config_manager.get_project_config(project_name)
            testing_config = project_config.testing or {}

            # Build RepairTestRunConfig list from project config
            test_configs = []
            for test_type_config in testing_config.get('types', []):
                test_type = test_type_config['type']
                test_configs.append(RepairTestRunConfig(
                    test_type=test_type,
                    max_iterations=test_type_config.get('max_iterations', 5),
                    review_warnings=test_type_config.get('review_warnings', True),
                    max_file_iterations=test_type_config.get('max_file_iterations', 3)
                ))

            # Get global settings
            max_total_agent_calls = stage_config.max_total_agent_calls or 100
            checkpoint_interval = stage_config.checkpoint_interval or 5

            # Create RepairCycleStage
            return RepairCycleStage(
                name=stage_config.name,
                test_configs=test_configs,
                agent_name=stage_config.default_agent,
                max_total_agent_calls=max_total_agent_calls,
                checkpoint_interval=checkpoint_interval,
                project_name=project_name
            )

        elif stage_config.stage_type == 'pr_review':
            # Create PRReviewStage
            project_config = config_manager.get_project_config(project_name)
            ci_config = project_config.ci or {}
            skip_ci_check = not ci_config.get('enabled', True)
            logger.info(f"Creating PRReviewStage for {stage_config.name} (skip_ci_check={skip_ci_check})")
            return PRReviewStage(
                name=stage_config.name,
                pr_review_agent=stage_config.default_agent,  # pr_code_reviewer
                requirements_verifier_agent="requirements_verifier",
                skip_ci_check=skip_ci_check,
                project_name=project_name,
            )

        else:
            logger.warning(f"Unknown stage_type: {stage_config.stage_type}")
            # Fall through to standard agent stage

    # Standard agent stage
    agent_config = config_manager.get_project_agent_config(
        project_name,
        stage_config.default_agent
    )
    return AgentStage(stage_config.default_agent, agent_config, project_name=project_name)


async def process_task_integrated(task, state_manager, logger):
    """
    Process task from task queue with branch management and auto-commit support.

    This is the entry point for task queue-based agent execution.
    Uses centralized AgentExecutor for consistent observability.
    """
    from services.project_workspace import workspace_manager
    from services.agent_executor import get_agent_executor
    from config.manager import config_manager
    from monitoring.observability import get_observability_manager
    from monitoring.decision_events import DecisionEventEmitter
    
    # Initialize decision observability
    obs = get_observability_manager()
    decision_events = DecisionEventEmitter(obs)

    task_context = task.context
    board_name = task_context.get('board', '')
    issue_number = task_context.get('issue_number')

    # For development pipelines, create/switch to feature branch
    # Check if this pipeline uses issue-based workflow (has workspace type 'issues')
    try:
        project_config = config_manager.get_project_config(task.project)
        # Find the pipeline with matching board_name
        pipeline = next(
            (p for p in project_config.pipelines if p.board_name == board_name),
            None
        )
        uses_git_workflow = pipeline and pipeline.workspace == 'issues'
    except Exception as e:
        logger.warning(f"Could not determine workspace type for board {board_name}: {e}")
        uses_git_workflow = False

    # Feature branch management handled by AgentExecutor's FeatureBranchManager
    # This provides hierarchical parent/sub-issue branch support

    # Extract pipeline_run_id for event tracking
    pipeline_run_id = None
    if hasattr(task, 'context') and task.context:
        pipeline_run_id = task.context.get('pipeline_run_id')

    # If not in task context, try to look it up
    if not pipeline_run_id and issue_number:
        try:
            from services.pipeline_run import get_pipeline_run_manager
            prm = get_pipeline_run_manager()
            active_run = prm.get_active_pipeline_run(task.project, issue_number)
            if active_run:
                pipeline_run_id = active_run.id
        except Exception:
            pass  # pipeline_run_id remains None

    # Validate task can run (check dev container requirements)
    validation_result = await validate_task_can_run(task, logger)
    if not validation_result['can_run']:
        logger.log_warning(f"Task {task.id} blocked: {validation_result['reason']}")

        # Build user-friendly error message
        base_message = validation_result['reason']
        if validation_result.get('needs_dev_setup'):
            user_message = (
                f"{base_message}. Agent '{task.agent}' requires a Docker development environment. "
                f"The system will automatically setup the environment and retry this task."
            )
        elif validation_result.get('defer'):
            user_message = (
                f"{base_message}. Agent '{task.agent}' will be retried automatically once setup completes."
            )
        else:
            user_message = (
                f"{base_message}. Agent '{task.agent}' cannot execute until this is resolved. "
                f"Please check the project configuration or wait for setup to complete."
            )

        # Queue dev_environment_setup task if needed
        if validation_result.get('needs_dev_setup'):
            # EMIT DECISION EVENT: queuing setup
            decision_events.emit_error_decision(
                error_type='TaskValidationError',
                error_message=user_message,
                context={
                    'task_id': task.id,
                    'agent': task.agent,
                    'issue_number': issue_number,
                    'board': board_name,
                    'requires_dev_container': True
                },
                recovery_action='queue_dev_environment_setup',
                success=False,
                project=task.project,
                pipeline_run_id=pipeline_run_id
            )
            try:
                outcome = await queue_dev_environment_setup(task.project, logger)

                # EMIT DECISION EVENT: what actually happened, which is not always
                # "queued" (#169 review). This used to report success/auto_queued
                # unconditionally, so a project deferring to somebody else's build
                # -- or one whose IN_PROGRESS mark failed to write -- produced the
                # same "has been queued" event on every 30s poll for the whole
                # window, and everything reading decision events (the ES pattern
                # indices, pipeline-recommendations, an operator asking why a
                # project is stuck) saw a stream of recoveries for work that was
                # never enqueued. That is the symptom the #152 review flagged;
                # these three branches are it fixed at the source.
                if outcome.queued:
                    recovery_message = (
                        f"Development environment setup has been queued for project '{task.project}'. "
                        f"Task will be retried automatically once the environment is ready."
                    )
                    recovery_action = 'queue_dev_environment_setup'
                    recovery_success = True
                elif outcome is DevSetupQueueOutcome.FAILED_STATUS_WRITE:
                    recovery_message = (
                        f"Development environment setup could NOT be queued for project "
                        f"'{task.project}': its in-progress mark could not be written, so "
                        f"nothing was enqueued. The next board poll retries."
                    )
                    recovery_action = 'queue_dev_environment_setup'
                    recovery_success = False
                else:
                    # A deferral is the correct recovery, not a failure: another
                    # setup/verify run already covers this project, and queuing a
                    # second one is the duplicate hour-scale rebuild the guard
                    # exists to prevent. Reported as its own action so it can never
                    # be counted as a queue.
                    recovery_message = (
                        f"Development environment setup was NOT queued for project "
                        f"'{task.project}' ({outcome.value}): a setup is already under way, "
                        f"so this task defers to it rather than queuing a duplicate. "
                        f"Task will be retried automatically once the environment is ready."
                    )
                    recovery_action = 'deferred_to_existing_dev_setup'
                    recovery_success = True
                decision_events.emit_error_decision(
                    error_type='TaskValidationError',
                    error_message=recovery_message,
                    context={
                        'task_id': task.id,
                        'agent': task.agent,
                        'issue_number': issue_number,
                        'board': board_name,
                        'auto_queued': outcome.queued,
                        'queue_outcome': outcome.value
                    },
                    recovery_action=recovery_action,
                    success=recovery_success,
                    project=task.project,
                    pipeline_run_id=pipeline_run_id
                )
            except Exception as queue_error:
                logger.error(
                    f"Failed to queue dev environment setup for {task.project}: {queue_error}. "
                    f"Task will be blocked until setup is manually triggered."
                )
                decision_events.emit_error_decision(
                    error_type='DevSetupQueueFailure',
                    error_message=f"Failed to auto-queue dev environment setup: {queue_error}",
                    context={
                        'task_id': task.id,
                        'agent': task.agent,
                        'issue_number': issue_number,
                        'board': board_name
                    },
                    recovery_action='manual_intervention_required',
                    success=False,
                    project=task.project,
                    pipeline_run_id=pipeline_run_id
                )

        elif validation_result.get('defer'):
            # Dev container setup is already in progress — re-enqueue the task with a
            # delay so it will be picked up again once the container is ready, instead of
            # being dropped or spinning in a tight busy-loop.
            try:
                from task_queue.task_manager import TaskQueue
                from datetime import datetime, timezone, timedelta
                defer_queue = TaskQueue(use_redis=True)
                task.not_before = (
                    datetime.now(timezone.utc) + timedelta(seconds=30)
                ).isoformat()
                defer_queue.enqueue(task)
                logger.info(
                    f"Deferred task {task.id} ({task.agent}) for {task.project} — "
                    f"re-enqueued with 30s delay, will retry after dev container setup completes"
                )
                decision_events.emit_error_decision(
                    error_type='TaskValidationError',
                    error_message=user_message,
                    context={
                        'task_id': task.id,
                        'agent': task.agent,
                        'issue_number': issue_number,
                        'board': board_name,
                        'requires_dev_container': True,
                        'deferred': True,
                        'retry_after_seconds': 30,
                    },
                    recovery_action='task_deferred',
                    success=True,
                    project=task.project,
                    pipeline_run_id=pipeline_run_id
                )
            except Exception as defer_error:
                logger.error(
                    f"Failed to re-enqueue deferred task {task.id} for {task.project}: {defer_error}. "
                    f"Task will be lost — dev container setup in progress."
                )
                decision_events.emit_error_decision(
                    error_type='TaskDeferralFailure',
                    error_message=f"Failed to re-enqueue task after dev container in-progress block: {defer_error}",
                    context={
                        'task_id': task.id,
                        'agent': task.agent,
                        'issue_number': issue_number,
                        'board': board_name
                    },
                    recovery_action='block_task',
                    success=False,
                    project=task.project,
                    pipeline_run_id=pipeline_run_id
                )

        else:
            # Permanent block (e.g. BLOCKED status) — emit and drop
            decision_events.emit_error_decision(
                error_type='TaskValidationError',
                error_message=user_message,
                context={
                    'task_id': task.id,
                    'agent': task.agent,
                    'issue_number': issue_number,
                    'board': board_name,
                    'requires_dev_container': True
                },
                recovery_action='block_task',
                success=False,
                project=task.project,
                pipeline_run_id=pipeline_run_id
            )

        from agents.non_retryable import NonRetryableAgentError
        raise NonRetryableAgentError(f"Task blocked: {user_message}")

    # Record execution start in work execution state.
    # Guard: only write a new probe entry if no in_progress entry already exists for
    # this agent/column. project_monitor writes a 'board_dispatch' probe before
    # enqueueing; creating a second 'task_queue' probe here would leave a redundant
    # in_progress entry if the orchestrator restarts before stamp_execution_task_id()
    # runs. So on the ordinary board-dispatch path this branch does NOT fire and the
    # record the empty-output watchdog eventually sees is the 'board_dispatch' one --
    # both names are on that gate's allowlist for exactly that reason (#166).
    if 'issue_number' in task_context and 'column' in task_context:
        from services.work_execution_state import work_execution_tracker
        issue_number_ctx = task_context['issue_number']
        column_ctx = task_context['column']
        state = work_execution_tracker.load_state(task.project, issue_number_ctx)
        has_in_progress = any(
            e.get('outcome') == 'in_progress' and
            e.get('agent') == task.agent and
            e.get('column') == column_ctx
            for e in state.get('execution_history', [])
        )
        if not has_in_progress:
            work_execution_tracker.record_execution_start(
                issue_number=issue_number_ctx,
                column=column_ctx,
                agent=task.agent,
                trigger_source='task_queue',
                project_name=task.project,
                board_name=board_name or None
            )
            logger.info(
                f"Recorded execution start for {task.agent} on {task.project}/#{issue_number_ctx} "
                f"in column {column_ctx} (trigger: task_queue)"
            )
        else:
            logger.info(
                f"Skipped duplicate execution start for {task.agent} on "
                f"{task.project}/#{issue_number_ctx} in column {column_ctx}: "
                f"existing in_progress entry found (pre-enqueue probe)"
            )

    # Execute agent using centralized executor
    executor = get_agent_executor()
    result = await executor.execute_agent(
        agent_name=task.agent,
        project_name=task.project,
        task_context=task.context,
        execution_type="task_queue"
    )

    # Auto-commit handled by FeatureBranchManager in AgentExecutor
    # This ensures commits are properly associated with parent/sub-issue branches

    # Auto-advance to next column if configured
    # CRITICAL: Skip auto-advancement if agent made manual progression
    manual_progression_made = result.get('manual_progression_made', False)
    if manual_progression_made:
        logger.info(
            f"Skipping auto-advancement for issue #{issue_number}: "
            f"agent made manual progression during execution"
        )

    current_column_name = task_context.get('column')
    if current_column_name and issue_number and not manual_progression_made:
        try:
            # Get workflow configuration
            from config.state_manager import state_manager

            project_config = config_manager.get_project_config(task.project)
            pipeline = next(
                (p for p in project_config.pipelines if p.board_name == board_name),
                None
            )

            if pipeline:
                workflow_template = config_manager.get_workflow_template(pipeline.workflow)

                # Find current column
                current_column = next(
                    (c for c in workflow_template.columns if c.name == current_column_name),
                    None
                )

                # Check if auto-advance is enabled
                if current_column and getattr(current_column, 'auto_advance_on_approval', False):
                    # Find next column
                    current_index = workflow_template.columns.index(current_column)
                    if current_index + 1 < len(workflow_template.columns):
                        next_column = workflow_template.columns[current_index + 1]

                        logger.info(
                            f"Auto-advancing issue #{issue_number} from {current_column_name} to {next_column.name}"
                        )

                        # Move the card
                        from services.pipeline_progression import PipelineProgression
                        from task_queue.task_manager import TaskQueue

                        task_queue = TaskQueue()
                        progression_service = PipelineProgression(task_queue)

                        moved = progression_service.move_issue_to_column(
                            project_name=task.project,
                            board_name=board_name,
                            issue_number=issue_number,
                            target_column=next_column.name,
                            trigger='agent_auto_advance'
                        )

                        if moved:
                            logger.info(
                                f"Successfully auto-advanced issue #{issue_number} to {next_column.name}"
                            )
                        else:
                            logger.warning(
                                f"Failed to auto-advance issue #{issue_number} to {next_column.name}"
                            )
        except Exception as e:
            logger.error(f"Error during auto-advancement: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            # EMIT DECISION EVENT: Error during auto-advancement
            decision_events.emit_error_decision(
                error_type='AutoAdvancementError',
                error_message=str(e),
                context={
                    'task_id': task.id,
                    'agent': task.agent,
                    'issue_number': issue_number,
                    'board': board_name,
                    'current_column': current_column_name
                },
                recovery_action='log_and_continue',
                success=False,
                project=task.project,
                pipeline_run_id=pipeline_run_id
            )

    return result


# Export the main integration function
__all__ = [
    'process_task_integrated',
    'AgentStage'
]
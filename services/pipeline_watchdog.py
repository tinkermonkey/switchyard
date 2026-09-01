"""
Pipeline Run Watchdog Service

Detects and cleans up zombie pipeline runs - runs that are marked as active
but have no corresponding agent container running. This prevents queue deadlock
caused by stuck pipeline runs that never complete.

A pipeline run is considered a zombie if:
1. Status is 'active' in Elasticsearch
2. Started more than 30 minutes ago
3. No agent container is running for the issue

Runs periodically as a background task to ensure automatic recovery.
"""

import logging
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Any, Optional
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)


# Number of times a given (project, issue) may zombie-cleanup and be auto-retried
# before the watchdog gives up and retains the lock for human intervention. Without
# this cap, a deterministically-failing stall (e.g. a stage whose context lookup can
# never succeed) would auto-retry forever, burning zombie_threshold_minutes every
# cycle; with too low a cap, a single transient blip (e.g. one rate-limited API call)
# would immediately demand manual intervention instead of self-healing.
ZOMBIE_AUTO_RETRY_LIMIT = 2

# How long a zombie-retry count is remembered before it decays, so an issue that
# stalled a couple of times weeks ago doesn't count against a fresh, unrelated stall.
ZOMBIE_RETRY_COUNT_TTL_SECONDS = 24 * 3600


class PipelineWatchdog:
    """
    Monitors pipeline runs for zombie states and automatically cleans them up.

    This service runs periodically to detect pipeline runs that are stuck in
    'active' status despite having no agent executing work.
    """

    def __init__(
        self,
        es_client: Optional[Elasticsearch] = None,
        pipeline_run_manager=None,
        lock_manager=None,
        zombie_threshold_minutes: int = 30,
        check_interval_seconds: int = 300,  # 5 minutes
        project_monitor=None,
    ):
        """
        Initialize the watchdog.

        Args:
            es_client: Elasticsearch client for querying pipeline runs
            pipeline_run_manager: PipelineRunManager instance for ending runs
            lock_manager: PipelineLockManager instance for clearing/marking retained locks
            zombie_threshold_minutes: Minutes before run is considered zombie
            check_interval_seconds: Seconds between watchdog checks
            project_monitor: ProjectMonitor instance for self-heal redispatch
                (_redispatch_same_issue). Optional — falls back to the process-
                global instance via services.project_monitor.get_project_monitor()
                when not injected, which is what production wiring relies on
                (this param exists mainly so tests can inject a fake one).
        """
        self.es = es_client
        self.pipeline_run_manager = pipeline_run_manager
        self.lock_manager = lock_manager
        self.zombie_threshold_minutes = zombie_threshold_minutes
        self.check_interval_seconds = check_interval_seconds
        self.project_monitor = project_monitor
        self.running = False

    def check_for_zombie_runs(self) -> Dict[str, Any]:
        """
        Find and clean up zombie pipeline runs.

        KNOWN CONCURRENCY CAVEAT: this method (via scheduled_tasks.py's
        _cleanup_zombie_pipeline_runs) runs on asyncio's default executor —
        it is the first path that drives ProjectMonitor.trigger_agent_for_
        status() (via _redispatch_same_issue -> _cleanup_zombie_run /
        _actively_resume_run) from a thread other than ProjectMonitor's own
        poll thread. That method's duplicate-dispatch guards (the pending-
        task scan and pipeline_queue.mark_issue_active()) are check-then-act
        and are not covered by ProjectMonitor._last_state_lock (which only
        guards its status-cache reads/writes). A poll cycle landing on the
        exact same issue at the same moment as a self-heal redispatch could
        theoretically slip two dispatches through. This is a pre-existing
        category of risk (two poll paths racing on one issue) that this PR
        makes reachable from a second thread for the first time, rather than
        a new mechanism; a per-(project, issue) dispatch mutex would close it
        properly but is a larger change deferred as follow-up work.

        Returns:
            Dict with summary of cleanup results:
            {
                'checked': int,
                'zombies_found': int,
                'zombies_cleaned': int,
                'errors': int,
                'details': List[Dict]
            }
        """
        # Coarse global freeze: while the Claude Code usage-limit breaker is open,
        # skip this entire pass untouched — every active pipeline_run stays exactly
        # as it is (lock held, cycle_state undisturbed), rather than being reaped
        # and restarted from scratch for a known, account-wide, timed pause. This
        # mirrors the same is_open() gate project_monitor.py's main poll loop
        # already uses to skip new dispatch. See detect_from_event()/trip() in
        # monitoring/claude_code_breaker.py for how/when this trips.
        try:
            from monitoring.claude_code_breaker import get_breaker
            if get_breaker().is_open():
                logger.info(
                    "Claude Code breaker is OPEN — skipping zombie run check entirely "
                    "this pass (every active run stays frozen, untouched, until it closes)"
                )
                return {
                    'checked': 0,
                    'zombies_found': 0,
                    'zombies_cleaned': 0,
                    'errors': 0,
                    'details': [],
                    'frozen_skip': True,
                }
        except Exception as e:
            logger.warning(f"Could not check Claude Code breaker state, proceeding with zombie check: {e}")

        if not self.es:
            logger.warning("Elasticsearch not available, skipping zombie check")
            return {
                'checked': 0,
                'zombies_found': 0,
                'zombies_cleaned': 0,
                'errors': 0,
                'details': []
            }

        logger.info("Starting zombie pipeline run check")

        results = {
            'checked': 0,
            'zombies_found': 0,
            'zombies_cleaned': 0,
            'errors': 0,
            'details': []
        }

        try:
            # Query for all active pipeline runs
            query = {
                "query": {
                    "term": {"status": "active"}
                },
                "size": 1000,  # Get all active runs
                "sort": [{"started_at": {"order": "asc"}}]  # Oldest first
            }

            response = self.es.search(index="pipeline-runs-*", **query)

            if response['hits']['total']['value'] == 0:
                logger.info("No active pipeline runs found")
                return results

            total_active = response['hits']['total']['value']
            logger.info(f"Found {total_active} active pipeline runs, checking for zombies")

            # Calculate threshold timestamp (timezone-aware UTC)
            threshold = datetime.now(timezone.utc) - timedelta(minutes=self.zombie_threshold_minutes)

            for hit in response['hits']['hits']:
                results['checked'] += 1
                run = hit['_source']

                pipeline_run_id = run['id']
                project = run['project']
                issue_number = run['issue_number']
                board = run.get('board', 'unknown')
                started_at_str = run['started_at']

                # Parse started_at timestamp
                try:
                    started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                except Exception as e:
                    logger.warning(f"Could not parse started_at for run {pipeline_run_id}: {e}")
                    continue

                # A run whose most recent execution outcome is 'frozen' was specifically
                # paused by the Claude Code breaker (see agent_executor.py's frozen-
                # outcome recording). This is a precise, per-run signal, distinct from
                # the coarse is_open() gate above (which only prevents checking at all
                # while still open) — it identifies, on a normal pass after the breaker
                # has closed, which SPECIFIC runs are zombie-shaped because of a
                # resolved freeze rather than genuine staleness. Bypasses the age gate
                # below entirely: a known-reason pause with a known end time shouldn't
                # have to wait out zombie_threshold_minutes to be noticed.
                try:
                    from services.work_execution_state import work_execution_tracker
                    was_frozen = work_execution_tracker.is_frozen_by_circuit_breaker(project, issue_number)
                except Exception as e:
                    logger.warning(f"Could not check frozen state for issue #{issue_number}: {e}")
                    was_frozen = False

                if not was_frozen:
                    # Check if run is old enough to be considered zombie
                    if started_at > threshold:
                        # Run is still young, skip
                        continue

                # Check if there's an agent container running for this issue
                has_container = self._check_for_agent_container(project, issue_number)

                if has_container:
                    # Container exists, run is legitimate
                    logger.debug(
                        f"Pipeline run {pipeline_run_id[:8]}... for {project} issue #{issue_number} "
                        f"has active container, keeping active"
                    )
                    continue

                if was_frozen:
                    # Uniform clean-restart across every pipeline type (review_cycle,
                    # human_feedback_loop, pr_review_stage alike) — takes priority over
                    # the feedback-loop/review-cycle exemptions below, since those exist
                    # to protect genuinely still-active work, not a resolved freeze.
                    results['zombies_found'] += 1
                    age_minutes = (datetime.now(timezone.utc) - started_at).total_seconds() / 60
                    logger.warning(
                        f"Pipeline run {pipeline_run_id[:8]}... for {project} issue #{issue_number} "
                        f"was frozen by the Claude Code breaker (now closed, no container, "
                        f"age: {age_minutes:.1f} minutes) — actively resuming"
                    )
                    try:
                        self._actively_resume_run(
                            pipeline_run_id=pipeline_run_id,
                            project=project,
                            board=board,
                            issue_number=issue_number,
                            started_at=started_at_str,
                        )
                        results['zombies_cleaned'] += 1
                        results['details'].append({
                            'pipeline_run_id': pipeline_run_id,
                            'project': project,
                            'issue_number': issue_number,
                            'age_minutes': age_minutes,
                            'action': 'actively_resumed',
                        })
                    except Exception as resume_error:
                        results['errors'] += 1
                        logger.error(
                            f"Failed to actively resume frozen run {pipeline_run_id}: {resume_error}",
                            exc_info=True
                        )
                        results['details'].append({
                            'pipeline_run_id': pipeline_run_id,
                            'project': project,
                            'issue_number': issue_number,
                            'age_minutes': age_minutes,
                            'action': 'resume_failed',
                            'error': str(resume_error),
                        })
                    continue

                # NOTE: We intentionally do NOT skip cleanup just because the lock is held.
                # The lock alone is not proof of life — a crashed container leaves the lock
                # held with nobody to release it. The review cycle and feedback loop checks
                # below (plus the 30-minute age threshold) cover every legitimate
                # non-containerized state. If none of those fire, the lock is stale and
                # _cleanup_zombie_run will release it.

                # Never clean up a run that has an active human feedback loop.
                # Feedback-listening phases legitimately have no Docker container running —
                # the agent has finished its turn and the orchestrator is waiting for a human
                # reply, which can take many hours.
                try:
                    from services.human_feedback_loop import human_feedback_loop_executor
                    lk = human_feedback_loop_executor._loop_key(project, issue_number)
                    if lk in human_feedback_loop_executor.active_loops:
                        logger.info(
                            f"Pipeline run {pipeline_run_id[:8]}... for {project} issue #{issue_number} "
                            f"has an active feedback loop - skipping zombie cleanup"
                        )
                        continue
                except Exception as e:
                    logger.warning(f"Could not check feedback loop state for issue #{issue_number}: {e}")
                    continue  # Fail-safe: don't kill a run we can't verify

                # Never clean up a run that has an active review cycle (any non-completed
                # status). Between iterations the container exits normally but the cycle
                # is still orchestrating the next iteration.
                try:
                    from services.review_cycle import review_cycle_executor
                    ck = review_cycle_executor._cycle_key(project, issue_number)
                    cycle_state = review_cycle_executor.active_cycles.get(ck)
                    if cycle_state and cycle_state.status != 'completed':
                        logger.info(
                            f"Pipeline run {pipeline_run_id[:8]}... for {project} issue #{issue_number} "
                            f"has active review cycle (status: {cycle_state.status}) - skipping zombie cleanup"
                        )
                        continue
                except Exception as e:
                    logger.warning(f"Could not check review cycle state for issue #{issue_number}: {e}")
                    continue  # Fail-safe: don't kill a run we can't verify

                # No container, old enough, no active review cycle or feedback loop = ZOMBIE
                # Coordination guard: prevent double-processing with other cleanup mechanisms
                try:
                    from services.cleanup_guard import try_claim_cleanup
                    if not try_claim_cleanup(project, issue_number, "zombie_watchdog"):
                        continue
                except Exception as e:
                    logger.warning(f"Cleanup guard unavailable, proceeding without coordination: {e}")

                results['zombies_found'] += 1

                age_minutes = (datetime.now(timezone.utc) - started_at).total_seconds() / 60
                logger.warning(
                    f"Found zombie pipeline run {pipeline_run_id[:8]}... for {project} "
                    f"issue #{issue_number} (age: {age_minutes:.1f} minutes, no container)"
                )

                # Try to clean up the zombie
                try:
                    self._cleanup_zombie_run(
                        pipeline_run_id=pipeline_run_id,
                        project=project,
                        board=board,
                        issue_number=issue_number,
                        started_at=started_at_str
                    )

                    results['zombies_cleaned'] += 1
                    results['details'].append({
                        'pipeline_run_id': pipeline_run_id,
                        'project': project,
                        'issue_number': issue_number,
                        'age_minutes': age_minutes,
                        'action': 'cleaned_up'
                    })

                except Exception as cleanup_error:
                    results['errors'] += 1
                    logger.error(
                        f"Failed to cleanup zombie run {pipeline_run_id}: {cleanup_error}",
                        exc_info=True
                    )
                    results['details'].append({
                        'pipeline_run_id': pipeline_run_id,
                        'project': project,
                        'issue_number': issue_number,
                        'age_minutes': age_minutes,
                        'action': 'cleanup_failed',
                        'error': str(cleanup_error)
                    })

        except Exception as e:
            logger.error(f"Error during zombie check: {e}", exc_info=True)
            results['errors'] += 1

        # Log summary
        logger.info(
            f"Zombie check complete: checked={results['checked']}, "
            f"zombies_found={results['zombies_found']}, "
            f"cleaned={results['zombies_cleaned']}, "
            f"errors={results['errors']}"
        )

        return results

    def _check_for_agent_container(self, project: str, issue_number: int) -> bool:
        """
        Check if there's an agent container running for the given issue.

        Uses Docker label filters rather than container name matching. All managed
        containers (agent containers via docker_runner.py and repair cycle containers
        via project_monitor.py) are labeled with org.switchyard.project and
        org.switchyard.issue_number, so a single label query covers every container
        type regardless of naming convention.

        Args:
            project: Project name
            issue_number: Issue number

        Returns:
            True if container exists, False otherwise
        """
        try:
            result = subprocess.run(
                [
                    'docker', 'ps',
                    '--filter', f'label=org.switchyard.project={project}',
                    '--filter', f'label=org.switchyard.issue_number={issue_number}',
                    '--format', '{{.Names}}',
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )

            if result.returncode == 0 and result.stdout.strip():
                containers = [c for c in result.stdout.strip().split('\n') if c]
                logger.debug(f"Found {len(containers)} container(s) for issue #{issue_number}: {containers}")
                return True

            return False

        except Exception as e:
            logger.warning(f"Error checking for agent container: {e}")
            # Fail safe: assume container exists to avoid false positives
            return True

    def _is_workspace_git_broken(self, project: str) -> bool:
        """
        Check whether the project's shared workspace is stuck in an unresolved
        git conflict (unmerged/conflicted paths) right now — the kind of state
        that no amount of retrying can fix on its own; it needs a human to
        resolve the conflict or hard-reset the checkout. Used to stop the
        zombie watchdog's auto-retry from releasing the lock into a workspace
        that will fail identically for the next issue that picks it up too.

        Mirrors GitWorkflowManager.get_conflicting_files(), duplicated here
        (rather than imported) to keep this a plain, synchronous, best-effort
        check — it must never block or fail the zombie sweep itself.
        """
        project_dir = f"/workspace/{project}"
        try:
            result = subprocess.run(
                ['git', 'diff', '--name-only', '--diff-filter=U'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if result.returncode != 0:
                # Not a git repo, missing directory, etc. — not a conflict signal,
                # and not this check's job to diagnose.
                return False
            return bool(result.stdout.strip())
        except Exception as e:
            logger.warning(f"Could not check workspace git state for {project}: {e}")
            # Fail open — an inconclusive check shouldn't itself block auto-retry.
            return False

    def _cleanup_zombie_run(
        self,
        pipeline_run_id: str,
        project: str,
        board: str,
        issue_number: int,
        started_at: str
    ):
        """
        Clean up a zombie pipeline run.

        Args:
            pipeline_run_id: ID of the zombie run
            project: Project name
            board: Board name
            issue_number: Issue number
            started_at: ISO timestamp when run started
        """
        logger.info(
            f"Cleaning up zombie pipeline run {pipeline_run_id[:8]}... "
            f"for {project} issue #{issue_number}"
        )

        # Defense-in-depth: never auto-retry into a workspace that is provably
        # broken on disk right now. A stall caused by an unresolved git conflict
        # (unmerged paths, a stash that can't be written) will fail identically
        # for every other issue that touches this project's shared checkout — it
        # is not the kind of transient blip the auto-retry budget below exists
        # for. Check the actual git state directly rather than trusting retry
        # history, since a first-time occurrence would otherwise sail through
        # with retry_count=1 and release the lock straight into the same failure.
        workspace_broken = self._is_workspace_git_broken(project)

        # Track how many times this (project, issue) has zombie-cleaned up recently.
        # The first ZOMBIE_AUTO_RETRY_LIMIT times, self-heal: keep the lock held
        # by THIS issue throughout and redispatch it directly, instead of sitting
        # locked and inert until a human notices. Beyond that, fall back to the
        # original behavior (retain lock, require manual intervention) —
        # repeated identical failures are a sign the auto-retry itself can't
        # succeed, so continuing would just burn zombie_threshold_minutes forever
        # with no chance of self-healing.
        #
        # Self-heal NEVER releases the pipeline lock outright — see incident
        # e42ca133 (investigated live in a pipeline-investigate session, not a
        # separate written postmortem; issue numbers below are from the
        # managed documentation_robotics project's repo, not this one), where
        # releasing it let a different queued issue (#854) acquire the lock
        # before anything could re-pick the zombie's own issue (#853),
        # orphaning its already-posted CHANGES NEEDED review with no revision
        # ever dispatched. Instead: end_pipeline_run always retains the
        # lock (which durably marks it retained via mark_lock_failed, since
        # outcome="failed" below), and then — only within budget —
        # clear_retained_reason() immediately lifts that block and
        # _redispatch_same_issue() re-triggers the SAME issue directly, so no
        # other issue can ever acquire the lock in between. If end_pipeline_run
        # itself fails, or either the clear or redispatch step fails,
        # requires_manual_intervention is raised to true (each with its own
        # specific reason string) so the lock ends up durably retained
        # rather than silently idle.
        retry_count = self._increment_zombie_retry_count(project, issue_number)
        exceeded_auto_retry = retry_count > ZOMBIE_AUTO_RETRY_LIMIT
        self_heal = not (exceeded_auto_retry or workspace_broken)
        requires_manual_intervention = exceeded_auto_retry or workspace_broken
        # Accurate, call-site-specific text for _notify_lock_stuck's GitHub
        # comment — set here for the two "never even attempted self-heal"
        # causes, and overwritten below if self-heal itself fails instead.
        # Passing the wrong text (e.g. "auto-retry exhausted" from a self-heal
        # failure that never touched the retry budget at all) was flagged in
        # PR review as actively misleading to whoever reads the comment.
        manual_intervention_reason = None
        if workspace_broken:
            manual_intervention_reason = (
                f"This project's shared workspace ({project}) has unresolved git "
                f"conflicts (unmerged paths), so auto-retry would just repeat this "
                f"exact failure for every other issue on this board — the workspace "
                f"needs manual git repair."
            )
        elif exceeded_auto_retry:
            manual_intervention_reason = (
                f"This issue's pipeline has zombie-cleaned up {retry_count} times in "
                f"a row within {ZOMBIE_RETRY_COUNT_TTL_SECONDS // 3600}h (no agent "
                f"container found for over {self.zombie_threshold_minutes} minutes "
                f"each time) and auto-retry has been exhausted since repeating the "
                f"same failure wouldn't self-heal."
            )

        if workspace_broken:
            logger.error(
                f"Project {project}'s shared workspace has unresolved git conflicts "
                f"(unmerged paths) — retaining lock for issue #{issue_number} "
                f"regardless of retry count ({retry_count}/{ZOMBIE_AUTO_RETRY_LIMIT}). "
                f"Auto-retry would just repeat this exact failure for every other "
                f"issue on this board; the workspace needs manual git repair."
            )
        elif exceeded_auto_retry:
            logger.warning(
                f"Issue #{issue_number} in {project} has zombie-cleaned up "
                f"{retry_count} times within {ZOMBIE_RETRY_COUNT_TTL_SECONDS // 3600}h "
                f"(limit: {ZOMBIE_AUTO_RETRY_LIMIT}) — retaining lock, requires manual intervention"
            )
        else:
            logger.info(
                f"Issue #{issue_number} in {project} zombie-cleanup attempt "
                f"{retry_count}/{ZOMBIE_AUTO_RETRY_LIMIT} — retrying same issue, lock stays held throughout"
            )

        # Remove any stale review cycle state FIRST, before any redispatch
        # below. Must run before, not after: a synchronous self-heal
        # redispatch can reach far enough to create a fresh ReviewCycleState
        # for this same issue — if this cleanup ran afterward, it would find
        # that brand-new state (indistinguishable in shape from a genuinely
        # stale one) and delete it, leaving the freshly-dispatched reviewer's
        # eventual verdict with nothing to act on it. That is exactly incident
        # e42ca133's root cause, reproduced inside the fix for it — this
        # ordering is what prevents that.
        try:
            from services.review_cycle import review_cycle_executor
            ck = review_cycle_executor._cycle_key(project, issue_number)
            cycle_state = review_cycle_executor.active_cycles.get(ck)
            if cycle_state is None:
                # Also check disk (cycle may not be in-memory after a restart)
                all_cycles = review_cycle_executor._load_active_cycles(project)
                cycle_state = next((c for c in all_cycles if c.issue_number == issue_number), None)
            if cycle_state:
                review_cycle_executor._remove_cycle_state(cycle_state)
                if ck in review_cycle_executor.active_cycles:
                    del review_cycle_executor.active_cycles[ck]
                logger.info(f"Removed stale review cycle state for {project} issue #{issue_number} during zombie cleanup")
        except Exception as e:
            logger.warning(f"Failed to clean up review cycle state during zombie cleanup for {project} issue #{issue_number}: {e}")

        # End the pipeline run. Always retain the lock here (never
        # retain_lock=False — see the comment above for why self-heal clears
        # the resulting durable mark itself instead of releasing outright).
        if self.pipeline_run_manager:
            ended = self.pipeline_run_manager.end_pipeline_run(
                project=project,
                issue_number=issue_number,
                reason=f"Zombie pipeline run cleanup (started: {started_at}, no container found)",
                outcome="failed",
                retain_lock=True,
            )

            if ended:
                logger.info(f"Ended zombie pipeline run {pipeline_run_id[:8]}...")
            else:
                logger.warning(f"Failed to end zombie pipeline run {pipeline_run_id[:8]}...")

            if self_heal:
                if not ended:
                    # end_pipeline_run itself failed (e.g. get_active_pipeline_run
                    # found nothing — a real race: something else ended this run
                    # between the zombie query and this call, not hypothetical).
                    # The lock's actual state is now unknown to us, so this
                    # cannot safely proceed as a self-heal — fail safe toward
                    # manual intervention rather than silently doing nothing.
                    logger.error(
                        f"Zombie self-heal: end_pipeline_run did not end an "
                        f"active run for {project} issue #{issue_number} — "
                        f"cannot safely self-heal, requires manual intervention"
                    )
                    requires_manual_intervention = True
                    manual_intervention_reason = (
                        f"The pipeline watchdog found this issue's run marked "
                        f"active with no agent container running, but could not "
                        f"confirm/end that run while attempting an automatic "
                        f"retry (it may have already ended through another path). "
                        f"The lock's state couldn't be safely verified, so "
                        f"auto-retry was skipped rather than risk an unsafe retry."
                    )
                else:
                    lock_mgr = self.lock_manager
                    if lock_mgr is None:
                        from services.pipeline_lock_manager import get_pipeline_lock_manager
                        lock_mgr = get_pipeline_lock_manager()
                    cleared = lock_mgr.clear_retained_reason(project, board, issue_number)
                    redispatched = cleared and self._redispatch_same_issue(project, board, issue_number)
                    if redispatched:
                        logger.info(
                            f"Zombie self-heal: issue #{issue_number} in {project} "
                            f"redispatched, lock never left issue #{issue_number}"
                        )
                    else:
                        if cleared:
                            # Redispatch failed AFTER we lifted the durable block —
                            # restore it now rather than leave the lock un-retained
                            # with nothing dispatched (any other issue could then
                            # acquire it, the exact orphaning this fix exists to stop).
                            lock_mgr.mark_lock_failed(
                                project, board, issue_number,
                                reason=(
                                    f"Zombie self-heal redispatch failed on retry "
                                    f"{retry_count}/{ZOMBIE_AUTO_RETRY_LIMIT}"
                                ),
                            )
                        logger.error(
                            f"Zombie self-heal: could not complete redispatch for "
                            f"{project} issue #{issue_number} (cleared={cleared}) — "
                            f"retaining lock, requires manual intervention"
                        )
                        requires_manual_intervention = True
                        manual_intervention_reason = (
                            f"The pipeline watchdog attempted an automatic retry "
                            f"(attempt {retry_count}/{ZOMBIE_AUTO_RETRY_LIMIT}) for this "
                            f"issue's zombie pipeline run, but "
                            f"{'the redispatch itself did not actually dispatch anything' if cleared else 'it could not clear the lock for retry'} "
                            f"— the lock has been re-retained rather than left in an "
                            f"ambiguous state."
                        )

        if requires_manual_intervention:
            self._notify_lock_stuck(
                project, board, issue_number, pipeline_run_id, retry_count,
                reason=manual_intervention_reason,
            )

        # Log cleanup event to observability
        try:
            from monitoring.observability_server import observability_server

            observability_server.index_decision_event(
                decision_type="zombie_pipeline_run_cleanup",
                project=project,
                board=board,
                issue_number=issue_number,
                reason="No agent container found after timeout",
                details={
                    "pipeline_run_id": pipeline_run_id,
                    "started_at": started_at,
                    "zombie_threshold_minutes": self.zombie_threshold_minutes
                }
            )
        except Exception as e:
            logger.debug(f"Could not log cleanup event to observability: {e}")

    def _redispatch_same_issue(self, project: str, board: str, issue_number: int) -> bool:
        """
        Directly re-dispatch the agent for (project, issue_number) at its
        current board column, via the single canonical dispatch entry point
        (ProjectMonitor.trigger_agent_for_status), while the pipeline lock is
        already held by this same issue.

        Self-heal-only: callers must have already cleared any durable
        retained_reason on the lock (see PipelineLockManager.
        clear_retained_reason) before calling this, and must treat a False
        return as "the retry did not actually happen" — never leave the lock
        merely un-retained with nothing dispatched, since nothing else polls
        for that state on a recurring basis (the board's normal poll loop
        only reacts to column *changes*, and ProjectMonitor's stalled-item
        rescan — _rescan_boards_for_stalled_items() — only runs once, at
        startup; the separate per-cycle _check_and_process_waiting_issues_
        failsafe doesn't help either, since it explicitly skips any board
        whose lock is still 'locked' regardless of retained_reason).

        Before dispatching, this also abandons any stale 'in_progress'
        work_execution_state entry for this issue (mirroring what
        agent_container_recovery.py's startup recovery does). This is
        required, not optional: a zombie run's dead container almost always
        left its last execution record at outcome='in_progress' (nothing
        wrote a terminal outcome), and should_execute_work() refuses to
        dispatch on 'in_progress' unconditionally, with no staleness
        timeout — without this, redispatch would silently no-op via
        trigger_agent_for_status()'s own 'work_already_in_progress' guard on
        every call, which is exactly the failure this PR's fix is for.

        Returns:
            True only if trigger_agent_for_status() returned a real dispatch
            result (non-None) — see incident e42ca133's follow-up
            investigation: trigger_agent_for_status() has over a dozen
            legitimate internal branches that dispatch nothing and return
            None (retained-lock gate, duplicate task, queue-priority wait,
            closed issue, etc.), several silent by design. Treating "no
            exception raised" as success (the original version of this
            method) let the self-heal report success while nothing actually
            ran.
        """
        try:
            from config.manager import config_manager

            project_monitor = self.project_monitor
            if project_monitor is None:
                from services.project_monitor import get_project_monitor
                project_monitor = get_project_monitor()
            if not project_monitor:
                logger.error(
                    f"_redispatch_same_issue: no ProjectMonitor registered — "
                    f"cannot redispatch {project} issue #{issue_number}"
                )
                return False

            project_config = config_manager.get_project_config(project)
            if not project_config:
                logger.error(
                    f"_redispatch_same_issue: no project config for {project} "
                    f"— cannot redispatch issue #{issue_number}"
                )
                return False

            # Board-aware column lookup (mirrors ProjectMonitor.
            # _get_parent_column_on_board's project.number filter) — this
            # issue may sit on more than one Projects v2 board at once (a
            # project can have several enabled pipelines), so an unfiltered
            # lookup could resolve a different board's column entirely.
            current_column = project_monitor.get_issue_column_sync(
                project, board, issue_number
            )
            if not current_column:
                logger.error(
                    f"_redispatch_same_issue: could not determine current "
                    f"column for {project} issue #{issue_number} on board "
                    f"'{board}' — cannot redispatch"
                )
                return False

            # Clear the stale in_progress record left by the zombie's dead
            # container — see docstring. active_task_ids is empty because
            # this is only ever called for an issue _cleanup_zombie_run/
            # _actively_resume_run have already confirmed has no running
            # container (that's the definition of zombie/frozen), so every
            # in_progress entry for this issue is unconditionally stale.
            try:
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.abandon_stale_in_progress_entries(
                    project_name=project,
                    issue_number=issue_number,
                    active_task_ids=set(),
                )
            except Exception as e:
                logger.warning(
                    f"_redispatch_same_issue: could not abandon stale "
                    f"in_progress entries for {project} issue #{issue_number} "
                    f"(redispatch will likely no-op on work_already_in_progress "
                    f"if any exist): {e}"
                )

            result = project_monitor.trigger_agent_for_status(
                project_name=project,
                board_name=board,
                issue_number=issue_number,
                status=current_column,
                repository=project_config.github['repo'],
                lock_already_acquired=True,
            )
            if result is None:
                logger.error(
                    f"_redispatch_same_issue: trigger_agent_for_status did not "
                    f"dispatch anything for {project} issue #{issue_number} "
                    f"(column '{current_column}') — treating as redispatch "
                    f"failure"
                )
                return False

            logger.info(
                f"_redispatch_same_issue: redispatched {project} issue "
                f"#{issue_number} in column '{current_column}'"
            )
            return True
        except Exception as e:
            logger.error(
                f"_redispatch_same_issue failed for {project} issue "
                f"#{issue_number}: {e}",
                exc_info=True,
            )
            return False

    def _actively_resume_run(
        self,
        pipeline_run_id: str,
        project: str,
        board: str,
        issue_number: int,
        started_at: str,
    ):
        """
        Promptly resume a pipeline_run that was frozen by the Claude Code breaker
        and is now eligible to continue (breaker closed, no container running).

        Uniform clean-restart across every pipeline type (review_cycle,
        human_feedback_loop, pr_review_stage alike): end this pipeline_run and
        redispatch the SAME issue directly via _redispatch_same_issue() — the
        same self-heal mechanism _cleanup_zombie_run uses for a genuine
        zombie's auto-retry, EXCEPT this is deliberately NOT counted against
        ZOMBIE_AUTO_RETRY_LIMIT (a known-reason pause is not evidence the work
        itself is broken) and does not wait for the zombie_threshold_minutes
        age gate (see the was_frozen check in check_for_zombie_runs()).

        The lock is NEVER released outright here — see
        PipelineLockManager.clear_retained_reason()'s docstring and incident
        e42ca133: releasing the lock at this point (the previous behavior)
        risks a different queued issue acquiring it before this one's resume
        actually happens, orphaning whatever work this pipeline_run had
        already posted. locked_by_issue stays this issue throughout.

        If the frozen execution captured a Claude Code session_id with evidence of
        prior progress, agent_executor.execute_agent() picks it up automatically on
        the very next dispatch for this (project, issue, column, agent) and resumes
        that session with a short continuation prompt instead of rebuilding the
        stage's normal prompt from scratch — see the frozen-session resume fork in
        agent_executor.py. This method doesn't need to know about that; it always
        performs the same "retain lock, then redispatch" action regardless.
        """
        logger.info(
            f"Actively resuming pipeline run {pipeline_run_id[:8]}... for {project} "
            f"issue #{issue_number} (frozen by Claude Code breaker, now closed)"
        )

        # Remove any stale review cycle in-memory state FIRST, before any
        # redispatch below — mirrors _cleanup_zombie_run's equivalent
        # reordering. Must run before, not after: a synchronous redispatch can
        # reach far enough to create a fresh ReviewCycleState for this same
        # issue, and running this cleanup afterward could delete that
        # brand-new state instead of a genuinely stale one — see incident
        # e42ca133 and _cleanup_zombie_run's matching comment.
        try:
            from services.review_cycle import review_cycle_executor
            ck = review_cycle_executor._cycle_key(project, issue_number)
            cycle_state = review_cycle_executor.active_cycles.get(ck)
            if cycle_state is None:
                all_cycles = review_cycle_executor._load_active_cycles(project)
                cycle_state = next((c for c in all_cycles if c.issue_number == issue_number), None)
            if cycle_state:
                review_cycle_executor._remove_cycle_state(cycle_state)
                if ck in review_cycle_executor.active_cycles:
                    del review_cycle_executor.active_cycles[ck]
                logger.info(
                    f"Removed stale review cycle state for {project} issue #{issue_number} "
                    f"during active resume"
                )
        except Exception as e:
            logger.warning(
                f"Failed to clean up review cycle state during active resume for "
                f"{project} issue #{issue_number}: {e}"
            )

        if self.pipeline_run_manager:
            ended = self.pipeline_run_manager.end_pipeline_run(
                project=project,
                issue_number=issue_number,
                reason=f"Actively resumed after Claude Code breaker closed (started: {started_at})",
                outcome="failed",
                retain_lock=True,
            )
            if ended:
                logger.info(f"Ended frozen pipeline run {pipeline_run_id[:8]}... for prompt resume")
            else:
                logger.warning(f"Failed to end frozen pipeline run {pipeline_run_id[:8]}...")

            if not ended:
                # end_pipeline_run itself failed (e.g. no active run found —
                # a real race, not hypothetical). The lock's actual state is
                # unknown to us, so this cannot safely proceed — fail safe
                # toward manual intervention rather than silently doing
                # nothing. Mirrors _cleanup_zombie_run's equivalent gap fix.
                logger.error(
                    f"Active resume: end_pipeline_run did not end an active "
                    f"run for {project} issue #{issue_number} — cannot "
                    f"safely resume, requires manual intervention"
                )
                self._notify_lock_stuck(
                    project, board, issue_number, pipeline_run_id, retry_count=0,
                    reason=(
                        f"The pipeline watchdog tried to actively resume this "
                        f"issue after the Claude Code breaker closed, but could "
                        f"not confirm/end its pipeline run (it may have already "
                        f"ended through another path). The lock's state couldn't "
                        f"be safely verified, so the resume was skipped rather "
                        f"than risk an unsafe retry."
                    ),
                )
            else:
                lock_mgr = self.lock_manager
                if lock_mgr is None:
                    from services.pipeline_lock_manager import get_pipeline_lock_manager
                    lock_mgr = get_pipeline_lock_manager()
                cleared = lock_mgr.clear_retained_reason(project, board, issue_number)
                redispatched = cleared and self._redispatch_same_issue(project, board, issue_number)
                if redispatched:
                    logger.info(
                        f"Active resume: issue #{issue_number} in {project} "
                        f"redispatched, lock never left issue #{issue_number}"
                    )
                else:
                    if cleared:
                        # Redispatch failed AFTER we lifted the durable block —
                        # restore it now rather than leave the lock un-retained
                        # with nothing dispatched. Same fail-safe as
                        # _cleanup_zombie_run's equivalent branch.
                        lock_mgr.mark_lock_failed(
                            project, board, issue_number,
                            reason="Active-resume redispatch failed after Claude Code breaker closed",
                        )
                    logger.error(
                        f"Active resume: could not complete redispatch for "
                        f"{project} issue #{issue_number} (cleared={cleared}) — "
                        f"retaining lock, requires manual intervention"
                    )
                    self._notify_lock_stuck(
                        project, board, issue_number, pipeline_run_id, retry_count=0,
                        reason=(
                            f"The pipeline watchdog resumed this issue's pipeline "
                            f"after the Claude Code breaker closed, but "
                            f"{'the redispatch itself did not actually dispatch anything' if cleared else 'it could not clear the lock for the resume'} "
                            f"— the lock has been re-retained rather than left in "
                            f"an ambiguous state. This is NOT a zombie/no-container "
                            f"stall and is not counted against the auto-retry budget."
                        ),
                    )

        try:
            from monitoring.observability_server import observability_server

            observability_server.index_decision_event(
                decision_type="frozen_pipeline_run_resumed",
                project=project,
                board=board,
                issue_number=issue_number,
                reason="Claude Code breaker closed — resumed promptly, not counted against zombie auto-retry limit",
                details={
                    "pipeline_run_id": pipeline_run_id,
                    "started_at": started_at,
                }
            )
        except Exception as e:
            logger.debug(f"Could not log active-resume event to observability: {e}")

    def _increment_zombie_retry_count(self, project: str, issue_number: int) -> int:
        """
        Increment and return the rolling zombie-cleanup count for (project, issue).

        Backed by Redis with a TTL so old counts decay rather than accumulating
        forever. Falls back to always-exceeded (1 + limit) if Redis isn't reachable,
        so a Redis outage fails safe toward "require manual intervention" rather than
        silently permitting unbounded auto-retry.
        """
        try:
            redis_client = getattr(self.pipeline_run_manager, 'redis', None)
            if not redis_client:
                return ZOMBIE_AUTO_RETRY_LIMIT + 1
            key = f"zombie_cleanup_count:{project}:{issue_number}"
            count = redis_client.incr(key)
            redis_client.expire(key, ZOMBIE_RETRY_COUNT_TTL_SECONDS)
            return int(count)
        except Exception as e:
            logger.warning(
                f"Could not track zombie retry count for {project} issue #{issue_number}: {e}"
            )
            return ZOMBIE_AUTO_RETRY_LIMIT + 1

    def _notify_lock_stuck(
        self,
        project: str,
        board: str,
        issue_number: int,
        pipeline_run_id: str,
        retry_count: int,
        reason: Optional[str] = None,
    ):
        """
        Surface a permanently-retained lock somewhere a human will actually see it.

        Previously this state was only visible as a passive Elasticsearch decision
        event — nobody would notice until they went looking. This posts a comment on
        the issue itself (where the team already looks) and logs a distinctly-named
        decision event for dashboard/query visibility.

        Args:
            reason: Human-readable sentence describing WHY the lock is stuck,
                used in place of the default "zombie-cleaned up N times...
                auto-retry exhausted" framing. Required for any call site that
                isn't reporting a genuine exhausted zombie-retry budget (e.g. a
                self-heal redispatch failure, an active-resume failure, or a
                workspace-broken case) — reusing the zombie-exhausted wording
                for those produced factually wrong GitHub comments (e.g.
                "zombie-cleaned up 0 times" from an active-resume path that
                was never a zombie cleanup at all). Defaults to the original
                zombie-exhausted framing for backward compatibility.
        """
        reason_text = reason or (
            f"This issue's pipeline has zombie-cleaned up {retry_count} times in a row "
            f"(no agent container found for over {self.zombie_threshold_minutes} minutes each time) "
            f"and auto-retry has been exhausted since repeating the same failure wouldn't self-heal."
        )
        message = (
            f"## \N{WARNING SIGN} Pipeline Stuck — Manual Intervention Required\n\n"
            f"{reason_text} The `{board}` board lock is now being retained deliberately.\n\n"
            f"This blocks every other issue queued behind it on this board.\n\n"
            f"Someone needs to diagnose why this issue's pipeline keeps stalling "
            f"(check `/pipeline-investigate {pipeline_run_id}`) and then release the lock "
            f"before the board can proceed.\n\n"
            f"---\n_Reported by the pipeline watchdog_"
        )

        try:
            from config.manager import config_manager
            from services.github_integration import GitHubIntegration

            project_config = config_manager.get_project_config(project)
            if project_config and hasattr(project_config, 'github'):
                github = GitHubIntegration(
                    repo_owner=project_config.github['org'],
                    repo_name=project_config.github['repo'],
                )
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    import concurrent.futures
                    with concurrent.futures.ThreadPoolExecutor() as pool:
                        pool.submit(asyncio.run, github.post_comment(issue_number, message)).result()
                except RuntimeError:
                    asyncio.run(github.post_comment(issue_number, message))
        except Exception as e:
            # Best-effort — GitHub may itself be the reason we're stuck (e.g. the same
            # circuit breaker that caused the original stall). The decision event below
            # is the durable record either way.
            logger.warning(f"Could not post lock-stuck notification comment to issue #{issue_number}: {e}")

        try:
            from monitoring.observability_server import observability_server

            observability_server.index_decision_event(
                decision_type="pipeline_lock_stuck_requires_intervention",
                project=project,
                board=board,
                issue_number=issue_number,
                reason=reason or f"Zombie-cleaned up {retry_count} times, auto-retry exhausted",
                details={
                    "pipeline_run_id": pipeline_run_id,
                    "retry_count": retry_count,
                    "auto_retry_limit": ZOMBIE_AUTO_RETRY_LIMIT,
                }
            )
        except Exception as e:
            logger.debug(f"Could not log lock-stuck event to observability: {e}")

    def start(self):
        """
        Start the watchdog background task.

        This runs in a loop, checking for zombies periodically.
        Should be called in a background thread.
        """
        self.running = True
        logger.info(
            f"Pipeline watchdog started (check interval: {self.check_interval_seconds}s, "
            f"zombie threshold: {self.zombie_threshold_minutes}m)"
        )

        while self.running:
            try:
                self.check_for_zombie_runs()
            except Exception as e:
                logger.error(f"Watchdog check failed: {e}", exc_info=True)

            # Sleep until next check
            if self.running:
                time.sleep(self.check_interval_seconds)

        logger.info("Pipeline watchdog stopped")

    def stop(self):
        """Stop the watchdog background task."""
        self.running = False


# Global watchdog instance
_watchdog_instance = None


def get_pipeline_watchdog(
    es_client=None,
    pipeline_run_manager=None,
    lock_manager=None
) -> PipelineWatchdog:
    """
    Get or create the global pipeline watchdog instance.

    Args:
        es_client: Elasticsearch client (optional, uses existing if not provided)
        pipeline_run_manager: PipelineRunManager instance (optional)
        lock_manager: PipelineLockManager instance (optional)

    Returns:
        PipelineWatchdog instance
    """
    global _watchdog_instance

    if _watchdog_instance is None:
        _watchdog_instance = PipelineWatchdog(
            es_client=es_client,
            pipeline_run_manager=pipeline_run_manager,
            lock_manager=lock_manager
        )

    return _watchdog_instance

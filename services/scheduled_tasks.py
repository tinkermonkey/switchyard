"""
Scheduled Tasks Service

Runs periodic maintenance tasks like cleanup of orphaned branches.
Uses APScheduler for Python-native scheduling.
"""

import logging
import asyncio
import os
import random
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)


class ScheduledTasksService:
    """Manages periodic background tasks for the orchestrator"""

    def __init__(self):
        self.scheduler = AsyncIOScheduler()
        self.running = False

    def start(self):
        """Start the scheduler"""
        if self.running:
            logger.warning("Scheduler already running")
            return

        # Schedule cleanup task - daily at 2 AM
        self.scheduler.add_job(
            self._cleanup_orphaned_branches,
            trigger=CronTrigger(hour=2, minute=0),
            id='cleanup_orphaned_branches',
            name='Cleanup orphaned feature branches',
            replace_existing=True
        )

        # Schedule stale branch warnings - daily at 9 AM
        self.scheduler.add_job(
            self._check_stale_branches,
            trigger=CronTrigger(hour=9, minute=0),
            id='check_stale_branches',
            name='Check for stale feature branches',
            replace_existing=True
        )

        # Schedule orphaned container cleanup - every 20 minutes
        self.scheduler.add_job(
            self._cleanup_orphaned_containers,
            trigger=CronTrigger(minute='*/20'),
            id='cleanup_orphaned_containers',
            name='Cleanup orphaned agent container tracking keys',
            replace_existing=True
        )

        # Schedule orphaned testcontainers reaper - every 15 minutes
        self.scheduler.add_job(
            self._reap_orphaned_test_containers,
            trigger=CronTrigger(minute='*/15'),
            id='reap_orphaned_test_containers',
            name='Force-remove orphaned testcontainers-managed containers',
            replace_existing=True
        )

        # Schedule queue state reconciliation with GitHub - every 10 minutes
        self.scheduler.add_job(
            self._reconcile_queue_state,
            trigger=CronTrigger(minute='*/10'),
            id='reconcile_queue_state',
            name='Force sync pipeline queues with GitHub boards',
            replace_existing=True
        )

        # Schedule empty output detection - every 15 minutes
        self.scheduler.add_job(
            self._detect_empty_outputs,
            trigger=CronTrigger(minute='*/15'),
            id='detect_empty_outputs',
            name='Detect and retry executions with empty outputs',
            replace_existing=True
        )

        # Schedule orphaned-parent sweep - every 15 minutes
        self.scheduler.add_job(
            self._sweep_orphaned_parents,
            trigger=CronTrigger(minute='*/15'),
            id='sweep_orphaned_parents',
            name='Re-check parents stranded in "In Development" for PR-ready advance',
            replace_existing=True
        )

        # Token metrics: frequent short-lookback job keeps recent data fresh,
        # full job with longer lookback fills in historical gaps.
        token_metrics_hours = int(os.environ.get('TOKEN_METRICS_INTERVAL_HOURS', '3'))
        self.scheduler.add_job(
            self._run_token_metrics,
            trigger=IntervalTrigger(hours=token_metrics_hours),
            id='token_metrics',
            name=f'Compute token usage metrics (every {token_metrics_hours}h)',
            replace_existing=True
        )
        self.scheduler.add_job(
            self._run_token_metrics_recent,
            trigger=IntervalTrigger(minutes=15),
            id='token_metrics_recent',
            name='Compute recent token metrics (every 15m)',
            replace_existing=True
        )

        # Run token metrics shortly after startup so restarts don't create gaps
        token_startup_jitter = random.uniform(30, 120)
        self.scheduler.add_job(
            self._run_token_metrics_recent,
            trigger=DateTrigger(run_date=datetime.now(timezone.utc) + timedelta(seconds=token_startup_jitter)),
            id='token_metrics_startup',
            name='Token metrics catchup (startup)',
            replace_existing=True
        )

        # Schedule project metrics computation - daily at 3 AM
        self.scheduler.add_job(
            self._run_project_metrics,
            trigger=CronTrigger(hour=3, minute=30),
            id='project_metrics_daily',
            name='Compute per-project daily rollup metrics',
            replace_existing=True
        )

        # Backfill project metrics on startup (7-day lookback) with jitter
        jitter_seconds = random.uniform(60, 600)
        startup_time = datetime.now(timezone.utc) + timedelta(seconds=jitter_seconds)
        self.scheduler.add_job(
            self._run_project_metrics_backfill,
            trigger=DateTrigger(run_date=startup_time),
            id='project_metrics_startup_backfill',
            name='Project metrics startup backfill (7-day)',
            replace_existing=True
        )

        # Schedule zombie pipeline run cleanup - every 30 minutes
        self.scheduler.add_job(
            self._cleanup_zombie_pipeline_runs,
            trigger=IntervalTrigger(minutes=30),
            id='zombie_pipeline_run_cleanup',
            name='Cleanup zombie pipeline runs (active in ES, no container)',
            replace_existing=True
        )

        # Schedule Docker disk cleanup - weekly on Sunday at 3 AM
        self.scheduler.add_job(
            self._cleanup_docker_disk,
            trigger=CronTrigger(day_of_week='sun', hour=3, minute=0),
            id='docker_disk_cleanup',
            name='Cleanup Docker dangling images, non-latest agent tags, and build cache',
            replace_existing=True
        )

        # Schedule test-cycle stats rollup - weekly on Sunday at 4 AM
        self.scheduler.add_job(
            self._run_test_cycle_stats,
            trigger=CronTrigger(day_of_week='sun', hour=4, minute=0),
            id='test_cycle_stats_weekly',
            name='Compute per-project test-cycle duration rollup stats',
            replace_existing=True
        )

        self.scheduler.start()
        self.running = True
        logger.info("Scheduled tasks service started")
        logger.info("- Orphaned branch cleanup: Daily at 2 AM")
        logger.info("- Stale branch checks: Daily at 9 AM")
        logger.info("- Orphaned container cleanup: Every 20 minutes")
        logger.info("- Orphaned testcontainers reaper: Every 15 minutes")
        logger.info("- Docker state reconciliation: Every 5 minutes")
        logger.info("- Queue state reconciliation: Every 10 minutes")
        logger.info("- Empty output detection: Every 15 minutes")
        logger.info("- Orphaned-parent sweep: Every 15 minutes")
        logger.info(f"- Token metrics (recent): Once at startup in ~{token_startup_jitter:.0f}s, then every 15m (1h lookback)")
        logger.info(f"- Token metrics (full): Every {token_metrics_hours}h")
        logger.info("- Project metrics rollup: Daily at 3:30 AM")
        logger.info(f"- Project metrics backfill: Once at startup in ~{jitter_seconds:.0f}s")
        logger.info("- Zombie pipeline run cleanup: Every 30 minutes")
        logger.info("- Docker disk cleanup: Weekly on Sunday at 3 AM")
        logger.info("- Test-cycle stats rollup: Weekly on Sunday at 4 AM")

    def stop(self):
        """Stop the scheduler"""
        if not self.running:
            return

        self.scheduler.shutdown()
        self.running = False
        logger.info("Scheduled tasks service stopped")

    def schedule_claude_breaker_resume_check(self, reset_time: datetime):
        """
        Schedule a one-off check shortly after the Claude Code breaker's reset_time
        so recovery doesn't have to wait for the next pipeline_watchdog sweep
        (every 30 minutes — see _cleanup_zombie_pipeline_runs). This is purely a
        latency optimization: if this job is lost (e.g. orchestrator restart, since
        the scheduler has no persistent jobstore), the watchdog's own periodic
        check_for_zombie_runs() pass remains the durable fallback that will
        eventually notice the breaker closed and actively resume frozen runs.

        A later trip() with a new (possibly extended) reset_time reschedules this
        job via replace_existing=True rather than stacking duplicate jobs.
        """
        self.scheduler.add_job(
            self._check_claude_breaker_resume,
            trigger=DateTrigger(run_date=reset_time + timedelta(seconds=5)),
            id='claude_breaker_resume_check',
            name='Nudge Claude Code breaker check after rate limit reset',
            replace_existing=True,
        )

    async def _check_claude_breaker_resume(self):
        """Nudge the breaker to re-evaluate state; also proactively resumes any
        pipeline_runs pipeline_watchdog had frozen while the breaker was open."""
        try:
            from monitoring.claude_code_breaker import get_breaker
            get_breaker().check_and_close()
        except Exception as e:
            logger.warning(f"Error in scheduled Claude Code breaker resume check: {e}")

        try:
            await self._cleanup_zombie_pipeline_runs()
        except Exception as e:
            logger.warning(f"Error running zombie sweep from breaker resume check: {e}")

    async def _cleanup_orphaned_branches(self):
        """Cleanup orphaned branches for all projects"""
        logger.info("Starting scheduled cleanup of orphaned branches")

        try:
            from services.feature_branch_manager import feature_branch_manager
            from services.github_integration import GitHubIntegration
            from config.manager import config_manager

            # Get all visible projects
            project_names = config_manager.list_visible_projects()

            cleanup_count = 0
            error_count = 0

            for project_name in project_names:
                project_config = config_manager.get_project_config(project_name)
                try:
                    # Get repository info from github config
                    if 'github' not in project_config.__dict__ or not project_config.github:
                        logger.warning(f"No GitHub config for project {project_name}")
                        continue

                    repo_owner = project_config.github.get('org')
                    repo_name = project_config.github.get('repo')
                    
                    if not repo_owner or not repo_name:
                        logger.warning(f"Invalid GitHub config for {project_name}")
                        continue

                    gh_integration = GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)

                    # Run cleanup
                    logger.info(f"Cleaning up orphaned branches for project: {project_name}")
                    await feature_branch_manager.cleanup_orphaned_branches(
                        project=project_name,
                        github_integration=gh_integration
                    )

                    cleanup_count += 1

                except Exception as e:
                    logger.error(f"Error cleaning up project {project_name}: {e}", exc_info=True)
                    error_count += 1

            logger.info(
                f"Orphaned branch cleanup complete: "
                f"{cleanup_count} projects processed, {error_count} errors"
            )

        except Exception as e:
            logger.error(f"Fatal error in orphaned branch cleanup: {e}", exc_info=True)

    async def _check_stale_branches(self):
        """Check for stale branches and post warnings"""
        logger.info("Starting scheduled stale branch check")

        try:
            from services.feature_branch_manager import feature_branch_manager
            from services.github_integration import GitHubIntegration
            from config.manager import config_manager

            # Get all visible projects
            project_names = config_manager.list_visible_projects()

            warning_count = 0
            error_count = 0

            for project_name in project_names:
                project_config = config_manager.get_project_config(project_name)
                try:
                    # Get repository info from github config
                    if 'github' not in project_config.__dict__ or not project_config.github:
                        logger.warning(f"No GitHub config for project {project_name}")
                        continue

                    repo_owner = project_config.github.get('org')
                    repo_name = project_config.github.get('repo')
                    
                    if not repo_owner or not repo_name:
                        logger.warning(f"Invalid GitHub config for {project_name}")
                        continue

                    gh_integration = GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)

                    # Get all feature branches
                    feature_branches = feature_branch_manager.get_all_feature_branches(project_name)

                    for fb in feature_branches:
                        # Check staleness
                        import os
                        project_dir = os.path.join(
                            feature_branch_manager.workspace_root,
                            project_name
                        )

                        commits_behind = await feature_branch_manager.get_commits_behind_main(
                            project_dir,
                            fb.branch_name
                        )

                        # Update state
                        fb.commits_behind_main = commits_behind
                        feature_branch_manager.save_feature_branch_state(project_name, fb)

                        # Warn if very stale
                        if commits_behind > 50:
                            stale_pipeline_run_id = None
                            try:
                                from services.pipeline_run import get_pipeline_run_manager
                                active_run = get_pipeline_run_manager().get_active_pipeline_run(project_name, fb.parent_issue)
                                if active_run:
                                    stale_pipeline_run_id = active_run.id
                            except Exception:
                                pass
                            await feature_branch_manager.escalate_stale_branch(
                                gh_integration,
                                fb.parent_issue,
                                fb.branch_name,
                                commits_behind,
                                pipeline_run_id=stale_pipeline_run_id,
                            )
                            warning_count += 1
                            logger.warning(
                                f"Escalated stale branch {fb.branch_name}: "
                                f"{commits_behind} commits behind"
                            )

                except Exception as e:
                    logger.error(f"Error checking stale branches for {project_name}: {e}")
                    error_count += 1

            logger.info(
                f"Stale branch check complete: "
                f"{warning_count} warnings posted, {error_count} errors"
            )

        except Exception as e:
            logger.error(f"Fatal error in stale branch check: {e}", exc_info=True)

    async def _cleanup_orphaned_containers(self):
        """Cleanup orphaned agent container tracking keys in Redis and stuck execution states"""
        logger.info("Starting scheduled cleanup of orphaned agent container tracking keys and stuck states")

        try:
            from claude.docker_runner import DockerAgentRunner
            from services.work_execution_state import work_execution_tracker
            
            # Run blocking operations in thread pool to avoid blocking the event loop
            loop = asyncio.get_event_loop()
            
            # Run the Redis key cleanup (it's synchronous)
            await loop.run_in_executor(None, DockerAgentRunner.cleanup_orphaned_redis_keys)
            
            # Run the execution state cleanup (it's synchronous)
            # This ensures that if a container dies silently, the state is updated to 'failure'
            await loop.run_in_executor(None, work_execution_tracker.cleanup_stuck_in_progress_states)
            
            logger.info("Orphaned container and stuck state cleanup completed successfully")

        except Exception as e:
            logger.error(f"Error in orphaned container cleanup: {e}", exc_info=True)

    async def _reap_orphaned_test_containers(self):
        """
        Force-remove orphaned testcontainers-managed containers.

        Some projects' own test suites (currently: codetoreum) use the Python
        testcontainers library to spin up real Docker fixtures (Elasticsearch,
        Redis, etc.) for integration tests. Cleanup normally happens via
        testcontainers' "Ryuk" reaper sidecar, or barring that, the owning
        test process's own try/finally blocks and a pytest_sessionfinish
        hook. All three require the test process to still be alive: Ryuk
        needs a live reverse connection back to it, and the in-process hooks
        only run on a graceful exit. codetoreum disables Ryuk entirely
        (TESTCONTAINERS_RYUK_DISABLED=true in its tests/conftest.py) because
        it can't reliably reach back to the test process across this host's
        Docker-in-Docker network topology, so it relies solely on the
        in-process paths (see codetoreum PRs #957/#958) — which do nothing if
        the test process itself is killed outright rather than exiting
        cleanly (e.g. its container gets SIGTERM'd under host memory
        pressure). Orphaned Elasticsearch fixtures left running for hours
        have driven host swap to near-total exhaustion this way more than
        once.

        This is the external safety net for that gap: anything labeled
        org.testcontainers=true that has been running longer than
        TESTCONTAINERS_REAP_AGE_MINUTES (default 240 -- an hour past the
        3-hour hard timeout on the longest-running agent, config/foundations/
        agents.yaml, which is the actual binding constraint on how long a
        legitimate fixture can stay up) has almost certainly outlived its
        test session, so it's safe to force-remove regardless of which
        project or session started it — this doesn't depend on any
        project-specific knowledge.
        """
        logger.info("Starting orphaned testcontainers reaper sweep")

        try:
            reap_age_minutes = int(os.environ.get('TESTCONTAINERS_REAP_AGE_MINUTES', '240'))
            if reap_age_minutes < 1:
                raise ValueError(f"must be >= 1, got {reap_age_minutes}")
        except ValueError as e:
            logger.warning(f"Invalid TESTCONTAINERS_REAP_AGE_MINUTES ({e}); using default of 240 minutes")
            reap_age_minutes = 240

        def _parse_docker_created(created_str: str):
            """Parse Docker's RFC3339Nano `Created` timestamp.

            Go's time.RFC3339Nano formatter strips trailing zeros from the
            fractional-second field, so its length varies from 0 to 9 digits
            (e.g. "...45Z" with no fraction at all is valid). Pad/truncate to
            exactly 6 digits (microseconds) instead of assuming a fixed slice
            width, so every valid length parses correctly.
            """
            from datetime import datetime
            base, _, frac = created_str.rstrip('Z').partition('.')
            frac = (frac + '000000')[:6]
            return datetime.fromisoformat(f"{base}.{frac}+00:00")

        def _sweep():
            import subprocess
            import json
            from datetime import datetime, timezone

            result = subprocess.run(
                ['docker', 'ps', '-a', '--filter', 'label=org.testcontainers=true', '-q'],
                capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                raise RuntimeError(f"docker ps failed: {result.stderr.strip()}")
            container_ids = [c for c in result.stdout.strip().splitlines() if c]
            if not container_ids:
                return {'found': 0, 'reaped': [], 'skipped': []}

            inspect = subprocess.run(
                ['docker', 'inspect'] + container_ids,
                capture_output=True, text=True, timeout=30
            )
            if inspect.returncode != 0 and not inspect.stdout.strip():
                raise RuntimeError(f"docker inspect failed: {inspect.stderr.strip()}")
            if inspect.returncode != 0:
                # docker inspect prints results for every ID it could still find even
                # when some have vanished since `docker ps` ran (e.g. removed by this
                # same job on a concurrent tick); use the partial results rather than
                # discarding a whole sweep over one race.
                logger.warning(
                    f"docker inspect reported errors for some containers (likely "
                    f"removed concurrently); continuing with partial results: "
                    f"{inspect.stderr.strip()}"
                )
            try:
                containers = json.loads(inspect.stdout) if inspect.stdout.strip() else []
            except json.JSONDecodeError as e:
                raise RuntimeError(f"docker inspect returned unparseable JSON: {e}")

            now = datetime.now(timezone.utc)
            reaped = []
            skipped = []
            for c in containers:
                container_id = c.get('Id')
                name = c.get('Name', '?').lstrip('/')
                if not container_id:
                    logger.warning(f"Testcontainers reaper: inspect entry missing 'Id' ({name}); skipping")
                    continue

                created_str = c.get('Created', '')
                try:
                    created = _parse_docker_created(created_str)
                except Exception as parse_exc:
                    logger.warning(
                        f"Testcontainers reaper: could not parse Created={created_str!r} for "
                        f"container {container_id[:12]} ({name}); skipping this sweep ({parse_exc})"
                    )
                    skipped.append(container_id)
                    continue

                age_minutes = (now - created).total_seconds() / 60

                # Leading-indicator audit (log-only -- never blocks or reaps):
                # flag any testcontainers-labeled container publishing a FIXED
                # host port instead of letting Docker assign an ephemeral one.
                # Not proof of an actual collision by itself, just a signal
                # worth surfacing alongside the port-allocation guidance given
                # to docker-socket-access agents (see switchyard #51). Runs
                # for every found container, independent of reap eligibility.
                fixed_ports = sorted({
                    f"{container_port}->{binding.get('HostPort')}"
                    for container_port, bindings in
                    ((c.get('HostConfig', {}) or {}).get('PortBindings', {}) or {}).items()
                    for binding in (bindings or [])
                    # HostPort "0" (or "") is exactly the ephemeral/dynamic-assignment
                    # request this audit exists to encourage (see docker_socket_access.md's
                    # "bind host port 0" guidance) -- docker inspect's PortBindings echoes
                    # back the literal requested HostPort, not the resolved ephemeral one
                    # (that only appears in NetworkSettings.Ports), so "0" here means
                    # correctly configured, not a fixed port. Found in #51 review: treating
                    # it as fixed produced a false-positive warning on every container that
                    # followed the guidance correctly.
                    if binding.get('HostPort') and binding.get('HostPort') != '0'
                })
                if fixed_ports:
                    logger.warning(
                        f"Testcontainers reaper: container {name} (age={round(age_minutes, 1)}m) "
                        f"publishes fixed host port(s) {', '.join(fixed_ports)} instead of ephemeral "
                        f"assignment -- potential port-collision risk under concurrent test runs "
                        f"(see switchyard #51)"
                    )

                if age_minutes < reap_age_minutes:
                    continue

                labels = (c.get('Config', {}) or {}).get('Labels', {}) or {}
                if labels.get('org.testcontainers.reaper-exempt') == 'true':
                    logger.info(
                        f"Testcontainers reaper: skipping exempt container "
                        f"{name} (org.testcontainers.reaper-exempt=true), age={round(age_minutes, 1)}m"
                    )
                    continue

                info = {
                    'name': name,
                    'image': (c.get('Config', {}) or {}).get('Image', 'unknown'),
                    'session_id': labels.get('org.testcontainers.session-id', 'unknown'),
                    'age_minutes': round(age_minutes, 1),
                }
                # Isolate each removal: one container's failure (hung daemon,
                # unexpected error) must not abort the batch and lose the
                # already-completed removals still sitting in `reaped`.
                try:
                    rm = subprocess.run(
                        ['docker', 'rm', '-f', container_id],
                        capture_output=True, text=True, timeout=30
                    )
                    info['removed'] = rm.returncode == 0
                    if rm.returncode != 0:
                        info['error'] = rm.stderr.strip()
                except Exception as rm_exc:
                    info['removed'] = False
                    info['error'] = f"{type(rm_exc).__name__}: {rm_exc}"
                reaped.append(info)

            return {'found': len(containers), 'reaped': reaped, 'skipped': skipped}

        try:
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, _sweep)
            reaped = result['reaped']
            if reaped:
                for d in reaped:
                    logger.warning(
                        f"Reaped orphaned testcontainer '{d['name']}' (image={d['image']}, "
                        f"session={d['session_id']}, age={d['age_minutes']}m, "
                        f"removed={d['removed']}" + (f", error={d['error']}" if not d['removed'] else "") + ")"
                    )
                logger.warning(
                    f"Orphaned testcontainers reaper: removed {sum(1 for d in reaped if d['removed'])} "
                    f"of {result['found']} testcontainers-labeled container(s) "
                    f"older than {reap_age_minutes}m"
                    + (f" ({len(result['skipped'])} skipped due to unparseable timestamp)" if result['skipped'] else "")
                )
            else:
                logger.info(
                    f"Orphaned testcontainers reaper: {result['found']} testcontainers-labeled "
                    f"container(s) found, none older than {reap_age_minutes}m"
                    + (f" ({len(result['skipped'])} skipped due to unparseable timestamp)" if result['skipped'] else "")
                )
        except Exception as e:
            logger.error(f"Error in orphaned testcontainers reaper: {type(e).__name__}: {e}", exc_info=True)

    async def _reconcile_queue_state(self):
        """
        Force synchronize pipeline queues with GitHub board state.

        This runs every 10 minutes to ensure queues don't drift from GitHub reality.
        Uses force_sync_with_github which always overwrites local state.
        """
        logger.info("Starting queue state reconciliation with GitHub")

        try:
            from config.manager import config_manager
            from services.pipeline_queue_manager import PipelineQueueManager
            from pathlib import Path
            import os

            # Get all projects
            project_names = config_manager.list_visible_projects()

            reconciled_count = 0
            error_count = 0

            for project_name in project_names:
                try:
                    project_config = config_manager.get_project_config(project_name)

                    if not hasattr(project_config, 'pipelines') or not project_config.pipelines:
                        continue

                    # Reconcile each pipeline's queue
                    for pipeline in project_config.pipelines:
                        try:
                            board_name = pipeline.board_name

                            logger.info(
                                f"Force syncing queue for {project_name}/{board_name}"
                            )

                            # Get queue manager
                            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
                            state_dir = Path(orchestrator_root) / "state" / "pipeline_queues"
                            queue_manager = PipelineQueueManager(project_name, board_name, state_dir)

                            # Force sync with GitHub
                            queue_manager.force_sync_with_github()

                            reconciled_count += 1

                        except Exception as e:
                            logger.error(
                                f"Error reconciling queue for {project_name}/{board_name}: {e}"
                            )
                            error_count += 1

                except Exception as e:
                    logger.error(f"Error processing project {project_name}: {e}")
                    error_count += 1

            logger.info(
                f"Queue state reconciliation complete: "
                f"{reconciled_count} queues synced, {error_count} errors"
            )

        except Exception as e:
            logger.error(f"Error in queue state reconciliation: {e}", exc_info=True)

    async def _sweep_orphaned_parents(self):
        """
        Safety-net sweep for parent issues stranded in "In Development" whose sub-issues
        are all already complete but which never got advanced to "In Review".

        Normally, ProjectMonitor._check_pr_ready_on_issue_exit() advances a parent to
        "In Review" the instant its last sub-issue exits to Staged/Done. That check is
        one-shot and event-triggered — if a transient GitHub API failure (e.g. the
        rate-limit circuit breaker being open) hits it at that exact moment, the parent
        is stranded permanently, since the sub-issue that would have re-triggered the
        check will never re-exit the pipeline again.

        This job re-scans every project's planning board every 15 minutes and re-runs
        ProjectMonitor._advance_parent_for_pr_review() for any "In Development" parent
        that actually has sub-issues. That method re-verifies completeness itself and
        has its own cooldown/review-cycle-limit guards, so calling it redundantly here
        is safe — it no-ops for parents that are genuinely still in progress.
        """
        logger.info("Starting orphaned-parent sweep")

        try:
            from config.manager import config_manager
            from config.state_manager import state_manager
            from services.project_monitor import ProjectMonitor
            from services.feature_branch_manager import feature_branch_manager
            from services.github_integration import GitHubIntegration
            from task_queue.task_manager import TaskQueue

            task_queue = TaskQueue(use_redis=True)
            project_monitor = ProjectMonitor(task_queue, config_manager)

            checked_count = 0
            error_count = 0

            for project_name in config_manager.list_visible_projects():
                try:
                    project_config = config_manager.get_project_config(project_name)

                    planning_pipeline = None
                    for pipeline in project_config.pipelines:
                        if not pipeline.active:
                            continue
                        if 'planning' in pipeline.name.lower() or 'planning' in pipeline.workflow.lower():
                            planning_pipeline = pipeline
                            break

                    if not planning_pipeline:
                        continue

                    project_state = state_manager.load_project_state(project_name)
                    if not project_state:
                        continue

                    board_state = project_state.boards.get(planning_pipeline.board_name)
                    if not board_state:
                        continue

                    items = project_monitor.get_project_items(
                        project_config.github['org'], board_state.project_number
                    )
                    in_dev_items = [item for item in items if item.status == "In Development"]

                    if not in_dev_items:
                        continue

                    github = GitHubIntegration(
                        repo_owner=project_config.github['org'],
                        repo_name=project_config.github['repo']
                    )

                    for item in in_dev_items:
                        try:
                            parent_issue_data = await github.get_issue(item.issue_number)
                            if not parent_issue_data:
                                continue

                            sub_issues = await feature_branch_manager._get_sub_issues_from_parent(
                                github, parent_issue_data
                            )
                            if not sub_issues:
                                # Not a sub-issue-bearing parent — out of scope for this sweep
                                continue

                            checked_count += 1
                            await project_monitor._advance_parent_for_pr_review(
                                project_name, item.issue_number, project_config
                            )

                        except Exception as e:
                            logger.error(
                                f"Error sweeping parent #{item.issue_number} in "
                                f"{project_name}: {e}",
                                exc_info=True
                            )
                            error_count += 1

                except Exception as e:
                    logger.error(f"Error sweeping project {project_name}: {e}", exc_info=True)
                    error_count += 1

            logger.info(
                f"Orphaned-parent sweep complete: {checked_count} sub-issue-bearing "
                f"'In Development' parents re-checked, {error_count} errors"
            )

        except Exception as e:
            logger.error(f"Error in orphaned-parent sweep: {e}", exc_info=True)

    async def _detect_empty_outputs(self):
        """
        Detect and retry executions marked as 'success' but with no GitHub output.

        This is the watchdog that catches failures in result persistence. It runs every
        10 minutes and uses comprehensive race condition protections to prevent duplicate
        work launches.

        Key protections:
        1. has_active_execution() - Checks ALL 4 types of active work
        2. Pipeline lock verification
        3. Queue status check (via eligibility checks)
        4. Execution eligibility via _should_retry_failed_execution
        5. 5-minute recency check

        The watchdog ONLY marks executions as 'failure' - it does NOT trigger work directly.
        The project_monitor picks up failed executions and handles retry with its own
        race protections.
        """
        logger.info("Starting empty output detection watchdog")

        try:
            from services.work_execution_state import work_execution_tracker

            # Run the detection in a thread pool to avoid blocking the event loop
            # This method processes 447+ files with file locks and blocking I/O
            loop = asyncio.get_event_loop()
            retried_count = await loop.run_in_executor(
                None,  # Uses default ThreadPoolExecutor
                work_execution_tracker.detect_and_retry_empty_successful_executions
            )

            if retried_count > 0:
                logger.info(
                    f"Empty output watchdog marked {retried_count} executions for retry "
                    f"(project_monitor will pick them up)"
                )
            else:
                logger.info("Empty output watchdog: No executions need retry")

        except Exception as e:
            logger.error(f"Error in empty output detection: {e}", exc_info=True)

    def _run_token_metrics(self):
        """Run full token metrics computation job (long lookback)."""
        logger.info("Starting token metrics computation job")
        try:
            from services.token_metrics_service import get_token_metrics_service
            get_token_metrics_service().run_metrics_job()
        except Exception as e:
            logger.error(f"Fatal error in token metrics job: {e}", exc_info=True)

    def _run_token_metrics_recent(self):
        """Run token metrics with a short lookback to keep recent data fresh."""
        logger.info("Starting recent token metrics computation (1h lookback)")
        try:
            from services.token_metrics_service import get_token_metrics_service
            get_token_metrics_service().run_metrics_job(lookback_hours=1)
        except Exception as e:
            logger.error(f"Fatal error in recent token metrics job: {e}", exc_info=True)

    def _run_project_metrics(self):
        """Run daily project metrics rollup job (1-day lookback)."""
        logger.info("Starting project metrics rollup job")
        try:
            from services.project_metrics_service import get_project_metrics_service
            get_project_metrics_service().run_metrics_job(lookback_days=1)
        except Exception as e:
            logger.error(f"Fatal error in project metrics job: {e}", exc_info=True)

    def _run_project_metrics_backfill(self):
        """Backfill project metrics with a 7-day lookback on startup."""
        logger.info("Starting project metrics startup backfill (7-day lookback)")
        try:
            from services.project_metrics_service import get_project_metrics_service
            get_project_metrics_service().run_metrics_job(lookback_days=7)
        except Exception as e:
            logger.error(f"Fatal error in project metrics backfill: {e}", exc_info=True)

    async def _cleanup_zombie_pipeline_runs(self):
        """Clean up zombie pipeline runs using PipelineWatchdog."""
        try:
            from services.pipeline_watchdog import get_pipeline_watchdog
            from services.pipeline_run import get_pipeline_run_manager
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            from elasticsearch import Elasticsearch
            loop = asyncio.get_event_loop()
            try:
                es_client = Elasticsearch(["http://elasticsearch:9200"])
            except Exception as e:
                logger.warning(f"Zombie cleanup: could not connect to Elasticsearch: {e}")
                es_client = None
            watchdog = get_pipeline_watchdog(
                es_client=es_client,
                pipeline_run_manager=get_pipeline_run_manager(),
                lock_manager=get_pipeline_lock_manager()
            )
            results = await loop.run_in_executor(None, watchdog.check_for_zombie_runs)
            if results.get('zombies_cleaned', 0) > 0:
                logger.info(
                    f"Zombie pipeline run cleanup: {results['zombies_cleaned']} "
                    f"cleaned of {results['zombies_found']} found"
                )
            else:
                logger.info(
                    f"Zombie pipeline run cleanup: none found "
                    f"(checked {results.get('checked', 0)} active runs)"
                )
        except Exception as e:
            logger.error(f"Zombie pipeline run cleanup failed: {e}", exc_info=True)

    async def _cleanup_docker_disk(self):
        """Remove dangling images, non-latest agent tags, and build cache."""
        import subprocess

        def run(cmd: list[str]) -> tuple[int, str]:
            result = subprocess.run(cmd, capture_output=True, text=True)
            return result.returncode, (result.stdout + result.stderr).strip()

        logger.info("Starting Docker disk cleanup")
        try:
            # Dangling images
            code, out = run(["docker", "image", "prune", "-f"])
            logger.info(f"Dangling image prune: {out.splitlines()[-1] if out else 'done'}")

            # Non-latest agent image tags
            code, out = run([
                "docker", "images",
                "--format", "{{.Repository}}:{{.Tag}}\t{{.Size}}",
            ])
            removed = 0
            for line in out.splitlines():
                parts = line.split("\t")
                if len(parts) != 2:
                    continue
                ref, size = parts
                repo, _, tag = ref.rpartition(":")
                if repo.endswith("-agent") and tag != "latest":
                    rc, msg = run(["docker", "rmi", ref])
                    if rc == 0:
                        logger.info(f"Removed non-latest agent image: {ref} ({size})")
                        removed += 1
                    else:
                        logger.warning(f"Could not remove {ref}: {msg}")
            logger.info(f"Non-latest agent image cleanup: {removed} removed")

            # Build cache
            logger.info("Pruning build cache (may take a moment)")
            code, out = run(["docker", "builder", "prune", "-f"])
            logger.info(f"Build cache prune: {out.splitlines()[-1] if out else 'done'}")

            logger.info("Docker disk cleanup complete")
        except Exception as e:
            logger.error(f"Error in Docker disk cleanup: {e}", exc_info=True)

    def _run_test_cycle_stats(self):
        """Run weekly test-cycle duration stats rollup."""
        logger.info("Starting test-cycle stats rollup job")
        try:
            import os
            import sys
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
            from elasticsearch import Elasticsearch
            from scripts.calculate_test_cycle_stats import calculate_stats
            es_url = os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
            es = Elasticsearch([es_url])
            count = calculate_stats(es=es)
            logger.info(f"Test-cycle stats rollup complete: {count} groups updated")
        except Exception as e:
            logger.error(f"Fatal error in test-cycle stats rollup: {e}", exc_info=True)

    def run_test_cycle_stats_now(self):
        """Run test-cycle stats rollup immediately (for testing/manual trigger)."""
        logger.info("Manually triggering test-cycle stats rollup")
        self._run_test_cycle_stats()

    def run_cleanup_now(self):
        """Run cleanup task immediately (for testing/manual trigger)"""
        logger.info("Manually triggering orphaned branch cleanup")
        asyncio.create_task(self._cleanup_orphaned_branches())

    def run_stale_check_now(self):
        """Run stale branch check immediately (for testing/manual trigger)"""
        logger.info("Manually triggering stale branch check")
        asyncio.create_task(self._check_stale_branches())

    def run_container_cleanup_now(self):
        """Run container cleanup immediately (for testing/manual trigger)"""
        logger.info("Manually triggering orphaned container cleanup")
        asyncio.create_task(self._cleanup_orphaned_containers())

    def run_orphaned_container_cleanup_now(self):
        """Alias for run_container_cleanup_now (replaces removed Docker reconciliation)"""
        self.run_container_cleanup_now()

    def run_test_container_reaper_now(self):
        """Run orphaned testcontainers reaper immediately (for testing/manual trigger)"""
        logger.info("Manually triggering orphaned testcontainers reaper")
        asyncio.create_task(self._reap_orphaned_test_containers())

    def run_queue_reconciliation_now(self):
        """Run queue state reconciliation immediately (for testing/manual trigger)"""
        logger.info("Manually triggering queue state reconciliation")
        asyncio.create_task(self._reconcile_queue_state())

    def run_empty_output_detection_now(self):
        """Run empty output detection immediately (for testing/manual trigger)"""
        logger.info("Manually triggering empty output detection")
        asyncio.create_task(self._detect_empty_outputs())

    def run_token_metrics_now(self):
        """Run token metrics computation immediately (for testing/manual trigger)"""
        logger.info("Manually triggering token metrics computation")
        self._run_token_metrics()

    def run_full_history_token_metrics_now(self):
        """Backfill token metrics across all available history without affecting the cron cadence."""
        logger.info("Manually triggering full-history token metrics backfill")
        self._run_full_history_token_metrics()

    def _run_full_history_token_metrics(self):
        """Run token metrics job with a lookback that covers all available event history."""
        logger.info("Starting full-history token metrics backfill")
        try:
            from services.token_metrics_service import get_token_metrics_service
            service = get_token_metrics_service()
            lookback_hours = service.find_oldest_event_hours_ago()
            logger.info(f"Full-history backfill: oldest event is ~{lookback_hours}h ago")
            service.run_metrics_job(lookback_hours=lookback_hours)
        except Exception as e:
            logger.error(f"Fatal error in full-history token metrics backfill: {e}", exc_info=True)

    def run_project_metrics_now(self):
        """Run project metrics rollup immediately (for testing/manual trigger)."""
        logger.info("Manually triggering project metrics rollup")
        self._run_project_metrics()

    def run_project_metrics_backfill_now(self):
        """Run project metrics 7-day backfill immediately (for testing/manual trigger)."""
        logger.info("Manually triggering project metrics backfill")
        self._run_project_metrics_backfill()

    def run_docker_cleanup_now(self):
        """Run Docker disk cleanup immediately (for testing/manual trigger)."""
        logger.info("Manually triggering Docker disk cleanup")
        asyncio.create_task(self._cleanup_docker_disk())


# Global instance
_scheduled_tasks_service = None


def get_scheduled_tasks_service() -> ScheduledTasksService:
    """Get the global scheduler instance"""
    global _scheduled_tasks_service
    if _scheduled_tasks_service is None:
        _scheduled_tasks_service = ScheduledTasksService()
    return _scheduled_tasks_service

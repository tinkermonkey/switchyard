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

        # Schedule review learning pipeline - daily at 3 AM
        self.scheduler.add_job(
            self._run_review_learning_cycle,
            trigger=CronTrigger(hour=3, minute=0),
            id='review_learning',
            name='Review feedback learning and pattern detection',
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

        # Schedule Docker state reconciliation - every 5 minutes
        self.scheduler.add_job(
            self._reconcile_docker_state,
            trigger=CronTrigger(minute='*/5'),
            id='reconcile_docker_state',
            name='Reconcile Docker containers with tracking state',
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

        # Schedule token metrics computation
        token_metrics_hours = int(os.environ.get('TOKEN_METRICS_INTERVAL_HOURS', '3'))
        self.scheduler.add_job(
            self._run_token_metrics,
            trigger=IntervalTrigger(hours=token_metrics_hours),
            id='token_metrics',
            name=f'Compute token usage metrics (every {token_metrics_hours}h)',
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

        # Pipeline analysis catch-up on startup (handles restarts missing the trigger)
        analysis_startup_jitter = random.uniform(120, 300)  # 2–5 min, let ES warm up
        self.scheduler.add_job(
            self._run_pipeline_analysis_catchup,
            trigger=DateTrigger(
                run_date=datetime.now(timezone.utc) + timedelta(seconds=analysis_startup_jitter)
            ),
            id='pipeline_analysis_catchup_startup',
            name='Pipeline analysis catchup (startup)',
            replace_existing=True
        )

        # Pipeline analysis catch-up every 10 minutes
        self.scheduler.add_job(
            self._run_pipeline_analysis_catchup,
            trigger=IntervalTrigger(minutes=10),
            id='pipeline_analysis_catchup',
            name='Pipeline analysis catchup (periodic)',
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

        self.scheduler.start()
        self.running = True
        logger.info("Scheduled tasks service started")
        logger.info("- Orphaned branch cleanup: Daily at 2 AM")
        logger.info("- Review learning pipeline: Daily at 3 AM")
        logger.info("- Stale branch checks: Daily at 9 AM")
        logger.info("- Orphaned container cleanup: Every 20 minutes")
        logger.info("- Docker state reconciliation: Every 5 minutes")
        logger.info("- Queue state reconciliation: Every 10 minutes")
        logger.info("- Empty output detection: Every 15 minutes")
        logger.info(f"- Token metrics computation: Every {token_metrics_hours} hours")
        logger.info("- Project metrics rollup: Daily at 3:30 AM")
        logger.info(f"- Project metrics backfill: Once at startup in ~{jitter_seconds:.0f}s")
        logger.info(f"- Pipeline analysis catchup: Once at startup in ~{analysis_startup_jitter:.0f}s, then every 10 min")
        logger.info("- Zombie pipeline run cleanup: Every 30 minutes")

    def stop(self):
        """Stop the scheduler"""
        if not self.running:
            return

        self.scheduler.shutdown()
        self.running = False
        logger.info("Scheduled tasks service stopped")

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

    async def _run_review_learning_cycle(self):
        """
        Run review learning pipeline to detect patterns and update filters.

        This runs daily to:
        1. Detect low-value review patterns with high ignore rates
        2. Generate or update filter rules
        3. Prune stale/ineffective filters
        4. Track overall filter effectiveness
        """
        logger.info("Starting scheduled review learning cycle")

        try:
            from services.review_pattern_detector import get_review_pattern_detector
            from services.review_filter_manager import get_review_filter_manager

            pattern_detector = get_review_pattern_detector()
            filter_manager = get_review_filter_manager()

            # 1. Detect low-value patterns (30-day window)
            logger.info("Detecting low-value review patterns (30d lookback)")
            patterns = await pattern_detector.detect_low_value_patterns(lookback_days=30)
            logger.info(f"Detected {len(patterns)} low-value patterns")

            # 2. Generate or update filters for each pattern
            new_filters = 0
            updated_filters = 0

            for pattern in patterns:
                # Check if filter already exists
                existing = await filter_manager.get_filter_by_pattern(
                    agent=pattern['agent'],
                    category=pattern['category'],
                    pattern_sig=pattern['pattern_description']
                )

                if existing:
                    # Update existing filter with new stats
                    await filter_manager.update_filter_stats(
                        filter_id=existing['filter_id'],
                        new_stats={
                            'ignore_rate': pattern['ignore_rate'],
                            'acceptance_rate': pattern['acceptance_rate'],
                            'sample_size': pattern['sample_size'],
                            'confidence': pattern['confidence']
                        }
                    )
                    updated_filters += 1
                    logger.info(
                        f"Updated filter for {pattern['agent']}/{pattern['category']}: "
                        f"ignore_rate={pattern['ignore_rate']:.1%}"
                    )
                else:
                    # Create new filter
                    filter_id = await filter_manager.create_filter({
                        'agent': pattern['agent'],
                        'category': pattern['category'],
                        'severity': pattern['severity'],
                        'pattern_description': pattern['pattern_description'],
                        'reason_ignored': pattern['reason_ignored'],
                        'action': pattern['suggested_action'],
                        'confidence': pattern['confidence'],
                        'sample_size': pattern['sample_size'],
                        'ignore_rate': pattern['ignore_rate'],
                        'acceptance_rate': pattern['acceptance_rate'],
                        'active': True
                    })
                    new_filters += 1
                    logger.info(
                        f"Created new filter {filter_id} for {pattern['agent']}/{pattern['category']}: "
                        f"ignore_rate={pattern['ignore_rate']:.1%}"
                    )

            # 3. Prune stale or ineffective filters
            logger.info("Pruning stale filters (age > 90d or effectiveness < 50%)")
            pruned_count = await filter_manager.prune_stale_filters(
                max_age_days=90,
                min_effectiveness=0.5
            )

            # 4. Get overall metrics
            metrics = await filter_manager.get_filter_metrics()

            logger.info(
                f"Review learning cycle complete:\n"
                f"  - Patterns detected: {len(patterns)}\n"
                f"  - New filters: {new_filters}\n"
                f"  - Updated filters: {updated_filters}\n"
                f"  - Pruned filters: {pruned_count}\n"
                f"  - Total active filters: {metrics.get('active_filters', 0)}\n"
                f"  - Filter precision: {metrics.get('precision', 0):.1%}\n"
                f"  - Total applications: {metrics.get('total_applications', 0)}"
            )

        except Exception as e:
            logger.error(f"Fatal error in review learning cycle: {e}", exc_info=True)

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

    async def _reconcile_docker_state(self):
        """
        Reconcile Docker container state with tracking systems.

        Force reconciliation between:
        - Actual Docker containers (docker ps)
        - Redis tracking keys
        - Work execution state
        - Pipeline locks

        This runs every 5 minutes to catch state divergence early.
        """
        logger.info("Starting Docker state reconciliation")

        try:
            import subprocess
            import redis
            from services.work_execution_state import work_execution_tracker
            from services.pipeline_lock_manager import PipelineLockManager

            # Get actual running agent containers from Docker (run in thread pool - blocking I/O)
            def get_docker_containers():
                return subprocess.run(
                    ['docker', 'ps', '--filter', 'name=claude-agent-', '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=10
                )
            
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(None, get_docker_containers)

            actual_containers = set()
            if result.returncode == 0 and result.stdout.strip():
                actual_containers = set(result.stdout.strip().split('\n'))

            logger.info(f"Found {len(actual_containers)} actual running containers")

            # Get tracked containers from Redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            tracked_keys = redis_client.keys('agent:container:*')

            tracked_containers = set()
            for key in tracked_keys:
                # Extract container name from key
                container_name = key.replace('agent:container:', '')
                tracked_containers.add(container_name)

            logger.info(f"Found {len(tracked_containers)} tracked containers in Redis")

            # Find discrepancies
            orphaned_tracking = tracked_containers - actual_containers
            untracked_containers = actual_containers - tracked_containers

            if orphaned_tracking:
                logger.warning(
                    f"Found {len(orphaned_tracking)} orphaned tracking entries "
                    f"(tracked but not running)"
                )

                for container_name in orphaned_tracking:
                    try:
                        # Read structured data from Redis BEFORE deleting the key.
                        # The hash stores project, issue_number, agent, task_id reliably.
                        container_info = redis_client.hgetall(f'agent:container:{container_name}')

                        issue_number = None
                        project_name = None
                        if container_info:
                            try:
                                issue_number = int(container_info['issue_number']) if container_info.get('issue_number') else None
                            except (ValueError, TypeError):
                                pass
                            project_name = container_info.get('project')

                        if not issue_number or not project_name:
                            # Fallback: extract from container name (less reliable)
                            parts = container_name.split('_')
                            for i, part in enumerate(parts):
                                if part.isdigit() and i > 0:
                                    if i < len(parts) // 2 and int(part) < 10000:
                                        issue_number = int(part)
                                        if i >= 3:
                                            project_name = parts[i-3]
                                        break

                        logger.info(
                            f"Cleaning up orphaned tracking for {container_name} "
                            f"(project={project_name}, issue=#{issue_number})"
                        )

                        # If we can identify the issue, mark execution as failed and release locks
                        if issue_number and project_name:
                            # Coordination guard: prevent double-processing with other mechanisms
                            try:
                                from services.cleanup_guard import try_claim_cleanup
                                if not try_claim_cleanup(project_name, issue_number, "docker_reconciliation"):
                                    # Still clean up the orphaned tracking key even if another
                                    # mechanism owns the cleanup for this issue's execution state.
                                    redis_client.delete(f'agent:container:{container_name}')
                                    continue
                            except Exception as e:
                                logger.warning(f"Cleanup guard unavailable, proceeding without coordination: {e}")

                            # Skip if issue has an active review cycle or feedback loop
                            # — these legitimately have no container running.
                            try:
                                from services.review_cycle import review_cycle_executor
                                from services.human_feedback_loop import human_feedback_loop_executor
                                rc = review_cycle_executor.active_cycles.get(issue_number)
                                fl = human_feedback_loop_executor.active_loops.get(issue_number)
                                if ((rc and rc.project_name == project_name and rc.status == 'awaiting_human_feedback') or
                                        (fl and fl.project_name == project_name)):
                                    logger.info(
                                        f"Skipping orphaned tracking cleanup for {project_name}/#{issue_number} "
                                        f"- awaiting feedback"
                                    )
                                    redis_client.delete(f'agent:container:{container_name}')
                                    continue
                            except Exception as e:
                                logger.warning(
                                    f"Could not check review/feedback state for {project_name}/#{issue_number} "
                                    f"during Docker reconciliation: {e}"
                                )
                                # Fail-safe: don't clean up execution state for a run we can't verify,
                                # but still remove the orphaned tracking key (container is gone regardless).
                                redis_client.delete(f'agent:container:{container_name}')
                                continue

                        # Clean up Redis tracking (after guard checks pass)
                        redis_client.delete(f'agent:container:{container_name}')

                        if issue_number and project_name:
                            # Mark execution as failed (if still in_progress)
                            try:
                                if work_execution_tracker.has_active_execution(project_name, issue_number):
                                    state = work_execution_tracker.load_state(project_name, issue_number)
                                    for execution in state.get('execution_history', []):
                                        if execution.get('outcome') == 'in_progress':
                                            agent = execution.get('agent', 'unknown')
                                            column = execution.get('column', 'unknown')

                                            work_execution_tracker.record_execution_outcome(
                                                issue_number=issue_number,
                                                column=column,
                                                agent=agent,
                                                outcome='failed',
                                                project_name=project_name,
                                                error=(
                                                    'Container disappeared without completing. '
                                                    'Detected by Docker state reconciliation.'
                                                )
                                            )

                                            logger.info(
                                                f"Docker reconciliation marked execution as failed: "
                                                f"{project_name}/#{issue_number} {agent}"
                                            )
                                            break  # Only mark the first in_progress execution
                            except Exception as e:
                                logger.warning(f"Could not update execution state: {e}")

                            # Release any pipeline locks held by this issue via the lock manager API.
                            # Using the API (instead of deleting lock files directly) ensures proper
                            # state management and allows the next queued issue to be dispatched.
                            try:
                                from services.pipeline_lock_manager import get_pipeline_lock_manager
                                lm = get_pipeline_lock_manager()
                                from config.manager import config_manager
                                project_config = config_manager.get_project_config(project_name)
                                for pipeline in project_config.pipelines:
                                    board = pipeline.board_name
                                    current_lock = lm.get_lock(project_name, board)
                                    if (current_lock and
                                            current_lock.lock_status == 'locked' and
                                            current_lock.locked_by_issue == issue_number):
                                        logger.info(
                                            f"Releasing lock for {project_name}/{board} "
                                            f"(held by orphaned issue #{issue_number})"
                                        )
                                        lm.release_lock(project_name, board, issue_number)
                            except Exception as e:
                                logger.warning(f"Could not release locks: {e}")

                    except Exception as e:
                        logger.error(f"Error cleaning up {container_name}: {e}")

            if untracked_containers:
                logger.warning(
                    f"Found {len(untracked_containers)} untracked containers "
                    f"(running but not tracked) - attempting recovery"
                )

                for container_name in untracked_containers:
                    try:
                        logger.info(f"Attempting to recover monitoring for {container_name}")
                        # This will be handled by agent_container_recovery on next startup
                        # For now, just log it
                    except Exception as e:
                        logger.error(f"Error recovering {container_name}: {e}")

            if not orphaned_tracking and not untracked_containers:
                logger.info("Docker state is consistent with tracking (no discrepancies)")

            logger.info("Docker state reconciliation completed")

        except Exception as e:
            logger.error(f"Error in Docker state reconciliation: {e}", exc_info=True)

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
        """Run token metrics computation job"""
        logger.info("Starting token metrics computation job")
        try:
            from services.token_metrics_service import get_token_metrics_service
            get_token_metrics_service().run_metrics_job()
        except Exception as e:
            logger.error(f"Fatal error in token metrics job: {e}", exc_info=True)

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

    async def _run_pipeline_analysis_catchup(self):
        """
        Catch-up scan: find completed pipeline runs missing a summary and analyse them.

        Queries ES for completed runs in the last 24h with no summary field, then
        runs analysis for each. This handles restarts where the inline trigger was missed.
        """
        logger.info("pipeline_run_analysis: starting catch-up scan")

        try:
            from services.pipeline_run_analysis import get_pipeline_run_analysis_service

            service = get_pipeline_run_analysis_service()

            result = service.es.search(
                index="pipeline-runs-*",
                body={
                    "query": {
                        "bool": {
                            "must": [{"term": {"status": "completed"}}],
                            "must_not": [{"exists": {"field": "summary"}}],
                            "filter": [{"range": {"ended_at": {"gte": "now-24h"}}}],
                        }
                    },
                    "size": 20,
                    "_source": ["id", "started_at"],
                },
            )

            hits = result.get("hits", {}).get("hits", [])
            logger.info(f"pipeline_run_analysis: catch-up scan found {len(hits)} unanalysed runs")

            loop = asyncio.get_event_loop()
            for hit in hits:
                source = hit.get("_source", {})
                run_id = source.get("id")
                started_at = source.get("started_at", "")
                if run_id:
                    # Run in thread pool — blocking subprocess call
                    await loop.run_in_executor(
                        None, service.run_analysis_for_run, run_id, started_at
                    )

            logger.info("pipeline_run_analysis: catch-up scan complete")

        except Exception as e:
            logger.error(f"pipeline_run_analysis: error in catch-up scan: {e}", exc_info=True)

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

    def run_cleanup_now(self):
        """Run cleanup task immediately (for testing/manual trigger)"""
        logger.info("Manually triggering orphaned branch cleanup")
        asyncio.create_task(self._cleanup_orphaned_branches())

    def run_stale_check_now(self):
        """Run stale branch check immediately (for testing/manual trigger)"""
        logger.info("Manually triggering stale branch check")
        asyncio.create_task(self._check_stale_branches())

    def run_review_learning_now(self):
        """Run review learning cycle immediately (for testing/manual trigger)"""
        logger.info("Manually triggering review learning cycle")
        asyncio.create_task(self._run_review_learning_cycle())

    def run_container_cleanup_now(self):
        """Run container cleanup immediately (for testing/manual trigger)"""
        logger.info("Manually triggering orphaned container cleanup")
        asyncio.create_task(self._cleanup_orphaned_containers())

    def run_docker_reconciliation_now(self):
        """Run Docker state reconciliation immediately (for testing/manual trigger)"""
        logger.info("Manually triggering Docker state reconciliation")
        asyncio.create_task(self._reconcile_docker_state())

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


# Global instance
_scheduled_tasks_service = None


def get_scheduled_tasks_service() -> ScheduledTasksService:
    """Get the global scheduler instance"""
    global _scheduled_tasks_service
    if _scheduled_tasks_service is None:
        _scheduled_tasks_service = ScheduledTasksService()
    return _scheduled_tasks_service

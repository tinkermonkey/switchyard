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

        self.scheduler.start()
        self.running = True
        logger.info("Scheduled tasks service started")
        logger.info("- Orphaned branch cleanup: Daily at 2 AM")
        logger.info("- Stale branch checks: Daily at 9 AM")
        logger.info("- Orphaned container cleanup: Every 20 minutes")
        logger.info("- Docker state reconciliation: Every 5 minutes")
        logger.info("- Queue state reconciliation: Every 10 minutes")
        logger.info("- Empty output detection: Every 15 minutes")
        logger.info(f"- Token metrics (recent): Once at startup in ~{token_startup_jitter:.0f}s, then every 15m (1h lookback)")
        logger.info(f"- Token metrics (full): Every {token_metrics_hours}h")
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

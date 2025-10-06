"""
Scheduled Tasks Service

Runs periodic maintenance tasks like cleanup of orphaned branches.
Uses APScheduler for Python-native scheduling.
"""

import logging
import asyncio
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime

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

        self.scheduler.start()
        self.running = True
        logger.info("Scheduled tasks service started")
        logger.info("- Orphaned branch cleanup: Daily at 2 AM")
        logger.info("- Review learning pipeline: Daily at 3 AM")
        logger.info("- Stale branch checks: Daily at 9 AM")

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

            # Get all projects
            project_configs = config_manager.get_all_project_configs()

            cleanup_count = 0
            error_count = 0

            for project_name, project_config in project_configs.items():
                try:
                    if not hasattr(project_config, 'repository'):
                        continue

                    # Parse repository
                    repo_parts = project_config.repository.split('/')
                    if len(repo_parts) != 2:
                        logger.warning(f"Invalid repository format for {project_name}")
                        continue

                    repo_owner, repo_name = repo_parts
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

            # Get all projects
            project_configs = config_manager.get_all_project_configs()

            warning_count = 0
            error_count = 0

            for project_name, project_config in project_configs.items():
                try:
                    if not hasattr(project_config, 'repository'):
                        continue

                    # Parse repository
                    repo_parts = project_config.repository.split('/')
                    if len(repo_parts) != 2:
                        continue

                    repo_owner, repo_name = repo_parts
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
                            await feature_branch_manager.escalate_stale_branch(
                                gh_integration,
                                fb.parent_issue,
                                fb.branch_name,
                                commits_behind
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


# Global instance
_scheduled_tasks_service = None


def get_scheduled_tasks_service() -> ScheduledTasksService:
    """Get the global scheduler instance"""
    global _scheduled_tasks_service
    if _scheduled_tasks_service is None:
        _scheduled_tasks_service = ScheduledTasksService()
    return _scheduled_tasks_service

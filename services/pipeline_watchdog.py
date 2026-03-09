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
        check_interval_seconds: int = 300  # 5 minutes
    ):
        """
        Initialize the watchdog.

        Args:
            es_client: Elasticsearch client for querying pipeline runs
            pipeline_run_manager: PipelineRunManager instance for ending runs
            lock_manager: PipelineLockManager instance for releasing locks
            zombie_threshold_minutes: Minutes before run is considered zombie
            check_interval_seconds: Seconds between watchdog checks
        """
        self.es = es_client
        self.pipeline_run_manager = pipeline_run_manager
        self.lock_manager = lock_manager
        self.zombie_threshold_minutes = zombie_threshold_minutes
        self.check_interval_seconds = check_interval_seconds
        self.running = False

    def check_for_zombie_runs(self) -> Dict[str, Any]:
        """
        Find and clean up zombie pipeline runs.

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

                # Never clean up a run that still holds the pipeline lock.
                # The lock gates all important flows (review cycles, repair cycles, escalations
                # awaiting human feedback, etc.) — if it's held, work is in progress or pending.
                if self.lock_manager:
                    current_lock = self.lock_manager.get_lock(project, board)
                    if current_lock and current_lock.lock_status == 'locked' and current_lock.locked_by_issue == issue_number:
                        logger.info(
                            f"Pipeline run {pipeline_run_id[:8]}... for {project} issue #{issue_number} "
                            f"holds the pipeline lock - skipping zombie cleanup"
                        )
                        continue

                # No container, old enough, and no lock held = ZOMBIE
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

        Args:
            project: Project name
            issue_number: Issue number

        Returns:
            True if container exists, False otherwise
        """
        try:
            # Check for regular agent containers (pattern: claude-agent-{project}-*)
            result = subprocess.run(
                ['docker', 'ps', '--filter', f'name=claude-agent-{project}',
                 '--format', '{{.Names}}'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0:
                containers = result.stdout.strip().split('\n')
                # Check if any container name contains the issue number
                for container in containers:
                    if container and str(issue_number) in container:
                        logger.debug(f"Found agent container for issue #{issue_number}: {container}")
                        return True

            # Also check for repair cycle containers
            result = subprocess.run(
                ['docker', 'ps', '--filter', f'name=repair-cycle-{project}-{issue_number}',
                 '--format', '{{.Names}}'],
                capture_output=True,
                text=True,
                timeout=5
            )

            if result.returncode == 0 and result.stdout.strip():
                logger.debug(f"Found repair cycle container for issue #{issue_number}")
                return True

            return False

        except Exception as e:
            logger.warning(f"Error checking for agent container: {e}")
            # Fail safe: assume container exists to avoid false positives
            return True

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

        # End the pipeline run
        if self.pipeline_run_manager:
            ended = self.pipeline_run_manager.end_pipeline_run(
                project=project,
                issue_number=issue_number,
                reason=f"Zombie pipeline run cleanup (started: {started_at}, no container found)",
                outcome="failed"
            )

            if ended:
                logger.info(f"Ended zombie pipeline run {pipeline_run_id[:8]}...")
            else:
                logger.warning(f"Failed to end zombie pipeline run {pipeline_run_id[:8]}...")

        # Release the pipeline lock
        if self.lock_manager:
            released = self.lock_manager.release_lock(project, board, issue_number)

            if released:
                logger.info(
                    f"Released pipeline lock for {project} issue #{issue_number} "
                    f"after zombie cleanup"
                )
            else:
                logger.warning(
                    f"Lock was not held for {project} issue #{issue_number} "
                    f"during zombie cleanup (may have already been released)"
                )

        # Remove any stale review cycle state so it doesn't block future runs
        try:
            from services.review_cycle import review_cycle_executor
            cycle_state = review_cycle_executor.active_cycles.get(issue_number)
            if cycle_state is None:
                # Also check disk (cycle may not be in-memory after a restart)
                all_cycles = review_cycle_executor._load_active_cycles(project)
                cycle_state = next((c for c in all_cycles if c.issue_number == issue_number), None)
            if cycle_state:
                review_cycle_executor._remove_cycle_state(cycle_state)
                if issue_number in review_cycle_executor.active_cycles:
                    del review_cycle_executor.active_cycles[issue_number]
                logger.info(f"Removed stale review cycle state for {project} issue #{issue_number} during zombie cleanup")
        except Exception as e:
            logger.warning(f"Failed to clean up review cycle state during zombie cleanup for {project} issue #{issue_number}: {e}")

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

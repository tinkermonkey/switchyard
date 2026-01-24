"""
Pipeline Queue Manager

Manages the queue of issues waiting to execute in a pipeline.
Uses GitHub board column order as the source of truth for execution order.

Only ONE issue can execute at a time per pipeline (enforced by PipelineLockManager).
Other issues wait in queue based on their position in the GitHub board column.
"""

import yaml
import logging
import fcntl
import contextlib
from pathlib import Path
from typing import Optional, List, Dict, Any
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class PipelineQueueManager:
    """Manages pipeline issue queue with GitHub board order as source of truth"""

    def __init__(self, project_name: str, board_name: str, state_dir: Path = None):
        """
        Initialize pipeline queue manager.

        Args:
            project_name: Project name
            board_name: Board name
            state_dir: Directory for queue state persistence
        """
        if state_dir is None:
            import os
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "pipeline_queues"

        self.project_name = project_name
        self.board_name = board_name
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        logger.debug(
            f"PipelineQueueManager initialized for {project_name}/{board_name}"
        )

    def _get_state_file(self) -> Path:
        """Get YAML state file path for queue"""
        return self.state_dir / f"{self.project_name}_{self.board_name}.yaml"

    def _get_lock_file(self) -> Path:
        """Get lock file path for queue operations"""
        return self.state_dir / f"{self.project_name}_{self.board_name}.lock"

    @contextlib.contextmanager
    def _queue_lock(self, timeout: int = 10):
        """
        Context manager for exclusive queue file access.

        Uses POSIX file locking (fcntl) to prevent concurrent modifications
        to the queue state file. This prevents race conditions when multiple
        threads/processes attempt to sync or modify the queue simultaneously.

        Args:
            timeout: Maximum seconds to wait for lock (default 10)

        Yields:
            None - just provides locked context

        Raises:
            TimeoutError: If lock cannot be acquired within timeout
        """
        lock_file = self._get_lock_file()
        lock_file.parent.mkdir(parents=True, exist_ok=True)

        # Open lock file (create if doesn't exist)
        f = open(lock_file, 'w')

        try:
            # Try to acquire exclusive lock with timeout
            import time
            start_time = time.time()

            while True:
                try:
                    # LOCK_EX = exclusive lock, LOCK_NB = non-blocking
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    logger.debug(f"Acquired queue lock for {self.project_name}/{self.board_name}")
                    break
                except (IOError, OSError):
                    # Lock is held by another process
                    if time.time() - start_time > timeout:
                        raise TimeoutError(
                            f"Could not acquire queue lock for {self.project_name}/{self.board_name} "
                            f"within {timeout} seconds"
                        )
                    time.sleep(0.1)  # Wait 100ms before retry

            yield  # Execute code within locked context

        finally:
            # Release lock
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                logger.debug(f"Released queue lock for {self.project_name}/{self.board_name}")
            except:
                pass

            # Close lock file
            try:
                f.close()
            except:
                pass

    def load_queue(self) -> List[Dict[str, Any]]:
        """Load queue state from YAML"""
        state_file = self._get_state_file()

        if not state_file.exists():
            return []

        try:
            with open(state_file, 'r') as f:
                data = yaml.safe_load(f)
                return data.get('queue', []) if data else []
        except Exception as e:
            logger.error(f"Failed to load queue state: {e}")
            return []

    def save_queue(self, queue: List[Dict[str, Any]]):
        """
        Save queue state to YAML.

        Raises:
            Exception: If save operation fails (disk full, permissions, etc.)

        Note: Does NOT swallow exceptions - caller must handle failures.
        """
        state_file = self._get_state_file()

        data = {
            'project': self.project_name,
            'board': self.board_name,
            'queue': queue,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }

        with open(state_file, 'w') as f:
            yaml.dump(data, f, default_flow_style=False, sort_keys=False)

        logger.debug(f"Saved queue state: {len(queue)} issues")

    def get_issues_in_column_order(self, column_name: str) -> List[Dict[str, Any]]:
        """
        Fetch current issue order from GitHub Projects v2 board column.

        This queries GitHub GraphQL API to get issues in the specified column,
        preserving their board order (top to bottom).

        Args:
            column_name: Column name to query (e.g., "Development")

        Returns:
            List of issues with positions, ordered top-to-bottom:
            [
                {'issue_number': 123, 'position': 0, 'item_id': '...', 'title': '...'},
                {'issue_number': 124, 'position': 1, 'item_id': '...', 'title': '...'},
                ...
            ]
        """
        try:
            from config.manager import config_manager
            from config.state_manager import state_manager
            from services.github_api_client import get_github_client
            from services.github_owner_utils import build_projects_v2_query, get_owner_type

            # Get project config
            project_config = config_manager.get_project_config(self.project_name)
            project_state = state_manager.load_project_state(self.project_name)

            if not project_state:
                logger.error(f"No GitHub state found for {self.project_name}")
                return []

            # Get board state
            board_state = project_state.boards.get(self.board_name)
            if not board_state:
                logger.error(
                    f"Board '{self.board_name}' not found in project state"
                )
                return []

            # Build GraphQL query
            query = build_projects_v2_query(
                project_config.github['org'],
                board_state.project_number
            )

            if query is None:
                logger.error(
                    f"Cannot build query for {project_config.github['org']}"
                )
                return []

            # Execute query
            github_client = get_github_client()
            success, data = github_client.graphql(query)

            if not success:
                logger.error(f"GraphQL query failed: {data}")
                return []

            # Extract project data based on owner type
            owner_type = get_owner_type(project_config.github['org'])
            if owner_type == 'user':
                project_data = data.get('user', {}).get('projectV2', {})
            else:  # organization
                project_data = data.get('organization', {}).get('projectV2', {})

            # Get items and filter by column
            items = project_data.get('items', {}).get('nodes', [])

            # Extract issues in the target column, maintaining order
            issues_in_order = []
            position = 0

            for item in items:
                # Get status field value by iterating through fieldValues
                # (similar to project_monitor.py line 415-418)
                status_name = None
                field_values = item.get('fieldValues', {}).get('nodes', [])
                for field_value in field_values:
                    if field_value and field_value.get('field', {}).get('name') == 'Status':
                        status_name = field_value.get('name')
                        break

                # Check if this item is in the target column
                if status_name != column_name:
                    continue

                # Get issue details
                content = item.get('content', {})
                if content.get('__typename') != 'Issue':
                    continue

                issue_number = content.get('number')
                issue_state = content.get('state', 'OPEN')

                # Only include open issues
                if issue_state != 'OPEN':
                    continue

                if issue_number:
                    issues_in_order.append({
                        'issue_number': issue_number,
                        'position': position,
                        'item_id': item.get('id'),
                        'title': content.get('title', '')
                    })
                    position += 1

            logger.info(
                f"Fetched {len(issues_in_order)} issues from "
                f"{self.project_name}/{self.board_name}/{column_name} in board order"
            )

            return issues_in_order

        except Exception as e:
            logger.error(f"Failed to fetch board order from GitHub: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return []

    def is_issue_in_queue(self, issue_number: int) -> bool:
        """
        Check if issue is already in queue.

        Uses lock to prevent reading partially written queue state.
        """
        with self._queue_lock():
            queue = self.load_queue()
            return any(issue['issue_number'] == issue_number for issue in queue)

    def enqueue_issue(self, issue_number: int, column: str, timestamp: str):
        """
        Add issue to pipeline queue or update if already exists.

        This method is simplified - it no longer handles "completed" status.
        Issues are automatically removed from the queue when they leave the trigger column
        via sync_queue_with_github().

        Args:
            issue_number: Issue number to add
            column: Column the issue is in
            timestamp: Timestamp when queued

        Note: GitHub API call is performed OUTSIDE the lock to minimize lock hold time.
        Only file I/O operations are performed within the lock.
        """
        # STEP 1: Fetch position from GitHub OUTSIDE the lock (network I/O)
        issues_in_column = self.get_issues_in_column_order(column)
        position = next(
            (item['position'] for item in issues_in_column
             if item['issue_number'] == issue_number),
            999  # Default if not found
        )

        # STEP 2: Hold lock ONLY for file read-modify-write
        with self._queue_lock():
            queue = self.load_queue()

            # Check if already in queue
            existing_issue = next((issue for issue in queue if issue['issue_number'] == issue_number), None)

            if existing_issue:
                # If issue is active, don't modify it (work in progress)
                if existing_issue['status'] == 'active':
                    logger.debug(f"Issue #{issue_number} already active in queue, skipping enqueue")
                    return

                # If waiting, update the timestamp and position (issue was re-detected)
                existing_issue['queued_at'] = timestamp
                existing_issue['last_position_check'] = timestamp
                existing_issue['position_in_column'] = position
                self.save_queue(queue)
                logger.debug(f"Updated timestamp for issue #{issue_number} in queue")
                return

            queue.append({
                'issue_number': issue_number,
                'queued_at': timestamp,
                'initial_column': column,
                'status': 'waiting',
                'last_position_check': timestamp,
                'position_in_column': position
            })

            self.save_queue(queue)

            logger.info(
                f"Added issue #{issue_number} to pipeline queue "
                f"(position: {position}, status: waiting)"
            )

    def mark_issue_active(self, issue_number: int):
        """Mark issue as active (currently executing)"""
        with self._queue_lock():
            queue = self.load_queue()

            for issue in queue:
                if issue['issue_number'] == issue_number:
                    issue['status'] = 'active'
                    issue['activated_at'] = datetime.now(timezone.utc).isoformat()
                    break

            self.save_queue(queue)

            logger.info(f"Marked issue #{issue_number} as active in pipeline queue")

    def sync_queue_with_github(self) -> None:
        """
        Synchronize queue state with GitHub column positions.

        This is the key method that makes GitHub the source of truth:
        - Removes issues that are no longer in the trigger column
        - Updates positions for issues still in the column
        - Re-calculates status based on GitHub state
        - Adds newly discovered issues from GitHub

        Call this before selecting the next issue to execute.

        Note: GitHub API call is performed OUTSIDE the lock to minimize lock hold time.
        Only file I/O operations are performed within the lock.
        """
        # STEP 1: Fetch data from GitHub OUTSIDE the lock (network I/O)
        trigger_column = self._get_pipeline_trigger_column()
        issues_in_column = self.get_issues_in_column_order(trigger_column)
        github_issue_numbers = {item['issue_number'] for item in issues_in_column}

        if not issues_in_column:
            logger.debug(f"No issues in trigger column '{trigger_column}', queue sync skipped")
            return

        # STEP 2: Hold lock ONLY for file read-modify-write
        with self._queue_lock():
            queue = self.load_queue()
            updated_queue = []

            for issue in queue:
                issue_number = issue['issue_number']

                # If issue is no longer in trigger column, remove it from queue
                # (unless it's currently active - let the agent finish)
                if issue_number not in github_issue_numbers:
                    if issue['status'] == 'active':
                        # Keep active issues even if moved out - they need to complete
                        logger.info(
                            f"Issue #{issue_number} moved out of trigger column but still active, "
                            f"keeping in queue until completion"
                        )
                        updated_queue.append(issue)
                    else:
                        logger.info(
                            f"Issue #{issue_number} no longer in trigger column '{trigger_column}', "
                            f"removing from queue"
                        )
                        # Don't add to updated_queue (effectively removing it)
                    continue

                # Issue is still in column - update its position
                github_position = next(
                    (item['position'] for item in issues_in_column
                     if item['issue_number'] == issue_number),
                    None
                )

                if github_position is not None:
                    old_position = issue.get('position_in_column')
                    if old_position != github_position:
                        logger.info(
                            f"Issue #{issue_number} position changed: {old_position} → {github_position}"
                        )
                        issue['position_in_column'] = github_position
                        issue['last_position_check'] = datetime.now(timezone.utc).isoformat()

                updated_queue.append(issue)

            # Add newly discovered issues from GitHub that aren't in the queue yet
            queued_issue_numbers = {issue['issue_number'] for issue in updated_queue}
            for github_item in issues_in_column:
                if github_item['issue_number'] not in queued_issue_numbers:
                    new_issue = {
                        'issue_number': github_item['issue_number'],
                        'position_in_column': github_item['position'],
                        'status': 'waiting',
                        'added_at': datetime.now(timezone.utc).isoformat(),
                        'last_position_check': datetime.now(timezone.utc).isoformat(),
                        'title': github_item.get('title', f"Issue #{github_item['issue_number']}")
                    }
                    updated_queue.append(new_issue)
                    logger.info(f"Added newly discovered issue #{github_item['issue_number']} to queue with status=waiting")

            # Save the synchronized queue
            if len(updated_queue) != len(queue):
                logger.info(
                    f"Queue sync complete: {len(queue)} → {len(updated_queue)} issues "
                    f"({len(queue) - len(updated_queue)} removed or added)"
                )

            self.save_queue(updated_queue)

    def remove_issue_from_queue(self, issue_number: int):
        """Remove issue from queue entirely"""
        with self._queue_lock():
            queue = self.load_queue()
            queue = [issue for issue in queue if issue['issue_number'] != issue_number]
            self.save_queue(queue)

            logger.info(f"Removed issue #{issue_number} from pipeline queue")

    def force_sync_with_github(self):
        """
        Force complete resynchronization with GitHub board state.

        This is a more aggressive version of sync_queue_with_github that:
        - Removes ALL issues not currently in the GitHub trigger column
        - Does NOT preserve "active" issues that have moved
        - Treats GitHub as absolute source of truth

        Use this for state recovery when queue has become corrupted or out of sync.
        Scheduled to run every 10 minutes to prevent long-term drift.
        """
        logger.info(
            f"FORCE SYNC: Completely resynchronizing {self.project_name}/{self.board_name} "
            f"queue with GitHub board state"
        )

        # STEP 1: Fetch data from GitHub OUTSIDE the lock
        trigger_column = self._get_pipeline_trigger_column()
        issues_in_column = self.get_issues_in_column_order(trigger_column)
        github_issue_numbers = {item['issue_number'] for item in issues_in_column}

        # STEP 2: Hold lock for file read-modify-write
        with self._queue_lock():
            queue = self.load_queue()
            updated_queue = []
            removed_count = 0
            updated_count = 0

            for issue in queue:
                issue_number = issue['issue_number']

                # If issue is NO LONGER in trigger column, REMOVE it (even if active)
                if issue_number not in github_issue_numbers:
                    logger.info(
                        f"FORCE SYNC: Removing issue #{issue_number} "
                        f"(not in GitHub '{trigger_column}' column, status was '{issue['status']}')"
                    )
                    removed_count += 1
                    continue  # Don't add to updated_queue

                # Issue is still in column - update its position from GitHub
                github_position = next(
                    (item['position'] for item in issues_in_column
                     if item['issue_number'] == issue_number),
                    None
                )

                if github_position is not None:
                    old_position = issue.get('position_in_column')
                    if old_position != github_position:
                        logger.info(
                            f"FORCE SYNC: Issue #{issue_number} position updated: "
                            f"{old_position} → {github_position}"
                        )
                        issue['position_in_column'] = github_position
                        issue['last_position_check'] = datetime.now(timezone.utc).isoformat()
                        updated_count += 1

                updated_queue.append(issue)

            # Add newly discovered issues from GitHub that aren't in the queue yet
            queued_issue_numbers = {issue['issue_number'] for issue in updated_queue}
            added_count = 0

            for github_item in issues_in_column:
                if github_item['issue_number'] not in queued_issue_numbers:
                    new_issue = {
                        'issue_number': github_item['issue_number'],
                        'position_in_column': github_item['position'],
                        'status': 'waiting',
                        'added_at': datetime.now(timezone.utc).isoformat(),
                        'last_position_check': datetime.now(timezone.utc).isoformat(),
                        'title': github_item.get('title', f"Issue #{github_item['issue_number']}")
                    }
                    updated_queue.append(new_issue)
                    added_count += 1
                    logger.info(
                        f"FORCE SYNC: Added new issue #{github_item['issue_number']} "
                        f"at position {github_item['position']}"
                    )

            # Save the synchronized queue
            self.save_queue(updated_queue)

            logger.info(
                f"FORCE SYNC complete for {self.project_name}/{self.board_name}: "
                f"{len(updated_queue)} issues in queue "
                f"({removed_count} removed, {added_count} added, {updated_count} updated)"
            )

    def reset_issue_to_waiting(self, issue_number: int):
        """
        Reset an issue from active back to waiting status.

        This is used to recover from stale locks when an agent was killed/crashed.

        Args:
            issue_number: Issue number to reset
        """
        with self._queue_lock():
            queue = self.load_queue()

            for issue in queue:
                if issue['issue_number'] == issue_number:
                    if issue['status'] == 'active':
                        issue['status'] = 'waiting'

                        # Clear activation timestamp
                        if 'activated_at' in issue:
                            del issue['activated_at']

                        self.save_queue(queue)
                        logger.info(
                            f"Reset issue #{issue_number} from active to waiting in queue "
                            f"(recovering from stale lock)"
                        )
                        return True
                    else:
                        logger.debug(
                            f"Issue #{issue_number} has status {issue['status']}, no reset needed"
                        )
                        return False

            logger.debug(f"Issue #{issue_number} not found in queue, no reset performed")
            return False

    def get_issue_status(self, issue_number: int) -> Optional[str]:
        """
        Get status of issue in queue.

        Uses lock to prevent reading partially written queue state.
        """
        with self._queue_lock():
            queue = self.load_queue()

            for issue in queue:
                if issue['issue_number'] == issue_number:
                    return issue['status']

            return None

    def _get_pipeline_trigger_column(self) -> str:
        """
        Get the column that triggers pipeline execution.

        For SDLC workflow, this is typically "Development".
        """
        from config.manager import config_manager

        # Get workflow template
        project_config = config_manager.get_project_config(self.project_name)

        for pipeline in project_config.pipelines:
            if pipeline.board_name == self.board_name:
                workflow_template = config_manager.get_workflow_template(
                    pipeline.workflow
                )

                # Find first column with an agent (that triggers work)
                for column in workflow_template.columns:
                    if column.agent and column.agent != 'null':
                        return column.name

        # Default fallback
        return "Development"

    def get_next_waiting_issue(self) -> Optional[Dict]:
        """
        Get the next issue that should execute based on CURRENT GitHub board order.

        This method first syncs with GitHub to ensure queue state is accurate,
        then selects the highest priority waiting issue.

        Returns:
            Issue dict with 'issue_number', 'position', etc., or None

        Note: Uses atomic read under lock after sync to prevent race conditions.
        """
        # STEP 1: Sync with GitHub first to ensure accurate state (has its own locking)
        logger.info(
            f"Syncing queue with GitHub for {self.project_name}/{self.board_name}"
        )
        self.sync_queue_with_github()

        # STEP 2: Atomically read queue under lock to prevent torn reads
        with self._queue_lock():
            queue = self.load_queue()

            # Filter to 'waiting' issues only
            waiting_issues = [
                issue for issue in queue
                if issue['status'] == 'waiting'
            ]

            if not waiting_issues:
                logger.debug(
                    f"No waiting issues in pipeline queue for "
                    f"{self.project_name}/{self.board_name}"
                )
                return None

            # Sort by position (lowest = topmost on board = highest priority)
            waiting_issues.sort(key=lambda x: x.get('position_in_column', 999))
            next_issue = waiting_issues[0]

        trigger_column = self._get_pipeline_trigger_column()
        logger.info(
            f"Next issue to execute: #{next_issue['issue_number']} "
            f"(position {next_issue['position_in_column']} in '{trigger_column}')"
        )

        return next_issue

    def get_queue_summary(self) -> Dict[str, Any]:
        """
        Get summary of current queue state.

        Returns:
            Dictionary with queue statistics and waiting issues

        Uses lock to prevent reading partially written queue state.
        """
        with self._queue_lock():
            queue = self.load_queue()

            active_issues = [i for i in queue if i['status'] == 'active']
            waiting_issues = [i for i in queue if i['status'] == 'waiting']

            return {
                'project': self.project_name,
                'board': self.board_name,
                'total_issues': len(queue),
                'active_count': len(active_issues),
                'waiting_count': len(waiting_issues),
                'active_issue': active_issues[0] if active_issues else None,
                'waiting_issues': waiting_issues,
                'last_updated': datetime.now(timezone.utc).isoformat()
            }

    def get_blocked_issues(self) -> List[Dict]:
        """
        Get issues that are blocking the pipeline.

        An issue is "blocked" if:
        - It has status='active' (holds the lock)
        - Its last execution outcome is 'failure'
        - No container is currently running for it

        This indicates the agent failed and the pipeline is stuck waiting
        for manual intervention.

        Returns:
            List of blocked issue dicts with failure details:
            [
                {
                    'issue_number': 123,
                    'position': 0,
                    'failed_agent': 'senior_software_engineer',
                    'error': 'Agent execution interrupted...',
                    'failed_at': '2025-01-24T10:30:00Z',
                    'column': 'Development'
                },
                ...
            ]
        """
        from services.work_execution_state import work_execution_tracker
        import subprocess

        with self._queue_lock():
            queue = self.load_queue()
            blocked_issues = []

            for issue in queue:
                if issue['status'] != 'active':
                    continue

                issue_number = issue['issue_number']

                # Check if last execution failed
                state = work_execution_tracker.load_state(
                    self.project_name, issue_number
                )

                if not state or not state.get('execution_history'):
                    continue

                last_exec = state['execution_history'][-1]

                if last_exec.get('outcome') == 'failure':
                    # Verify no container is running
                    try:
                        result = subprocess.run(
                            ['docker', 'ps', '--filter',
                             f'name=claude-agent-{self.project_name}-',
                             '--format', '{{.Names}}'],
                            capture_output=True, text=True, timeout=5
                        )

                        has_container = bool(result.stdout.strip())

                        if not has_container:
                            blocked_issues.append({
                                'issue_number': issue_number,
                                'position': issue.get('position_in_column', 0),
                                'failed_agent': last_exec.get('agent'),
                                'error': last_exec.get('error'),
                                'failed_at': last_exec.get('timestamp'),
                                'column': last_exec.get('column')
                            })
                    except Exception as e:
                        logger.error(
                            f"Failed to check container for issue #{issue_number}: {e}"
                        )
                        # Include as blocked if we can't verify container status
                        blocked_issues.append({
                            'issue_number': issue_number,
                            'position': issue.get('position_in_column', 0),
                            'failed_agent': last_exec.get('agent'),
                            'error': last_exec.get('error'),
                            'failed_at': last_exec.get('timestamp'),
                            'column': last_exec.get('column'),
                            'container_check_failed': True
                        })

            return blocked_issues


def get_pipeline_queue_manager(project_name: str, board_name: str) -> PipelineQueueManager:
    """Get pipeline queue manager instance"""
    return PipelineQueueManager(project_name, board_name)

"""
Pipeline Queue Manager

Manages the queue of issues waiting to execute in a pipeline.
Uses GitHub board column order as the source of truth for execution order.

Only ONE issue can execute at a time per pipeline (enforced by PipelineLockManager).
Other issues wait in queue based on their position in the GitHub board column.
"""

import yaml
import logging
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
        """Save queue state to YAML"""
        state_file = self._get_state_file()

        try:
            data = {
                'project': self.project_name,
                'board': self.board_name,
                'queue': queue,
                'last_updated': datetime.now(timezone.utc).isoformat()
            }

            with open(state_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=False)

            logger.debug(f"Saved queue state: {len(queue)} issues")
        except Exception as e:
            logger.error(f"Failed to save queue state: {e}")

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
        """Check if issue is already in queue"""
        queue = self.load_queue()
        return any(issue['issue_number'] == issue_number for issue in queue)

    def enqueue_issue(self, issue_number: int, column: str, timestamp: str):
        """
        Add issue to pipeline queue.

        Args:
            issue_number: Issue number to add
            column: Column the issue is in
            timestamp: Timestamp when queued
        """
        queue = self.load_queue()

        # Don't add if already in queue
        if self.is_issue_in_queue(issue_number):
            logger.debug(f"Issue #{issue_number} already in queue")
            return

        # Fetch current position from GitHub
        issues_in_column = self.get_issues_in_column_order(column)
        position = next(
            (item['position'] for item in issues_in_column
             if item['issue_number'] == issue_number),
            999  # Default if not found
        )

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
        queue = self.load_queue()

        for issue in queue:
            if issue['issue_number'] == issue_number:
                issue['status'] = 'active'
                issue['activated_at'] = datetime.now(timezone.utc).isoformat()
                break

        self.save_queue(queue)

        logger.info(f"Marked issue #{issue_number} as active in pipeline queue")

    def mark_issue_completed(self, issue_number: int):
        """Mark issue as completed (reached exit column)"""
        queue = self.load_queue()

        for issue in queue:
            if issue['issue_number'] == issue_number:
                issue['status'] = 'completed'
                issue['completed_at'] = datetime.now(timezone.utc).isoformat()
                break

        self.save_queue(queue)

        logger.info(f"Marked issue #{issue_number} as completed in pipeline queue")

    def remove_issue_from_queue(self, issue_number: int):
        """Remove issue from queue entirely"""
        queue = self.load_queue()
        queue = [issue for issue in queue if issue['issue_number'] != issue_number]
        self.save_queue(queue)

        logger.info(f"Removed issue #{issue_number} from pipeline queue")

    def get_issue_status(self, issue_number: int) -> Optional[str]:
        """Get status of issue in queue"""
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

        This fetches fresh ordering from GitHub before selecting the next issue,
        ensuring we respect any re-ordering the user has done.

        Returns:
            Issue dict with 'issue_number', 'position', etc., or None
        """
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

        # CRITICAL: Fetch current board order from GitHub
        # This is the source of truth for execution order
        logger.info(
            f"Fetching current board order from GitHub for "
            f"{self.project_name}/{self.board_name}"
        )

        trigger_column = self._get_pipeline_trigger_column()
        current_board_order = self.get_issues_in_column_order(trigger_column)

        if not current_board_order:
            logger.warning(
                f"Could not fetch board order from GitHub, "
                f"falling back to cached positions"
            )
            # Fallback: use cached positions
            waiting_issues.sort(key=lambda x: x.get('position_in_column', 999))
            return waiting_issues[0]

        # Update cached positions from GitHub
        board_positions = {
            item['issue_number']: item['position']
            for item in current_board_order
        }

        for issue in waiting_issues:
            if issue['issue_number'] in board_positions:
                old_position = issue.get('position_in_column')
                new_position = board_positions[issue['issue_number']]

                if old_position != new_position:
                    logger.info(
                        f"Issue #{issue['issue_number']} position changed: "
                        f"{old_position} → {new_position}"
                    )
                    issue['position_in_column'] = new_position
                    issue['last_position_check'] = datetime.now(timezone.utc).isoformat()

        # Save updated positions
        self.save_queue(queue)

        # Sort waiting issues by their current GitHub board position
        waiting_issues_with_positions = [
            issue for issue in waiting_issues
            if issue['issue_number'] in board_positions
        ]

        if not waiting_issues_with_positions:
            logger.warning(
                f"Waiting issues not found in GitHub board column: "
                f"{[issue['issue_number'] for issue in waiting_issues]}"
            )
            # Clean up queue - remove issues not found on board
            for issue in waiting_issues:
                if issue['issue_number'] not in board_positions:
                    logger.info(
                        f"Removing issue #{issue['issue_number']} from queue "
                        f"(no longer in trigger column)"
                    )
                    self.remove_issue_from_queue(issue['issue_number'])
            return None

        # Sort by position (lowest = topmost on board = highest priority)
        waiting_issues_with_positions.sort(
            key=lambda x: x['position_in_column']
        )

        next_issue = waiting_issues_with_positions[0]

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
        """
        queue = self.load_queue()

        active_issues = [i for i in queue if i['status'] == 'active']
        waiting_issues = [i for i in queue if i['status'] == 'waiting']
        completed_issues = [i for i in queue if i['status'] == 'completed']

        return {
            'project': self.project_name,
            'board': self.board_name,
            'total_issues': len(queue),
            'active_count': len(active_issues),
            'waiting_count': len(waiting_issues),
            'completed_count': len(completed_issues),
            'active_issue': active_issues[0] if active_issues else None,
            'waiting_issues': waiting_issues,
            'last_updated': datetime.now(timezone.utc).isoformat()
        }


def get_pipeline_queue_manager(project_name: str, board_name: str) -> PipelineQueueManager:
    """Get pipeline queue manager instance"""
    return PipelineQueueManager(project_name, board_name)

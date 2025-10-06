#!/usr/bin/env python3
"""
Pipeline Progression Service

Handles automatic progression of issues through pipeline stages:
- Moves issues to next column in GitHub Projects v2
- Triggers next agent in the pipeline
"""

import subprocess
import json
import logging
from typing import Optional, Dict, Any
from config.manager import config_manager
from config.state_manager import state_manager
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from datetime import datetime
import time

logger = logging.getLogger(__name__)


class PipelineProgression:
    """Manage automatic progression through pipeline stages"""

    def __init__(self, task_queue: TaskQueue):
        self.task_queue = task_queue

    def get_next_column(self, project_name: str, board_name: str, current_column: str) -> Optional[str]:
        """Get the next column in the workflow"""
        try:
            project_config = config_manager.get_project_config(project_name)

            # Find the pipeline config
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                logger.error(f"No pipeline config found for board {board_name}")
                return None

            # Get workflow template
            workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)

            # Find current column index
            current_index = None
            for i, column in enumerate(workflow_template.columns):
                if column.name == current_column:
                    current_index = i
                    break

            if current_index is None:
                logger.error(f"Current column '{current_column}' not found in workflow")
                return None

            # Get next column (skip columns without agents)
            for i in range(current_index + 1, len(workflow_template.columns)):
                next_col = workflow_template.columns[i]
                if next_col.agent and next_col.agent != 'null':
                    return next_col.name

            # No more columns with agents
            logger.info(f"No next column found after '{current_column}' - pipeline complete")
            return None

        except Exception as e:
            logger.error(f"Error getting next column: {e}")
            return None

    def move_issue_to_column(self, project_name: str, board_name: str, issue_number: int,
                            target_column: str) -> bool:
        """Move an issue to a specific column in GitHub Projects v2"""
        try:
            project_config = config_manager.get_project_config(project_name)
            project_state = state_manager.load_project_state(project_name)

            if not project_state:
                logger.error(f"No project state found for {project_name}")
                return False

            board_state = project_state.boards.get(board_name)
            if not board_state:
                logger.error(f"No board state found for {board_name}")
                return False

            # Get the field ID for Status from board state
            status_field_id = board_state.status_field_id
            if not status_field_id:
                logger.error(f"No status_field_id found in board state for {board_name}")
                logger.error("Board may need to be re-reconciled to capture the status field ID")
                return False

            # Get the option ID for the target column from the columns list
            column_option_id = None
            for column in board_state.columns:
                if column.name == target_column:
                    column_option_id = column.id
                    break

            if not column_option_id:
                logger.error(f"Column '{target_column}' not found in board {board_name}")
                logger.error(f"Available columns: {[c.name for c in board_state.columns]}")
                return False

            # First, get the project item ID for this issue
            github_org = project_config.github['org']
            github_repo = project_config.github['repo']

            # Query to find the item ID
            query = f'''{{
                repository(owner: "{github_org}", name: "{github_repo}") {{
                    issue(number: {issue_number}) {{
                        projectItems(first: 10) {{
                            nodes {{
                                id
                                project {{
                                    number
                                }}
                            }}
                        }}
                    }}
                }}
            }}'''

            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True, text=True, check=True
            )

            data = json.loads(result.stdout)
            project_items = data['data']['repository']['issue']['projectItems']['nodes']

            # Find the item for our project
            item_id = None
            for item in project_items:
                if item['project']['number'] == board_state.project_number:
                    item_id = item['id']
                    break

            if not item_id:
                logger.error(f"Issue #{issue_number} not found in project")
                return False

            # Update the item's status field
            mutation = f'''
                mutation {{
                    updateProjectV2ItemFieldValue(
                        input: {{
                            projectId: "{board_state.project_id}"
                            itemId: "{item_id}"
                            fieldId: "{status_field_id}"
                            value: {{
                                singleSelectOptionId: "{column_option_id}"
                            }}
                        }}
                    ) {{
                        projectV2Item {{
                            id
                        }}
                    }}
                }}
            '''

            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={mutation}'],
                capture_output=True, text=True, check=True
            )

            # Record status change with 'auto' trigger (from pipeline progression)
            from services.work_execution_state import work_execution_tracker
            # Note: We don't have the old status here, but the tracker will handle it
            work_execution_tracker.record_status_change(
                issue_number=issue_number,
                from_status=None,  # Could track this if needed
                to_status=target_column,
                trigger='auto',  # Automatic progression
                project_name=project_name
            )

            logger.info(f"Moved issue #{issue_number} to column '{target_column}' in {board_name}")
            return True

        except Exception as e:
            logger.error(f"Error moving issue to column: {e}")
            return False

    def progress_to_next_stage(self, project_name: str, board_name: str, issue_number: int,
                               current_column: str, repository: str, issue_data: Dict[str, Any]) -> bool:
        """
        Progress an issue to the next stage in the pipeline

        Returns True if progression was successful, False otherwise
        """
        try:
            # Get next column
            next_column = self.get_next_column(project_name, board_name, current_column)

            if not next_column:
                logger.info(f"No next stage for issue #{issue_number} - pipeline complete")
                return False

            # Move issue to next column
            if not self.move_issue_to_column(project_name, board_name, issue_number, next_column):
                logger.error(f"Failed to move issue #{issue_number} to '{next_column}'")
                return False

            # Get the agent for the next column
            project_config = config_manager.get_project_config(project_name)
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
            next_agent = None
            for column in workflow_template.columns:
                if column.name == next_column:
                    next_agent = column.agent
                    break

            if not next_agent or next_agent == 'null':
                logger.info(f"No agent assigned to column '{next_column}'")
                return True  # Successfully moved, but no agent to trigger

            # Create task for next agent
            task_context = {
                'project': project_name,
                'board': board_name,
                'pipeline': pipeline_config.name,
                'repository': repository,
                'issue_number': issue_number,
                'issue': issue_data,
                'column': next_column,
                'trigger': 'pipeline_progression',
                'timestamp': datetime.now().isoformat()
            }

            task = Task(
                id=f"{next_agent}_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                agent=next_agent,
                project=project_name,
                priority=TaskPriority.MEDIUM,
                context=task_context,
                created_at=datetime.now().isoformat()
            )

            self.task_queue.enqueue(task)

            # Record execution start with 'pipeline_progression' trigger
            from services.work_execution_state import work_execution_tracker
            work_execution_tracker.record_execution_start(
                issue_number=issue_number,
                column=next_column,
                agent=next_agent,
                trigger_source='pipeline_progression',
                project_name=project_name
            )

            logger.info(f"Queued {next_agent} for issue #{issue_number} in column '{next_column}'")
            return True

        except Exception as e:
            logger.error(f"Error progressing to next stage: {e}")
            return False
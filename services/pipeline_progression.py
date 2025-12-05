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
from services.pipeline_lock_manager import get_pipeline_lock_manager
from services.pipeline_queue_manager import get_pipeline_queue_manager
from services.pipeline_run import get_pipeline_run_manager

logger = logging.getLogger(__name__)


class PipelineProgression:
    """Manage automatic progression through pipeline stages"""

    def __init__(self, task_queue: TaskQueue):
        self.task_queue = task_queue
        
        # Initialize decision observability
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)

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
                            target_column: str, trigger: str = 'manual') -> bool:
        """
        Move an issue to a specific column in GitHub Projects v2
        
        Args:
            project_name: Project name
            board_name: Board/pipeline name
            issue_number: Issue number to move
            target_column: Target column name
            trigger: What triggered the move ('manual', 'auto', 'review_cycle', 'agent_completion')
        
        Returns:
            True if successful, False otherwise
        """
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
            
            # Determine current status (for decision event)
            current_status = None
            try:
                # Try to get current status from GitHub
                github_org = project_config.github['org']
                github_repo = project_config.github['repo']
                
                query = f'''{{
                    repository(owner: "{github_org}", name: "{github_repo}") {{
                        issue(number: {issue_number}) {{
                            projectItems(first: 10) {{
                                nodes {{
                                    id
                                    project {{
                                        number
                                    }}
                                    fieldValueByName(name: "Status") {{
                                        ... on ProjectV2ItemFieldSingleSelectValue {{
                                            name
                                        }}
                                    }}
                                }}
                            }}
                        }}
                    }}
                }}'''
                
                result = subprocess.run(
                    ['gh', 'api', 'graphql', '-f', f'query={query}'],
                    capture_output=True, text=True, check=True, timeout=30
                )
                data = json.loads(result.stdout)

                # Safely access nested dictionary structure
                if data and data.get('data'):
                    repo_data = data['data'].get('repository')
                    if repo_data:
                        issue_data = repo_data.get('issue')
                        if issue_data:
                            project_items_data = issue_data.get('projectItems')
                            if project_items_data:
                                project_items = project_items_data.get('nodes', [])

                                for item in project_items:
                                    if item.get('project', {}).get('number') == board_state.project_number:
                                        field_value = item.get('fieldValueByName')
                                        if field_value:
                                            current_status = field_value.get('name')
                                        break
            except Exception as e:
                logger.debug(f"Could not determine current status: {e}")
            
            # EMIT DECISION EVENT: Status progression started
            self.decision_events.emit_status_progression(
                issue_number=issue_number,
                project=project_name,
                board=board_name,
                from_status=current_status or 'unknown',
                to_status=target_column,
                trigger=trigger,
                success=None  # Not yet executed
            )

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
                capture_output=True, text=True, check=True, timeout=30
            )

            data = json.loads(result.stdout)

            # Safely access nested dictionary structure
            project_items = []
            issue_exists = False
            if data and data.get('data'):
                repo_data = data['data'].get('repository')
                if repo_data:
                    issue_data = repo_data.get('issue')
                    if issue_data:
                        issue_exists = True
                        project_items_data = issue_data.get('projectItems')
                        if project_items_data:
                            project_items = project_items_data.get('nodes', [])

            # Check if issue exists in repository
            if not issue_exists:
                logger.error(f"Issue #{issue_number} does not exist in repository {github_org}/{github_repo}")
                logger.error(f"The issue may have been deleted, or the task has stale data")
                return False

            # Find the item for our project
            item_id = None
            for item in project_items:
                if item.get('project', {}).get('number') == board_state.project_number:
                    item_id = item.get('id')
                    break

            if not item_id:
                logger.error(f"Issue #{issue_number} exists but is not in project '{board_name}' (project #{board_state.project_number})")
                if project_items:
                    other_projects = [item.get('project', {}).get('number') for item in project_items]
                    logger.error(f"Issue is in projects: {other_projects}")
                else:
                    logger.error(f"Issue #{issue_number} is not in any projects")
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

            # Retry logic for mutation
            max_retries = 3
            for attempt in range(max_retries):
                try:
                    result = subprocess.run(
                        ['gh', 'api', 'graphql', '-f', f'query={mutation}'],
                        capture_output=True, text=True, check=True, timeout=30
                    )
                    break # Success
                except subprocess.CalledProcessError as e:
                    if attempt < max_retries - 1:
                        logger.warning(f"Failed to move issue (attempt {attempt+1}/{max_retries}): {e.stderr}. Retrying...")
                        time.sleep(2 * (attempt + 1)) # Exponential backoff
                    else:
                        raise # Re-raise on last attempt

            # Record status change with trigger (from pipeline progression)
            from services.work_execution_state import work_execution_tracker
            work_execution_tracker.record_status_change(
                issue_number=issue_number,
                from_status=current_status,
                to_status=target_column,
                trigger=trigger,
                project_name=project_name
            )
            
            # EMIT DECISION EVENT: Status progression completed
            self.decision_events.emit_status_progression(
                issue_number=issue_number,
                project=project_name,
                board=board_name,
                from_status=current_status or 'unknown',
                to_status=target_column,
                trigger=trigger,
                success=True
            )

            logger.info(f"Moved issue #{issue_number} to column '{target_column}' in {board_name}")
            return True

        except Exception as e:
            logger.error(f"Error moving issue to column: {e}")
            
            # EMIT DECISION EVENT: Status progression failed
            self.decision_events.emit_status_progression(
                issue_number=issue_number,
                project=project_name,
                board=board_name,
                from_status=current_status or 'unknown',
                to_status=target_column,
                trigger=trigger,
                success=False,
                error=str(e)
            )
            
            return False

    def _get_issue_details(self, repository: str, issue_number: int, org: str) -> Dict[str, Any]:
        """Fetch full issue details from GitHub"""
        try:
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'title,body,labels,state,author,createdAt,updatedAt,url'],
                capture_output=True, text=True, check=True
            )
            return json.loads(result.stdout)
        except Exception as e:
            logger.error(f"Error fetching issue #{issue_number} details: {e}")
            return {'title': f'Issue #{issue_number}', 'body': '', 'labels': []}

    def _release_lock_and_process_next(self, project_name: str, board_name: str, issue_number: int, 
                                      exit_column: str, repository: str):
        """Release pipeline lock and process next waiting issue"""
        try:
            lock_manager = get_pipeline_lock_manager()
            pipeline_queue = get_pipeline_queue_manager(project_name, board_name)
            pipeline_run_manager = get_pipeline_run_manager()

            # Release lock
            lock_manager.release_lock(project_name, board_name, issue_number)
            logger.info(f"Released pipeline lock for {project_name}/{board_name} (issue #{issue_number} reached '{exit_column}')")
            
            # Remove from queue
            if pipeline_queue.is_issue_in_queue(issue_number):
                pipeline_queue.remove_issue_from_queue(issue_number)
                
            # End pipeline run
            pipeline_run_manager.end_pipeline_run(
                project=project_name,
                issue_number=issue_number,
                reason=f"Issue reached exit column '{exit_column}'"
            )
            
            # Process next waiting issue
            next_issue = pipeline_queue.get_next_waiting_issue()
            if next_issue:
                logger.info(f"Processing next queued issue #{next_issue['issue_number']} for {project_name}/{board_name}")
                
                # Acquire lock
                acquired, reason = lock_manager.try_acquire_lock(
                    project=project_name,
                    board=board_name,
                    issue_number=next_issue['issue_number']
                )
                
                if acquired:
                    pipeline_queue.mark_issue_active(next_issue['issue_number'])
                    
                    # Get current column for next issue
                    current_column = next_issue.get('column')
                    
                    # Get agent for this column
                    project_config = config_manager.get_project_config(project_name)
                    pipeline_config = next(p for p in project_config.pipelines if p.board_name == board_name)
                    workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)
                    
                    agent = None
                    for col in workflow_template.columns:
                        if col.name == current_column:
                            agent = col.agent
                            break
                            
                    if agent and agent != 'null':
                        # Fetch issue details
                        issue_data = self._get_issue_details(repository, next_issue['issue_number'], project_config.github['org'])
                        
                        # Create task
                        task_context = {
                            'project': project_name,
                            'board': board_name,
                            'pipeline': pipeline_config.name,
                            'repository': repository,
                            'issue_number': next_issue['issue_number'],
                            'issue': issue_data,
                            'column': current_column,
                            'trigger': 'pipeline_progression', # Triggered by previous issue exiting
                            'timestamp': datetime.now().isoformat()
                        }

                        task = Task(
                            id=f"{agent}_{project_name}_{board_name}_{next_issue['issue_number']}_{int(time.time())}",
                            agent=agent,
                            project=project_name,
                            priority=TaskPriority.MEDIUM,
                            context=task_context,
                            created_at=datetime.now().isoformat()
                        )

                        self.task_queue.enqueue(task)
                        
                        # Record execution start
                        from services.work_execution_state import work_execution_tracker
                        work_execution_tracker.record_execution_start(
                            issue_number=next_issue['issue_number'],
                            column=current_column,
                            agent=agent,
                            trigger_source='pipeline_progression',
                            project_name=project_name
                        )
                        
                        logger.info(f"Triggered agent {agent} for next waiting issue #{next_issue['issue_number']}")
                    else:
                        logger.warning(f"Next issue #{next_issue['issue_number']} is in column '{current_column}' which has no agent")
                else:
                    logger.error(f"Failed to acquire lock for next issue #{next_issue['issue_number']}: {reason}")
            else:
                logger.info(f"No waiting issues in pipeline queue for {project_name}/{board_name}")
                
        except Exception as e:
            logger.error(f"Error releasing lock and processing next: {e}")
            import traceback
            logger.error(traceback.format_exc())

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

            # Move issue to next column (decision events emitted inside move_issue_to_column)
            moved = self.move_issue_to_column(
                project_name, 
                board_name, 
                issue_number, 
                next_column,
                trigger='pipeline_progression'
            )
            
            if not moved:
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
            
            # Check if this is an exit column
            is_exit_column = False
            if hasattr(workflow_template, 'pipeline_exit_columns') and workflow_template.pipeline_exit_columns:
                is_exit_column = next_column in workflow_template.pipeline_exit_columns

            if is_exit_column:
                logger.info(f"Issue #{issue_number} moved to exit column '{next_column}'. Releasing pipeline lock.")
                self._release_lock_and_process_next(project_name, board_name, issue_number, next_column, repository)
                return True

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
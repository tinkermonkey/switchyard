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
import uuid
from typing import Optional, Dict, Any
from config.manager import config_manager
from config.state_manager import state_manager
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from datetime import datetime
import time
from services.pipeline_lock_manager import ReleaseResult, get_pipeline_lock_manager
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

            # Get pipeline_run_id for event tracking.
            # Use get_recent_pipeline_run_id (read-only) — the run may already be
            # completed but status progression events still belong to it.
            pipeline_run_id = None
            try:
                from services.pipeline_run import get_pipeline_run_manager
                pipeline_run_manager = get_pipeline_run_manager()
                pipeline_run_id = pipeline_run_manager.get_recent_pipeline_run_id(
                    project_name, issue_number
                )
            except Exception as e:
                logger.debug(f"Could not get pipeline_run_id: {e}")

            # EMIT DECISION EVENT: Status progression started
            self.decision_events.emit_status_progression(
                issue_number=issue_number,
                project=project_name,
                board=board_name,
                from_status=current_status or 'unknown',
                to_status=target_column,
                trigger=trigger,
                success=None,  # Not yet executed
                pipeline_run_id=pipeline_run_id
            )

            # Get the field ID for Status from board state
            status_field_id = board_state.status_field_id
            if not status_field_id:
                logger.error(f"No status_field_id found in board state for {board_name}")
                logger.warning(f"Attempting auto-repair: refreshing board field IDs from GitHub")

                # Auto-repair by fetching field ID from GitHub
                try:
                    # Use existing state_manager method to refresh field IDs atomically
                    refresh_success = state_manager.refresh_board_field_ids(project_name, board_name)

                    if not refresh_success:
                        logger.error("Auto-repair failed: could not refresh field IDs from GitHub")
                        logger.error("Board may need to be re-reconciled to capture the status field ID")
                        return False

                    # Reload state to get the refreshed field ID
                    refreshed_state = state_manager.load_project_state(project_name)
                    if refreshed_state:
                        refreshed_board = refreshed_state.boards.get(board_name)
                        if refreshed_board and refreshed_board.status_field_id:
                            status_field_id = refreshed_board.status_field_id
                            # Update board_state reference for use below
                            board_state = refreshed_board
                            logger.info(f"Auto-repair succeeded: status_field_id = {status_field_id}")
                        else:
                            logger.error("Auto-repair failed: status_field_id still missing after refresh")
                            return False
                    else:
                        logger.error("Auto-repair failed: could not reload state after refresh")
                        return False
                except Exception as e:
                    logger.error(f"Auto-repair failed with exception: {e}")
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
                self.decision_events.emit_status_progression(
                    issue_number=issue_number,
                    project=project_name,
                    board=board_name,
                    from_status=current_status or 'unknown',
                    to_status=target_column,
                    trigger=trigger,
                    success=False,
                    error=f"Column '{target_column}' not found in board {board_name}",
                    pipeline_run_id=pipeline_run_id
                )
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
                success=True,
                pipeline_run_id=pipeline_run_id
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
                error=str(e),
                pipeline_run_id=pipeline_run_id
            )
            
            return False

    def _get_issue_details(self, repository: str, issue_number: int, org: str) -> Dict[str, Any]:
        """Fetch full issue details from GitHub.

        Retries transient `gh` CLI failures before giving up — see
        ProjectMonitor.get_issue_details() for the confirmed production
        incident (bc70ac46) this mirrors. On exhausted retries, raises
        instead of returning a placeholder indistinguishable from a
        genuinely-empty issue.
        """
        last_error = None
        for attempt in range(3):
            try:
                result = subprocess.run(
                    ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'title,body,labels,state,author,createdAt,updatedAt,url'],
                    capture_output=True, text=True, check=True
                )
                return json.loads(result.stdout)
            except Exception as e:
                last_error = e
                if attempt < 2:
                    logger.warning(
                        f"Transient failure fetching issue #{issue_number} details "
                        f"(attempt {attempt + 1}/3): {e}; retrying"
                    )
                    time.sleep(0.5 * (attempt + 1))

        logger.error(f"Error fetching issue #{issue_number} details after 3 attempts: {last_error}")
        raise RuntimeError(
            f"Could not fetch issue #{issue_number} details from GitHub after 3 attempts: {last_error}"
        ) from last_error

    def _release_lock_on_exit_column(self, project_name: str, board_name: str, issue_number: int,
                                     exit_column: str, repository: str):
        """Release the pipeline lock and close out a run that reached an exit column.

        Deliberately does NOT dispatch the next queued issue -- see the note where
        that body used to be (#158). ProjectMonitor owns next-issue dispatch,
        because only it has the column-type routing that makes dispatch safe.

        `repository` is unused now that nothing here fetches issue details; it is
        kept so the call sites and the routed sibling,
        ProjectMonitor._release_pipeline_lock_and_process_next(), keep the same
        shape.
        """
        try:
            lock_manager = get_pipeline_lock_manager()
            pipeline_queue = get_pipeline_queue_manager(project_name, board_name)
            pipeline_run_manager = get_pipeline_run_manager()

            # release_lock() returns False both when this issue's lock is
            # genuinely retained due to a failed run AND when this issue simply
            # doesn't hold the lock at all (held_by_other) — the latter is the
            # NORMAL case for e.g. conversational issues, which never acquire
            # the lock in the first place (see the identical
            # lock_held_by_us gate in project_monitor.py's sibling,
            # _check_pr_ready_on_issue_exit). Without checking which case this
            # is first, that normal path was being misdiagnosed as "likely
            # retained" and permanently stalling the board: queue cleanup never
            # ran, and the run stayed "active" forever.
            lock = lock_manager.get_lock(project_name, board_name)
            lock_held_by_us = bool(lock and lock.locked_by_issue == issue_number)

            if lock and not lock_held_by_us:
                logger.info(
                    f"Issue #{issue_number} reached exit column '{exit_column}' without "
                    f"holding the pipeline lock for {project_name}/{board_name} "
                    f"(held by #{lock.locked_by_issue}) — skipping release, continuing "
                    f"with queue cleanup"
                )
            elif lock_held_by_us:
                # Does NOT force — if this issue's lock is actually retained due
                # to a failed run (which shouldn't normally happen for an issue
                # that legitimately reached an exit column, but defense in depth
                # matters here since this ends the run as a SUCCESS), the release
                # is correctly refused rather than silently discarding the
                # durable failure record and proceeding as if everything
                # succeeded.
                released = lock_manager.release_lock(project_name, board_name, issue_number)
                if released is ReleaseResult.SERIALIZATION_FAILED:
                    # NOT the retained-lock case, and misreporting it as one
                    # sent operators to scripts/release_lock.py looking for a
                    # durable failure record that does not exist (found in the
                    # WI-8 review round). Nothing was attempted: the release is
                    # still outstanding, so the board must not advance — but
                    # this is transient contention on the lock's own acquire
                    # guard, not a decision anyone has to make.
                    logger.error(
                        f"Could not release pipeline lock for {project_name}/{board_name} "
                        f"(issue #{issue_number} reached '{exit_column}') — the release "
                        f"could not be serialized against a concurrent acquire or liveness "
                        f"refresh, so it did not happen. NOT ending the pipeline run as "
                        f"successful. This is lock "
                        f"contention, not a retained/failed lock; the run stays 'active' "
                        f"for PipelineWatchdog.check_for_zombie_runs() to reap, or run "
                        f"scripts/release_lock.py to clear it now."
                    )
                    return
                if not released:
                    logger.error(
                        f"Could not release pipeline lock for {project_name}/{board_name} "
                        f"(issue #{issue_number} reached '{exit_column}') — it is held by "
                        f"this issue but likely retained due to a failed run. NOT ending "
                        f"the pipeline run as successful while this is unresolved. Use "
                        f"scripts/release_lock.py to "
                        f"investigate."
                    )
                    return
                logger.info(f"Released pipeline lock for {project_name}/{board_name} (issue #{issue_number} reached '{exit_column}')")

            # Remove from queue
            if pipeline_queue.is_issue_in_queue(issue_number):
                pipeline_queue.remove_issue_from_queue(issue_number)
                
            # End pipeline run
            pipeline_run_manager.end_pipeline_run(
                project=project_name,
                issue_number=issue_number,
                reason=f"Issue reached exit column '{exit_column}'",
                outcome="success"
            )
            
            # NO next-issue dispatch here, deliberately (#158).
            #
            # This exit path used to end with a full "dispatch the next queued
            # issue" body -- acquire the board lock, mark_issue_active(), resolve
            # the agent, build a Task, enqueue. It never dispatched anything in
            # production: it read the column off the queue entry, and
            # PipelineQueueManager never writes a 'column' key (enqueue_issue()
            # writes 'initial_column'; sync_queue_with_github() /
            # force_sync_with_github() write no column field at all), so the
            # agent lookup never matched and every candidate was deferred.
            # ProjectMonitor's FAILSAFE relies on the same fact -- it uses
            # `'column' in next_issue` to tell a stalled candidate from a queue
            # row.
            #
            # Deleted rather than activated because the body had NONE of
            # ProjectMonitor.trigger_agent_for_status()'s column-type routing:
            # no conversational, review, repair_cycle or pr_review handling, and
            # none of its duplicate-task / active-execution / cancellation
            # guards. On a board whose trigger column is 'conversational'
            # (Planning & Design) it would have enqueued a plain one-shot Task
            # instead of starting a feedback loop AND left the board's exclusive
            # lock held by a conversational issue -- which by design never holds
            # it -- blocking every other issue on that board. Replicating the
            # routing here would add a sixth copy of the logic #57 already flags
            # as dangerously duplicated, and this class has no ProjectMonitor
            # handle to route through instead.
            #
            # Nothing is stranded by not dispatching. The deferral's rollback
            # ("release the lock, reset the entry to 'waiting'") is not just
            # preserved but made unnecessary: the candidate is never touched, so
            # it is already in the "waiting entry + unlocked board" state the
            # rollback existed to restore. That is ProjectMonitor's FAILSAFE
            # SCENARIO 2, which dispatches through trigger_agent_for_status()
            # WITH routing -- as does the monitor's own exit-column handler,
            # _release_pipeline_lock_and_process_next(), on its next poll.
            #
            # Not acquiring is also strictly better than acquiring-and-unwinding:
            # the transient lock this site took for a candidate it was never
            # going to dispatch could make a concurrent FAILSAFE pass see the
            # board as busy and skip it for a whole poll interval, and the
            # get_next_n_waiting_issues() call that drove it resynced the queue
            # against GitHub on every exit-column progression for no benefit.

        except Exception as e:
            logger.error(f"Error releasing lock on exit column: {e}")
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
                self._release_lock_on_exit_column(project_name, board_name, issue_number, next_column, repository)
                return True

            next_agent = None
            for column in workflow_template.columns:
                if column.name == next_column:
                    next_agent = column.agent
                    break

            if not next_agent or next_agent == 'null':
                logger.info(f"No agent assigned to column '{next_column}'")
                return True  # Successfully moved, but no agent to trigger

            # Get or create pipeline run for this issue
            from services.pipeline_run import get_pipeline_run_manager
            pipeline_run_manager = get_pipeline_run_manager()
            pipeline_run_id = pipeline_run_manager.ensure_pipeline_run_for_task(
                project=project_name,
                board=board_name,
                issue_number=issue_number,
                issue_data=issue_data  # Already fetched earlier
            )

            if not pipeline_run_id:
                logger.warning(
                    f"Failed to create/retrieve pipeline run for issue #{issue_number}, "
                    f"continuing without run ID"
                )
                pipeline_run_id = None  # Continue anyway

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
                'pipeline_run_id': pipeline_run_id,  # ADD THIS
                'timestamp': datetime.now().isoformat()
            }

            task = Task(
                id=str(uuid.uuid4()),
                agent=next_agent,
                project=project_name,
                priority=TaskPriority.MEDIUM,
                context=task_context,
                created_at=datetime.now().isoformat()
            )

            # Record execution start with 'pipeline_progression' trigger FIRST
            # CRITICAL: Must happen before enqueue to prevent race condition
            from services.work_execution_state import work_execution_tracker
            work_execution_tracker.record_execution_start(
                issue_number=issue_number,
                column=next_column,
                agent=next_agent,
                trigger_source='pipeline_progression',
                project_name=project_name,
                board_name=board_name
            )

            # Enqueue task LAST so workers find in_progress state
            self.task_queue.enqueue(task)

            logger.info(f"Queued {next_agent} for issue #{issue_number} in column '{next_column}'")
            return True

        except Exception as e:
            logger.error(f"Error progressing to next stage: {e}")
            return False
"""
Work Execution State Tracker

Tracks execution history, outcomes, and status changes to enable:
- Intelligent work restart on status changes
- Prevention of double-triggering on auto-progression
- Retry on failure
- Complete audit trail
"""

import yaml
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class ExecutionRecord:
    """Record of a single work execution attempt"""
    column: str
    agent: str
    timestamp: str
    outcome: str  # 'success', 'failure', 'blocked', 'in_progress'
    trigger_source: str  # 'manual_move', 'pipeline_progression', 'webhook'
    error: Optional[str] = None


@dataclass
class StatusChange:
    """Record of a status change"""
    from_status: Optional[str]
    to_status: str
    timestamp: str
    trigger: str  # 'manual', 'auto'


class WorkExecutionStateTracker:
    """Tracks work execution state and determines when to execute/skip work"""

    def __init__(self, state_dir: Path = None):
        """Initialize work execution state tracker"""
        if state_dir is None:
            # CRITICAL: Use absolute path to orchestrator's state directory
            # This prevents state from being created inside project directories when
            # agents execute with project working directory
            import os
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "execution_history"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"WorkExecutionStateTracker initialized with state_dir: {state_dir}")

    def get_state_file(self, project_name: str, issue_number: int) -> Path:
        """Get the state file path for an issue"""
        return self.state_dir / f"{project_name}_issue_{issue_number}.yaml"

    def load_state(self, project_name: str, issue_number: int) -> Dict:
        """Load execution state for an issue"""
        state_file = self.get_state_file(project_name, issue_number)

        if not state_file.exists():
            return {
                'issue_number': issue_number,
                'project_name': project_name,
                'execution_history': [],
                'status_changes': [],
                'current_status': None,
                'last_updated': None
            }

        try:
            from utils.file_lock import file_lock

            # Use file lock when reading to prevent reading partial writes
            lock_file = state_file.with_suffix(state_file.suffix + '.lock')
            with file_lock(lock_file):
                if state_file.exists():  # Check again inside lock
                    with open(state_file, 'r') as f:
                        state = yaml.safe_load(f)
                        # Ensure all expected keys exist
                        state.setdefault('execution_history', [])
                        state.setdefault('status_changes', [])
                        return state
            # File doesn't exist inside lock, return default
            return {
                'issue_number': issue_number,
                'project_name': project_name,
                'execution_history': [],
                'status_changes': [],
                'current_status': None,
                'last_updated': None
            }
        except Exception as e:
            logger.error(f"Failed to load state for {project_name}/#{issue_number}: {e}")
            return {
                'issue_number': issue_number,
                'project_name': project_name,
                'execution_history': [],
                'status_changes': [],
                'current_status': None,
                'last_updated': None
            }

    def save_state(self, project_name: str, issue_number: int, state: Dict):
        """Save execution state for an issue with thread-safe file locking"""
        from utils.file_lock import safe_yaml_write

        state_file = self.get_state_file(project_name, issue_number)

        try:
            state['last_updated'] = datetime.now(timezone.utc).isoformat()
            with safe_yaml_write(state_file):
                with open(state_file, 'w') as f:
                    yaml.dump(state, f, default_flow_style=False, sort_keys=False)

            logger.debug(f"Saved execution state for {project_name}/#{issue_number}")
        except Exception as e:
            logger.error(f"Failed to save state for {project_name}/#{issue_number}: {e}")

    def record_execution_start(
        self,
        issue_number: int,
        column: str,
        agent: str,
        trigger_source: str,
        project_name: str
    ):
        """
        Record the start of work execution.

        CRITICAL: This MUST be called BEFORE enqueuing the task to prevent
        race conditions where the task completes before the in_progress
        state is recorded.

        Correct order:
        1. record_execution_start()  <- Creates in_progress state
        2. task_queue.enqueue()      <- Worker can now find in_progress state

        Args:
            issue_number: Issue number for the execution
            column: Workflow column/status
            agent: Agent name
            trigger_source: Source of the trigger (e.g., 'manual', 'pipeline_progression')
            project_name: Project name
        """
        state = self.load_state(project_name, issue_number)

        execution = {
            'column': column,
            'agent': agent,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'outcome': 'in_progress',
            'trigger_source': trigger_source
        }

        state['execution_history'].append(execution)
        state['current_status'] = column

        self.save_state(project_name, issue_number, state)

        logger.info(
            f"Recorded execution start: {project_name}/#{issue_number} "
            f"{agent} in {column} (trigger: {trigger_source})"
        )

    def record_execution_outcome(
        self,
        issue_number: int,
        column: str,
        agent: str,
        outcome: str,
        project_name: str,
        error: Optional[str] = None
    ):
        """Record the outcome of work execution"""
        state = self.load_state(project_name, issue_number)

        # Find the most recent in_progress execution for this agent/column
        for execution in reversed(state['execution_history']):
            if (execution['column'] == column and
                execution['agent'] == agent and
                execution['outcome'] == 'in_progress'):

                execution['outcome'] = outcome
                if error:
                    execution['error'] = error

                self.save_state(project_name, issue_number, state)

                logger.info(
                    f"Recorded execution outcome: {project_name}/#{issue_number} "
                    f"{agent} in {column} → {outcome}"
                )
                return

        # If we get here, no in_progress execution was found
        # Create a new record (edge case where start wasn't recorded)
        logger.error(
            f"No in_progress execution found for {agent} in {column}, "
            f"creating new record with outcome {outcome}. "
            f"This should only happen after orchestrator restart/crash."
        )

        execution = {
            'column': column,
            'agent': agent,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'outcome': outcome,
            'trigger_source': 'unknown'
        }

        if error:
            execution['error'] = error

        state['execution_history'].append(execution)
        self.save_state(project_name, issue_number, state)

    def record_status_change(
        self,
        issue_number: int,
        from_status: Optional[str],
        to_status: str,
        trigger: str,
        project_name: str
    ):
        """Record a status change"""
        state = self.load_state(project_name, issue_number)

        status_change = {
            'from_status': from_status,
            'to_status': to_status,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'trigger': trigger
        }

        state['status_changes'].append(status_change)
        state['current_status'] = to_status

        self.save_state(project_name, issue_number, state)

        logger.info(
            f"Recorded status change: {project_name}/#{issue_number} "
            f"{from_status} → {to_status} (trigger: {trigger})"
        )

    def should_execute_work(
        self,
        issue_number: int,
        column: str,
        agent: str,
        trigger_source: str,
        project_name: str
    ) -> Tuple[bool, str]:
        """
        Determine if agent should execute work in this column

        Returns:
            Tuple[bool, str]: (should_execute, reason)
        """
        state = self.load_state(project_name, issue_number)

        # Get executions for this column/agent
        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column and e['agent'] == agent
        ]

        last_execution = column_executions[-1] if column_executions else None

        # Get status changes to this column
        status_changes_to_column = [
            sc for sc in state['status_changes']
            if sc['to_status'] == column
        ]

        last_status_change = status_changes_to_column[-1] if status_changes_to_column else None

        # Case 1: First time in this column
        if not last_execution:
            logger.debug(
                f"Should execute {agent} on {project_name}/#{issue_number}: "
                f"first_execution"
            )
            return True, "first_execution"

        # Case 2: Status changed back to this column after previous execution
        # (indicates manual rework needed)
        if last_status_change:
            last_exec_time = datetime.fromisoformat(last_execution['timestamp'])
            status_change_time = datetime.fromisoformat(last_status_change['timestamp'])

            if status_change_time > last_exec_time:
                logger.debug(
                    f"Should execute {agent} on {project_name}/#{issue_number}: "
                    f"manual_rework_detected (status changed at {status_change_time}, "
                    f"last execution at {last_exec_time})"
                )
                return True, "manual_rework_detected"

        # Case 3: Previous execution failed or was blocked
        if last_execution['outcome'] in ['failure', 'blocked']:
            logger.debug(
                f"Should execute {agent} on {project_name}/#{issue_number}: "
                f"retry_after_{last_execution['outcome']}"
            )
            return True, f"retry_after_{last_execution['outcome']}"

        # Case 4: Automatic progression triggering after successful execution
        # (prevent double-triggering)
        if trigger_source == 'pipeline_progression':
            if last_execution['outcome'] == 'success':
                # Check if there was a status change after the execution
                if last_status_change:
                    last_exec_time = datetime.fromisoformat(last_execution['timestamp'])
                    status_change_time = datetime.fromisoformat(last_status_change['timestamp'])

                    if status_change_time <= last_exec_time:
                        logger.debug(
                            f"Should skip {agent} on {project_name}/#{issue_number}: "
                            f"skip_auto_progression_after_success"
                        )
                        return False, "skip_auto_progression_after_success"
                else:
                    logger.debug(
                        f"Should skip {agent} on {project_name}/#{issue_number}: "
                        f"skip_auto_progression_after_success (no status change)"
                    )
                    return False, "skip_auto_progression_after_success"

        # Case 5: Work is already in progress
        if last_execution['outcome'] == 'in_progress':
            logger.debug(
                f"Should skip {agent} on {project_name}/#{issue_number}: "
                f"work_already_in_progress"
            )
            return False, "work_already_in_progress"

        # Case 6: Successful execution, no status change, manual trigger
        # (allow explicit retry)
        if trigger_source in ['manual_move', 'webhook', 'manual']:
            logger.debug(
                f"Should execute {agent} on {project_name}/#{issue_number}: "
                f"explicit_manual_trigger"
            )
            return True, "explicit_manual_trigger"

        # Default: skip (already processed successfully)
        logger.debug(
            f"Should skip {agent} on {project_name}/#{issue_number}: "
            f"already_processed_successfully"
        )
        return False, "already_processed_successfully"

    def get_last_execution(
        self,
        project_name: str,
        issue_number: int,
        column: str,
        agent: str
    ) -> Optional[Dict]:
        """Get the last execution record for a column/agent"""
        state = self.load_state(project_name, issue_number)

        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column and e['agent'] == agent
        ]

        return column_executions[-1] if column_executions else None

    def get_execution_history(
        self,
        project_name: str,
        issue_number: int
    ) -> List[Dict]:
        """Get full execution history for an issue"""
        state = self.load_state(project_name, issue_number)
        return state['execution_history']

    def was_recent_programmatic_change(
        self,
        project_name: str,
        issue_number: int,
        to_status: str,
        time_window_seconds: int = 60
    ) -> bool:
        """
        Check if a status change to the given status was recently made programmatically.
        
        This helps avoid duplicate event emission when the project monitor detects
        a status change that was already emitted by the pipeline progression service.
        
        Args:
            project_name: Project name
            issue_number: Issue number
            to_status: Target status to check
            time_window_seconds: Time window in seconds to consider "recent" (default: 60)
        
        Returns:
            True if a programmatic status change to this status was made within the time window
        """
        state = self.load_state(project_name, issue_number)
        
        # Check status_changes for recent programmatic changes
        for status_change in reversed(state.get('status_changes', [])):
            if status_change['to_status'] != to_status:
                continue
            
            # Check if trigger indicates programmatic change
            trigger = status_change.get('trigger', '')
            if trigger in ['agent_auto_advance', 'pipeline_progression', 'review_cycle', 
                          'review_cycle_completion', 'repair_cycle_completion',
                          'agent_completion', 'auto']:
                # Check if it's recent
                try:
                    change_time = datetime.fromisoformat(status_change['timestamp'])
                    now = datetime.now(timezone.utc)
                    time_diff = (now - change_time).total_seconds()
                    
                    if time_diff <= time_window_seconds:
                        logger.debug(
                            f"Found recent programmatic status change for {project_name}/#{issue_number} "
                            f"to {to_status} (trigger: {trigger}, {time_diff:.1f}s ago)"
                        )
                        return True
                except Exception as e:
                    logger.warning(f"Error parsing timestamp for status change: {e}")
                    continue
        
        return False

    def _check_redis_tracking_for_agent(self, project: str, agent: str, issue_number: int) -> bool:
        """
        Check if there's a Redis tracking key for an active agent container.
        
        Args:
            project: Project name
            agent: Agent name
            issue_number: Issue number
            
        Returns:
            True if Redis tracking exists for this agent, False otherwise
        """
        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            
            # Get all agent container tracking keys
            agent_keys = redis_client.keys('agent:container:*')
            
            for key in agent_keys:
                try:
                    container_info = redis_client.hgetall(key)
                    if (container_info.get('project') == project and
                        container_info.get('agent') == agent and
                        container_info.get('issue_number') == str(issue_number)):
                        return True
                except Exception as e:
                    logger.warning(f"Error checking Redis key {key}: {e}")
                    continue
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking Redis tracking: {e}")
            return False
    
    def _check_redis_repair_cycle_tracking(self, project: str, issue_number: int) -> bool:
        """
        Check if there's a Redis tracking key for an active repair cycle container.
        
        Args:
            project: Project name
            issue_number: Issue number
            
        Returns:
            True if Redis tracking exists for this repair cycle, False otherwise
        """
        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            
            # Check for repair cycle tracking key (format: repair_cycle:container:{project}:{issue})
            redis_key = f"repair_cycle:container:{project}:{issue_number}"
            exists = redis_client.exists(redis_key)
            
            if exists:
                logger.debug(f"Found repair cycle tracking in Redis: {redis_key}")
                return True
            
            return False

        except Exception as e:
            logger.error(f"Error checking repair cycle Redis tracking: {e}")
            return False

    def _should_retry_failed_execution(
        self,
        project_name: str,
        issue_number: int,
        agent: str,
        column: str,
        execution: dict
    ) -> tuple:
        """
        Determine if a failed execution should be retried by watchdog.

        Performs comprehensive eligibility checks:
        - Retry limit not exceeded
        - Issue still in same active column
        - Column still requires agent
        - Pipeline run still active
        - Issue still open
        - Circuit breakers not open

        Args:
            project_name: Project name
            issue_number: Issue number
            agent: Agent name
            column: Column name
            execution: Execution record dict

        Returns:
            (should_retry, reason) tuple
        """
        import os

        # Check 1: Retry limit
        max_retries = int(os.environ.get('WATCHDOG_MAX_RETRIES', '3'))
        retry_count = execution.get('watchdog_retry_count', 0)

        if retry_count >= max_retries:
            return False, f"max_retries_exceeded (count={retry_count}, max={max_retries})"

        # Check 2 & 3 & 5: Issue state and column (combined GitHub query)
        try:
            from config.manager import config_manager
            from services.github_api_client import get_github_client

            project_config = config_manager.get_project_config(project_name)
            if not project_config:
                return False, "project_config_not_found"

            github_client = get_github_client()

            # Get issue details (state and current column)
            query = """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $number) {
                  state
                  projectItems(first: 10) {
                    nodes {
                      fieldValueByName(name: "Status") {
                        ... on ProjectV2ItemFieldSingleSelectValue {
                          name
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            success, data = github_client.graphql(query, {
                'owner': project_config.github['org'],
                'repo': project_config.github['repo'],
                'number': issue_number
            })

            if not success:
                logger.warning(f"Failed to query issue state for {project_name}/#{issue_number}: {data}")
                return False, "github_query_failed"

            issue_data = data.get('repository', {}).get('issue', {})

            # Check 5: Issue state
            if issue_data.get('state', '').upper() == 'CLOSED':
                return False, "issue_closed"

            # Check 2: Current column
            current_column = None
            for item in issue_data.get('projectItems', {}).get('nodes', []):
                field_value = item.get('fieldValueByName')
                if field_value:
                    current_column = field_value.get('name')
                    break

            if current_column != column:
                return False, f"issue_moved_to_different_column (was={column}, now={current_column})"

        except Exception as e:
            logger.error(f"Error checking issue state: {e}")
            return False, f"error_checking_issue_state: {str(e)}"

        # Check 3: Pipeline run active (moved before workflow check to get board name)
        try:
            from services.pipeline_run import get_pipeline_run_manager

            pipeline_run_mgr = get_pipeline_run_manager()
            active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

            if not active_run:
                return False, "no_active_pipeline_run"

        except Exception as e:
            logger.error(f"Error checking pipeline run: {e}")
            return False, f"error_checking_pipeline_run: {str(e)}"

        # Check 4: Column requires agent
        try:
            workflow_template = config_manager.get_project_workflow(project_name, active_run.board)
            if not workflow_template:
                return False, "workflow_template_not_found"

            column_config = None
            for col in workflow_template.columns:
                if col.name == column:
                    column_config = col
                    break

            if not column_config or not column_config.agent or column_config.agent == 'null':
                return False, "column_no_longer_requires_agent"

        except Exception as e:
            logger.error(f"Error checking workflow template: {e}")
            return False, f"error_checking_workflow: {str(e)}"

        # Check 6: Claude Code circuit breaker
        try:
            from monitoring.claude_code_breaker import get_claude_code_breaker

            claude_breaker = get_claude_code_breaker()
            if claude_breaker.is_open():
                return False, "claude_code_breaker_open"

        except Exception as e:
            logger.warning(f"Error checking Claude Code breaker: {e}")
            # Continue - don't block on breaker check failure

        # Check 7: Agent-specific circuit breaker
        # Note: Agent circuit breakers are checked in agent_executor, not globally accessible
        # Skip this check for now - agent_executor will handle it on retry

        # All checks passed
        return True, "eligible_for_retry"

    def cleanup_stuck_in_progress_states(self):
        """
        Clean up execution states that are stuck as 'in_progress'.

        This handles cases where:
        - Orchestrator was restarted while agents were running
        - Agents completed but record_execution_outcome() was never called
        - Execution states remain permanently stuck as 'in_progress'

        Strategy:
        - Find all state files with in_progress executions
        - Check if corresponding agent container still exists
        - If container is gone, mark execution as 'failure' with reason 'orchestrator_restart'
        """
        import subprocess

        if not self.state_dir.exists():
            logger.info("No execution state directory found, skipping cleanup")
            return

        state_files = list(self.state_dir.glob("*.yaml"))

        if not state_files:
            logger.info("No execution state files found, skipping cleanup")
            return

        logger.info(f"Checking {len(state_files)} execution state files for stuck in_progress states")

        cleaned_count = 0
        for state_file in state_files:
            try:
                from utils.file_lock import file_lock

                # Use file lock for entire read-modify-write cycle
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(lock_file):
                    # Load state
                    if not state_file.exists():  # Check inside lock
                        continue
                    with open(state_file, 'r') as f:
                        state = yaml.safe_load(f)

                    if not state or 'execution_history' not in state:
                        continue

                    project_name = state.get('project_name')
                    issue_number = state.get('issue_number')

                    # Find in_progress executions
                    modified = False
                    for execution in state['execution_history']:
                        if execution.get('outcome') == 'in_progress':
                            agent = execution.get('agent')
                            column = execution.get('column')
                            timestamp = execution.get('timestamp')

                            logger.info(
                                f"Found stuck in_progress execution: {project_name}/#{issue_number} "
                                f"{agent} in {column} from {timestamp}"
                            )

                            # Check if agent/repair cycle container still exists using two methods:
                            # 1. Docker ps (checks if container actually exists)
                            # 2. Redis tracking keys (checks orchestrator's view of active agents)

                            # Check if this issue is in the pipeline queue
                            # If it is, it's waiting to start, not stuck
                            try:
                                import redis
                                import json
                                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                                
                                # Scan for all board queues for this project
                                queue_keys = redis_client.keys(f"orchestrator:pipeline_queue:{project_name}:*")
                                is_in_queue = False
                                
                                for key in queue_keys:
                                    # Get all items in the queue
                                    items = redis_client.lrange(key, 0, -1)
                                    for item_json in items:
                                        try:
                                            item = json.loads(item_json)
                                            if str(item.get('issue_number')) == str(issue_number):
                                                is_in_queue = True
                                                break
                                        except:
                                            pass
                                    if is_in_queue:
                                        break
                                
                                if is_in_queue:
                                    logger.info(
                                        f"Issue {project_name}/#{issue_number} is in pipeline queue, "
                                        f"skipping stuck state cleanup"
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"Failed to check pipeline queue: {e}")

                            # Method 1: Check if Docker container is running
                            # Use specific prefix pattern to avoid false positives from other projects
                            # Agent container naming: claude-agent-{project}-{task_id}
                            result = subprocess.run(
                                ['docker', 'ps', '--filter', f'name=claude-agent-{project_name}-',
                                 '--format', '{{.Names}}'],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            has_agent_container = bool(result.stdout.strip())

                            # Also check for RUNNING repair cycle containers (format: repair-cycle-{project}-{issue}-{run_id})
                            # Important: Use 'docker ps' not 'docker ps -a' to only find running containers
                            result = subprocess.run(
                                ['docker', 'ps', '--filter', f'name=repair-cycle-{project_name}-{issue_number}',
                                 '--format', '{{.Names}}'],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            has_repair_cycle_container = bool(result.stdout.strip())

                            has_docker_container = has_agent_container or has_repair_cycle_container

                            # Method 2: Check Redis tracking keys for agents
                            has_redis_tracking = self._check_redis_tracking_for_agent(project_name, agent, issue_number)

                            # Also check for repair cycle Redis tracking
                            has_repair_cycle_tracking = self._check_redis_repair_cycle_tracking(project_name, issue_number)

                            has_redis_tracking = has_redis_tracking or has_repair_cycle_tracking

                            # IMPORTANT: Docker is the source of truth for running containers.
                            # Redis tracking keys can persist after containers die (orphaned keys).
                            # Only trust Docker to determine if a container is actually running.
                            has_running_container = has_docker_container

                            if has_docker_container and not has_redis_tracking:
                                logger.warning(
                                    f"Container exists in Docker but not in Redis tracking for "
                                    f"{project_name}/#{issue_number} {agent}"
                                )
                            elif has_redis_tracking and not has_docker_container:
                                logger.warning(
                                    f"Redis tracking exists but container not found in Docker for "
                                    f"{project_name}/#{issue_number} {agent} (orphaned tracking key)"
                                )
                                # Clean up orphaned Redis tracking keys
                                try:
                                    import redis
                                    redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

                                    # Clean up agent tracking key if exists
                                    agent_key = f"agent:container:claude-agent-{project_name}-*"
                                    # For repair cycle tracking
                                    repair_cycle_key = f"repair_cycle:container:{project_name}:{issue_number}"
                                    deleted = redis_client.delete(repair_cycle_key)
                                    if deleted:
                                        logger.info(f"Cleaned up orphaned repair cycle Redis key: {repair_cycle_key}")
                                except Exception as e:
                                    logger.warning(f"Failed to clean up orphaned Redis keys: {e}")

                            if not has_running_container:
                                # No container found - agent may have finished or been killed
                                # Check if we can determine the actual outcome by looking for output

                                # For now, we conservatively mark as failed since we can't verify success
                                # If the agent completed successfully, agent_executor should have recorded it
                                # The fact that it's still in_progress means either:
                                # 1. Agent crashed/was killed before completing
                                # 2. Orchestrator was killed before outcome could be recorded
                                # 3. Bug in execution outcome recording (which we just fixed above)

                                execution['outcome'] = 'failure'
                                execution['error'] = (
                                    'Agent execution interrupted by orchestrator restart. '
                                    'Container no longer exists and execution state was not updated. '
                                    'This indicates the agent did not complete normally or the outcome was not recorded before restart.'
                                )
                                modified = True
                                cleaned_count += 1

                                logger.info(
                                    f"Marked stuck execution as failed: {project_name}/#{issue_number} "
                                    f"{agent} in {column} (no container found, outcome not recorded)"
                                )

                                # NEW: Check if we should retry this failed execution
                                should_retry, retry_reason = self._should_retry_failed_execution(
                                    project_name, issue_number, agent, column, execution
                                )

                                # Emit decision event
                                from monitoring.observability import get_observability_manager, EventType
                                obs = get_observability_manager()
                                obs.emit(
                                    EventType.RETRY_ATTEMPTED,
                                    agent=agent,
                                    task_id=f"watchdog_{project_name}_issue_{issue_number}",
                                    project=project_name,
                                    data={
                                        "decision_category": "watchdog_retry_decision",
                                        "issue_number": issue_number,
                                        "should_retry": should_retry,
                                        "reason": retry_reason,
                                        "column": column,
                                        "retry_count": execution.get('watchdog_retry_count', 0),
                                        "execution_timestamp": execution['timestamp']
                                    }
                                )

                                if should_retry:
                                    # TODO: Retry mechanism requires refactoring
                                    # The get_project_monitor() function doesn't exist and this code path
                                    # was never functional. Watchdog retries need to be redesigned.
                                    logger.warning(
                                        f"Watchdog retry mechanism not implemented for {project_name}/#{issue_number}. "
                                        f"Marking execution as failed without retry."
                                    )
                                    # Skip retry logic for now
                                    if False:  # Disabled - needs implementation
                                        # Import here to avoid circular dependency
                                        from services.project_monitor import get_project_monitor

                                        monitor = get_project_monitor()
                                        if not monitor:
                                            logger.error("ProjectMonitor not available for retry trigger")
                                        else:
                                            # Get repository from project config
                                            from config.manager import config_manager
                                            project_config = config_manager.get_project_config(project_name)

                                            if not project_config:
                                                logger.error(f"Cannot retry: project config not found for {project_name}")
                                            else:
                                                # Get board name from active pipeline run
                                                from services.pipeline_run import get_pipeline_run_manager
                                                pipeline_run_mgr = get_pipeline_run_manager()
                                                active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

                                                if not active_run:
                                                    logger.warning(f"Cannot retry: no active pipeline run for {project_name}/#{issue_number}")
                                                else:
                                                    logger.info(
                                                        f"Triggering watchdog retry for {project_name}/#{issue_number} "
                                                        f"in column '{column}' (attempt {execution.get('watchdog_retry_count', 0) + 1})"
                                                    )

                                                    # Increment retry counter
                                                    execution['watchdog_retry_count'] = execution.get('watchdog_retry_count', 0) + 1
                                                    execution['watchdog_last_retry_at'] = datetime.now(timezone.utc).isoformat()

                                                    # Trigger agent (this will create new execution record)
                                                    monitor.trigger_agent_for_status(
                                                        project_name=project_name,
                                                        board_name=active_run.board,
                                                        issue_number=issue_number,
                                                        status=column,
                                                        repository=project_config.github['repo']
                                                    )

                                                    # Emit retry triggered event
                                                    obs.emit(
                                                        EventType.RETRY_ATTEMPTED,
                                                        agent=agent,
                                                        task_id=f"watchdog_{project_name}_issue_{issue_number}",
                                                        project=project_name,
                                                        data={
                                                            "decision_category": "watchdog_retry_triggered",
                                                            "issue_number": issue_number,
                                                            "column": column,
                                                            "retry_count": execution['watchdog_retry_count'],
                                                            "previous_failure": execution.get('error', 'Unknown')
                                                        }
                                                    )

                                                    logger.info(
                                                        f"Successfully triggered watchdog retry for {project_name}/#{issue_number}"
                                                    )

                                    except Exception as retry_error:
                                        logger.error(
                                            f"Failed to trigger watchdog retry for {project_name}/#{issue_number}: {retry_error}",
                                            exc_info=True
                                        )
                                        # Don't re-raise - continue with cleanup
                                else:
                                    logger.info(
                                        f"Not retrying {project_name}/#{issue_number}: {retry_reason}"
                                    )
                            else:
                                logger.info(
                                    f"Agent container still running for {project_name}/#{issue_number}, "
                                    f"keeping in_progress state"
                                )

                    # Save if modified (still inside the lock)
                    if modified:
                        state['last_updated'] = datetime.now(timezone.utc).isoformat()
                        with open(state_file, 'w') as f:
                            yaml.dump(state, f, default_flow_style=False, sort_keys=False)

                        logger.info(f"Updated state file: {state_file}")

            except Exception as e:
                logger.error(f"Error processing state file {state_file}: {e}")

        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} stuck in_progress execution states")
        else:
            logger.info("No stuck in_progress execution states found")


# Global instance
work_execution_tracker = WorkExecutionStateTracker()

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
    outcome: str  # 'success', 'failure', 'blocked', 'cancelled', 'in_progress'
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

    def stamp_execution_task_id(self, project_name, issue_number, agent, column, task_id):
        """Stamp the execution-level task_id onto the current in_progress execution.

        Called by agent_executor after generating the task_id, which happens
        after record_execution_start() has already created the in_progress entry.
        This task_id matches the Redis key suffix in agent_result:{project}:{issue}:{task_id}.
        """
        state = self.load_state(project_name, issue_number)
        for execution in reversed(state['execution_history']):
            if (execution.get('outcome') == 'in_progress' and
                execution.get('agent') == agent and
                execution.get('column') == column):
                execution['task_id'] = task_id
                self.save_state(project_name, issue_number, state)
                logger.debug(
                    f"Stamped task_id={task_id} on execution for "
                    f"{project_name}/#{issue_number} {agent} in {column}"
                )
                return
        logger.warning(
            f"No matching in_progress execution found to stamp task_id={task_id} "
            f"for {project_name}/#{issue_number} {agent} in {column}"
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

        # Find the in_progress entry for this agent/column and update it.
        # Normally there is exactly one (the pre-enqueue probe created by project_monitor).
        # The loop handles any historical duplicates defensively.
        found_primary = False
        phantom_count = 0
        for execution in reversed(state['execution_history']):
            if (execution['column'] == column and
                execution['agent'] == agent and
                execution['outcome'] == 'in_progress'):

                execution['outcome'] = outcome
                if not found_primary:
                    # Most recent in_progress: the real execution entry
                    if error:
                        execution['error'] = error
                    found_primary = True
                else:
                    # Older in_progress entries are phantom probes (pre-enqueue).
                    # Mark them with the same outcome so they don't linger as in_progress.
                    execution['error'] = (
                        'Superseded by a later execution. This was a pre-enqueue probe '
                        'entry; the actual outcome was recorded on the newer tracking entry.'
                    )
                    phantom_count += 1

        if found_primary:
            self.save_state(project_name, issue_number, state)
            suffix = f" (cleaned up {phantom_count} phantom probe entries)" if phantom_count else ""
            logger.info(
                f"Recorded execution outcome: {project_name}/#{issue_number} "
                f"{agent} in {column} → {outcome}{suffix}"
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

        # Case 3: Previous execution failed, was blocked, cancelled, or abandoned
        if last_execution['outcome'] in ['failure', 'blocked', 'cancelled', 'abandoned']:
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

    def has_active_execution(
        self,
        project_name: str,
        issue_number: int
    ) -> bool:
        """
        Check if there's an active (in_progress) execution for this issue.

        This method checks ALL types of work that can be active on an issue:
        1. Regular agent execution (execution_history with outcome='in_progress')
        2. Active review cycles (maker-checker loops)
        3. Repair cycle containers (long-running test containers)
        4. Conversational feedback loops (human-in-the-loop)

        Returns True if ANY type of work is currently active for this issue,
        preventing duplicate work from being scheduled.

        This is critical for preventing race conditions between:
        - Project Monitor and Review Cycle Manager
        - Project Monitor and Repair Cycle containers
        - Pipeline Orchestrator and Project Monitor
        - Any concurrent work trigger sources
        """
        # Check 1: Regular agent execution in execution_history
        state = self.load_state(project_name, issue_number)
        for execution in state['execution_history']:
            if execution.get('outcome') == 'in_progress':
                logger.debug(
                    f"Active execution found in history for {project_name}/#{issue_number}: "
                    f"{execution.get('agent')} in {execution.get('column')}"
                )
                return True

        # Track if any service checks fail (indicates potential initialization issue)
        check_failures = []

        # Check 2: Active review cycles
        try:
            from services.review_cycle import review_cycle_executor
            if issue_number in review_cycle_executor.active_cycles:
                cycle = review_cycle_executor.active_cycles[issue_number]
                if cycle.project_name == project_name:
                    logger.debug(
                        f"Active review cycle found for {project_name}/#{issue_number}: "
                        f"iteration {cycle.current_iteration}, status={cycle.status}"
                    )
                    return True
        except (ImportError, AttributeError) as e:
            # Review cycle module may not be available or initialized
            logger.warning(
                f"Could not check review cycles for {project_name}/#{issue_number}: {e}. "
                f"This may indicate an initialization issue."
            )
            check_failures.append('review_cycle')

        # Check 3: Repair cycle containers
        try:
            if self._check_redis_repair_cycle_tracking(project_name, issue_number):
                logger.debug(
                    f"Active repair cycle container found for {project_name}/#{issue_number}"
                )
                return True
        except Exception as e:
            logger.warning(
                f"Could not check repair cycles for {project_name}/#{issue_number}: {e}"
            )
            check_failures.append('repair_cycle')

        # Check 4: Conversational feedback loops
        try:
            from services.human_feedback_loop import human_feedback_loop_executor
            if issue_number in human_feedback_loop_executor.active_loops:
                loop = human_feedback_loop_executor.active_loops[issue_number]
                if loop.project_name == project_name:
                    logger.debug(
                        f"Active feedback loop found for {project_name}/#{issue_number}"
                    )
                    return True
        except (ImportError, AttributeError) as e:
            # Feedback loop module may not be available or initialized
            logger.warning(
                f"Could not check feedback loops for {project_name}/#{issue_number}: {e}. "
                f"This may indicate an initialization issue."
            )
            check_failures.append('feedback_loop')

        # Fail-safe: If multiple service checks failed, assume work might be active
        # to prevent duplicate executions during degraded state
        if len(check_failures) >= 2:
            logger.error(
                f"Multiple service checks failed for {project_name}/#{issue_number}: {check_failures}. "
                f"Failing safe by assuming active execution to prevent duplicates."
            )
            return True

        # No active work found
        return False

    def is_blocked_by_circuit_breaker(
        self,
        project_name: str,
        issue_number: int
    ) -> bool:
        """
        Check if the last execution was blocked by circuit breaker.

        This allows the project monitor to detect when a previously blocked
        execution can be retried (after circuit breaker closes).

        Returns:
            True if last execution was blocked by circuit breaker, False otherwise
        """
        state = self.load_state(project_name, issue_number)
        history = state.get('execution_history', [])

        if not history:
            return False

        # Check the most recent execution
        last_execution = history[-1]
        outcome = last_execution.get('outcome')
        error = last_execution.get('error', '')

        # Check if outcome is 'blocked' and error mentions circuit breaker
        is_blocked = (
            outcome == 'blocked' and
            'circuit breaker' in error.lower()
        )

        if is_blocked:
            logger.debug(
                f"Issue #{issue_number} was blocked by circuit breaker: {error}"
            )

        return is_blocked

    def was_recent_programmatic_change(
        self,
        project_name: str,
        issue_number: int,
        to_status: str,
        time_window_seconds: Optional[int] = None
    ) -> bool:
        """
        Check if a status change to the given status was recently made programmatically.

        This helps avoid duplicate event emission when the project monitor detects
        a status change that was already emitted by the pipeline progression service.

        Args:
            project_name: Project name
            issue_number: Issue number
            to_status: Target status to check
            time_window_seconds: Time window in seconds to consider "recent"
                                If None, reads from env var PROGRAMMATIC_CHANGE_WINDOW_SECONDS
                                with fallback to 60 seconds

        Returns:
            True if a programmatic status change to this status was made within the time window
        """
        # Allow configurable time window via environment variable
        if time_window_seconds is None:
            import os
            time_window_seconds = int(os.environ.get('PROGRAMMATIC_CHANGE_WINDOW_SECONDS', '60'))
            logger.debug(f"Using programmatic change window: {time_window_seconds}s")
        state = self.load_state(project_name, issue_number)
        
        # Check status_changes for recent programmatic changes
        for status_change in reversed(state.get('status_changes', [])):
            if status_change['to_status'] != to_status:
                continue
            
            # Check if trigger indicates programmatic change
            trigger = status_change.get('trigger', '')
            if trigger in ['agent_auto_advance', 'pipeline_progression', 'review_cycle',
                          'review_cycle_completion', 'repair_cycle_completion',
                          'agent_completion', 'auto', 'all_subtasks_completed']:
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
            
            # Cursor-based scan (non-blocking, unlike keys())
            for key in redis_client.scan_iter(match='agent:container:*', count=100):
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

    def _discover_containers_for_execution(self, project: str, execution: dict, provided_container_names: list) -> list:
        """
        Discover containers for an execution using hierarchical discovery methods.

        Priority:
        1. Use provided container names (from caller's docker ps)
        2. Docker label filter by task_id (if available) - MOST PRECISE
        3. Docker label filter by project + issue_number (fallback)
        4. Name pattern matching (legacy fallback)

        Returns: List of container names matching this execution
        """
        import subprocess

        # If caller already found containers, use those
        if provided_container_names:
            logger.debug(
                f"Using {len(provided_container_names)} containers from caller for "
                f"{project}/#{execution.get('issue_number')}"
            )
            return provided_container_names

        task_id = execution.get('task_id')
        issue_number = execution.get('issue_number')

        # Method 1: Docker label filter by task_id (most precise)
        if task_id:
            try:
                result = subprocess.run(
                    ['docker', 'ps', '--filter', f'label=org.clauditoreum.task_id={task_id}',
                     '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    containers = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
                    if containers:
                        logger.info(
                            f"Discovered {len(containers)} container(s) via task_id label "
                            f"({task_id}) for {project}/#{issue_number}"
                        )
                        return containers
            except Exception as e:
                logger.warning(f"Failed to discover containers by task_id label: {e}")

        # Method 2: Docker label filter by project + issue_number (medium precision)
        if issue_number:
            try:
                result = subprocess.run(
                    ['docker', 'ps',
                     '--filter', f'label=org.clauditoreum.project={project}',
                     '--filter', f'label=org.clauditoreum.issue_number={issue_number}',
                     '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    containers = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
                    if containers:
                        logger.info(
                            f"Discovered {len(containers)} container(s) via project+issue labels "
                            f"for {project}/#{issue_number} (task_id unavailable)"
                        )
                        return containers
            except Exception as e:
                logger.warning(f"Failed to discover containers by project+issue labels: {e}")

        # Method 3: Name pattern matching (legacy fallback - least precise)
        logger.info(
            f"Falling back to name pattern matching for {project}/#{issue_number} "
            f"(labels not available)"
        )
        try:
            result = subprocess.run(
                ['docker', 'ps', '--filter', f'name=claude-agent-{project}-',
                 '--format', '{{.Names}}'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                containers = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
                if containers:
                    logger.warning(
                        f"Discovered {len(containers)} container(s) via name pattern for {project} - "
                        f"may include false positives, will validate with labels"
                    )
                    return containers
        except Exception as e:
            logger.warning(f"Failed to discover containers by name pattern: {e}")

        logger.info(f"No containers discovered for {project}/#{issue_number} task_id={task_id}")
        return []

    def _repair_missing_redis_tracking(self, execution: dict, project: str, container_names: list = None):
        """
        Repair missing Redis tracking keys by reading Docker container labels.

        When a container exists in Docker but has no Redis tracking key, this method
        inspects the container to extract metadata from labels and re-registers it.

        Args:
            execution: Execution record dict with task_id, issue_number, agent, etc.
            project: Project name
            container_names: Optional list of container names to repair (discovered if not provided)
        """
        import subprocess

        # Extract metadata from execution record
        task_id = execution.get('task_id')
        issue_number = execution.get('issue_number')
        agent = execution.get('agent')
        column = execution.get('column', 'unknown')
        timestamp = execution.get('timestamp')

        if not issue_number:
            logger.error(f"Execution record missing issue_number, cannot repair: {execution}")
            return

        if not agent:
            logger.error(f"Execution record missing agent field, cannot repair: {execution}")
            return

        logger.debug(
            f"Attempting repair for {project}/#{issue_number}: "
            f"agent={agent}, task_id={task_id}, column={column}, timestamp={timestamp}"
        )

        # Use provided containers or discover via hierarchical search
        if container_names is None:
            container_names = []

        if not container_names:
            container_names = self._discover_containers_for_execution(project, execution, [])

        if not container_names:
            logger.info(
                f"No containers found for {project}/#{issue_number} task_id={task_id} - "
                f"nothing to repair"
            )
            return

        logger.debug(
            f"Repairing Redis tracking for {project}/#{issue_number}. "
            f"Containers to inspect: {container_names}"
        )

        for container_name in container_names:
            try:
                # Extract container ID and labels via docker inspect
                inspect_result = subprocess.run(
                    ['docker', 'inspect', '--format',
                     '{{.Id}}|{{index .Config.Labels "org.clauditoreum.agent"}}|'
                     '{{index .Config.Labels "org.clauditoreum.project"}}|'
                     '{{index .Config.Labels "org.clauditoreum.task_id"}}|'
                     '{{index .Config.Labels "org.clauditoreum.issue_number"}}|'
                     '{{index .Config.Labels "org.clauditoreum.pipeline_run_id"}}',
                     container_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if inspect_result.returncode != 0:
                    logger.warning(f"Failed to inspect container {container_name}: {inspect_result.stderr}")
                    continue

                parts = inspect_result.stdout.strip().split('|')
                container_id = parts[0] if len(parts) > 0 else ''
                label_agent = parts[1] if len(parts) > 1 and parts[1] else agent
                label_project = parts[2] if len(parts) > 2 and parts[2] else project
                label_task_id = parts[3] if len(parts) > 3 and parts[3] else 'unknown'
                label_issue = parts[4] if len(parts) > 4 and parts[4] else str(issue_number)
                label_pipeline_run_id = parts[5] if len(parts) > 5 and parts[5] else ''

                # Validation hierarchy: task_id (primary) > issue_number (secondary) > agent (tertiary)

                # Level 1: Task ID validation (most reliable)
                if task_id and label_task_id and label_task_id != 'unknown':
                    if label_task_id != task_id:
                        logger.warning(
                            f"VALIDATION FAILED (task_id mismatch): Container {container_name} "
                            f"has task_id={label_task_id}, expected={task_id}. Skipping repair."
                        )
                        continue
                    logger.info(
                        f"VALIDATION PASSED (task_id): {container_name} matches task_id={task_id}"
                    )

                # Level 2: Issue number validation (consistency check)
                if label_issue != str(issue_number):
                    if task_id and label_task_id == task_id:
                        # Task ID matched but issue doesn't - data inconsistency
                        logger.error(
                            f"DATA INCONSISTENCY: Container {container_name} has matching task_id "
                            f"but mismatched issue (container: #{label_issue}, execution: #{issue_number}). "
                            f"Proceeding with repair."
                        )
                    else:
                        # Issue mismatch and no task_id confirmation - skip
                        logger.warning(
                            f"VALIDATION FAILED (issue_number): Container {container_name} "
                            f"is for issue #{label_issue}, not #{issue_number}. Skipping repair."
                        )
                        continue

                # Level 3: Agent consistency check (informational)
                if label_agent and agent and label_agent != agent:
                    logger.warning(
                        f"Agent mismatch for {container_name}: "
                        f"container={label_agent}, execution={agent}"
                    )

                # Level 4: Project validation (sanity check)
                if label_project != project:
                    logger.error(
                        f"VALIDATION FAILED (project): Container {container_name} "
                        f"project={label_project}, expected={project}. Skipping."
                    )
                    continue

                from datetime import datetime
                container_info = {
                    'container_name': container_name,
                    'container_id': container_id,
                    'agent': label_agent,
                    'project': label_project,
                    'task_id': label_task_id,
                    'started_at': datetime.now().isoformat(),
                    'issue_number': label_issue,
                    'pipeline_run_id': label_pipeline_run_id,
                    'repaired': 'true'
                }

                import redis
                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                redis_client.hset(f'agent:container:{container_name}', mapping=container_info)
                redis_client.expire(f'agent:container:{container_name}', 7200)

                logger.info(
                    f"REPAIRED Redis tracking for container {container_name} "
                    f"(agent={label_agent}, project={label_project}, issue=#{label_issue}, "
                    f"task_id={label_task_id}, validation={'task_id' if (task_id and label_task_id and label_task_id != 'unknown') else 'issue_number'})"
                )

            except Exception as e:
                logger.warning(f"Failed to repair Redis tracking for container {container_name}: {e}")

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

    def should_retry_execution(
        self,
        project_name: str,
        issue_number: int,
        max_retries: int = None
    ) -> tuple:
        """
        Check if execution should be retried (works for both failed and successful-but-empty executions).

        This is the public API for checking retry eligibility. It wraps _should_retry_failed_execution
        with additional context loading.

        Args:
            project_name: Project name
            issue_number: Issue number
            max_retries: Optional max retry limit (defaults to WATCHDOG_MAX_RETRIES env var)

        Returns:
            (should_retry: bool, reason: str)
        """
        state = self.load_state(project_name, issue_number)
        if not state or not state['execution_history']:
            return False, "No execution state found"

        # Get last execution
        last_execution = state['execution_history'][-1]
        agent = last_execution.get('agent')
        column = last_execution.get('column')

        if not agent or not column:
            return False, "Missing agent or column in execution state"

        # Use comprehensive eligibility checks
        return self._should_retry_failed_execution(
            project_name, issue_number, agent, column, last_execution
        )

    def detect_and_retry_empty_successful_executions(self) -> int:
        """
        Detect executions marked as 'success' but with no GitHub output.
        Mark them as 'failure' to trigger retry.

        This watchdog runs as a scheduled task and uses comprehensive race condition
        protections to prevent duplicate work launches:
        1. has_active_execution() - Checks ALL 4 types of active work
        2. Pipeline lock verification
        3. Queue status check
        4. Execution eligibility via _should_retry_failed_execution
        5. 5-minute recency check

        CRITICAL: This method only marks executions as 'failure' - it does NOT
        directly trigger work. The project_monitor picks up failed executions and
        handles retry with its own race protections.

        Returns:
            Number of executions marked for retry
        """
        import os
        from pathlib import Path
        from datetime import datetime, timedelta
        import re

        if not self.state_dir.exists():
            logger.debug("No execution state directory found, skipping empty output detection")
            return 0

        retried_count = 0
        state_files = list(self.state_dir.glob("*.yaml"))

        logger.info(f"Watchdog: Checking {len(state_files)} execution state files for empty outputs")

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

                    if not state or not state['execution_history']:
                        continue

                    # Get last execution
                    last_exec = state['execution_history'][-1]

                    # Only check successful executions
                    if last_exec.get('outcome') != 'success':
                        continue

                    # Parse project and issue from state
                    project_name = state.get('project_name')
                    issue_number = state.get('issue_number')

                    if not project_name or not issue_number:
                        logger.warning(f"Malformed state file {state_file}: missing project or issue")
                        continue

                    # PROTECTION 1: Check for active execution (ANY type of work)
                    if self.has_active_execution(project_name, issue_number):
                        logger.debug(
                            f"Watchdog: Skipping {project_name}/#{issue_number}: work already in progress"
                        )
                        continue

                    # PROTECTION 2: Check pipeline lock
                    try:
                        from services.pipeline_lock_manager import get_pipeline_lock_manager
                        from config.manager import config_manager

                        lock_manager = get_pipeline_lock_manager()
                        project_config = config_manager.get_project_config(project_name)

                        if project_config:
                            # Get pipeline config for this project
                            pipeline_configs = project_config.get('pipelines', {}).get('enabled', [])
                            for pipeline_config in pipeline_configs:
                                board_name = pipeline_config.get('workflow', 'unknown')
                                lock_status = lock_manager.get_lock_status(project_name, board_name)
                                if lock_status and lock_status.issue_number:
                                    logger.debug(
                                        f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                        f"pipeline locked by issue #{lock_status.issue_number}"
                                    )
                                    continue
                    except Exception as e:
                        logger.debug(f"Watchdog: Could not check pipeline lock: {e}")

                    # PROTECTION 3: Check queue state
                    try:
                        from services.pipeline_queue_manager import get_pipeline_queue

                        queue_manager = get_pipeline_queue()
                        # Check if issue is already queued or active
                        # Note: This is a simple check - queue manager would need to expose status API
                        # For now, skip this protection as it requires queue manager changes
                    except Exception as e:
                        logger.debug(f"Watchdog: Could not check queue status: {e}")

                    # PROTECTION 4: Check execution eligibility
                    agent = last_exec.get('agent')
                    column = last_exec.get('column')

                    if not agent or not column:
                        logger.warning(f"Watchdog: Missing agent or column for {project_name}/#{issue_number}")
                        continue

                    should_retry, reason = self._should_retry_failed_execution(
                        project_name, issue_number, agent, column, last_exec
                    )

                    if not should_retry:
                        logger.debug(
                            f"Watchdog: Not eligible for retry {project_name}/#{issue_number}: {reason}"
                        )
                        continue

                    # PROTECTION 5: Verify no recent execution started
                    # Check if execution completed within last 5 minutes
                    # (could be starting but not yet marked as in_progress)
                    if last_exec.get('completed_at'):
                        try:
                            completed_at_str = last_exec['completed_at'].replace('Z', '+00:00')
                            completed_at = datetime.fromisoformat(completed_at_str)
                            if datetime.now(completed_at.tzinfo) - completed_at < timedelta(minutes=5):
                                logger.debug(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                    f"execution too recent ({completed_at})"
                                )
                                continue
                        except Exception as e:
                            logger.debug(f"Could not parse completed_at timestamp: {e}")

                    # Check if GitHub output exists
                    if self._has_github_output(project_name, issue_number, last_exec):
                        logger.debug(
                            f"Watchdog: {project_name}/#{issue_number} has GitHub output, "
                            f"execution is truly successful"
                        )
                        continue

                    # ALL PROTECTIONS PASSED - Safe to mark for retry
                    logger.warning(
                        f"Watchdog: Detected successful execution with no output for "
                        f"{project_name}/#{issue_number} - marking as failure to trigger retry"
                    )

                    # Mark as failure to trigger retry
                    last_exec['outcome'] = 'failure'
                    last_exec['error'] = 'Execution marked as success but produced no visible GitHub output'
                    last_exec['watchdog_retry_triggered'] = True
                    last_exec['watchdog_retry_count'] = last_exec.get('watchdog_retry_count', 0) + 1
                    last_exec['watchdog_last_retry_at'] = datetime.now().isoformat() + 'Z'

                    # Write updated state
                    with open(state_file, 'w') as f:
                        yaml.dump(state, f, default_flow_style=False, sort_keys=False)

                    retried_count += 1

                    # Emit observability event
                    try:
                        from monitoring.observability import get_observability_manager, EventType
                        obs = get_observability_manager()
                        obs.emit(
                            EventType.RETRY_ATTEMPTED,
                            agent='watchdog',
                            project=project_name,
                            data={
                                'issue_number': issue_number,
                                'reason': 'empty_output_on_success',
                                'retry_count': last_exec['watchdog_retry_count']
                            }
                        )
                    except Exception as e:
                        logger.debug(f"Could not emit observability event: {e}")

            except Exception as e:
                logger.error(f"Watchdog: Error processing {state_file}: {e}", exc_info=True)

        if retried_count > 0:
            logger.info(f"Watchdog: Marked {retried_count} executions for retry (empty output)")

        return retried_count

    def _has_github_output(self, project_name: str, issue_number: int, execution: dict) -> bool:
        """
        Check if execution resulted in GitHub output (comment/discussion post).

        Args:
            project_name: Project name
            issue_number: Issue number
            execution: Execution record dict

        Returns:
            True if GitHub output exists, False otherwise
        """
        from datetime import datetime

        try:
            from services.github_api_client import get_github_client
            from config.manager import config_manager

            gh = get_github_client()
            project_config = config_manager.get_project_config(project_name)

            if not project_config:
                logger.warning(f"No project config for {project_name}")
                return False  # Can't verify, assume no output

            agent = execution.get('agent')
            completed_at = execution.get('completed_at')

            if not agent or not completed_at:
                logger.debug("Missing agent or completed_at in execution record")
                return False

            # Parse completion timestamp
            completed_at_str = completed_at.replace('Z', '+00:00')
            completed_dt = datetime.fromisoformat(completed_at_str)

            # Check for comments after completion time
            org = project_config['github']['org']
            repo = project_config['github']['repo']
            endpoint = f'repos/{org}/{repo}/issues/{issue_number}/comments'

            success, comments = gh.rest('GET', endpoint)

            if not success:
                logger.warning(f"Failed to fetch comments for {project_name}/#{issue_number}")
                return False  # Can't verify, assume no output to be safe

            # Check if any comment was created after completion
            for comment in comments:
                try:
                    created_at = datetime.fromisoformat(comment['created_at'].replace('Z', '+00:00'))
                    if created_at > completed_dt:
                        # Found a comment after execution - assume it's the output
                        logger.debug(
                            f"Found GitHub comment after execution completion for "
                            f"{project_name}/#{issue_number}"
                        )
                        return True
                except Exception as e:
                    logger.debug(f"Error parsing comment timestamp: {e}")
                    continue

            logger.debug(f"No GitHub output found for {project_name}/#{issue_number} after {completed_dt}")
            return False

        except Exception as e:
            logger.error(f"Error checking GitHub output for {project_name}/#{issue_number}: {e}")
            return False  # Can't verify, mark as no output to be safe

    def _try_recover_result_from_redis(self, project_name, issue_number, agent, column, execution):
        """
        Check Redis for a persisted agent result before marking execution as failed.

        The docker-claude-wrapper.py writes final results to Redis
        (agent_result:{project}:{issue_number}:{task_id}) with a 2-hour TTL
        before the container exits. When the orchestrator restarts and the
        container is gone (due to --rm), the result may still be in Redis.

        Two recovery strategies:
        1. Primary: Exact key lookup when execution has a stamped task_id (O(1), no ambiguity)
        2. Fallback: Wildcard scan with timestamp validation for old records without task_id

        Returns True if a result was recovered (execution dict is updated in place),
        False otherwise.
        """
        try:
            import redis
            import json

            redis_client = redis.Redis(
                host='redis', port=6379, decode_responses=True,
                socket_timeout=5, socket_connect_timeout=5
            )

            # Primary: exact key lookup when task_id is stamped on the execution
            execution_task_id = execution.get('task_id')
            if execution_task_id:
                exact_key = f"agent_result:{project_name}:{issue_number}:{execution_task_id}"
                result_json = redis_client.get(exact_key)
                if result_json:
                    try:
                        result_data = json.loads(result_json)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to parse Redis result at {exact_key}: {e}")
                        return False

                    if result_data.get('agent') != agent:
                        logger.warning(
                            f"Redis result at {exact_key} has agent={result_data.get('agent')}, "
                            f"expected {agent} — skipping recovery"
                        )
                        return False

                    recovered = self._apply_redis_result(
                        execution, result_data, exact_key, project_name,
                        issue_number, agent, column, redis_client
                    )
                    return recovered

                # No result for this specific execution — it never produced output
                logger.debug(
                    f"No Redis result for exact key {exact_key} — "
                    f"execution never completed or result already expired"
                )
                return False

            # Fallback: wildcard scan for old execution records without task_id
            # Add timestamp validation to prevent cross-execution contamination
            pattern = f"agent_result:{project_name}:{issue_number}:*"
            result_keys = list(redis_client.scan_iter(match=pattern, count=100))

            if not result_keys:
                return False

            for redis_key in result_keys:
                try:
                    result_json = redis_client.get(redis_key)
                    if not result_json:
                        continue

                    result_data = json.loads(result_json)

                    # Strict agent match
                    if result_data.get('agent') != agent:
                        continue

                    # Timestamp validation: reject results from earlier executions
                    execution_timestamp = execution.get('timestamp')
                    completed_at = result_data.get('completed_at')
                    if execution_timestamp and completed_at:
                        try:
                            exec_dt = datetime.fromisoformat(
                                execution_timestamp.replace('Z', '+00:00')
                            )
                            completed_dt = datetime.fromisoformat(
                                completed_at.replace('Z', '+00:00')
                            )
                            if completed_dt < exec_dt:
                                logger.info(
                                    f"Skipping stale Redis result at {redis_key}: "
                                    f"completed_at={completed_at} < execution_timestamp={execution_timestamp}"
                                )
                                continue
                        except (ValueError, TypeError) as e:
                            logger.warning(
                                f"Unparseable timestamps for {redis_key}: "
                                f"execution_timestamp={execution_timestamp!r}, "
                                f"completed_at={completed_at!r}: {e}. "
                                f"Skipping result to prevent cross-execution contamination."
                            )
                            continue

                    recovered = self._apply_redis_result(
                        execution, result_data, redis_key, project_name,
                        issue_number, agent, column, redis_client
                    )
                    if recovered:
                        return True

                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Failed to parse Redis result at {redis_key}: {e}")
                    continue

            return False

        except ImportError:
            logger.debug("Redis not available for result recovery")
            return False
        except Exception as e:
            # Distinguish expected connection failures from unexpected bugs
            if isinstance(e, OSError):
                logger.warning(f"Redis unavailable for result recovery: {e}")
            else:
                logger.error(
                    f"Unexpected error during Redis result recovery for "
                    f"{project_name}/#{issue_number} {agent}: {e}",
                    exc_info=True
                )
            return False

    def _apply_redis_result(self, execution, result_data, redis_key, project_name,
                            issue_number, agent, column, redis_client):
        """
        Apply a recovered Redis result to an execution record.

        Shared by both exact-key and wildcard-scan recovery paths.
        Returns True if result was applied, False if it was skipped.
        """
        exit_code = result_data.get('exit_code')
        if exit_code is None:
            logger.warning(
                f"Redis result at {redis_key} has no exit_code field, "
                f"cannot determine outcome — skipping recovery"
            )
            return False

        if exit_code == 0:
            execution['outcome'] = 'success'
            logger.info(
                f"Recovered successful result from Redis for "
                f"{project_name}/#{issue_number} {agent} in {column} "
                f"(key: {redis_key})"
            )
        else:
            execution['outcome'] = 'failure'
            output = result_data.get('output', '') or ''
            # Truncate output for error message
            error_snippet = output[-500:] if len(output) > 500 else output
            execution['error'] = (
                f"Agent exited with code {exit_code}. "
                f"Result recovered from Redis after container exited. "
                f"Output tail: {error_snippet}"
            )
            logger.warning(
                f"Recovered failed result from Redis for "
                f"{project_name}/#{issue_number} {agent} in {column} "
                f"(exit_code={exit_code}, key: {redis_key})"
            )

        # Delete the key after processing to prevent reprocessing.
        try:
            redis_client.delete(redis_key)
        except Exception as del_err:
            logger.warning(
                f"Failed to delete recovered Redis key {redis_key}: {del_err}. "
                f"Key will expire via TTL or be reprocessed next cycle."
            )

        return True

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
                            agent_ps_result = subprocess.run(
                                ['docker', 'ps', '--filter', f'name=claude-agent-{project_name}-',
                                 '--format', '{{.Names}}'],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            agent_container_names = [n for n in agent_ps_result.stdout.strip().split('\n') if n]
                            has_agent_container = bool(agent_container_names)

                            # Also check for RUNNING repair cycle containers (format: repair-cycle-{project}-{issue}-{run_id})
                            # Important: Use 'docker ps' not 'docker ps -a' to only find running containers
                            repair_ps_result = subprocess.run(
                                ['docker', 'ps', '--filter', f'name=repair-cycle-{project_name}-{issue_number}',
                                 '--format', '{{.Names}}'],
                                capture_output=True,
                                text=True,
                                timeout=5
                            )
                            repair_container_names = [n for n in repair_ps_result.stdout.strip().split('\n') if n]
                            has_repair_cycle_container = bool(repair_container_names)

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
                                all_container_names = agent_container_names + repair_container_names
                                logger.warning(
                                    f"Container exists in Docker but not in Redis tracking for "
                                    f"{project_name}/#{issue_number} {agent} - attempting repair"
                                )
                                # Enrich execution dict with state-level metadata
                                # (issue_number and project are stored at state level, not in execution record)
                                execution_with_metadata = {
                                    **execution,
                                    'issue_number': issue_number,
                                    'project': project_name
                                }
                                self._repair_missing_redis_tracking(
                                    execution=execution_with_metadata,
                                    project=project_name,
                                    container_names=all_container_names
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
                                # Before marking as failure, check if the container completed
                                # and persisted its result to Redis (written by docker-claude-wrapper.py)
                                recovered = self._try_recover_result_from_redis(
                                    project_name, issue_number, agent, column, execution
                                )

                                if not recovered:
                                    # No container AND no Redis result — truly lost execution
                                    execution['outcome'] = 'failure'
                                    execution['error'] = (
                                        'Agent execution interrupted. Container no longer exists and execution '
                                        'state was not updated. This may indicate the agent crashed, was killed, '
                                        'or the orchestrator was restarted before outcome could be recorded.'
                                    )

                                    # Special handling for dev_environment_verifier agent
                                    # Only reset to UNVERIFIED if the current state is NOT already VERIFIED.
                                    # A later verifier execution may have already succeeded, in which case
                                    # this stuck record is from a superseded execution and should not clobber
                                    # the verified state.
                                    if agent == 'dev_environment_verifier':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            current_status = dev_container_state.get_status(project_name)
                                            if current_status == DevContainerStatus.VERIFIED:
                                                logger.info(
                                                    f"Stuck dev_environment_verifier detected for {project_name}, "
                                                    f"but dev container is already VERIFIED — skipping reset"
                                                )
                                            else:
                                                logger.info(
                                                    f"Stuck dev_environment_verifier detected for {project_name}, "
                                                    f"resetting dev container state to UNVERIFIED"
                                                )
                                                dev_container_state.set_status(
                                                    project_name=project_name,
                                                    status=DevContainerStatus.UNVERIFIED,
                                                    error_message="Verification container died before completion"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    # Special handling for dev_environment_setup agent
                                    # Only reset if not already VERIFIED (a verifier may have already confirmed).
                                    if agent == 'dev_environment_setup':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            current_status = dev_container_state.get_status(project_name)
                                            if current_status == DevContainerStatus.VERIFIED:
                                                logger.info(
                                                    f"Stuck dev_environment_setup detected for {project_name}, "
                                                    f"but dev container is already VERIFIED — skipping reset"
                                                )
                                            else:
                                                logger.info(
                                                    f"Stuck dev_environment_setup detected for {project_name}, "
                                                    f"resetting dev container state to UNVERIFIED"
                                                )
                                                dev_container_state.set_status(
                                                    project_name=project_name,
                                                    status=DevContainerStatus.UNVERIFIED,
                                                    error_message="Setup container died before completion"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                modified = True
                                cleaned_count += 1

                                # Emit events based on recovered outcome for UX visibility
                                if execution['outcome'] == 'success':
                                    # Agent completed successfully but result was only recovered
                                    # from Redis after restart. The normal result-processing chain
                                    # (posting output to GitHub, triggering progression) was skipped.
                                    # Monitoring loop will re-detect card position and continue pipeline.
                                    logger.info(
                                        f"Reconciled successful execution from Redis: "
                                        f"{project_name}/#{issue_number} {agent} in {column}. "
                                        f"Monitoring loop will re-detect card position and continue pipeline."
                                    )

                                    # Verify dev container state consistency for dev_environment_verifier
                                    # The agent should have already set state to VERIFIED before exiting
                                    if agent == 'dev_environment_verifier':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            current_status = dev_container_state.get_status(project_name)
                                            if current_status != DevContainerStatus.VERIFIED:
                                                logger.warning(
                                                    f"Dev environment verifier succeeded for {project_name} but "
                                                    f"state is {current_status.value}, expected VERIFIED. "
                                                    f"Correcting state now."
                                                )
                                                dev_container_state.set_status(
                                                    project_name=project_name,
                                                    status=DevContainerStatus.VERIFIED,
                                                    image_name=f"{project_name}-agent:latest"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to verify dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    try:
                                        from monitoring.decision_events import DecisionEventEmitter
                                        from monitoring.observability import get_observability_manager
                                        from services.pipeline_run import get_pipeline_run_manager

                                        obs = get_observability_manager()
                                        decision_events = DecisionEventEmitter(obs)
                                        pipeline_run_mgr = get_pipeline_run_manager()
                                        active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

                                        decision_events.emit_execution_state_reconciled(
                                            agent=agent,
                                            project=project_name,
                                            issue_number=issue_number,
                                            column=column,
                                            recovered_outcome='success',
                                            context={
                                                'timestamp': timestamp,
                                                'recovered_from_redis': True,
                                            },
                                            pipeline_run_id=active_run.id if active_run else None
                                        )
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to emit reconciliation event for "
                                            f"{project_name}/#{issue_number} ({agent}): {e}",
                                            exc_info=True
                                        )
                                else:
                                    # Failure path — applies to both recovered-from-Redis failures
                                    # and truly lost executions where no result was found
                                    logger.warning(
                                        f"Marked stuck execution as failed: {project_name}/#{issue_number} "
                                        f"{agent} in {column} "
                                        f"({'recovered from Redis' if recovered else 'no container found, outcome not recorded'}). "
                                        f"Pipeline is now blocked - manual intervention required."
                                    )

                                    # Special handling for dev_environment_verifier agent failures
                                    # BUG FIX: Synchronize dev container state when verification fails
                                    # This handles both: (1) recovered-from-Redis failures, (2) non-recovered failures
                                    # Note: Non-recovered failures already handled above at line ~1460, but
                                    # this ensures recovered failures also update the state
                                    if agent == 'dev_environment_verifier' and recovered:
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            logger.info(
                                                f"Dev environment verification failed for {project_name} "
                                                f"(recovered from Redis with failure), marking as BLOCKED"
                                            )
                                            # Extract error from execution for context
                                            error_msg = execution.get('error', 'Verification failed')[:200]
                                            dev_container_state.set_status(
                                                project_name=project_name,
                                                status=DevContainerStatus.BLOCKED,
                                                error_message=error_msg
                                            )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    # Special handling for dev_environment_setup agent failures
                                    # Reset to UNVERIFIED so setup can be retried automatically
                                    if agent == 'dev_environment_setup':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            logger.info(
                                                f"Dev environment setup failed for {project_name}, "
                                                f"resetting dev container state to UNVERIFIED"
                                            )
                                            error_msg = execution.get('error', 'Setup failed')[:200]
                                            dev_container_state.set_status(
                                                project_name=project_name,
                                                status=DevContainerStatus.UNVERIFIED,
                                                error_message=error_msg
                                            )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    # CRITICAL CHANGE: DO NOT call end_pipeline_run()
                                    #
                                    # Previous behavior (REMOVED): Called end_pipeline_run() which:
                                    # - Released the pipeline lock
                                    # - Processed the next waiting issue in queue
                                    # - This caused issues to be skipped on failure (violated FIFO ordering)
                                    #
                                    # New behavior: DO NOT end the pipeline run when agent fails:
                                    # - Pipeline run remains active (keeps the lock)
                                    # - Failed issue blocks the queue (enforces FIFO ordering)
                                    # - Requires manual intervention to unblock:
                                    #   * Move issue to Backlog (releases lock, allows retry)
                                    #   * Move to non-trigger column (releases lock, skips issue)
                                    #   * Close issue (releases lock, abandons work)
                                    #
                                    # Emit comprehensive failure events for UX visibility
                                    try:
                                        from monitoring.decision_events import DecisionEventEmitter
                                        from monitoring.observability import get_observability_manager, EventType
                                        from services.pipeline_run import get_pipeline_run_manager

                                        obs = get_observability_manager()
                                        decision_events = DecisionEventEmitter(obs)
                                        pipeline_run_mgr = get_pipeline_run_manager()
                                        active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

                                        # Emit error decision event with blocking context
                                        decision_events.emit_error_decision(
                                            error_type='ExecutionContainerLost',
                                            error_message=execution['error'],
                                            context={
                                                'project': project_name,
                                                'issue_number': issue_number,
                                                'agent': agent,
                                                'column': column,
                                                'timestamp': timestamp,
                                                'recovered_from_redis': recovered,
                                                'blocking_pipeline': True,
                                                'lock_held': True,
                                            },
                                            recovery_action='manual_intervention_required',
                                            success=False,
                                            project=project_name,
                                            pipeline_run_id=active_run.id if active_run else None
                                        )

                                        # Emit specific pipeline blocked event
                                        if active_run:
                                            obs.emit(
                                                EventType.PIPELINE_RUN_FAILED,
                                                "pipeline_lifecycle",
                                                active_run.id,
                                                project_name,
                                                {
                                                    "pipeline_run_id": active_run.id,
                                                    "issue_number": issue_number,
                                                    "board": active_run.board,
                                                    "reason": "agent_execution_failed",
                                                    "error": execution['error'],
                                                    "blocking_pipeline": True,
                                                    "requires_manual_intervention": True,
                                                    "recovered_from_redis": recovered,
                                                },
                                                pipeline_run_id=active_run.id
                                            )

                                        logger.info(
                                            f"Emitted failure events for {project_name}/#{issue_number}. "
                                            f"UX should display: 'Pipeline blocked - manual intervention required'"
                                        )

                                    except Exception as e:
                                        logger.error(f"Failed to emit execution failure events: {e}", exc_info=True)
                                        # Continue anyway - the execution state is still marked as failed
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

    def abandon_stale_in_progress_entries(
        self,
        project_name: str,
        issue_number: int,
        active_task_ids: set
    ) -> int:
        """
        Mark orphaned in_progress entries as 'abandoned' after an orchestrator restart.

        An entry is considered stale (and safe to abandon) when:
        - It has no task_id (was never assigned to a container — a pure probe entry), OR
        - Its task_id is not in active_task_ids (the container is no longer running)

        Entries whose task_id IS in active_task_ids are left untouched because
        their container was successfully recovered.

        Args:
            project_name: Project name
            issue_number: Issue number
            active_task_ids: Set of task_ids belonging to currently running/recovered containers

        Returns:
            Number of entries marked as abandoned
        """
        state = self.load_state(project_name, issue_number)
        abandoned = 0

        for execution in state['execution_history']:
            if execution.get('outcome') != 'in_progress':
                continue

            task_id = execution.get('task_id')
            if task_id and task_id in active_task_ids:
                # Container is still running — leave this entry alone
                continue

            execution['outcome'] = 'abandoned'
            execution['error'] = 'Orchestrator restarted without completing this execution.'
            abandoned += 1

        if abandoned:
            self.save_state(project_name, issue_number, state)
            logger.info(
                f"Abandoned {abandoned} stale in_progress entries for "
                f"{project_name}/#{issue_number}"
            )

        return abandoned


# Global instance
work_execution_tracker = WorkExecutionStateTracker()

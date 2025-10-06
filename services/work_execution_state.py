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
            state_dir = Path("state/execution_history")

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
            with open(state_file, 'r') as f:
                state = yaml.safe_load(f)
                # Ensure all expected keys exist
                state.setdefault('execution_history', [])
                state.setdefault('status_changes', [])
                return state
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
        """Save execution state for an issue"""
        state_file = self.get_state_file(project_name, issue_number)

        try:
            state['last_updated'] = datetime.now(timezone.utc).isoformat()
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
        """Record the start of work execution"""
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
        logger.warning(
            f"No in_progress execution found for {agent} in {column}, "
            f"creating new record with outcome {outcome}"
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


# Global instance
work_execution_tracker = WorkExecutionStateTracker()

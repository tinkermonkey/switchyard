"""
Conversational Session State Management

Persists Claude Code session IDs for conversational feedback loops.
This enables session continuity across orchestrator restarts.
"""

import yaml
import logging
from pathlib import Path
from typing import Optional, Dict
from datetime import datetime, timezone, timedelta

logger = logging.getLogger(__name__)


class ConversationalSessionState:
    """State for a conversational session"""

    def __init__(
        self,
        session_id: str,
        issue_number: int,
        agent: str,
        project_name: str,
        last_interaction: str,
        workspace_type: str = 'issues'
    ):
        self.session_id = session_id
        self.issue_number = issue_number
        self.agent = agent
        self.project_name = project_name
        self.last_interaction = last_interaction
        self.workspace_type = workspace_type


class ConversationalSessionStateManager:
    """Manages Claude Code session persistence for conversational loops"""

    def __init__(self, state_dir: Path = None):
        """Initialize conversational session state manager"""
        if state_dir is None:
            # CRITICAL: Use absolute path to orchestrator's state directory
            # This prevents state from being created inside project directories when
            # agents execute with project working directory
            import os
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "conversational_sessions"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"ConversationalSessionStateManager initialized with state_dir: {state_dir}")

    def get_state_file(self, project_name: str, issue_number: int) -> Path:
        """Get the state file path for a conversational session"""
        return self.state_dir / f"{project_name}_issue_{issue_number}.yaml"

    def save_session(
        self,
        project_name: str,
        issue_number: int,
        session_id: str,
        agent: str,
        workspace_type: str = 'issues'
    ):
        """
        Save or update a conversational session state

        Args:
            project_name: Name of the project
            issue_number: Issue number
            session_id: Claude Code session ID
            agent: Agent name
            workspace_type: 'issues' or 'discussions'
        """
        state_file = self.get_state_file(project_name, issue_number)

        state = {
            'session_id': session_id,
            'issue_number': issue_number,
            'agent': agent,
            'project_name': project_name,
            'workspace_type': workspace_type,
            'last_interaction': datetime.now(timezone.utc).isoformat(),
            'updated_at': datetime.now(timezone.utc).isoformat()
        }

        try:
            with open(state_file, 'w') as f:
                yaml.dump(state, f, default_flow_style=False)

            logger.info(f"Saved conversational session for {project_name}/#{issue_number}: {session_id}")

        except Exception as e:
            logger.error(f"Failed to save session state: {e}")

    def load_session(
        self,
        project_name: str,
        issue_number: int,
        max_age_hours: int = 24
    ) -> Optional[ConversationalSessionState]:
        """
        Load a conversational session state

        Args:
            project_name: Name of the project
            issue_number: Issue number
            max_age_hours: Maximum session age in hours (default: 24)

        Returns:
            ConversationalSessionState if found and not stale, else None
        """
        state_file = self.get_state_file(project_name, issue_number)

        if not state_file.exists():
            logger.debug(f"No session state file for {project_name}/#{issue_number}")
            return None

        try:
            with open(state_file, 'r') as f:
                state = yaml.safe_load(f)

            # Check staleness
            last_interaction = datetime.fromisoformat(state['last_interaction'])
            age = datetime.now(timezone.utc) - last_interaction

            if age > timedelta(hours=max_age_hours):
                logger.info(
                    f"Session for {project_name}/#{issue_number} is stale "
                    f"(age: {age.total_seconds() / 3600:.1f} hours), ignoring"
                )
                return None

            logger.info(
                f"Loaded session for {project_name}/#{issue_number}: {state['session_id']} "
                f"(age: {age.total_seconds() / 60:.1f} minutes)"
            )

            return ConversationalSessionState(
                session_id=state['session_id'],
                issue_number=state['issue_number'],
                agent=state['agent'],
                project_name=state['project_name'],
                last_interaction=state['last_interaction'],
                workspace_type=state.get('workspace_type', 'issues')
            )

        except Exception as e:
            logger.error(f"Failed to load session state for {project_name}/#{issue_number}: {e}")
            return None

    def delete_session(self, project_name: str, issue_number: int):
        """
        Delete a conversational session state

        Args:
            project_name: Name of the project
            issue_number: Issue number
        """
        state_file = self.get_state_file(project_name, issue_number)

        if state_file.exists():
            try:
                state_file.unlink()
                logger.info(f"Deleted session state for {project_name}/#{issue_number}")
            except Exception as e:
                logger.error(f"Failed to delete session state: {e}")

    def update_last_interaction(self, project_name: str, issue_number: int):
        """
        Update the last_interaction timestamp for a session

        Args:
            project_name: Name of the project
            issue_number: Issue number
        """
        state_file = self.get_state_file(project_name, issue_number)

        if not state_file.exists():
            return

        try:
            with open(state_file, 'r') as f:
                state = yaml.safe_load(f)

            state['last_interaction'] = datetime.now(timezone.utc).isoformat()
            state['updated_at'] = datetime.now(timezone.utc).isoformat()

            with open(state_file, 'w') as f:
                yaml.dump(state, f, default_flow_style=False)

            logger.debug(f"Updated last_interaction for {project_name}/#{issue_number}")

        except Exception as e:
            logger.error(f"Failed to update last_interaction: {e}")

    def get_all_sessions(self) -> Dict[str, ConversationalSessionState]:
        """
        Get all active conversational sessions

        Returns:
            Dict mapping "project/issue" to ConversationalSessionState
        """
        sessions = {}

        for state_file in self.state_dir.glob("*.yaml"):
            try:
                with open(state_file, 'r') as f:
                    state = yaml.safe_load(f)

                key = f"{state['project_name']}/#{state['issue_number']}"
                sessions[key] = ConversationalSessionState(
                    session_id=state['session_id'],
                    issue_number=state['issue_number'],
                    agent=state['agent'],
                    project_name=state['project_name'],
                    last_interaction=state['last_interaction'],
                    workspace_type=state.get('workspace_type', 'issues')
                )
            except Exception as e:
                logger.error(f"Failed to load session from {state_file}: {e}")

        return sessions


# Global instance
conversational_session_state = ConversationalSessionStateManager()

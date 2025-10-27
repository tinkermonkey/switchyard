"""
GitHub State Manager for the Claude Code Orchestrator

This module manages the runtime state of GitHub projects, including:
- GitHub project IDs, board IDs, column IDs
- Configuration sync status
- State persistence and recovery
- Configuration reconciliation with GitHub
"""

import os
import yaml
import hashlib
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict
from datetime import datetime
import logging

from .manager import ConfigManager, ProjectConfig, WorkflowTemplate

logger = logging.getLogger(__name__)


@dataclass
class GitHubColumn:
    """GitHub project column state"""
    name: str
    id: str
    node_id: str


@dataclass
class GitHubBoard:
    """GitHub project board state"""
    project_number: int
    project_id: str
    node_id: str
    name: str
    columns: List[GitHubColumn]
    status_field_id: Optional[str] = None  # GraphQL field ID for the Status field


@dataclass
class GitHubProjectState:
    """Complete GitHub project state"""
    project_name: str
    org: str
    repo: str
    boards: Dict[str, GitHubBoard]
    labels_created: List[str]
    last_sync: str
    sync_hash: str
    issue_discussion_links: Optional[Dict[int, str]] = None  # issue_number -> discussion_id
    discussion_issue_links: Optional[Dict[str, int]] = None  # discussion_id -> issue_number


class GitHubStateManager:
    """
    Manages GitHub project state and configuration reconciliation
    """

    def __init__(self, state_root: Optional[str] = None, config_manager: Optional[ConfigManager] = None):
        """Initialize state manager

        Args:
            state_root: Root directory for state files. Defaults to ./state
            config_manager: Configuration manager instance
        """
        if state_root is None:
            state_root = Path(__file__).parent.parent / "state"

        self.state_root = Path(state_root)
        self.projects_state_dir = self.state_root / "projects"
        self.orchestrator_state_dir = self.state_root / "orchestrator"

        # Create directories if they don't exist
        self.projects_state_dir.mkdir(parents=True, exist_ok=True)
        self.orchestrator_state_dir.mkdir(parents=True, exist_ok=True)

        self.config_manager = config_manager or ConfigManager()

    def _get_project_state_file(self, project_name: str) -> Path:
        """Get the state file path for a project"""
        project_dir = self.projects_state_dir / project_name
        project_dir.mkdir(exist_ok=True)
        return project_dir / "github_state.yaml"

    def _calculate_config_hash(self, project_config: ProjectConfig) -> str:
        """Calculate hash of project configuration for sync tracking"""
        config_dict = {
            'pipelines': [asdict(p) for p in project_config.pipelines if p.active],
            'pipeline_routing': project_config.pipeline_routing,
            'github': project_config.github
        }
        config_str = yaml.dump(config_dict, sort_keys=True)
        return hashlib.sha256(config_str.encode()).hexdigest()[:16]

    def load_project_state(self, project_name: str) -> Optional[GitHubProjectState]:
        """Load GitHub state for a project"""
        state_file = self._get_project_state_file(project_name)

        if not state_file.exists():
            return None

        try:
            with open(state_file, 'r') as f:
                data = yaml.safe_load(f)

            # Parse boards
            boards = {}
            for board_name, board_data in data['github_state']['boards'].items():
                columns = []
                for col_data in board_data['columns']:
                    columns.append(GitHubColumn(
                        name=col_data['name'],
                        id=col_data['id'],
                        node_id=col_data['node_id']
                    ))

                boards[board_name] = GitHubBoard(
                    project_number=board_data['project_number'],
                    project_id=board_data['project_id'],
                    node_id=board_data['node_id'],
                    name=board_name,
                    columns=columns,
                    status_field_id=board_data.get('status_field_id')
                )

            # Parse issue/discussion links
            issue_discussion_links = {}
            discussion_issue_links = {}
            if 'issue_discussion_links' in data['github_state']:
                for issue_num_str, discussion_id in data['github_state']['issue_discussion_links'].items():
                    issue_discussion_links[int(issue_num_str)] = discussion_id
            if 'discussion_issue_links' in data['github_state']:
                for discussion_id, issue_num in data['github_state']['discussion_issue_links'].items():
                    discussion_issue_links[discussion_id] = int(issue_num)

            return GitHubProjectState(
                project_name=project_name,
                org=data['github_state']['org'],
                repo=data['github_state']['repo'],
                boards=boards,
                labels_created=data['github_state'].get('labels_created', []),
                last_sync=data['github_state']['last_sync'],
                sync_hash=data['github_state']['sync_hash'],
                issue_discussion_links=issue_discussion_links,
                discussion_issue_links=discussion_issue_links
            )

        except Exception as e:
            logger.error(f"Failed to load state for project {project_name}: {e}")
            return None

    def save_project_state(self, state: GitHubProjectState):
        """Save GitHub state for a project"""
        state_file = self._get_project_state_file(state.project_name)

        # Convert to serializable format
        boards_data = {}
        for board_name, board in state.boards.items():
            board_dict = {
                'project_number': board.project_number,
                'project_id': board.project_id,
                'node_id': board.node_id,
                'columns': [
                    {
                        'name': col.name,
                        'id': col.id,
                        'node_id': col.node_id
                    }
                    for col in board.columns
                ]
            }
            if board.status_field_id:
                board_dict['status_field_id'] = board.status_field_id
            boards_data[board_name] = board_dict

        # Convert issue/discussion links to serializable format
        issue_discussion_links = {}
        discussion_issue_links = {}
        if state.issue_discussion_links:
            issue_discussion_links = {str(k): v for k, v in state.issue_discussion_links.items()}
        if state.discussion_issue_links:
            discussion_issue_links = state.discussion_issue_links

        data = {
            'github_state': {
                'org': state.org,
                'repo': state.repo,
                'boards': boards_data,
                'labels_created': state.labels_created,
                'last_sync': state.last_sync,
                'sync_hash': state.sync_hash,
                'issue_discussion_links': issue_discussion_links,
                'discussion_issue_links': discussion_issue_links
            }
        }

        try:
            with open(state_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)
            logger.info(f"Saved state for project {state.project_name}")
        except Exception as e:
            logger.error(f"Failed to save state for project {state.project_name}: {e}")
            raise

    def is_state_fresh(self, project_name: str, max_age_hours: int = 24) -> bool:
        """
        Check if project state is fresh (recently synced and valid).

        Args:
            project_name: Name of the project
            max_age_hours: Maximum age in hours to consider state fresh (default: 24)

        Returns:
            True if state exists, is complete, and was synced within max_age_hours
        """
        try:
            state = self.load_project_state(project_name)
            if state is None:
                return False

            # Check if state has all required components
            if not state.boards:
                logger.debug(f"State for {project_name} is not fresh: no boards")
                return False

            # Check if all boards have complete data
            for board_name, board in state.boards.items():
                if not board.project_id or not board.status_field_id or not board.columns:
                    logger.debug(f"State for {project_name} is not fresh: incomplete board '{board_name}'")
                    return False

            # Check if state is recent enough
            if state.last_sync:
                try:
                    last_sync_time = datetime.fromisoformat(state.last_sync.replace('Z', '+00:00'))
                    age_hours = (datetime.now(last_sync_time.tzinfo) - last_sync_time).total_seconds() / 3600

                    if age_hours > max_age_hours:
                        logger.debug(f"State for {project_name} is stale: {age_hours:.1f} hours old (max: {max_age_hours})")
                        return False

                    logger.debug(f"State for {project_name} is fresh: {age_hours:.1f} hours old")
                    return True
                except Exception as e:
                    logger.warning(f"Could not parse last_sync timestamp for {project_name}: {e}")
                    return False

            return False
        except Exception as e:
            logger.error(f"Error checking state freshness for {project_name}: {e}")
            return False

    def needs_reconciliation(self, project_name: str) -> bool:
        """Check if project configuration has changed and needs reconciliation"""
        try:
            project_config = self.config_manager.get_project_config(project_name)
            current_hash = self._calculate_config_hash(project_config)

            state = self.load_project_state(project_name)
            if state is None:
                # No state exists, needs initial setup
                return True

            return state.sync_hash != current_hash
        except Exception as e:
            logger.error(f"Error checking reconciliation status for {project_name}: {e}")
            return True

    def create_initial_state(self, project_name: str) -> GitHubProjectState:
        """Create initial state structure for a project"""
        project_config = self.config_manager.get_project_config(project_name)
        current_hash = self._calculate_config_hash(project_config)

        return GitHubProjectState(
            project_name=project_name,
            org=project_config.github['org'],
            repo=project_config.github['repo'],
            boards={},
            labels_created=[],
            last_sync=datetime.utcnow().isoformat() + 'Z',
            sync_hash=current_hash,
            issue_discussion_links={},
            discussion_issue_links={}
        )

    def update_board_state(self, project_name: str, board_name: str,
                          project_number: int, project_id: str, node_id: str,
                          columns: List[Dict[str, str]]):
        """Update board state with GitHub API response data"""
        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

        # Extract status_field_id from the first column if present
        status_field_id = None
        if columns and 'status_field_id' in columns[0]:
            status_field_id = columns[0]['status_field_id']

        github_columns = []
        for col in columns:
            github_columns.append(GitHubColumn(
                name=col['name'],
                id=col['id'],
                node_id=col['node_id']
            ))

        state.boards[board_name] = GitHubBoard(
            project_number=project_number,
            project_id=project_id,
            node_id=node_id,
            name=board_name,
            columns=github_columns,
            status_field_id=status_field_id
        )

        state.last_sync = datetime.utcnow().isoformat() + 'Z'
        self.save_project_state(state)

    def mark_labels_created(self, project_name: str, labels: List[str]):
        """Mark labels as created in GitHub"""
        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

        for label in labels:
            if label not in state.labels_created:
                state.labels_created.append(label)

        state.last_sync = datetime.utcnow().isoformat() + 'Z'
        self.save_project_state(state)

    def refresh_board_field_ids(self, project_name: str, board_name: str) -> bool:
        """Refresh the field and option IDs for a board by querying GitHub

        This is used to update boards that were created with placeholder IDs
        """
        try:
            import subprocess
            import json

            state = self.load_project_state(project_name)
            if not state:
                logger.error(f"No state found for project {project_name}")
                return False

            board = state.boards.get(board_name)
            if not board:
                logger.error(f"No board found: {board_name}")
                return False

            # Query GitHub for the project field list
            cmd = ['gh', 'project', 'field-list', str(board.project_number),
                   '--owner', state.org, '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            fields_data = json.loads(result.stdout)

            # Find the Status field
            status_field = None
            for field in fields_data.get('fields', []):
                if field.get('name') == 'Status' and field.get('type') == 'ProjectV2SingleSelectField':
                    status_field = field
                    break

            if not status_field:
                logger.error(f"Status field not found for board {board_name}")
                return False

            # Update the board's status_field_id
            board.status_field_id = status_field['id']

            # Update column IDs with actual option IDs
            options = {opt['name']: opt['id'] for opt in status_field.get('options', [])}
            for col in board.columns:
                if col.name in options:
                    col.id = options[col.name]
                    col.node_id = options[col.name]

            # Save updated state
            self.save_project_state(state)
            logger.info(f"Refreshed field IDs for board {board_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to refresh board field IDs: {e}")
            return False

    def mark_synchronized(self, project_name: str):
        """Mark project as synchronized with current configuration"""
        project_config = self.config_manager.get_project_config(project_name)
        current_hash = self._calculate_config_hash(project_config)

        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

        state.sync_hash = current_hash
        state.last_sync = datetime.utcnow().isoformat() + 'Z'
        self.save_project_state(state)

    def get_board_by_name(self, project_name: str, board_name: str) -> Optional[GitHubBoard]:
        """Get board state by name"""
        state = self.load_project_state(project_name)
        if state is None:
            return None

        return state.boards.get(board_name)

    def get_column_by_name(self, project_name: str, board_name: str, column_name: str) -> Optional[GitHubColumn]:
        """Get column state by name"""
        board = self.get_board_by_name(project_name, board_name)
        if board is None:
            return None

        for column in board.columns:
            if column.name == column_name:
                return column

        return None

    def list_managed_projects(self) -> List[str]:
        """List all projects that have state files"""
        if not self.projects_state_dir.exists():
            return []

        projects = []
        for project_dir in self.projects_state_dir.iterdir():
            if project_dir.is_dir() and (project_dir / "github_state.yaml").exists():
                projects.append(project_dir.name)

        return sorted(projects)

    def cleanup_project_state(self, project_name: str):
        """Remove all state files for a project"""
        project_dir = self.projects_state_dir / project_name
        if project_dir.exists():
            import shutil
            shutil.rmtree(project_dir)
            logger.info(f"Cleaned up state for project {project_name}")

    def backup_state(self, project_name: str) -> str:
        """Create a backup of project state and return backup path"""
        state_file = self._get_project_state_file(project_name)
        if not state_file.exists():
            return ""

        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        backup_file = state_file.parent / f"github_state_backup_{timestamp}.yaml"

        import shutil
        shutil.copy2(state_file, backup_file)
        logger.info(f"Created state backup: {backup_file}")
        return str(backup_file)

    def link_issue_to_discussion(self, project_name: str, issue_number: int, discussion_id: str):
        """Create bidirectional link between issue and discussion"""
        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

        if state.issue_discussion_links is None:
            state.issue_discussion_links = {}
        if state.discussion_issue_links is None:
            state.discussion_issue_links = {}

        state.issue_discussion_links[issue_number] = discussion_id
        state.discussion_issue_links[discussion_id] = issue_number

        self.save_project_state(state)
        logger.info(f"Linked issue #{issue_number} to discussion {discussion_id}")

    def get_discussion_for_issue(self, project_name: str, issue_number: int) -> Optional[str]:
        """Get discussion ID for an issue"""
        state = self.load_project_state(project_name)
        if state is None or state.issue_discussion_links is None:
            return None

        return state.issue_discussion_links.get(issue_number)

    def get_issue_for_discussion(self, project_name: str, discussion_id: str) -> Optional[int]:
        """Get issue number for a discussion"""
        state = self.load_project_state(project_name)
        if state is None or state.discussion_issue_links is None:
            return None

        return state.discussion_issue_links.get(discussion_id)

    def unlink_issue_discussion(self, project_name: str, issue_number: int):
        """Remove link between issue and discussion"""
        state = self.load_project_state(project_name)
        if state is None:
            return

        if state.issue_discussion_links and issue_number in state.issue_discussion_links:
            discussion_id = state.issue_discussion_links[issue_number]
            del state.issue_discussion_links[issue_number]

            if state.discussion_issue_links and discussion_id in state.discussion_issue_links:
                del state.discussion_issue_links[discussion_id]

            self.save_project_state(state)
            logger.info(f"Unlinked issue #{issue_number} from discussion")


# Global state manager instance
state_manager = GitHubStateManager()
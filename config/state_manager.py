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
                    columns=columns
                )

            return GitHubProjectState(
                project_name=project_name,
                org=data['github_state']['org'],
                repo=data['github_state']['repo'],
                boards=boards,
                labels_created=data['github_state'].get('labels_created', []),
                last_sync=data['github_state']['last_sync'],
                sync_hash=data['github_state']['sync_hash']
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
            boards_data[board_name] = {
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

        data = {
            'github_state': {
                'org': state.org,
                'repo': state.repo,
                'boards': boards_data,
                'labels_created': state.labels_created,
                'last_sync': state.last_sync,
                'sync_hash': state.sync_hash
            }
        }

        try:
            with open(state_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)
            logger.info(f"Saved state for project {state.project_name}")
        except Exception as e:
            logger.error(f"Failed to save state for project {state.project_name}: {e}")
            raise

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
            last_sync=datetime.utcnow().isoformat(),
            sync_hash=current_hash
        )

    def update_board_state(self, project_name: str, board_name: str,
                          project_number: int, project_id: str, node_id: str,
                          columns: List[Dict[str, str]]):
        """Update board state with GitHub API response data"""
        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

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
            columns=github_columns
        )

        state.last_sync = datetime.utcnow().isoformat()
        self.save_project_state(state)

    def mark_labels_created(self, project_name: str, labels: List[str]):
        """Mark labels as created in GitHub"""
        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

        for label in labels:
            if label not in state.labels_created:
                state.labels_created.append(label)

        state.last_sync = datetime.utcnow().isoformat()
        self.save_project_state(state)

    def mark_synchronized(self, project_name: str):
        """Mark project as synchronized with current configuration"""
        project_config = self.config_manager.get_project_config(project_name)
        current_hash = self._calculate_config_hash(project_config)

        state = self.load_project_state(project_name)
        if state is None:
            state = self.create_initial_state(project_name)

        state.sync_hash = current_hash
        state.last_sync = datetime.utcnow().isoformat()
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


# Global state manager instance
state_manager = GitHubStateManager()
"""
Dev Container State Management

Tracks the state of project development container images:
- unverified: Default state for new projects
- in_progress: dev_environment_setup agent is working
- verified: Docker image built and tested successfully
- blocked: Unable to build working image
"""

import yaml
import logging
from pathlib import Path
from typing import Dict, Optional
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)


class DevContainerStatus(Enum):
    """Status of a project's development container"""
    UNVERIFIED = "unverified"  # Default for new projects
    IN_PROGRESS = "in_progress"  # Setup agent running
    VERIFIED = "verified"  # Image built and tested
    BLOCKED = "blocked"  # Failed to build working image


class DevContainerStateManager:
    """Manages development container state for projects"""

    def __init__(self, state_dir: Path = None):
        """Initialize dev container state manager"""
        if state_dir is None:
            state_dir = Path("state/dev_containers")

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"DevContainerStateManager initialized with state_dir: {state_dir}")

    def get_state_file(self, project_name: str) -> Path:
        """Get the state file path for a project"""
        return self.state_dir / f"{project_name}.yaml"

    def get_status(self, project_name: str) -> DevContainerStatus:
        """
        Get the current status of a project's dev container

        Args:
            project_name: Name of the project

        Returns:
            Current DevContainerStatus
        """
        state_file = self.get_state_file(project_name)

        if not state_file.exists():
            return DevContainerStatus.UNVERIFIED

        try:
            with open(state_file, 'r') as f:
                state = yaml.safe_load(f)

            status_str = state.get('status', 'unverified')
            return DevContainerStatus(status_str)

        except Exception as e:
            logger.error(f"Failed to read dev container state for {project_name}: {e}")
            return DevContainerStatus.UNVERIFIED

    def set_status(
        self,
        project_name: str,
        status: DevContainerStatus,
        image_name: Optional[str] = None,
        error_message: Optional[str] = None
    ):
        """
        Set the status of a project's dev container

        Args:
            project_name: Name of the project
            status: New status
            image_name: Docker image name (e.g., "context-studio-agent:latest")
            error_message: Error message if status is BLOCKED
        """
        state_file = self.get_state_file(project_name)

        # Load existing state or create new
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    state = yaml.safe_load(f) or {}
            except Exception as e:
                logger.warning(f"Failed to read existing state, creating new: {e}")
                state = {}
        else:
            state = {}

        # Update state
        state['status'] = status.value
        state['updated_at'] = datetime.now().isoformat()

        if image_name:
            state['image_name'] = image_name

        if error_message:
            state['error_message'] = error_message
        elif 'error_message' in state:
            # Clear error message if status changed from blocked
            if status != DevContainerStatus.BLOCKED:
                del state['error_message']

        # Save state
        try:
            with open(state_file, 'w') as f:
                yaml.dump(state, f, default_flow_style=False)

            logger.info(f"Updated dev container status for {project_name}: {status.value}")

        except Exception as e:
            logger.error(f"Failed to save dev container state for {project_name}: {e}")

    def get_image_name(self, project_name: str) -> Optional[str]:
        """
        Get the Docker image name for a project's dev container

        Args:
            project_name: Name of the project

        Returns:
            Image name (e.g., "context-studio-agent:latest") or None
        """
        state_file = self.get_state_file(project_name)

        if not state_file.exists():
            return None

        try:
            with open(state_file, 'r') as f:
                state = yaml.safe_load(f)

            return state.get('image_name')

        except Exception as e:
            logger.error(f"Failed to read dev container state for {project_name}: {e}")
            return None

    def is_verified(self, project_name: str) -> bool:
        """Check if a project's dev container is verified and ready"""
        return self.get_status(project_name) == DevContainerStatus.VERIFIED

    def is_blocked(self, project_name: str) -> bool:
        """Check if a project's dev container setup is blocked"""
        return self.get_status(project_name) == DevContainerStatus.BLOCKED

    def get_all_statuses(self) -> Dict[str, DevContainerStatus]:
        """
        Get status for all projects

        Returns:
            Dict mapping project names to their dev container status
        """
        statuses = {}

        for state_file in self.state_dir.glob("*.yaml"):
            project_name = state_file.stem
            statuses[project_name] = self.get_status(project_name)

        return statuses


# Global instance
dev_container_state = DevContainerStateManager()

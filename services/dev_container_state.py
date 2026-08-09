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
import subprocess
import os
from pathlib import Path
from typing import Dict, Optional
from enum import Enum
from datetime import datetime

logger = logging.getLogger(__name__)

# Label baked into switchyard's base Dockerfile (see repo-root Dockerfile) and
# inherited by every project's <project>-agent:latest image via `FROM
# switchyard-orchestrator:latest`. A project's own docker-compose.yml can
# happen to name one of its own services "agent", which — with no explicit
# `image:` tag — collides with this exact tag under Docker Compose's default
# `<compose-project>-<service>` naming. That silently swaps out the agent
# environment image for something unrelated (see incident: phone-home's own
# "agent" microservice, unrelated to this environment, overwrote
# phone-home-agent:latest and caused every senior_software_engineer container
# launch to boot the wrong entrypoint). Checking for this label — rather than
# just tag existence — catches that class of collision regardless of which
# unrelated image ends up holding the tag.
SWITCHYARD_AGENT_ENV_LABEL = "io.switchyard.agent-environment"


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
            # CRITICAL: Use absolute path to orchestrator's state directory
            # This prevents state from being created inside project directories when
            # agents execute with project working directory
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "dev_containers"

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

    def verify_image_exists(self, project_name: str) -> bool:
        """
        Verify that the Docker image for a project actually exists locally AND
        is genuinely a switchyard-built agent environment (carries
        SWITCHYARD_AGENT_ENV_LABEL) — not just something else that happens to
        hold the same tag.

        A same-named-but-foreign image (e.g. a project's own docker-compose
        service tagged identically via Compose's default naming) fails this
        check even though `docker image inspect` succeeds, since tag
        existence alone can't distinguish "our image" from "an unrelated
        image that overwrote our tag".

        Args:
            project_name: Name of the project

        Returns:
            True if the image exists locally and carries the switchyard
            agent-environment label, False otherwise
        """
        image_name = self.get_image_name(project_name)

        if not image_name:
            logger.debug(f"No image name recorded for {project_name}")
            return False

        try:
            result = subprocess.run(
                ['docker', 'image', 'inspect',
                 '--format', '{{ index .Config.Labels "%s" }}' % SWITCHYARD_AGENT_ENV_LABEL,
                 image_name],
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode != 0:
                logger.warning(f"Docker image {image_name} does not exist locally (state may be stale)")
                return False

            if result.stdout.strip() != "true":
                logger.warning(
                    f"Docker image {image_name} exists but is missing the "
                    f"{SWITCHYARD_AGENT_ENV_LABEL} label — it was not built from this "
                    f"project's Dockerfile.agent and has likely overwritten the tag "
                    f"(e.g. an unrelated docker-compose service sharing the same name). "
                    f"Treating as not verified; rebuild required."
                )
                return False

            logger.debug(f"Docker image {image_name} exists locally and is a genuine agent environment")
            return True

        except subprocess.TimeoutExpired:
            logger.error(f"Timeout checking if Docker image {image_name} exists")
            return False
        except Exception as e:
            logger.error(f"Error checking if Docker image {image_name} exists: {e}")
            return False

    def verify_and_update_status(self, project_name: str) -> bool:
        """
        Verify that a project's Docker image exists and update status if it doesn't

        This is useful after Docker context switches or system restarts where the
        state file may say "verified" but the image no longer exists.

        Args:
            project_name: Name of the project

        Returns:
            True if image exists (or status is not verified), False if image is missing
        """
        status = self.get_status(project_name)

        # Only verify if status is VERIFIED
        if status != DevContainerStatus.VERIFIED:
            return True  # No verification needed for other states

        # Check if image actually exists
        if self.verify_image_exists(project_name):
            return True  # Image exists, all good

        # Image is missing, or the tag now points at something that isn't a
        # genuine switchyard agent environment (see verify_image_exists) -
        # reset status to UNVERIFIED either way so a rebuild is forced.
        image_name = self.get_image_name(project_name)
        logger.warning(
            f"Project {project_name} marked as verified but image {image_name} is missing "
            f"or is not a genuine agent-environment image. Resetting status to unverified."
        )

        self.set_status(
            project_name,
            DevContainerStatus.UNVERIFIED,
            image_name=image_name,
            error_message=(
                "Image missing, or tag now points at an unrelated image (e.g. overwritten "
                "by another docker build/compose using the same name) - rebuild required"
            )
        )

        return False

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

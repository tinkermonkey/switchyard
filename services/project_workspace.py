import subprocess
import logging
from pathlib import Path
from typing import Dict, Optional
from config.manager import config_manager

logger = logging.getLogger(__name__)


class ProjectWorkspaceManager:
    """Manages project repository checkouts and branch management"""

    def __init__(self, workspace_root: Path = None):
        """
        Initialize workspace manager

        Args:
            workspace_root: Root directory for project checkouts (default: /workspace in container, or parent of orchestrator locally)
        """
        if workspace_root is None:
            # Check if running in container (has /workspace mount)
            container_workspace = Path('/workspace')
            if container_workspace.exists() and container_workspace.is_dir():
                workspace_root = container_workspace
                logger.info("Detected container environment, using /workspace")
            else:
                # Default to sibling directory of orchestrator for local development
                orchestrator_dir = Path(__file__).parent.parent
                workspace_root = orchestrator_dir.parent
                logger.info("Using local development workspace (parent of orchestrator)")

        self.workspace_root = workspace_root
        logger.info(f"ProjectWorkspaceManager initialized with workspace root: {workspace_root}")

    def initialize_all_projects(self) -> Dict[str, bool]:
        """
        Initialize workspaces for all configured projects

        Returns:
            Dict mapping project names to whether they need dev environment setup (True = newly cloned/missing Dockerfile.agent)
        """
        logger.info("Initializing all project workspaces")

        projects = config_manager.list_projects()
        needs_setup = {}

        for project_name in projects:
            try:
                project_config = config_manager.get_project_config(project_name)
                was_cloned = self.initialize_project(project_name, project_config)

                # Check if project needs dev environment setup
                project_dir = self.get_project_dir(project_name)
                dockerfile_agent = project_dir / 'Dockerfile.agent'

                # Need setup if: newly cloned OR missing Dockerfile.agent
                needs_setup[project_name] = was_cloned or not dockerfile_agent.exists()

                if needs_setup[project_name]:
                    logger.info(f"Project {project_name} needs dev environment setup (newly_cloned={was_cloned}, has_dockerfile={dockerfile_agent.exists()})")

            except Exception as e:
                logger.error(f"Failed to initialize project {project_name}: {e}")
                needs_setup[project_name] = False

        return needs_setup

    def initialize_project(self, project_name: str, project_config) -> bool:
        """
        Initialize a project workspace by checking if it exists

        Args:
            project_name: Name of the project
            project_config: Project configuration object

        Returns:
            True if project was newly cloned, False if it already existed
        """
        repo_url = project_config.github.get('repo_url')
        default_branch = project_config.github.get('branch', 'main')

        if not repo_url:
            raise ValueError(f"No repo_url configured for project {project_name}")

        project_dir = self.workspace_root / project_name
        was_cloned = False

        if project_dir.exists() and (project_dir / '.git').exists():
            logger.info(f"Project {project_name} found at {project_dir}")
            # Ensure we're on the default branch and up to date
            self._update_repository(project_dir, default_branch)
        else:
            # Try to clone if directory doesn't exist
            # Note: In container environments with mounted host directories, projects should already exist
            logger.warning(f"Project {project_name} not found at {project_dir}")
            logger.info(f"Attempting to clone from {repo_url}")
            try:
                self._clone_repository(repo_url, project_dir, default_branch)
                was_cloned = True
            except Exception as e:
                logger.error(f"Failed to clone {project_name}: {e}")
                logger.info("If running in Docker, ensure project is checked out on host and mounted correctly")
                raise

        return was_cloned

    def _clone_repository(self, repo_url: str, target_dir: Path, branch: str):
        """Clone a repository to the target directory"""
        try:
            # Convert SSH URLs to HTTPS if GITHUB_TOKEN is available
            import os
            github_token = os.environ.get('GITHUB_TOKEN')
            clone_url = repo_url

            if github_token and repo_url.startswith('git@github.com:'):
                # Convert git@github.com:user/repo.git to https://TOKEN@github.com/user/repo.git
                repo_path = repo_url.replace('git@github.com:', '').replace('.git', '')
                clone_url = f"https://{github_token}@github.com/{repo_path}.git"
                logger.info(f"Using HTTPS with token for cloning (converted from SSH)")

            cmd = ['git', 'clone', '--branch', branch, clone_url, str(target_dir)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode != 0:
                raise Exception(f"Git clone failed: {result.stderr}")

            logger.info(f"Successfully cloned repository to {target_dir}")
        except subprocess.TimeoutExpired:
            raise Exception("Git clone timed out")
        except Exception as e:
            raise Exception(f"Failed to clone repository: {e}")

    def _update_repository(self, repo_dir: Path, branch: str):
        """Update an existing repository to latest"""
        try:
            # Fetch latest changes
            result = subprocess.run(
                ['git', 'fetch', 'origin'],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.warning(f"Git fetch failed: {result.stderr}")
                return

            # Check current branch
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            current_branch = result.stdout.strip()

            # If not on desired branch, checkout
            if current_branch != branch:
                logger.info(f"Switching from {current_branch} to {branch}")
                result = subprocess.run(
                    ['git', 'checkout', branch],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    logger.warning(f"Git checkout failed: {result.stderr}")
                    return

            # Pull latest changes
            result = subprocess.run(
                ['git', 'pull', '--ff-only'],
                cwd=repo_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode != 0:
                logger.warning(f"Git pull failed: {result.stderr}")
            else:
                logger.info(f"Updated repository to latest {branch}")

        except Exception as e:
            logger.warning(f"Failed to update repository: {e}")

    def get_project_dir(self, project_name: str) -> Path:
        """Get the directory path for a project"""
        return self.workspace_root / project_name

    def ensure_branch(self, project_name: str, branch_name: str, create_if_missing: bool = True) -> bool:
        """
        DEPRECATED: Use GitWorkflowManager.checkout_branch() instead.
        
        This method is deprecated because it can create branches without proper tracking.
        Use services.git_workflow_manager.checkout_branch() for checkout operations,
        or services.feature_branch_manager.ensure_and_prepare_branch() for branch creation.

        Args:
            project_name: Name of the project
            branch_name: Branch to switch to
            create_if_missing: Create branch if it doesn't exist (DANGEROUS - use FeatureBranchManager instead)

        Returns:
            True if successful, False otherwise
        """
        logger.warning(
            f"DEPRECATED: ensure_branch() called for {project_name}/{branch_name}. "
            "Use GitWorkflowManager.checkout_branch() or FeatureBranchManager instead."
        )
        project_dir = self.get_project_dir(project_name)

        if not project_dir.exists():
            logger.error(f"Project directory does not exist: {project_dir}")
            return False

        try:
            # Check if branch exists
            result = subprocess.run(
                ['git', 'rev-parse', '--verify', f'origin/{branch_name}'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            branch_exists = result.returncode == 0

            if not branch_exists and create_if_missing:
                # Create new branch from current HEAD
                logger.info(f"Creating new branch {branch_name}")
                result = subprocess.run(
                    ['git', 'checkout', '-b', branch_name],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    logger.error(f"Failed to create branch: {result.stderr}")
                    return False
            else:
                # Checkout existing branch
                logger.info(f"Checking out branch {branch_name}")
                result = subprocess.run(
                    ['git', 'checkout', branch_name],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode != 0:
                    logger.error(f"Failed to checkout branch: {result.stderr}")
                    return False

            return True

        except Exception as e:
            logger.error(f"Failed to ensure branch {branch_name}: {e}")
            return False

    def get_current_branch(self, project_name: str) -> Optional[str]:
        """Get the current branch name for a project"""
        project_dir = self.get_project_dir(project_name)

        if not project_dir.exists():
            return None

        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if result.returncode == 0:
                return result.stdout.strip()

        except Exception as e:
            logger.error(f"Failed to get current branch: {e}")

        return None


# Global instance
workspace_manager = ProjectWorkspaceManager()

"""
Project Manager - Auto-clone repositories and manage project configurations
"""

import os
import re
import subprocess
import yaml
import logging
from pathlib import Path
from typing import Dict, Any, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

class ProjectManager:
    def __init__(self, projects_config_path: str = "config/projects.yaml", base_projects_dir: str = "/workspace"):
        self.projects_config_path = projects_config_path
        self.base_projects_dir = Path(base_projects_dir)
        self.projects_config = self._load_projects_config()

    def _load_projects_config(self) -> Dict[str, Any]:
        """Load projects configuration, falling back to example if main doesn't exist"""

        config_path = Path(self.projects_config_path)
        if not config_path.exists():
            # Fall back to example config
            example_path = Path(self.projects_config_path.replace('.yaml', '.example.yaml'))
            if example_path.exists():
                logger.info(f"Using example config: {example_path}")
                config_path = example_path
            else:
                return {"projects": {}}

        with open(config_path, 'r') as f:
            return yaml.safe_load(f) or {"projects": {}}

    def extract_repo_name(self, repo_url: str) -> str:
        """Extract repository name from Git URL"""

        # Handle various Git URL formats:
        # git@github.com:user/repo.git
        # https://github.com/user/repo.git
        # https://github.com/user/repo

        if repo_url.startswith('git@'):
            # SSH format: git@github.com:user/repo.git
            match = re.search(r':([^/]+)/([^/.]+)(?:\.git)?$', repo_url)
            if match:
                return match.group(2)
        else:
            # HTTPS format
            parsed = urlparse(repo_url)
            path_parts = parsed.path.strip('/').split('/')
            if len(path_parts) >= 2:
                repo_name = path_parts[-1]
                # Remove .git suffix if present
                if repo_name.endswith('.git'):
                    repo_name = repo_name[:-4]
                return repo_name

        raise ValueError(f"Could not extract repository name from: {repo_url}")

    def get_project_path(self, project_name: str) -> Path:
        """Get the local path for a project"""

        project_config = self.projects_config['projects'].get(project_name, {})
        repo_url = project_config.get('repo_url')

        if not repo_url:
            raise ValueError(f"No repo_url configured for project: {project_name}")

        # Extract repo name from URL
        repo_name = self.extract_repo_name(repo_url)

        # Use standard path: /workspace/{repo_name} (sibling to orchestrator)
        return self.base_projects_dir / repo_name

    def ensure_project_cloned(self, project_name: str) -> Path:
        """Ensure project is cloned locally, clone if necessary"""

        project_config = self.projects_config['projects'].get(project_name, {})
        repo_url = project_config.get('repo_url')
        branch = project_config.get('branch', 'main')

        if not repo_url:
            raise ValueError(f"No repo_url configured for project: {project_name}")

        project_path = self.get_project_path(project_name)

        # Create base projects directory if it doesn't exist
        self.base_projects_dir.mkdir(parents=True, exist_ok=True)

        if project_path.exists() and (project_path / '.git').exists():
            logger.info(f"Project {project_name} already cloned at {project_path}")

            # Ensure we're on the correct branch and pull latest
            try:
                subprocess.run(['git', 'checkout', branch], cwd=project_path, check=True, capture_output=True)
                subprocess.run(['git', 'pull'], cwd=project_path, check=True, capture_output=True)
                logger.info(f"Updated {project_name} to latest {branch}")
            except subprocess.CalledProcessError as e:
                logger.warning(f"Could not update {project_name}: {e}")

            return project_path

        # Clone the repository
        logger.info(f"Cloning {project_name} from {repo_url}...")

        try:
            cmd = ['git', 'clone', '--branch', branch, repo_url, str(project_path)]
            result = subprocess.run(cmd, check=True, capture_output=True, text=True)
            logger.info(f"Successfully cloned {project_name}")
            return project_path

        except subprocess.CalledProcessError as e:
            # If branch doesn't exist, try without branch specification
            try:
                cmd = ['git', 'clone', repo_url, str(project_path)]
                subprocess.run(cmd, check=True, capture_output=True, text=True)

                # Try to checkout the desired branch after cloning
                try:
                    subprocess.run(['git', 'checkout', branch], cwd=project_path, check=True, capture_output=True)
                except subprocess.CalledProcessError:
                    logger.warning(f"Branch '{branch}' not found, staying on default branch")

                logger.info(f"Successfully cloned {project_name}")
                return project_path

            except subprocess.CalledProcessError as clone_error:
                raise Exception(f"Failed to clone {project_name}: {clone_error.stderr}")

    def get_project_config(self, project_name: str) -> Dict[str, Any]:
        """Get full project configuration with derived values"""

        project_config = self.projects_config['projects'].get(project_name, {})

        if not project_config.get('repo_url'):
            raise ValueError(f"Project {project_name} not found or missing repo_url")

        # Ensure project is cloned
        project_path = self.ensure_project_cloned(project_name)

        # Return config with derived values
        return {
            **project_config,
            'local_path': str(project_path),
            'project_name': project_name,
            'repo_name': self.extract_repo_name(project_config['repo_url']),
            'branch': project_config.get('branch', 'main')
        }

    def list_configured_projects(self) -> list:
        """List all configured projects"""
        return list(self.projects_config['projects'].keys())

    def discover_github_projects(self, github_token: Optional[str] = None) -> Dict[str, str]:
        """Auto-discover GitHub projects for the configured organization"""

        if not github_token:
            github_token = os.environ.get('GITHUB_TOKEN')

        if not github_token:
            logger.warning("No GitHub token available for project discovery")
            return {}

        github_org = os.environ.get('GITHUB_ORG')
        if not github_org:
            logger.warning("No GITHUB_ORG configured for project discovery")
            return {}

        try:
            # Use GitHub CLI to list repositories
            cmd = ['gh', 'repo', 'list', github_org, '--limit', '100', '--json', 'name,sshUrl']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            import json
            repos = json.loads(result.stdout)

            discovered = {}
            for repo in repos:
                discovered[repo['name']] = repo['sshUrl']

            logger.info(f"Discovered {len(discovered)} repositories in {github_org}")
            return discovered

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            logger.error(f"Could not discover GitHub projects: {e}")
            return {}
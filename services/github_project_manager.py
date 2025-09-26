"""
GitHub Project Board Manager - Auto-discover and configure Kanban boards
"""

import os
import json
import yaml
import subprocess
from typing import Dict, Any, List, Optional, Tuple

class GitHubProjectManager:
    def __init__(self, templates_config_path: str = "config/kanban_templates.yaml"):
        self.templates_config_path = templates_config_path
        self.templates = self._load_templates()

    def _load_templates(self) -> Dict[str, Any]:
        """Load Kanban templates configuration"""
        try:
            with open(self.templates_config_path, 'r') as f:
                return yaml.safe_load(f)
        except FileNotFoundError:
            print(f"⚠️ Templates config not found: {self.templates_config_path}")
            return {"templates": {}, "default_template": "simple"}

    def discover_project_boards(self, repo_name: str) -> List[Dict[str, Any]]:
        """Discover existing project boards for a repository"""
        try:
            # List project boards associated with the repository
            cmd = ['gh', 'project', 'list', '--owner', os.environ.get('GITHUB_ORG', ''), '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            projects = json.loads(result.stdout)

            # Filter projects that might be related to this repo
            repo_projects = []
            for project in projects.get('projects', []):
                title = project.get('title', '').lower()
                if repo_name.lower() in title or title.startswith(repo_name.lower()):
                    repo_projects.append({
                        'id': project.get('id'),
                        'number': project.get('number'),
                        'title': project.get('title'),
                        'url': project.get('url')
                    })

            return repo_projects

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"⚠️ Could not discover project boards for {repo_name}: {e}")
            return []

    def get_project_columns(self, project_id: str) -> List[Dict[str, Any]]:
        """Get columns for a specific project board"""
        try:
            # Use GitHub CLI to get project details
            cmd = ['gh', 'project', 'view', project_id, '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            project_data = json.loads(result.stdout)

            # Extract column information
            columns = []
            fields = project_data.get('fields', [])

            for field in fields:
                if field.get('name') == 'Status' and field.get('type') == 'single_select':
                    for option in field.get('options', []):
                        columns.append({
                            'name': option.get('name'),
                            'id': option.get('id')
                        })
                    break

            return columns

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"⚠️ Could not get columns for project {project_id}: {e}")
            return []

    def create_project_board(self, repo_name: str, template_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """Create a new project board with standard columns"""

        template_name = template_name or self.templates.get('default_template', 'simple')
        template = self.templates.get('templates', {}).get(template_name)

        if not template:
            print(f"❌ Template '{template_name}' not found")
            return None

        github_org = os.environ.get('GITHUB_ORG')
        if not github_org:
            print("❌ GITHUB_ORG environment variable not set")
            return None

        try:
            # Create the project
            project_title = f"{repo_name} - {template['name']}"
            project_description = template.get('description', self.templates.get('github_project_settings', {}).get('description_template', ''))

            cmd = [
                'gh', 'project', 'create',
                '--owner', github_org,
                '--title', project_title,
                '--body', project_description,
                '--format', 'json'
            ]

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            project_data = json.loads(result.stdout)

            project_id = project_data.get('id')
            project_number = project_data.get('number')

            print(f"✅ Created project board: {project_title}")

            # Configure the Status field with our columns
            self._configure_project_columns(project_id, template['columns'])

            return {
                'id': project_id,
                'number': project_number,
                'title': project_title,
                'url': project_data.get('url'),
                'template_used': template_name
            }

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"❌ Failed to create project board for {repo_name}: {e}")
            return None

    def _configure_project_columns(self, project_id: str, columns: List[Dict[str, Any]]):
        """Configure project columns using GitHub CLI"""
        try:
            # First, get the current Status field
            cmd = ['gh', 'project', 'view', project_id, '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            project_data = json.loads(result.stdout)

            # Find the Status field
            status_field_id = None
            for field in project_data.get('fields', []):
                if field.get('name') == 'Status':
                    status_field_id = field.get('id')
                    break

            if not status_field_id:
                print("⚠️ Could not find Status field in project")
                return

            # Clear existing options and add our columns
            # Note: This is a simplified approach - GitHub's API for this is complex
            # In practice, you might need to use the GraphQL API directly

            for column in columns:
                try:
                    # Add column option to Status field
                    cmd = [
                        'gh', 'project', 'field-option-create',
                        project_id,
                        status_field_id,
                        '--name', column['name']
                    ]
                    subprocess.run(cmd, capture_output=True, text=True, check=True)
                    print(f"   ✅ Added column: {column['name']}")

                except subprocess.CalledProcessError:
                    print(f"   ⚠️ Could not add column: {column['name']} (might already exist)")

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"⚠️ Could not configure project columns: {e}")

    def generate_kanban_config(self, project_data: Dict[str, Any], template_name: str) -> Dict[str, Any]:
        """Generate kanban_columns configuration for projects.yaml"""

        template = self.templates.get('templates', {}).get(template_name, {})
        columns_config = {}

        for column in template.get('columns', []):
            columns_config[column['name']] = column['agent']

        return {
            'kanban_board_id': project_data.get('number'),  # Use project number, not ID
            'kanban_columns': columns_config
        }

    def list_templates(self) -> List[str]:
        """List available Kanban templates"""
        return list(self.templates.get('templates', {}).keys())

    def get_template_info(self, template_name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a template"""
        return self.templates.get('templates', {}).get(template_name)
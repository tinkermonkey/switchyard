"""
GitHub Project Board Manager - Auto-discover and configure Kanban boards
"""

import os
import json
import yaml
import subprocess
import requests
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
            return {"standard_workflow": {"columns": []}}

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

        template_name = template_name or 'standard_workflow'
        template = self.templates.get(template_name)

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
            project_description = template.get('description', self.templates.get('github_project_settings', {}).get('description_template', 'Automated project board managed by Claude Code Orchestrator'))

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
            self._configure_project_columns(project_id, template['columns'], github_org)

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

    def create_project_board_with_columns(self, repo_name: str, columns: List[Dict[str, Any]], github_org: str) -> Optional[Dict[str, Any]]:
        """Create a new project board with custom columns from project config"""

        try:
            # Create the project
            project_title = f"{repo_name} - Orchestrator"
            project_description = "Automated project board managed by Claude Code Orchestrator"

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

            # Configure the Status field with project's columns
            self._configure_project_columns(project_id, columns, github_org)

            return {
                'id': project_id,
                'number': project_number,
                'title': project_title,
                'url': project_data.get('url'),
                'columns_configured': True
            }

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"❌ Failed to create project board for {repo_name}: {e}")
            return None

    def _configure_project_columns(self, project_id: str, columns: List[Dict[str, Any]], github_org: str):
        """Configure project columns using GitHub CLI"""
        try:

            # First, check if Status field exists
            cmd = ['gh', 'project', 'field-list', str(project_id), '--owner', github_org, '--format', 'json']
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            fields_data = json.loads(result.stdout)

            # First, try to update the built-in Status field using GraphQL
            status_field = None
            for field in fields_data.get('fields', []):
                if field.get('name') == 'Status':
                    status_field = field
                    break

            if status_field:
                print("🔧 Found built-in Status field - attempting to update via GraphQL...")

                # Try to update the Status field with our columns
                if self._update_status_field_via_graphql(project_id, status_field['id'], columns, github_org):
                    print("✅ Successfully updated built-in Status field")
                    return
                else:
                    print("⚠️ GraphQL update failed, falling back to custom Workflow Status field...")

            # Fallback: Look for our custom "Workflow Status" field
            workflow_field = None
            for field in fields_data.get('fields', []):
                if field.get('name') == 'Workflow Status':
                    workflow_field = field
                    break

            if not workflow_field:
                # Create Workflow Status field with all column options
                column_names = [col['name'] for col in columns]
                options_str = ','.join(column_names)

                cmd = [
                    'gh', 'project', 'field-create', str(project_id),
                    '--owner', github_org,
                    '--name', 'Workflow Status',
                    '--data-type', 'SINGLE_SELECT',
                    '--single-select-options', options_str
                ]

                subprocess.run(cmd, capture_output=True, text=True, check=True)
                print(f"✅ Created Workflow Status field with {len(columns)} columns")

                for column in columns:
                    print(f"   ✅ Added column: {column['name']}")
            else:
                # Workflow Status field exists - check if we need to add missing columns
                existing_options = [opt['name'] for opt in workflow_field.get('options', [])]
                needed_columns = [col['name'] for col in columns if col['name'] not in existing_options]

                if needed_columns:
                    print(f"🔧 Workflow Status field exists but missing {len(needed_columns)} columns")

                    # Delete and recreate the custom Workflow Status field
                    try:
                        field_id = workflow_field.get('id')
                        cmd = ['gh', 'project', 'field-delete', '--id', field_id]
                        subprocess.run(cmd, capture_output=True, text=True, check=True)
                        print("🗑️ Deleted existing Workflow Status field")

                        # Now create new Workflow Status field with all columns
                        column_names = [col['name'] for col in columns]
                        options_str = ','.join(column_names)

                        cmd = [
                            'gh', 'project', 'field-create', str(project_id),
                            '--owner', github_org,
                            '--name', 'Workflow Status',
                            '--data-type', 'SINGLE_SELECT',
                            '--single-select-options', options_str
                        ]

                        subprocess.run(cmd, capture_output=True, text=True, check=True)
                        print(f"✅ Recreated Workflow Status field with {len(columns)} columns")

                        for column in columns:
                            print(f"   ✅ Added column: {column['name']}")

                    except subprocess.CalledProcessError as e:
                        print(f"⚠️ Could not delete/recreate Workflow Status field: {e}")
                        print("🔧 Manual column management may be needed")
                else:
                    print("✅ Workflow Status field already has all required columns")

        except (subprocess.CalledProcessError, json.JSONDecodeError) as e:
            print(f"⚠️ Could not configure project columns: {e}")

    def _update_status_field_via_graphql(self, project_id: str, field_id: str, columns: List[Dict[str, Any]], github_org: str):
        """Update existing Status field options using GitHub GraphQL API"""
        try:
            # Get GitHub token from gh CLI
            result = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, check=True)
            token = result.stdout.strip()

            # GraphQL mutation to update field options
            mutation = """
            mutation UpdateProjectV2Field($input: UpdateProjectV2FieldInput!) {
              updateProjectV2Field(input: $input) {
                projectV2Field {
                  ... on ProjectV2SingleSelectField {
                    id
                    name
                    options {
                      id
                      name
                    }
                  }
                }
              }
            }
            """

            # Prepare the options for the field
            options = []
            for column in columns:
                options.append({
                    "name": column['name'],
                    "description": column.get('description', f"Workflow stage: {column['name']}"),
                    "color": "GRAY"  # Default color, could be made configurable
                })

            variables = {
                "input": {
                    "fieldId": field_id,
                    "singleSelectOptions": options
                }
            }

            # Make the GraphQL request
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            }

            response = requests.post(
                "https://api.github.com/graphql",
                json={"query": mutation, "variables": variables},
                headers=headers
            )

            if response.status_code == 200:
                result = response.json()
                if "errors" in result:
                    print(f"⚠️ GraphQL errors: {result['errors']}")
                    return False
                else:
                    print(f"✅ Updated Status field with {len(columns)} columns via GraphQL")
                    return True
            else:
                print(f"⚠️ GraphQL request failed: {response.status_code} - {response.text}")
                return False

        except Exception as e:
            print(f"⚠️ GraphQL update failed: {e}")
            return False

    def generate_kanban_config(self, project_data: Dict[str, Any], template_name: str) -> Dict[str, Any]:
        """Generate kanban_columns configuration for projects.yaml"""

        template = self.templates.get(template_name, {})
        columns_config = {}

        for column in template.get('columns', []):
            columns_config[column['name']] = column['agent']

        return {
            'kanban_board_id': project_data.get('number'),  # Use project number, not ID
            'kanban_columns': columns_config
        }

    def get_template_info(self, template_name: str) -> Optional[Dict[str, Any]]:
        """Get detailed information about a template"""
        return self.templates.get(template_name)
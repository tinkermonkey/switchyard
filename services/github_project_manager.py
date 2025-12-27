"""
GitHub Project Board Manager with Configuration Reconciliation

This manager ensures that GitHub project boards match the desired configuration
defined in the project configuration files. It implements the reconciliation
pattern where the orchestrator becomes the authoritative source for project structure.
"""

import os
import json
import subprocess
import requests
import time
from datetime import datetime
from typing import Dict, Any, List, Optional, Tuple
import logging

from config.manager import ConfigManager, ProjectConfig, WorkflowTemplate
from config.state_manager import GitHubStateManager
from services.github_api_client import get_github_client

logger = logging.getLogger(__name__)

# Workflow state labels used by git workflow automation
WORKFLOW_STATE_LABELS = [
    {
        'name': 'approved',
        'color': '0e8a16',  # Green
        'description': 'PR has been approved by review cycle'
    },
    # Future labels can be added here (e.g., 'needs-rebase', 'merge-ready', etc.)
]


class GitHubProjectManager:
    """
    GitHub Project Manager with Configuration Reconciliation

    This class manages GitHub project boards by ensuring they match the
    configuration defined in project configuration files. It implements
    a reconciliation loop that creates, updates, and maintains project
    boards based on declarative configuration.
    """

    def __init__(self, config_manager: ConfigManager, state_manager: GitHubStateManager):
        self.config_manager = config_manager
        self.state_manager = state_manager

    async def reconcile_project(self, project_name: str) -> bool:
        """
        Reconcile a project's GitHub state with its configuration

        This is the main entry point for configuration reconciliation.
        It ensures GitHub projects match the desired configuration.

        Args:
            project_name: Name of the project to reconcile

        Returns:
            True if reconciliation was successful, False otherwise
        """
        try:
            # Check if circuit breaker is open before attempting reconciliation
            github_client = get_github_client()
            if github_client.breaker.is_open():
                time_until_reset = github_client.breaker.reset_time - datetime.now() if github_client.breaker.reset_time else None
                wait_msg = f" (resets in {int(time_until_reset.total_seconds())}s)" if time_until_reset else ""
                logger.warning(f"Skipping reconciliation for '{project_name}' - GitHub API circuit breaker is OPEN{wait_msg}")
                return False

            # Check if state is fresh and config hasn't changed - skip reconciliation to save API quota
            # Freshness threshold: 1 hour (configurable via environment variable)
            freshness_hours = int(os.environ.get('RECONCILIATION_FRESHNESS_HOURS', '1'))

            config_changed = self.state_manager.needs_reconciliation(project_name)
            state_is_fresh = self.state_manager.is_state_fresh(project_name, max_age_hours=freshness_hours)

            if not config_changed and state_is_fresh:
                logger.info(f"Skipping reconciliation for '{project_name}' - state is fresh and config unchanged (saves ~18-27 API calls)")
                return True

            if config_changed:
                logger.info(f"Starting reconciliation for project: {project_name} (config changed)")
            else:
                logger.info(f"Starting reconciliation for project: {project_name} (state is stale or incomplete)")

            # Load project configuration
            project_config = self.config_manager.get_project_config(project_name)

            # Backup current state before making changes
            backup_path = self.state_manager.backup_state(project_name)
            if backup_path:
                logger.info(f"Created state backup: {backup_path}")

            # Reconcile each enabled pipeline
            for pipeline in project_config.pipelines:
                if not pipeline.active:
                    continue

                success = await self._reconcile_pipeline_board(project_name, pipeline, project_config)
                if not success:
                    logger.error(f"Failed to reconcile pipeline '{pipeline.name}' for project '{project_name}'")
                    return False

            # Create repository labels
            await self._reconcile_labels(project_name, project_config)

            # Mark project as synchronized
            self.state_manager.mark_synchronized(project_name)

            logger.info(f"Successfully reconciled project: {project_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to reconcile project '{project_name}': {e}")
            return False

    async def _reconcile_pipeline_board(self, project_name: str, pipeline_config, project_config: ProjectConfig) -> bool:
        """Reconcile a single pipeline board with configuration"""
        try:
            # Check if board already exists in state
            existing_board = self.state_manager.get_board_by_name(project_name, pipeline_config.board_name)

            # If we have state, check if it's complete to skip verification
            if existing_board is not None:
                # Check if board state is complete - if so, trust it without verifying
                workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)
                expected_column_names = set(col.name for col in workflow_template.columns)
                existing_column_names = set(col.name for col in existing_board.columns)

                board_is_complete = (
                    existing_board.project_id and
                    existing_board.status_field_id and
                    len(existing_board.columns) > 0 and
                    expected_column_names == existing_column_names
                )

                # Only verify board exists if state is incomplete
                if not board_is_complete:
                    logger.info(f"Verifying board exists in GitHub: {pipeline_config.board_name} (#{existing_board.project_number})")
                    board_exists = await self._verify_board_exists(existing_board.project_number, project_config.github['org'])

                    if not board_exists:
                        logger.warning(f"Board #{existing_board.project_number} no longer exists in GitHub, will search for existing board by name")
                        existing_board = None
                else:
                    logger.debug(f"Board state complete for '{pipeline_config.board_name}' - skipping existence verification")

            # If no state or board doesn't exist, try to discover existing board by name
            if existing_board is None:
                logger.info(f"Searching for existing board by name: {pipeline_config.board_name}")
                discovered_board = await self._discover_board_by_name(
                    project_config.github['org'],
                    f"{project_name} - {pipeline_config.description}"
                )

                if discovered_board:
                    logger.info(f"Found existing board: {pipeline_config.board_name} (#{discovered_board['number']})")
                    
                    # Link discovered board to repository to make it repository-scoped
                    await self._link_board_to_repository(
                        discovered_board['number'],
                        project_config.github['org'],
                        project_config.github['repo'],
                        pipeline_config.board_name
                    )
                    
                    # Update state with discovered board
                    workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)
                    columns = await self._configure_board_columns(
                        discovered_board['number'],
                        discovered_board['id'],
                        workflow_template,
                        project_config.github['org']
                    )

                    # Only update state if we successfully retrieved columns
                    if not columns:
                        logger.error(f"Failed to configure columns for discovered board '{pipeline_config.board_name}' - skipping state update to preserve existing state")
                        return False

                    self.state_manager.update_board_state(
                        project_name,
                        pipeline_config.board_name,
                        discovered_board['number'],
                        discovered_board['id'],
                        discovered_board['node_id'],
                        columns
                    )
                    logger.info(f"Discovered and updated board: {pipeline_config.board_name}")
                    return True
                else:
                    # No existing board found, create new one
                    logger.info(f"No existing board found, creating new board: {pipeline_config.board_name}")
                    board_data = await self._create_project_board(
                        project_name,
                        pipeline_config,
                        project_config
                    )
                    if not board_data:
                        return False
            else:
                # Check if board state is already complete and valid
                workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)
                expected_column_names = set(col.name for col in workflow_template.columns)
                existing_column_names = set(col.name for col in existing_board.columns)

                board_is_complete = (
                    existing_board.project_id and
                    existing_board.status_field_id and
                    len(existing_board.columns) > 0 and
                    expected_column_names == existing_column_names
                )

                if board_is_complete:
                    logger.info(f"Board state is complete and valid for '{pipeline_config.board_name}' - skipping column configuration to reduce API usage")
                    return True

                logger.info(f"Board exists in GitHub: {pipeline_config.board_name}, updating columns")

                # Link existing board to repository if not already linked
                await self._link_board_to_repository(
                    existing_board.project_number,
                    project_config.github['org'],
                    project_config.github['repo'],
                    pipeline_config.board_name
                )

                # Update existing board columns
                project_number = existing_board.project_number

                if project_number:
                    columns = await self._configure_board_columns(
                        project_number,
                        existing_board.project_id,
                        workflow_template,
                        project_config.github['org']
                    )

                    # Only update state if we successfully retrieved columns
                    if not columns:
                        logger.error(f"Failed to configure columns for existing board '{pipeline_config.board_name}' - skipping state update to preserve existing state")
                        return False

                    # Update state with new columns
                    self.state_manager.update_board_state(
                        project_name,
                        pipeline_config.board_name,
                        existing_board.project_number,
                        existing_board.project_id,
                        existing_board.node_id,
                        columns
                    )
                    logger.info(f"Updated {len(columns)} columns for board: {pipeline_config.board_name}")

            return True

        except Exception as e:
            logger.error(f"Failed to reconcile pipeline board {pipeline_config.board_name}: {e}")
            return False

    async def _create_project_board(self, project_name: str, pipeline_config, project_config: ProjectConfig) -> Optional[Dict[str, Any]]:
        """Create a new project board based on configuration"""
        try:
            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            # Create the GitHub project at the organization level first
            # Then link it to the repository to make it repository-scoped
            github_client = get_github_client()
            
            cmd = [
                'gh', 'project', 'create',
                '--owner', project_config.github['org'],
                '--title', f"{project_name} - {pipeline_config.description}",
                '--format', 'json'
            ]

            success, result = github_client.gh_cli(cmd)
            if not success:
                logger.error(f"Failed to create GitHub project board '{pipeline_config.board_name}' for {project_name}")
                logger.error(f"Error: {result}")
                
                # Automatically diagnose the issue
                logger.error("AUTOMATIC DIAGNOSTICS:")
                await self._diagnose_github_issue(project_config.github['org'])
                
                logger.error("Orchestrator cannot manage GitHub projects without successful board creation")
                return None
            
            project_data = result
            project_id = project_data.get('id')
            project_number = project_data.get('number')
            node_id = project_data.get('node_id')

            logger.info(f"Created project board: {pipeline_config.board_name} (#{project_number})")

            # Link the project to the repository to make it repository-scoped
            link_cmd = [
                'gh', 'project', 'link', str(project_number),
                '--owner', project_config.github['org'],
                '--repo', project_config.github['repo']
            ]
            
            link_success, link_result = github_client.gh_cli(link_cmd)
            if link_success:
                logger.info(f"Linked project #{project_number} to repository {project_config.github['repo']}")
            else:
                logger.warning(f"Failed to link project to repository: {link_result}")
                logger.warning("Project created but not linked to repository - will be org-level instead of repo-level")

            # Configure columns
            columns = await self._configure_board_columns(project_number, project_id, workflow_template, project_config.github['org'])

            # Only update state if we successfully retrieved columns
            if not columns:
                logger.error(f"Failed to configure columns for newly created board '{pipeline_config.board_name}' - board created but state not updated")
                return None

            # Update state
            self.state_manager.update_board_state(
                project_name,
                pipeline_config.board_name,
                project_number,
                project_id,
                node_id,
                columns
            )

            return {
                'id': project_id,
                'number': project_number,
                'node_id': node_id,
                'title': f"{project_name} - {pipeline_config.description}"
            }

        except Exception as e:
            logger.error(f"Failed to create GitHub project board '{pipeline_config.board_name}' for {project_name}: {e}")
            
            # Automatically diagnose the issue
            logger.error("AUTOMATIC DIAGNOSTICS:")
            try:
                await self._diagnose_github_issue(project_config.github['org'])
            except:
                pass
            
            logger.error("Orchestrator cannot manage GitHub projects without successful board creation")
            return None

    async def _configure_board_columns(self, project_number: int, project_id: str, workflow_template: WorkflowTemplate, github_org: str) -> List[Dict[str, str]]:
        """Configure project board columns using GraphQL"""
        try:
            github_client = get_github_client()
            
            # Get the project's field information using project number
            cmd = ['gh', 'project', 'field-list', str(project_number), '--owner', github_org, '--format', 'json']
            success, result = github_client.gh_cli(cmd)
            
            if not success:
                logger.error(f"Failed to get project fields: {result}")
                return []
            
            fields_data = result

            # Find the Status field
            status_field = None
            for field in fields_data.get('fields', []):
                if field.get('name') == 'Status':
                    status_field = field
                    break

            if not status_field:
                logger.error("Status field not found")
                return []

            # Get current options from the status field
            current_options = status_field.get('options', [])
            current_option_names = set(opt.get('name') for opt in current_options if opt.get('name'))

            # Prepare column options from workflow template
            desired_options = []
            desired_option_names = set()
            for column in workflow_template.columns:
                desired_options.append({
                    "name": column.name,
                    "description": column.description,
                    "color": "GRAY"
                })
                desired_option_names.add(column.name)

            # Check if options match - if they do, skip the update to preserve statuses
            if current_option_names == desired_option_names:
                logger.info(f"Status field options already match configuration, skipping update to preserve item statuses")
                graphql_options = current_options
            else:
                logger.info(f"Status field options differ from configuration, updating: {current_option_names} -> {desired_option_names}")
                # Update the Status field with GraphQL
                graphql_options = await self._update_status_field_graphql(project_id, status_field['id'], desired_options)

            if graphql_options:
                # Return column data for state management with actual GraphQL option IDs
                columns = []
                for column in workflow_template.columns:
                    # Find matching option by name
                    matching_option = next(
                        (opt for opt in graphql_options if opt['name'] == column.name),
                        None
                    )
                    if matching_option:
                        columns.append({
                            'name': column.name,
                            'id': matching_option['id'],  # Actual GraphQL option ID
                            'node_id': matching_option['id']  # Use same ID
                        })
                    else:
                        logger.warning(f"No GraphQL option ID found for column '{column.name}'")

                # Also store the status field ID for later use
                if columns:
                    logger.info(f"Stored Status field ID: {status_field['id']}")
                    # Store this in the board state for later use in pipeline_progression
                    columns[0]['status_field_id'] = status_field['id']

                return columns
            else:
                return []

        except Exception as e:
            logger.error(f"Failed to configure columns: {e}")
            return []

    async def _update_status_field_graphql(self, project_id: str, field_id: str, options: List[Dict[str, str]]) -> Optional[List[Dict[str, str]]]:
        """Update Status field options using GraphQL

        Args:
            project_id: The global ID of the project (e.g., PVT_kwHOABgBzM4BGgJa)
            field_id: The global ID of the Status field
            options: List of option dictionaries with 'name', 'description', 'color' keys

        Returns:
            List of option dictionaries with 'id' and 'name' keys, or None on failure
        """
        try:
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

            variables = {
                "input": {
                    "fieldId": field_id,
                    "singleSelectOptions": options
                }
            }

            # Debug logging
            import json
            logger.info(f"UpdateProjectV2Field mutation input:")
            logger.info(f"  projectId (context): {project_id}")
            logger.info(f"  fieldId: {field_id}")
            logger.info(f"  options count: {len(options)}")
            logger.info(f"  Full variables JSON: {json.dumps(variables, indent=2)}")

            # Make GraphQL request using the client
            github_client = get_github_client()
            success, result = github_client.graphql(mutation, variables)

            if success:
                # Extract the option IDs from the response
                field_data = result.get('updateProjectV2Field', {}).get('projectV2Field', {})
                returned_options = field_data.get('options', [])
                logger.info(f"Configured {len(returned_options)} columns via GraphQL")
                return returned_options
            else:
                logger.error(f"GraphQL request failed: {result}")
                return None

        except Exception as e:
            logger.error(f"Failed to update status field: {e}")
            return None

    async def _reconcile_labels(self, project_name: str, project_config: ProjectConfig) -> bool:
        """Create repository labels for pipeline routing"""
        try:
            # Get workflow templates for enabled pipelines
            workflow_templates = self.config_manager.get_workflow_templates()

            labels_to_create = []

            # Add workflow state labels (used by git workflow automation)
            labels_to_create.extend(WORKFLOW_STATE_LABELS)

            # Collect all labels from enabled pipelines
            for pipeline in project_config.pipelines:
                if not pipeline.active:
                    continue

                workflow_template = workflow_templates.get(pipeline.workflow)
                if not workflow_template:
                    continue

                # Add pipeline label
                labels_to_create.append({
                    'name': f'pipeline:{pipeline.name}',
                    'color': '0075ca',
                    'description': f'{pipeline.description} workflow'
                })

                # Add stage labels
                for column in workflow_template.columns:
                    if column.stage_mapping:
                        labels_to_create.append({
                            'name': f'stage:{column.stage_mapping}',
                            'color': 'cfd3d7',
                            'description': f'Stage: {column.name}'
                        })

            # Create labels in repository
            created_labels = []
            for label_config in labels_to_create:
                try:
                    cmd = [
                        'gh', 'label', 'create',
                        label_config['name'],
                        '--description', label_config['description'],
                        '--color', label_config['color'],
                        '--force'  # Update if exists
                    ]

                    subprocess.run(cmd, capture_output=True, text=True, check=True)
                    created_labels.append(label_config['name'])
                    logger.info(f"Created label: {label_config['name']}")

                except subprocess.CalledProcessError:
                    # Label might already exist, that's fine
                    pass

            # Update state with created labels
            if created_labels:
                self.state_manager.mark_labels_created(project_name, created_labels)

            return True

        except Exception as e:
            logger.error(f"Failed to reconcile labels for {project_name}: {e}")
            return False

    async def _link_board_to_repository(self, project_number: int, org: str, repo: str, board_name: str) -> bool:
        """Link a project board to a repository to make it repository-scoped
        
        Args:
            project_number: The project number to link
            org: GitHub organization
            repo: Repository name
            board_name: Name of the board for logging
            
        Returns:
            True if linking succeeded or board was already linked, False on error
        """
        try:
            link_cmd = [
                'gh', 'project', 'link', str(project_number),
                '--owner', org,
                '--repo', repo
            ]
            result = subprocess.run(link_cmd, capture_output=True, text=True, check=False)
            
            if result.returncode == 0:
                logger.info(f"Linked project '{board_name}' (#{project_number}) to repository {org}/{repo}")
                return True
            elif "already linked" in result.stderr.lower() or "already associated" in result.stderr.lower():
                logger.debug(f"Project '{board_name}' (#{project_number}) already linked to repository {org}/{repo}")
                return True
            else:
                logger.warning(f"Failed to link project '{board_name}' to repository: {result.stderr.strip()}")
                logger.warning(f"Project remains at organization level instead of repository level")
                return False
                
        except Exception as e:
            logger.error(f"Error linking board to repository: {e}")
            return False

    def get_project_state(self, project_name: str):
        """Get the current GitHub state for a project"""
        return self.state_manager.load_project_state(project_name)

    def cleanup_project(self, project_name: str):
        """Remove all GitHub state for a project"""
        self.state_manager.cleanup_project_state(project_name)

    async def _diagnose_github_issue(self, org: str):
        """Automatically diagnose GitHub connectivity and permissions issues"""

        # 1. Check GitHub CLI authentication
        try:
            auth_result = subprocess.run(['gh', 'auth', 'status'], capture_output=True, text=True)
            if auth_result.returncode == 0:
                logger.info("GitHub CLI authentication: SUCCESS")
                # Parse the output to get token info
                if "Token:" in auth_result.stderr:
                    logger.error(f"Auth details: {auth_result.stderr.strip()}")
            else:
                logger.error("GitHub CLI authentication: FAILED")
                logger.error(f"Auth error: {auth_result.stderr.strip()}")
                logger.error("Fix: Run 'gh auth login' to authenticate")
                return
        except Exception as e:
            logger.error(f"Cannot check GitHub CLI authentication: {e}")
            return

        # 2. Check if user can access the organization
        try:
            org_result = subprocess.run(['gh', 'api', f'orgs/{org}'], capture_output=True, text=True)
            if org_result.returncode == 0:
                logger.info(f"Organization access ({org}): SUCCESS")
            else:
                logger.error(f"Organization access ({org}): FAILED")
                logger.error(f"Org error: {org_result.stderr.strip()}")
                logger.error(f"Fix: Check if you have access to organization '{org}'")
                return
        except Exception as e:
            logger.error(f"Cannot check organization access: {e}")

        # 3. Check GitHub Projects v2 permissions specifically
        try:
            projects_result = subprocess.run(['gh', 'project', 'list', '--owner', org, '--format', 'json'],
                                           capture_output=True, text=True)
            if projects_result.returncode == 0:
                logger.info(f"GitHub Projects v2 access ({org}): SUCCESS")
                try:
                    projects_data = json.loads(projects_result.stdout)
                    project_count = len(projects_data.get('projects', []))
                    logger.error(f"Found {project_count} existing projects in {org}")
                except:
                    logger.error("Projects list returned but could not parse JSON")
            else:
                logger.error(f"GitHub Projects v2 access ({org}): FAILED")
                logger.error(f"Projects error: {projects_result.stderr.strip()}")
                if "Resource not accessible by personal access token" in projects_result.stderr:
                    logger.error("DIAGNOSIS: Token missing 'project' scope")
                    logger.error("Fix: Add 'project' scope at https://github.com/settings/tokens")
                elif "Not Found" in projects_result.stderr:
                    logger.error(f"DIAGNOSIS: No access to organization '{org}' or org doesn't exist")
                    logger.error("Fix: Check organization name and your membership")
        except Exception as e:
            logger.error(f"Cannot check GitHub Projects access: {e}")

        # 4. Test a simple project creation to see what specific error we get
        try:
            logger.error("Testing project creation permissions...")
            test_result = subprocess.run([
                'gh', 'project', 'create',
                '--owner', org,
                '--title', f'orchestrator-test-{int(time.time())}',
                '--format', 'json'
            ], capture_output=True, text=True, timeout=30)

            if test_result.returncode == 0:
                logger.info("Test project creation: SUCCESS")
                # Clean up the test project
                try:
                    test_data = json.loads(test_result.stdout)
                    test_project_id = test_data.get('id')
                    if test_project_id:
                        subprocess.run(['gh', 'project', 'delete', test_project_id, '--confirm'],
                                     capture_output=True, timeout=10)
                        logger.info("Cleaned up test project")
                except:
                    logger.error("Test project created but cleanup failed - may need manual deletion")
            else:
                logger.error("Test project creation: FAILED")
                logger.error(f"Creation error: {test_result.stderr.strip()}")

        except subprocess.TimeoutExpired:
            logger.error("Test project creation: TIMEOUT")
        except Exception as e:
            logger.error(f"Cannot test project creation: {e}")

    async def _verify_board_exists(self, project_number: int, org: str) -> bool:
        """Verify that a project board still exists in GitHub

        Args:
            project_number: The project number to verify
            org: GitHub organization

        Returns:
            True if the board exists, False otherwise
        """
        try:
            from services.github_owner_utils import get_owner_type
            
            owner_type = get_owner_type(org)
            if owner_type is None:
                logger.error(f"Cannot verify board - unable to determine owner type for '{org}'")
                return False
            
            # Build GraphQL query to check if project exists
            if owner_type == 'user':
                query = f'''{{
                    user(login: "{org}") {{
                        projectV2(number: {project_number}) {{
                            id
                            number
                        }}
                    }}
                }}'''
            else:  # organization
                query = f'''{{
                    organization(login: "{org}") {{
                        projectV2(number: {project_number}) {{
                            id
                            number
                        }}
                    }}
                }}'''
            
            result = subprocess.run(
                ['gh', 'api', 'graphql', '-f', f'query={query}'],
                capture_output=True,
                text=True,
                check=False
            )
            
            if result.returncode == 0:
                data = json.loads(result.stdout)
                owner_key = 'user' if owner_type == 'user' else 'organization'
                project_data = data.get('data', {}).get(owner_key, {}).get('projectV2')
                return project_data is not None
            else:
                logger.debug(f"Board #{project_number} not found: {result.stderr}")
                return False
                
        except Exception as e:
            logger.error(f"Error verifying board existence: {e}")
            return False

    async def _discover_board_by_name(self, org: str, title: str) -> Optional[Dict[str, Any]]:
        """Discover an existing project board by its title

        Args:
            org: GitHub organization
            title: Project board title to search for

        Returns:
            Dictionary with board info (number, id, node_id) if found, None otherwise
        """
        try:
            from services.github_owner_utils import get_projects_list_for_owner
            
            # List all projects for the owner (user or organization)
            projects = get_projects_list_for_owner(org)
            
            if projects is None:
                logger.error(f"Failed to list projects for owner: {org}")
                return None

            # Search for a project with matching title
            for project in projects:
                if project.get('title') == title:
                    logger.info(f"Discovered existing board: {title} (#{project.get('number')})")
                    return {
                        'number': project.get('number'),
                        'id': project.get('id'),
                        'node_id': project.get('id'),  # Use same ID for node_id
                        'title': project.get('title')
                    }

            logger.debug(f"No existing board found with title: {title}")
            return None

        except Exception as e:
            logger.error(f"Error discovering board by name: {e}")
            return None
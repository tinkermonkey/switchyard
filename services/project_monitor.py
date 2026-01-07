#!/usr/bin/env python3

import os
import time
import json
import subprocess
import logging
from datetime import datetime
from typing import Dict, Any, List, Optional
from dataclasses import dataclass
from monitoring.timestamp_utils import utc_isoformat
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from config.manager import ConfigManager
from services.github_api_client import get_github_client

logger = logging.getLogger(__name__)

@dataclass
class ProjectItem:
    """Represents an item in a GitHub Projects v2 board"""
    item_id: str
    content_id: str
    issue_number: int
    title: str
    status: str
    repository: str
    last_updated: str


def _save_repair_cycle_context(
    project_dir: str,
    context: Dict[str, Any],
    test_configs: List[Any]
) -> str:
    """
    Save repair cycle context to JSON file for container execution.
    
    Args:
        project_dir: Project directory path (for reference, not used for file location)
        context: Stage context dictionary
        test_configs: List of RepairTestRunConfig objects
        
    Returns:
        Path to saved context file (in orchestrator_data directory)
    """
    from pathlib import Path
    
    # Serialize test configs
    serialized_configs = []
    for tc in test_configs:
        serialized_configs.append({
            'test_type': tc.test_type.value,
            'timeout': tc.timeout,
            'max_iterations': tc.max_iterations,
            'review_warnings': tc.review_warnings,
            'max_file_iterations': tc.max_file_iterations
        })
    
    # Build context for container
    container_context = {
        'project': context.get('project'),
        'board': context.get('board'),
        'pipeline': context.get('pipeline'),
        'repository': context.get('repository'),
        'issue_number': context.get('issue_number'),
        'issue': context.get('issue'),
        'previous_stage_output': context.get('previous_stage_output'),
        'column': context.get('column'),
        'workspace_type': context.get('workspace_type'),
        'discussion_id': context.get('discussion_id'),
        'pipeline_run_id': context.get('pipeline_run_id'),
        'project_dir': context.get('project_dir'),  # Keep original project_dir path
        'use_docker': True,
        'task_id': context.get('task_id'),
        'test_configs': serialized_configs,
        'agent_name': context.get('agent_name'),
        'max_total_agent_calls': context.get('max_total_agent_calls'),
        'checkpoint_interval': context.get('checkpoint_interval'),
        'stage_name': context.get('stage_name')
    }
    
    # Save to orchestrator_data directory (keeps project workspace clean)
    project_name = context.get('project')
    issue_number = context.get('issue_number')
    
    # Create directory structure: clauditoreum/orchestrator_data/repair_cycles/{project}/{issue}/
    orchestrator_data_dir = Path("/workspace/clauditoreum/orchestrator_data/repair_cycles")
    repair_cycle_dir = orchestrator_data_dir / project_name / str(issue_number)
    repair_cycle_dir.mkdir(parents=True, exist_ok=True)
    
    context_file = repair_cycle_dir / "context.json"
    with open(context_file, 'w') as f:
        json.dump(container_context, f, indent=2, default=str)
    
    logger.info(f"Saved repair cycle context to {context_file}")
    return str(context_file)


def _launch_repair_cycle_container(
    project_name: str,
    issue_number: int,
    pipeline_run_id: str,
    stage_name: str,
    context_file: str,
    project_dir: str
) -> Optional[str]:
    """
    Launch a Docker container to run the repair cycle.
    
    Args:
        project_name: Project name
        issue_number: GitHub issue number
        pipeline_run_id: Pipeline run ID
        stage_name: Stage name (e.g., "Testing")
        context_file: Path to context JSON file
        project_dir: Project directory path
        
    Returns:
        Container name if successful, None otherwise
    """
    from claude.docker_runner import DockerAgentRunner
    
    # Generate container name
    # Format: repair-cycle-{project}-{issue}-{run_id[:8]}
    short_run_id = pipeline_run_id[:8] if len(pipeline_run_id) >= 8 else pipeline_run_id
    container_name = f"repair-cycle-{project_name}-{issue_number}-{short_run_id}"
    
    # Sanitize container name
    container_name = DockerAgentRunner._sanitize_container_name(container_name)
    
    logger.info(f"Launching repair cycle container: {container_name}")
    
    try:
        # Get Docker runner for path detection
        docker_runner = DockerAgentRunner()
        host_workspace_path = docker_runner._detect_host_workspace_path()
        
        # Get environment variables
        from config.environment import load_environment
        env = load_environment()
        
        # Repair cycle containers use the orchestrator image
        # (which contains the repair cycle runner code)
        # The project workspace is mounted to provide project files
        # Agents are launched as sub-containers with the project's agent image
        repair_cycle_image = "clauditoreum-orchestrator"
        
        # Build Docker run command
        docker_cmd = [
            'docker', 'run',
            '--rm',  # Auto-remove container when it exits
            '--name', container_name,
            '--network', docker_runner.network_name,
            '--detach',  # Run in background
            '--user', '1000',  # Run as orchestrator user (UID only, inherit groups from image)
            
            # Volume mounts
            # Mount orchestrator code (live code for development)
            '-v', f'{host_workspace_path}/clauditoreum:/app',
            # ALSO mount orchestrator at /workspace/clauditoreum for checkpoint paths
            '-v', f'{host_workspace_path}/clauditoreum:/workspace/clauditoreum',
            # Mount project workspace at standard path (same as agent containers)
            '-v', f'{host_workspace_path}/{project_name}:/workspace/{project_name}',
            # Mount Docker socket (for launching agent containers)
            '-v', '/var/run/docker.sock:/var/run/docker.sock',
            # Mount GitHub App private key directory (if it exists)
            '-v', f'{os.path.expanduser("~")}/.orchestrator:/home/orchestrator/.orchestrator:ro',
            
            # Environment variables
            '-e', f'REDIS_HOST={env.redis_url.split("://")[1].split(":")[0]}',
            '-e', 'ELASTICSEARCH_HOST=elasticsearch',  # Elasticsearch host for observability events
            '-e', f'ANTHROPIC_API_KEY={env.anthropic_api_key.get_secret_value() if env.anthropic_api_key else ""}',
            '-e', f'CLAUDE_CODE_OAUTH_TOKEN={env.claude_code_oauth_token.get_secret_value() if env.claude_code_oauth_token else ""}',
            '-e', f'GITHUB_TOKEN={env.github_token.get_secret_value() if env.github_token else ""}',
            '-e', f'GH_TOKEN={env.github_token.get_secret_value() if env.github_token else ""}',  # For gh CLI
            '-e', f'HOST_WORKSPACE_PATH={host_workspace_path}',  # Pass host workspace path for Docker-in-Docker
            '-e', f'HOST_HOME={os.path.expanduser("~")}',  # Pass host home for SSH/git mounts
            '-e', 'PYTHONUNBUFFERED=1',  # Ensure logs are flushed
            '-e', f'PIPELINE_RUN_ID={pipeline_run_id}',  # Pass pipeline_run_id for event tracking
            
            # Image and command
            repair_cycle_image,
            'python', '-m', 'pipeline.repair_cycle_runner',
            '--project', project_name,
            '--issue', str(issue_number),
            '--pipeline-run-id', pipeline_run_id,
            '--stage', stage_name,
            '--context', f'/workspace/clauditoreum/orchestrator_data/repair_cycles/{project_name}/{issue_number}/context.json'
        ]
        
        # Launch container
        logger.debug(f"Docker command: {' '.join(docker_cmd)}")
        result = subprocess.run(
            docker_cmd,
            capture_output=True,
            text=True,
            timeout=30
        )
        
        if result.returncode != 0:
            logger.error(
                f"Failed to launch repair cycle container: {result.stderr}"
            )
            return None
        
        container_id = result.stdout.strip()
        logger.info(
            f"Repair cycle container launched: {container_name} (ID: {container_id[:12]})"
        )
        
        # Emit container started event
        try:
            from monitoring.observability import get_observability_manager
            obs_manager = get_observability_manager()
            obs_manager.emit_repair_cycle_container_started(
                project=project_name,
                issue_number=issue_number,
                container_name=container_name,
                run_id=pipeline_run_id,
                pipeline_run_id=pipeline_run_id
            )
        except Exception as e:
            logger.warning(f"Failed to emit container started event: {e}")
        
        return container_name
        
    except subprocess.TimeoutExpired:
        logger.error("Timeout launching repair cycle container")
        return None
    except Exception as e:
        logger.error(f"Error launching repair cycle container: {e}", exc_info=True)
        return None


def _register_repair_cycle_container(
    project_name: str,
    issue_number: int,
    container_name: str,
    redis_client
) -> bool:
    """
    Register repair cycle container in Redis for recovery tracking.

    Args:
        project_name: Project name
        issue_number: Issue number
        container_name: Docker container name
        redis_client: Redis client instance

    Returns:
        True if registered successfully
    """
    try:
        # Key format: repair_cycle:container:{project}:{issue}
        redis_key = f"repair_cycle:container:{project_name}:{issue_number}"

        # Store with 2 hour TTL (repair cycles shouldn't take longer)
        redis_client.setex(redis_key, 7200, container_name)

        logger.info(
            f"Registered repair cycle container in Redis: {redis_key} -> {container_name}"
        )
        return True

    except Exception as e:
        logger.error(f"Failed to register repair cycle container in Redis: {e}")
        return False


def _load_repair_cycle_result_from_redis(
    project_name: str,
    issue_number: int,
    run_id: str
) -> Optional[Dict[str, Any]]:
    """
    Load repair cycle result from Redis.

    This replaces file-based result loading to avoid polluting project repos.

    Args:
        project_name: Project name
        issue_number: Issue number
        run_id: Pipeline run ID

    Returns:
        Result dictionary if found, None otherwise
    """
    try:
        import redis

        # Connect to Redis
        redis_client = redis.Redis(
            host='redis',
            port=6379,
            decode_responses=True
        )

        # Key format: repair_cycle:result:{project}:{issue}:{run_id}
        redis_key = f"repair_cycle:result:{project_name}:{issue_number}:{run_id}"

        # Get result from Redis
        result_json = redis_client.get(redis_key)

        if not result_json:
            logger.warning(f"No result found in Redis for key: {redis_key}")
            return None

        # Parse JSON
        result = json.loads(result_json)
        logger.debug(f"Loaded result from Redis: {redis_key}")

        return result

    except Exception as e:
        logger.error(f"Failed to load result from Redis: {e}", exc_info=True)
        return None


def _cleanup_repair_cycle_state(project_name: str, issue_number: int, run_id: str = None) -> bool:
    """
    Clean up repair cycle state after successful completion.

    Removes checkpoint and context files from orchestrator_data directory,
    and deletes result from Redis.

    Args:
        project_name: Project name
        issue_number: Issue number
        run_id: Pipeline run ID (optional, for Redis cleanup)

    Returns:
        True if cleanup successful
    """
    try:
        from pathlib import Path
        import shutil
        import redis

        # Clean up file-based state (context and checkpoint)
        orchestrator_data_dir = Path("/workspace/clauditoreum/orchestrator_data/repair_cycles")
        repair_cycle_dir = orchestrator_data_dir / project_name / str(issue_number)

        if repair_cycle_dir.exists():
            # Remove the entire directory
            shutil.rmtree(repair_cycle_dir)
            logger.info(f"Cleaned up repair cycle files for {project_name} issue #{issue_number}")

            # Try to clean up parent directories if empty
            try:
                project_rc_dir = orchestrator_data_dir / project_name
                if project_rc_dir.exists() and not any(project_rc_dir.iterdir()):
                    project_rc_dir.rmdir()
                    logger.debug(f"Removed empty project repair cycle directory: {project_rc_dir}")
            except Exception as e:
                logger.debug(f"Could not remove parent directory (may not be empty): {e}")

        # Clean up Redis result
        if run_id:
            try:
                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                redis_key = f"repair_cycle:result:{project_name}:{issue_number}:{run_id}"
                redis_client.delete(redis_key)
                logger.debug(f"Deleted repair cycle result from Redis: {redis_key}")
            except Exception as e:
                logger.warning(f"Failed to delete result from Redis: {e}")

        return True

    except Exception as e:
        logger.warning(f"Failed to cleanup repair cycle state: {e}")
        return False


class ProjectMonitor:
    """Monitor GitHub Projects v2 boards for changes and trigger agent workflows"""

    def __init__(self, task_queue: TaskQueue, config_manager: ConfigManager = None):
        self.task_queue = task_queue
        self.config_manager = config_manager or ConfigManager()
        self.last_state = {}  # Store last known state of each project

        # Signal when startup rescan is complete (for coordinating worker startup)
        import threading
        self.rescan_complete = threading.Event()

        # Initialize feedback manager
        from services.feedback_manager import FeedbackManager
        self.feedback_manager = FeedbackManager()

        # Initialize workspace router for discussions
        from services.workspace_router import WorkspaceRouter
        from services.github_discussions import GitHubDiscussions
        self.workspace_router = WorkspaceRouter()
        self.discussions = GitHubDiscussions()

        # Initialize decision observability
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)

        # Initialize pipeline run manager
        from services.pipeline_run import get_pipeline_run_manager
        self.pipeline_run_manager = get_pipeline_run_manager()

        # Get polling interval from first project's orchestrator config (for now)
        projects = self.config_manager.list_projects()
        if projects:
            first_project = self.config_manager.get_project_config(projects[0])
            self.poll_interval = first_project.orchestrator.get("polling_interval", 15)
        else:
            self.poll_interval = 30

    def get_project_items(self, project_owner: str, project_number: int) -> List[ProjectItem]:
        """Query GitHub Projects v2 API to get current project items (excludes closed issues)"""
        from services.github_owner_utils import build_projects_v2_query, get_owner_type
        from services.github_api_client import get_github_client
        import time
        
        # Build the correct query based on owner type
        query = build_projects_v2_query(project_owner, project_number)
        
        if query is None:
            logger.error(f"Cannot query project items - unable to determine owner type for '{project_owner}'")
            return []

        # Check if circuit breaker is already open - if so, don't waste time retrying
        github_client = get_github_client()
        if github_client.breaker.is_open():
            time_until = (github_client.breaker.reset_time - datetime.now()).total_seconds() if github_client.breaker.reset_time else None
            if time_until and time_until > 0:
                logger.debug(f"Circuit breaker is open for {time_until:.0f}s, skipping project item query")
            return []

        # Retry up to 3 times with exponential backoff for transient errors
        max_retries = 3
        retry_delay = 1  # Start with 1 second
        
        for attempt in range(max_retries):
            try:
                github_client = get_github_client()
                
                # Check again before each attempt (in case breaker opened during a previous call)
                if github_client.breaker.is_open():
                    logger.debug(f"Circuit breaker opened during retry loop, stopping retries")
                    return []
                
                success, data = github_client.graphql(query)

                if not success:
                    logger.warning(f"GraphQL query failed (attempt {attempt + 1}/{max_retries}): {data}")
                    if attempt < max_retries - 1:
                        logger.info(f"Retrying in {retry_delay} seconds...")
                        time.sleep(retry_delay)
                        retry_delay *= 2  # Exponential backoff
                    continue
                
                # Get project data from the correct path based on owner type
                owner_type = get_owner_type(project_owner)
                if owner_type == 'user':
                    project_data = data['user']['projectV2']
                else:  # organization
                    project_data = data['organization']['projectV2']

                items = []
                for node in project_data['items']['nodes']:
                    content = node.get('content')
                    if not content:  # Skip draft items
                        continue

                    # Include closed issues so we can detect moves to Done/Exit columns and release locks
                    # trigger_agent_for_status will handle skipping agent execution for closed issues
                    issue_state = content.get('state', '').upper()

                    # Find status field
                    status = "No Status"
                    for field_value in node['fieldValues']['nodes']:
                        if field_value and field_value.get('field', {}).get('name') == 'Status':
                            status = field_value.get('name', 'No Status')
                            break

                    item = ProjectItem(
                        item_id=node['id'],
                        content_id=content['id'],
                        issue_number=content['number'],
                        title=content['title'],
                        status=status,
                        repository=content['repository']['name'],
                        last_updated=content['updatedAt']
                    )
                    items.append(item)

                return items

            except Exception as e:
                logger.warning(f"GraphQL query failed (attempt {attempt + 1}/{max_retries}): {e}")
                if attempt < max_retries - 1:
                    logger.info(f"Retrying in {retry_delay} seconds...")
                    time.sleep(retry_delay)
                    retry_delay *= 2  # Exponential backoff
                else:
                    logger.error("Max retries exceeded for GraphQL query")
                    return []

        return []  # Should never reach here, but just in case

    async def get_issue_column_async(self, project_name: str, board_name: str, issue_number: int) -> Optional[str]:
        """
        Get the current column/status for a specific issue asynchronously
        
        Args:
            project_name: Project name (e.g., 'what_am_i_watching')
            board_name: Board/pipeline name (e.g., 'Planning & Design')
            issue_number: GitHub issue number
            
        Returns:
            Column name (status) if found, None otherwise
        """
        try:
            from config.manager import config_manager
            import asyncio
            
            # Get project config
            project_config = config_manager.get_project_config(project_name)
            
            # Find the board state
            board_state = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    board_state = self.board_state.get(f"{project_name}_{board_name}")
                    break
            
            if not board_state:
                logger.debug(f"No board state found for {project_name}/{board_name}")
                return None
            
            # Query project items (run in thread pool to avoid blocking)
            loop = asyncio.get_event_loop()
            items = await loop.run_in_executor(
                None, 
                self.get_project_items,
                project_config.github['org'],
                board_state.project_number
            )
            
            # Find the specific issue
            for item in items:
                if item.issue_number == issue_number:
                    return item.status
                    
            logger.debug(f"Issue #{issue_number} not found in {project_name}/{board_name}")
            return None
            
        except Exception as e:
            logger.error(f"Error getting column for issue #{issue_number}: {e}")
            return None

    def detect_changes(self, project_name: str, current_items: List[ProjectItem]) -> List[Dict[str, Any]]:
        """Detect changes in project items since last poll"""
        changes = []

        # Create lookup by issue number for current items
        current_by_issue = {item.issue_number: item for item in current_items}

        # Get last known state
        last_items = self.last_state.get(project_name, {})

        # Check for status changes
        for issue_number, current_item in current_by_issue.items():
            last_item = last_items.get(issue_number)

            if last_item is None:
                # New item added to project
                changes.append({
                    'type': 'item_added',
                    'project': project_name,
                    'issue_number': issue_number,
                    'title': current_item.title,
                    'status': current_item.status,
                    'repository': current_item.repository
                })
            elif last_item.status != current_item.status:
                # Status changed
                changes.append({
                    'type': 'status_changed',
                    'project': project_name,
                    'issue_number': issue_number,
                    'title': current_item.title,
                    'old_status': last_item.status,
                    'new_status': current_item.status,
                    'repository': current_item.repository
                })
                
                # EMIT DECISION EVENT: Status change detected (if not already emitted)
                # Check if this was a recent programmatic change to avoid duplicate events
                from services.work_execution_state import work_execution_tracker
                
                # Extract project/board names from project_name key (format: project_board)
                actual_project = project_name.split('_', 1)[0] if '_' in project_name else project_name
                board_name = project_name.split('_', 1)[1] if '_' in project_name else 'unknown'
                
                # Check if this status change was recently made programmatically
                # (in which case the event was already emitted by pipeline_progression)
                was_programmatic = work_execution_tracker.was_recent_programmatic_change(
                    project_name=actual_project,
                    issue_number=issue_number,
                    to_status=current_item.status,
                    time_window_seconds=60
                )
                
                if not was_programmatic:
                    # Only emit if this appears to be a manual status change
                    self.decision_events.emit_status_progression(
                        issue_number=issue_number,
                        project=actual_project,
                        board=board_name,
                        from_status=last_item.status,
                        to_status=current_item.status,
                        trigger='manual',  # Status changes from GitHub polling are manual
                        success=True  # Already successfully moved in GitHub
                    )
                else:
                    logger.debug(
                        f"Skipping duplicate status_progression event for #{issue_number} "
                        f"({last_item.status} → {current_item.status}) - already emitted programmatically"
                    )

        # Update last state
        self.last_state[project_name] = current_by_issue

        return changes

    def get_issue_details(self, repository: str, issue_number: int, org: str) -> Dict[str, Any]:
        """Fetch full issue details from GitHub"""
        try:
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'title,body,labels,state,author,createdAt,updatedAt'],
                capture_output=True, text=True, check=True
            )
            return json.loads(result.stdout)
        except Exception as e:
            logger.error(f"Error fetching issue #{issue_number} details: {e}")
            return {'title': f'Issue #{issue_number}', 'body': '', 'labels': []}

    def get_previous_stage_context(self, repository: str, issue_number: int, org: str,
                                   current_column: str, workflow_template,
                                   workspace_type: str = 'issues',
                                   discussion_id: Optional[str] = None,
                                   pipeline_config=None,
                                   current_stage_config=None,
                                   project_name: Optional[str] = None) -> str:
        """
        Fetch comments from the previous workflow stage agent and user comments since then.
        Works with both issues and discussions workspaces.

        If current_stage_config has inputs_from defined, will gather outputs from those
        specific agents instead of just the previous stage.

        Returns formatted context string.
        """
        # Check if this stage has specific input requirements
        if current_stage_config and hasattr(current_stage_config, 'inputs_from') and current_stage_config.inputs_from:
            # Determine expected workspace for the current pipeline
            # Get pipeline config to check workspace type
            pipeline_workspace = None
            if project_name:
                try:
                    from config.manager import config_manager
                    project_config = config_manager.get_project_config(project_name)
                    if project_config:
                        for pipeline_instance in project_config.pipelines:
                            # Get the pipeline template to find stages and workspace
                            pipeline_template = config_manager.get_pipeline_template(pipeline_instance.template)
                            if pipeline_template and hasattr(pipeline_template, 'stages'):
                                for stage in pipeline_template.stages:
                                    if stage.stage == current_stage_config.stage:
                                        pipeline_workspace = getattr(pipeline_template, 'workspace', 'discussions')
                                        break
                                if pipeline_workspace:
                                    break
                except Exception as e:
                    logger.debug(f"Could not determine pipeline workspace: {e}")

            # Default to discussions if we can't determine
            if not pipeline_workspace:
                pipeline_workspace = 'discussions'

            # Only attempt discussion lookup if pipeline uses discussions workspace
            # OR if we're explicitly crossing workspaces (has discussion_id already)
            actual_discussion_id = discussion_id

            if pipeline_workspace == 'discussions' and not actual_discussion_id and workspace_type == 'issues' and project_name:
                # We're in issues workspace but pipeline expects discussions
                # Look up the discussion associated with this issue's parent
                logger.debug(f"Pipeline uses discussions workspace, looking for linked discussion for issue #{issue_number}")
                try:
                    # For sub-issues, we need to find the parent issue's discussion
                    from config.state_manager import GitHubStateManager
                    state_manager = GitHubStateManager()
                    github_state = state_manager.load_project_state(project_name)

                    if github_state and github_state.issue_discussion_links:
                        # Try to get discussion for this issue
                        # Convert to string - YAML keys are strings even for numeric values
                        actual_discussion_id = github_state.issue_discussion_links.get(str(issue_number))

                        if not actual_discussion_id:
                            # This might be a sub-issue, look for parent issue's discussion
                            # Get parent issue number from GitHub (sub-issues have trackedIn field)
                            try:
                                import subprocess
                                result = subprocess.run(
                                    ['gh', 'issue', 'view', str(issue_number),
                                     '--repo', f"{org}/{repository}",
                                     '--json', 'body'],
                                    capture_output=True, text=True, check=True
                                )
                                issue_data = json.loads(result.stdout)
                                body = issue_data.get('body', '')

                                # Look for "Part of #NNN" pattern in issue body
                                import re
                                parent_match = re.search(r'Part of #(\d+)', body)
                                if parent_match:
                                    parent_issue_number = int(parent_match.group(1))
                                    # Convert to string - YAML keys are strings even for numeric values
                                    actual_discussion_id = github_state.issue_discussion_links.get(str(parent_issue_number))
                                    if actual_discussion_id:
                                        logger.info(f"Found parent issue #{parent_issue_number} discussion for sub-issue #{issue_number}")
                                    else:
                                        logger.debug(f"Parent issue #{parent_issue_number} found but has no discussion")
                                else:
                                    logger.debug(f"No 'Part of #NNN' pattern found in issue #{issue_number} body")
                            except Exception as e:
                                logger.debug(f"Could not look up parent issue for #{issue_number}: {e}")

                    if actual_discussion_id:
                        logger.info(f"Found associated discussion {actual_discussion_id} for issue #{issue_number}")
                except Exception as e:
                    logger.warning(f"Error looking up discussion for issue #{issue_number}: {e}")

            # Use discussion-based approach for gathering specific agent outputs
            if actual_discussion_id:
                logger.info(f"Gathering inputs from specific agents via discussion: {current_stage_config.inputs_from}")
                return self._get_agent_outputs_from_discussion(actual_discussion_id, current_stage_config.inputs_from)
            else:
                # For issues workspace, this is expected behavior - use issue-based gathering
                if pipeline_workspace == 'issues':
                    logger.debug(f"inputs_from specified for issues-workspace pipeline, using issue-based gathering for #{issue_number}")
                else:
                    logger.warning(f"inputs_from specified but no discussion found for discussion-workspace pipeline (issue #{issue_number})")
                
                # Fallback 1: Check the current issue for agent outputs
                logger.info(f"Checking current issue #{issue_number} for outputs from: {current_stage_config.inputs_from}")
                issue_outputs = self._get_agent_outputs_from_issue(repository, issue_number, org, current_stage_config.inputs_from)
                
                if issue_outputs:
                    logger.info(f"Found agent outputs in current issue #{issue_number}")
                    return issue_outputs
                
                # Fallback 2: Check parent issue if one exists
                logger.info(f"No outputs found in issue #{issue_number}, checking for parent issue")
                try:
                    import asyncio
                    from services.github_integration import GitHubIntegration
                    from services.feature_branch_manager import feature_branch_manager
                    
                    # Get GitHub integration with proper repo context
                    github_integration = GitHubIntegration(repo_owner=org, repo_name=repository)
                    
                    # Create event loop if needed
                    try:
                        loop = asyncio.get_running_loop()
                        # If a loop is already running (e.g. called from main.py startup),
                        # we cannot use run_until_complete. Run in a separate thread.
                        import concurrent.futures
                        with concurrent.futures.ThreadPoolExecutor() as pool:
                            parent_issue_number = pool.submit(
                                asyncio.run, 
                                feature_branch_manager.get_parent_issue(
                                    github_integration,
                                    issue_number,
                                    project=project_name
                                )
                            ).result()
                    except RuntimeError:
                        # No running loop, create one
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        
                        # Get parent issue number
                        parent_issue_number = loop.run_until_complete(
                            feature_branch_manager.get_parent_issue(
                                github_integration,
                                issue_number,
                                project=project_name
                            )
                        )
                    
                    if parent_issue_number:
                        logger.info(f"Found parent issue #{parent_issue_number}, checking for agent outputs")
                        
                        # Check if parent issue has a discussion associated with it
                        parent_discussion_id = None
                        try:
                            from config.state_manager import GitHubStateManager
                            state_manager = GitHubStateManager()
                            github_state = state_manager.load_project_state(project_name)
                            if github_state and github_state.issue_discussion_links:
                                # Convert to string - YAML keys are strings even for numeric values
                                parent_discussion_id = github_state.issue_discussion_links.get(str(parent_issue_number))
                        except Exception as e:
                            logger.warning(f"Error looking up discussion for parent issue #{parent_issue_number}: {e}")

                        parent_outputs = None
                        if parent_discussion_id:
                            logger.info(f"Found discussion {parent_discussion_id} for parent issue #{parent_issue_number}, checking for agent outputs")
                            parent_outputs = self._get_agent_outputs_from_discussion(
                                parent_discussion_id, current_stage_config.inputs_from
                            )
                        
                        # If no outputs found in discussion (or no discussion), check issue comments
                        if not parent_outputs:
                            logger.info(f"Checking parent issue #{parent_issue_number} comments for agent outputs")
                            parent_outputs = self._get_agent_outputs_from_issue(
                                repository, parent_issue_number, org, current_stage_config.inputs_from
                            )
                        
                        if parent_outputs:
                            logger.info(f"Found agent outputs in parent issue #{parent_issue_number}")
                            return parent_outputs
                        else:
                            logger.info(f"No outputs found in parent issue #{parent_issue_number}")
                    else:
                        logger.info(f"No parent issue found for issue #{issue_number}")
                        
                except Exception as e:
                    logger.warning(f"Error checking parent issue: {e}")
                    import traceback
                    logger.warning(traceback.format_exc())
                
                # Fallback 3: Use general issue context
                logger.info(
                    f"No outputs from {current_stage_config.inputs_from} found for issue #{issue_number}, "
                    f"using general issue context (fallback)"
                )
                # Track metric
                try:
                    from monitoring.metrics_collector import metrics_collector
                    metrics_collector.record_metric(
                        metric_name="pipeline.input_fallback",
                        value=1,
                        tags={
                            "stage": current_stage_config.stage,
                            "requested_agents": ",".join(current_stage_config.inputs_from),
                            "issue": str(issue_number)
                        }
                    )
                except Exception as e:
                    logger.debug(f"Could not record fallback metric: {e}")
                return self._get_issue_context(repository, issue_number, org, current_column, workflow_template)

        # For hybrid pipelines, determine if previous stage was in discussions or issues
        should_use_discussion = False

        if workspace_type == 'hybrid' and pipeline_config:
            # Find previous column
            column_names = [col.name for col in workflow_template.columns]
            if current_column in column_names:
                current_index = column_names.index(current_column)
                if current_index > 0:
                    previous_column_name = workflow_template.columns[current_index - 1].name
                    # Check if previous stage is in discussion_stages
                    discussion_stages = getattr(pipeline_config, 'discussion_stages', [])
                    # Convert column name to stage key (e.g., "Design" -> "design")
                    previous_stage_key = previous_column_name.lower().replace(' ', '_')
                    if previous_stage_key in [s.lower() for s in discussion_stages]:
                        should_use_discussion = True
                        logger.info(f"Hybrid pipeline: previous stage '{previous_column_name}' is in discussion_stages, will use discussion context")

        # Route to appropriate workspace
        if (workspace_type == 'discussions' or should_use_discussion) and discussion_id:
            return self._get_discussion_context(discussion_id, current_column, workflow_template)
        else:
            return self._get_issue_context(repository, issue_number, org, current_column, workflow_template)

    def _get_issue_context(self, repository: str, issue_number: int, org: str,
                           current_column: str, workflow_template) -> str:
        """
        Get previous stage context from issue comments.

        Now gathers ALL agent outputs and user feedback from the entire thread,
        not just the immediately previous column. This ensures that when an issue
        moves backwards in the workflow (e.g., from Testing back to Development),
        the agent receives all relevant context including QA feedback.
        """
        try:
            # Fetch all comments
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            data = json.loads(result.stdout)

            from dateutil import parser as date_parser
            from datetime import timezone

            # Collect ALL agent comments with their timestamps
            agent_comments = []
            for comment in data.get('comments', []):
                body = comment.get('body', '')
                
                # Match agent signature pattern
                agent_name = None
                if '_Processed by the ' in body and ' agent_' in body:
                    import re
                    match = re.search(r'_Processed by the (.+?) agent_', body)
                    if match:
                        agent_name = match.group(1)
                elif 'Processed by the ' in body and ' agent' in body:
                    import re
                    match = re.search(r'Processed by the (.+?) agent', body)
                    if match:
                        agent_name = match.group(1)

                if agent_name:
                    timestamp = comment.get('createdAt')
                    parsed_time = date_parser.parse(timestamp)
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=timezone.utc)

                    agent_comments.append({
                        'agent': agent_name,
                        'body': body,
                        'timestamp': parsed_time,
                        'raw_timestamp': timestamp
                    })

            if not agent_comments:
                return ""  # No agent comments yet

            # Sort agent comments chronologically
            agent_comments.sort(key=lambda x: x['timestamp'])

            # Collect user comments (non-bot comments)
            user_comments = []
            for comment in data.get('comments', []):
                if not comment.get('author', {}).get('isBot', False):
                    timestamp = comment.get('createdAt')
                    parsed_time = date_parser.parse(timestamp)
                    if parsed_time.tzinfo is None:
                        parsed_time = parsed_time.replace(tzinfo=timezone.utc)

                    user_comments.append({
                        'author': comment.get('author', {}).get('login', 'unknown'),
                        'body': comment.get('body', ''),
                        'timestamp': parsed_time
                    })

            # Build chronological context with all agent outputs and user feedback
            context_parts = []
            context_parts.append("## Previous Work and Feedback")
            context_parts.append("\nThe following is a complete history of agent outputs and user feedback for this issue:\n")

            # Merge agent comments and user comments in chronological order
            all_items = []
            for ac in agent_comments:
                all_items.append(('agent', ac))
            for uc in user_comments:
                all_items.append(('user', uc))

            all_items.sort(key=lambda x: x[1]['timestamp'])

            # Format chronologically
            for item_type, item in all_items:
                if item_type == 'agent':
                    context_parts.append(f"\n### Output from {item['agent'].replace('_', ' ').title()}")
                    context_parts.append(item['body'])
                else:
                    context_parts.append(f"\n**User Feedback (@{item['author']})**:")
                    context_parts.append(item['body'])

            return "\n\n".join(context_parts)

        except Exception as e:
            logger.error(f"Error fetching previous stage context: {e}")
            return ""

    def _get_discussion_context(self, discussion_id: str, current_column: str, workflow_template) -> str:
        """Get previous stage context from discussion comments and threaded replies"""
        try:
            # Find previous column in workflow
            column_names = [col.name for col in workflow_template.columns]
            if current_column not in column_names:
                return ""

            current_index = column_names.index(current_column)
            if current_index == 0:
                return ""  # First column, no previous stage

            previous_column = workflow_template.columns[current_index - 1]
            previous_agent = previous_column.agent

            if not previous_agent or previous_agent == 'null':
                return ""  # No agent in previous stage

            # Get discussion with all comments AND REPLIES
            from services.github_app import github_app
            from dateutil import parser as date_parser
            from datetime import timezone

            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion {discussion_id}")
                return ""

            all_comments = result['node']['comments']['nodes']

            # Find the last top-level comment from the previous agent
            # We only want the specific comment thread, not replies buried in other threads
            previous_agent_comment = None
            previous_agent_timestamp = None
            agent_signature = f"_Processed by the {previous_agent} agent_"

            for comment in reversed(all_comments):
                # Only check top-level comments for the agent's output
                if agent_signature in comment.get('body', ''):
                    comment_time = date_parser.parse(comment.get('createdAt'))
                    if previous_agent_timestamp is None or comment_time > previous_agent_timestamp:
                        previous_agent_comment = comment
                        previous_agent_timestamp = comment_time

            if not previous_agent_comment:
                return ""  # Previous agent hasn't processed yet

            # Make timezone-aware if naive
            if previous_agent_timestamp.tzinfo is None:
                previous_agent_timestamp = previous_agent_timestamp.replace(tzinfo=timezone.utc)

            # Get the agent's output
            previous_agent_output = previous_agent_comment.get('body', '')

            # Collect ONLY replies to this specific comment (threaded replies)
            user_feedback = []
            for reply in previous_agent_comment.get('replies', {}).get('nodes', []):
                reply_author = reply.get('author', {})
                reply_author_login = reply_author.get('login', '') if reply_author else ''
                reply_is_bot = 'bot' in reply_author_login.lower()

                # Only include non-bot replies
                if not reply_is_bot:
                    reply_time = date_parser.parse(reply.get('createdAt'))
                    if reply_time.tzinfo is None:
                        reply_time = reply_time.replace(tzinfo=timezone.utc)

                    user_feedback.append({
                        'author': reply_author_login,
                        'body': reply.get('body', ''),
                        'type': 'reply',
                        'time': reply_time
                    })

            # Sort feedback chronologically
            user_feedback.sort(key=lambda x: x['time'])

            # Format context
            context_parts = []
            context_parts.append(f"## Output from {previous_agent.replace('_', ' ').title()}")
            context_parts.append(previous_agent_output)

            if user_feedback:
                context_parts.append("\n## User Feedback Since Then")
                for feedback in user_feedback:
                    feedback_type = " (reply)" if feedback['type'] == 'reply' else ""
                    context_parts.append(f"**@{feedback['author']}**{feedback_type}: {feedback['body']}")

            return "\n\n".join(context_parts)

        except Exception as e:
            logger.error(f"Error fetching discussion context: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _get_agent_outputs_from_discussion(self, discussion_id: str, agent_names: List[str]) -> str:
        """
        Get outputs from specific agents with full threaded context.
        Used when a stage has inputs_from specified.
        
        For each agent:
        1. Find their final output (could be top-level OR a threaded reply)
        2. Collect all threaded conversation (human feedback + agent replies)
        3. Return complete context for each input agent

        Args:
            discussion_id: The discussion ID
            agent_names: List of agent names to get outputs from

        Returns:
            Formatted context string with all specified agent outputs and their threaded conversations
        """
        try:
            from services.github_app import github_app
            from dateutil import parser as date_parser

            # GraphQL query WITH replies to capture threaded conversations
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion {discussion_id}")
                return ""

            all_comments = result['node']['comments']['nodes']

            # Find outputs from each requested agent
            context_parts = []

            for agent_name in agent_names:
                agent_signature = f"_Processed by the {agent_name} agent_"
                alt_signature = f"Processed by the {agent_name} agent"

                # Find agent's FINAL output (could be in top-level OR threaded reply)
                final_output = None
                parent_comment_id = None
                final_timestamp = None

                # Check threaded replies first (most recent refinements)
                for comment in all_comments:
                    for reply in comment.get('replies', {}).get('nodes', []):
                        body = reply.get('body', '')
                        if agent_signature in body or alt_signature in body:
                            reply_time = date_parser.parse(reply.get('createdAt'))
                            if final_timestamp is None or reply_time > final_timestamp:
                                final_output = reply
                                parent_comment_id = comment['id']
                                final_timestamp = reply_time

                # Check top-level comments (initial outputs)
                for comment in all_comments:
                    body = comment.get('body', '')
                    if agent_signature in body or alt_signature in body:
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if final_timestamp is None or comment_time > final_timestamp:
                            final_output = comment
                            parent_comment_id = comment['id']
                            final_timestamp = comment_time

                if not final_output:
                    logger.warning(f"No output found from agent '{agent_name}' in discussion {discussion_id}")
                    continue

                # Build complete context for this agent
                agent_context = []
                agent_context.append(f"## Output from {agent_name.replace('_', ' ').title()}")

                # If we have a parent comment, get the full thread history
                if parent_comment_id:
                    thread_history = self.get_full_thread_history(all_comments, parent_comment_id)

                    if thread_history:
                        # Format thread chronologically
                        for msg in thread_history:
                            author = msg['author']
                            body = msg['body']
                            role = msg['role']

                            if role == 'agent':
                                agent_context.append(f"\n**{agent_name}** (agent):")
                                agent_context.append(body)
                            else:
                                agent_context.append(f"\n**@{author}** (human feedback):")
                                agent_context.append(body)
                    else:
                        # Fallback: just the final output
                        agent_context.append(final_output.get('body', ''))
                else:
                    # Just the final output (no thread)
                    agent_context.append(final_output.get('body', ''))

                context_parts.append('\n'.join(agent_context))

            return "\n\n---\n\n".join(context_parts) if context_parts else ""

        except Exception as e:
            logger.error(f"Error fetching agent outputs from discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _get_agent_outputs_from_issue(self, repository: str, issue_number: int, org: str, agent_names: List[str]) -> str:
        """
        Get outputs from specific agents from an issue.
        Similar to _get_agent_outputs_from_discussion but for issues.
        
        For each agent:
        1. Find their most recent output in the issue comments
        2. Return the complete comment body

        Args:
            repository: Repository name
            issue_number: Issue number
            org: Organization name
            agent_names: List of agent names to get outputs from

        Returns:
            Formatted context string with all specified agent outputs
        """
        try:
            import subprocess
            import json
            from dateutil import parser as date_parser

            # Fetch all comments from the issue
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo', f"{org}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            data = json.loads(result.stdout)
            all_comments = data.get('comments', [])

            # Find outputs from each requested agent
            context_parts = []

            for agent_name in agent_names:
                agent_signature = f"_Processed by the {agent_name} agent_"
                alt_signature = f"Processed by the {agent_name} agent"

                # Find agent's most recent output
                final_output = None
                final_timestamp = None

                for comment in all_comments:
                    body = comment.get('body', '')
                    if agent_signature in body or alt_signature in body:
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if final_timestamp is None or comment_time > final_timestamp:
                            final_output = comment
                            final_timestamp = comment_time

                if not final_output:
                    # Determine appropriate log level based on context
                    # Check if this is expected (standalone issue) or a workflow gap (sub-issue without parent)
                    log_level = self._determine_missing_input_severity(
                        repository, issue_number, org, agent_name
                    )

                    if log_level == "WARNING":
                        logger.warning(
                            f"No output found from agent '{agent_name}' in issue #{issue_number} "
                            f"(expected parent epic with design)"
                        )
                    elif log_level == "INFO":
                        logger.info(
                            f"Agent '{agent_name}' output not found in issue #{issue_number}, "
                            f"using fallback context"
                        )
                    else:  # DEBUG
                        logger.debug(
                            f"Agent '{agent_name}' output not required for issue #{issue_number}"
                        )
                    continue

                # Build context for this agent
                agent_context = []
                agent_context.append(f"## Output from {agent_name.replace('_', ' ').title()}")
                agent_context.append(final_output.get('body', ''))

                context_parts.append('\n'.join(agent_context))

            return "\n\n---\n\n".join(context_parts) if context_parts else ""

        except subprocess.CalledProcessError as e:
            logger.error(f"Error fetching issue comments: {e}")
            return ""
        except Exception as e:
            logger.error(f"Error fetching agent outputs from issue: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return ""

    def _determine_missing_input_severity(
        self, repository: str, issue_number: int, org: str, agent_name: str
    ) -> str:
        """
        Determine appropriate log severity for missing agent output.

        Returns:
            "WARNING" if issue should have parent/discussion with agent output
            "INFO" if missing output is acceptable with fallback
            "DEBUG" if output is optional for this issue type
        """
        try:
            import subprocess
            import json
            import re

            # Get issue metadata
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number),
                 '--repo', f"{org}/{repository}",
                 '--json', 'body,labels'],
                capture_output=True, text=True, check=True
            )
            issue_data = json.loads(result.stdout)
            body = issue_data.get('body', '')
            labels = [label['name'] for label in issue_data.get('labels', [])]

            # Check if this is a sub-issue (should have parent with design)
            parent_match = re.search(r'Part of #(\d+)', body)
            is_sub_issue = parent_match is not None

            # Check if this is an environment/tooling issue (doesn't need architecture)
            is_env_issue = any(
                label in labels
                for label in ['pipeline:env', 'environment', 'tooling', 'dependencies']
            )

            # Determine severity
            if agent_name == 'software_architect':
                if is_env_issue:
                    return "DEBUG"  # Environment issues don't need architecture
                elif is_sub_issue:
                    return "WARNING"  # Sub-issue should have parent with design
                else:
                    return "INFO"  # Standalone issue, fallback is acceptable

            # For other agents, INFO is appropriate (fallback available)
            return "INFO"

        except Exception as e:
            logger.debug(f"Error determining log severity: {e}")
            return "INFO"  # Default to INFO on error

    def _check_agent_processed_issue_sync(self, issue_number: int, agent: str, repository: str, org: str, workspace_type: str = 'issues', discussion_id: Optional[str] = None) -> bool:
        """Synchronous wrapper for checking if agent has processed issue"""
        try:
            import asyncio
            from services.github_integration import GitHubIntegration
            github = GitHubIntegration(repo_owner=org, repo_name=repository)
            
            if workspace_type == 'discussions' and discussion_id:
                return asyncio.run(github.has_agent_processed_discussion(discussion_id, agent))
            else:
                return asyncio.run(github.has_agent_processed_issue(issue_number, agent, repository))
        except Exception as e:
            logger.warning(f"Could not check for prior agent work: {e}")
            return False

    def trigger_agent_for_status(self, project_name: str, board_name: str, issue_number: int, status: str, repository: str) -> Optional[str]:
        """Determine which agent should handle this status and create a task or review cycle"""
        try:
            # Get workflow template for this board
            project_config = self.config_manager.get_project_config(project_name)

            # Find the pipeline config for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                logger.info(f"No pipeline config found for board {board_name}")
                return None

            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            # DEFENSIVE: Check if issue is open before triggering any agents
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])
            issue_state = issue_data.get('state', '').upper()

            if issue_state == 'CLOSED':
                logger.info(f"Issue #{issue_number} is CLOSED - checking if lock needs release")
                
                # Check if lock is held and release it
                from services.pipeline_lock_manager import get_pipeline_lock_manager
                lock_manager = get_pipeline_lock_manager()
                lock = lock_manager.get_lock(project_name, board_name)
                
                if lock and lock.locked_by_issue == issue_number:
                    logger.info(f"Releasing pipeline lock for closed issue #{issue_number}")
                    self._release_pipeline_lock_and_process_next(
                        project_name, board_name, issue_number, status,
                        repository, workflow_template
                    )
                
                return None

            # Find the column that matches this status
            agent = None
            column = None
            for col in workflow_template.columns:
                if col.name == status:
                    agent = col.agent
                    column = col
                    break

            # NEW: Pipeline lock and queue management
            # Check if this column triggers pipeline execution (requires exclusive lock)
            is_trigger_column = False
            is_exit_column = False

            if hasattr(workflow_template, 'pipeline_trigger_columns') and workflow_template.pipeline_trigger_columns:
                is_trigger_column = status in workflow_template.pipeline_trigger_columns

            if hasattr(workflow_template, 'pipeline_exit_columns') and workflow_template.pipeline_exit_columns:
                is_exit_column = status in workflow_template.pipeline_exit_columns

            # If this is an exit column, release lock and process next waiting issue
            if is_exit_column:
                self._release_pipeline_lock_and_process_next(
                    project_name, board_name, issue_number, status,
                    repository, workflow_template
                )
                # Don't create new tasks for exit columns (no agents there)
                return None

            # If this is a trigger column with an agent, check pipeline lock
            if is_trigger_column and agent and agent != 'null':
                from services.pipeline_lock_manager import get_pipeline_lock_manager
                from services.pipeline_queue_manager import get_pipeline_queue_manager

                lock_manager = get_pipeline_lock_manager()
                pipeline_queue = get_pipeline_queue_manager(project_name, board_name)

                # Always call enqueue_issue to handle re-queueing of completed issues
                # The enqueue_issue method handles all cases:
                # - New issue: adds to queue
                # - Completed issue moved back: resets to waiting
                # - Already queued: no-op
                pipeline_queue.enqueue_issue(
                    issue_number=issue_number,
                    column=status,
                    timestamp=utc_isoformat()
                )

                # Check if this issue already holds the pipeline lock
                # If it does, skip queue priority check to avoid race conditions
                # (The lock was already acquired with priority validation)
                current_lock = lock_manager.get_lock(project_name, board_name)
                already_has_lock = (current_lock and
                                   current_lock.lock_status == 'locked' and
                                   current_lock.locked_by_issue == issue_number)

                if not already_has_lock:
                    # CRITICAL: Check if this issue is next in line based on GitHub board order
                    # This ensures we respect the user's ordering on the GitHub board
                    next_issue = pipeline_queue.get_next_waiting_issue()
                    if next_issue and next_issue['issue_number'] != issue_number:
                        logger.info(
                            f"Issue #{issue_number} waiting in queue: "
                            f"issue #{next_issue['issue_number']} is ahead in GitHub board order "
                            f"(position {next_issue.get('position_in_column', '?')})"
                        )
                        return None  # Wait for higher-priority issues to execute first

                    # Try to acquire pipeline lock
                    can_execute, reason = lock_manager.try_acquire_lock(
                        project=project_name,
                        board=board_name,
                        issue_number=issue_number
                    )

                    if not can_execute:
                        logger.info(
                            f"Issue #{issue_number} waiting for pipeline access: {reason}"
                        )
                        return None  # Don't create task yet - waiting in queue

                    # Lock acquired - mark issue as active in queue
                    pipeline_queue.mark_issue_active(issue_number)
                    logger.info(f"Issue #{issue_number} acquired pipeline lock, proceeding with execution")
                else:
                    # Issue already holds the lock (called from queue processor after lock acquisition)
                    # Ensure it's marked as active in the queue
                    pipeline_queue.mark_issue_active(issue_number)
                    logger.info(f"Issue #{issue_number} already holds pipeline lock, proceeding with execution")

            if agent and agent != 'null':
                # Determine workspace type and get discussion ID FIRST, before using them
                from config.state_manager import state_manager
                workspace_type = pipeline_config.workspace
                discussion_id = None

                if workspace_type in ['discussions', 'hybrid']:
                    discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

                # Get or create pipeline run early so we can tag all events
                # Fetch issue details for pipeline run
                issue_data_early = self.get_issue_details(repository, issue_number, project_config.github['org'])
                pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
                    issue_number=issue_number,
                    issue_title=issue_data_early.get('title', f'Issue #{issue_number}'),
                    issue_url=issue_data_early.get('url', ''),
                    project=project_name,
                    board=board_name
                )
                logger.debug(f"Using pipeline run {pipeline_run.id} for issue #{issue_number}")

                # EMIT DECISION EVENT: Agent routing decision
                # Collect alternative agents from workflow
                alternative_agents = [
                    col.agent for col in workflow_template.columns
                    if col.agent and col.agent != 'null' and col.agent != agent
                ]
                
                self.decision_events.emit_agent_routing_decision(
                    issue_number=issue_number,
                    project=project_name,
                    board=board_name,
                    current_status=status,
                    selected_agent=agent,
                    reason=f"Status '{status}' maps to agent '{agent}' in workflow '{pipeline_config.workflow}'",
                    alternatives=alternative_agents,
                    workspace_type=workspace_type,
                    pipeline_run_id=pipeline_run.id
                )
                
                # Check if there's already a pending task for this issue and agent
                existing_tasks = self.task_queue.get_pending_tasks()
                for existing_task in existing_tasks:
                    task_context = existing_task.context
                    if (task_context.get('issue_number') == issue_number and
                        task_context.get('project') == project_name and
                        task_context.get('board') == board_name and
                        existing_task.agent == agent):
                        logger.info(f"Task already exists for {agent} on issue #{issue_number} - skipping duplicate")
                        return None

                # Check if agent should execute work using execution state tracker
                # This check must happen BEFORE column type routing to prevent duplicate runs
                import asyncio
                try:
                    # Create a new event loop for this thread if needed
                    try:
                        loop = asyncio.get_running_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)

                    from services.work_execution_state import work_execution_tracker

                    # Determine trigger source (manual move from GitHub)
                    trigger_source = 'manual'

                    # Track if this issue has already been handled (don't start fresh work):
                    # 1. Work already executed (per execution state tracker)
                    # 2. Resume thread already started (for review/conversational columns)
                    already_handled = False

                    # Check if work should be executed using the new execution state logic
                    should_execute, reason = work_execution_tracker.should_execute_work(
                        issue_number=issue_number,
                        column=status,
                        agent=agent,
                        trigger_source=trigger_source,
                        project_name=project_name
                    )

                    if not should_execute:
                        already_handled = True  # Work was already executed
                        logger.info(
                            f"Skipping {agent} on issue #{issue_number} in {status}: {reason}"
                        )

                    # For backward compatibility, also check comment signatures for discussions
                    already_processed = False
                    if workspace_type in ['discussions', 'hybrid'] and discussion_id:
                        from services.github_integration import GitHubIntegration
                        github = GitHubIntegration(repo_owner=project_config.github['org'], repo_name=repository)

                        # For discussions, also check discussion comments (fallback)
                        # Use a separate thread to avoid "loop already running" errors if called from async context
                        import concurrent.futures
                        
                        def run_check():
                            new_loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(new_loop)
                            try:
                                return new_loop.run_until_complete(
                                    github.has_agent_processed_discussion(discussion_id, agent)
                                )
                            finally:
                                new_loop.close()

                        with concurrent.futures.ThreadPoolExecutor() as executor:
                            already_processed = executor.submit(run_check).result()

                        if already_processed:
                            logger.info(f"Agent {agent} has already processed discussion for issue #{issue_number} (comment signature found)")

                            # For review columns, attempt to resume the review cycle in background thread
                            if column and hasattr(column, 'type') and column.type == 'review':
                                logger.info(f"Attempting to resume review cycle for issue #{issue_number} in background thread")
                                try:
                                    from services.review_cycle import review_cycle_executor
                                    import threading

                                    def resume_in_thread():
                                        """Resume review cycle in background thread (non-blocking)"""
                                        try:
                                            loop = asyncio.new_event_loop()
                                            asyncio.set_event_loop(loop)

                                            next_column, success = loop.run_until_complete(
                                                review_cycle_executor.resume_review_cycle(
                                                    issue_number=issue_number,
                                                    repository=repository,
                                                    project_name=project_name,
                                                    board_name=board_name,
                                                    org=project_config.github['org'],
                                                    discussion_id=discussion_id,
                                                    column=column,
                                                    issue_data=self.get_issue_details(repository, issue_number, project_config.github['org']),
                                                    workflow_columns=workflow_template.columns
                                                )
                                            )

                                            if success:
                                                logger.info(f"Review cycle resumed successfully for issue #{issue_number}")
                                                if next_column and next_column != column.name:
                                                    logger.info(f"Review cycle complete, ready to advance to: {next_column}")
                                            else:
                                                logger.info(f"Review cycle could not be resumed for issue #{issue_number}")

                                            loop.close()
                                        except Exception as e:
                                            logger.error(f"Failed to resume review cycle: {e}")
                                            import traceback
                                            logger.error(traceback.format_exc())

                                    thread = threading.Thread(target=resume_in_thread, daemon=True)
                                    thread.start()
                                    logger.info(f"Review cycle resume thread started for issue #{issue_number}")

                                    # Resume thread is monitoring - don't start fresh work
                                    already_handled = True

                                except Exception as e:
                                    logger.error(f"Failed to start review cycle resume thread: {e}")
                                    import traceback
                                    logger.error(traceback.format_exc())
                            # For conversational columns, resume the feedback monitoring loop
                            elif column and hasattr(column, 'type') and column.type == 'conversational':
                                logger.info(f"Resuming conversational feedback loop for issue #{issue_number} in background thread")
                                try:
                                    from services.human_feedback_loop import human_feedback_loop_executor
                                    import threading

                                    def resume_feedback_loop():
                                        """Resume conversational feedback loop in background thread"""
                                        try:
                                            loop = asyncio.new_event_loop()
                                            asyncio.set_event_loop(loop)

                                            # Initialize executor (cleanup stale locks on first use)
                                            loop.run_until_complete(
                                                human_feedback_loop_executor.initialize()
                                            )

                                            # Just start monitoring - no initial execution needed
                                            from services.human_feedback_loop import HumanFeedbackState

                                            state = HumanFeedbackState(
                                                issue_number=issue_number,
                                                repository=repository,
                                                agent=agent,
                                                project_name=project_name,
                                                board_name=board_name,
                                                workspace_type=workspace_type,
                                                discussion_id=discussion_id,
                                                pipeline_run_id=pipeline_run.id
                                            )

                                            # Load persisted session_id for continuity across restarts
                                            from services.conversational_session_state import conversational_session_state
                                            persisted_session = conversational_session_state.load_session(
                                                project_name=project_name,
                                                issue_number=issue_number,
                                                max_age_hours=24
                                            )
                                            if persisted_session:
                                                state.claude_session_id = persisted_session.session_id
                                                logger.info(f"Restored Claude Code session for #{issue_number}: {state.claude_session_id}")

                                            # Load previous outputs from discussion to rebuild conversation history
                                            loop.run_until_complete(
                                                human_feedback_loop_executor._load_previous_outputs_from_discussion(
                                                    state,
                                                    project_config.github['org']
                                                )
                                            )

                                            # Register and start monitoring
                                            human_feedback_loop_executor.active_loops[issue_number] = state
                                            human_feedback_loop_executor.workflow_columns = workflow_template.columns

                                            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

                                            loop.run_until_complete(
                                                human_feedback_loop_executor._conversational_loop(
                                                    state, column, issue_data, project_config.github['org']
                                                )
                                            )

                                            loop.close()
                                        except Exception as e:
                                            logger.error(f"Failed to resume feedback loop: {e}")
                                            import traceback
                                            logger.error(traceback.format_exc())

                                    thread = threading.Thread(target=resume_feedback_loop, daemon=True)
                                    thread.start()
                                    logger.info(f"Conversational feedback loop monitoring resumed for issue #{issue_number}")

                                    # Resume thread is monitoring - don't start fresh work
                                    already_handled = True

                                except Exception as e:
                                    logger.error(f"Failed to start conversational feedback loop thread: {e}")
                                    import traceback
                                    logger.error(traceback.format_exc())
                            else:
                                logger.info(f"Not a review or conversational column, skipping resume attempt")
                    else:
                        # For issues workspace, check deduplication
                        # BUT skip this check for review and conversational columns
                        if column and column.type == 'review':
                            # Review columns are handled by review cycle executor
                            # Don't use deduplication - cycles need reviewers to run multiple times
                            logger.debug(f"Skipping deduplication check for review column")
                        elif column and column.type == 'conversational':
                            # Check if there's existing work to resume
                            # Only resume if there's evidence of prior agent activity
                            has_prior_work = self._check_agent_processed_issue_sync(
                                issue_number, agent, repository, project_config.github['org'],
                                workspace_type=workspace_type, discussion_id=discussion_id
                            )

                            if has_prior_work:
                                # Resume existing conversational feedback loop
                                logger.info(f"Resuming conversational feedback loop for issue #{issue_number} (workspace: {workspace_type})")
                                try:
                                    from services.human_feedback_loop import human_feedback_loop_executor
                                    import threading

                                    def resume_feedback_loop():
                                        """Resume conversational feedback loop in background thread"""
                                        try:
                                            loop_new = asyncio.new_event_loop()
                                            asyncio.set_event_loop(loop_new)

                                            # Initialize executor (cleanup stale locks on first use)
                                            loop_new.run_until_complete(
                                                human_feedback_loop_executor.initialize()
                                            )

                                            # Create state for this feedback loop
                                            from services.human_feedback_loop import HumanFeedbackState

                                            state = HumanFeedbackState(
                                                issue_number=issue_number,
                                                repository=repository,
                                                agent=agent,
                                                project_name=project_name,
                                                board_name=board_name,
                                                workspace_type=workspace_type,
                                                discussion_id=discussion_id,
                                                pipeline_run_id=pipeline_run.id
                                            )

                                            # Load persisted session_id for continuity across restarts
                                            from services.conversational_session_state import conversational_session_state
                                            persisted_session = conversational_session_state.load_session(
                                                project_name=project_name,
                                                issue_number=issue_number,
                                                max_age_hours=24
                                            )
                                            if persisted_session:
                                                state.claude_session_id = persisted_session.session_id
                                                logger.info(f"Restored Claude Code session for #{issue_number}: {state.claude_session_id}")

                                            # Load previous outputs to rebuild conversation history
                                            if workspace_type == 'discussions':
                                                loop_new.run_until_complete(
                                                    human_feedback_loop_executor._load_previous_outputs_from_discussion(
                                                        state,
                                                        project_config.github['org']
                                                    )
                                                )
                                            else:
                                                loop_new.run_until_complete(
                                                    human_feedback_loop_executor._load_previous_outputs_from_issue(
                                                        state,
                                                        project_config.github['org']
                                                    )
                                                )

                                            # Register and start monitoring
                                            human_feedback_loop_executor.active_loops[issue_number] = state
                                            human_feedback_loop_executor.workflow_columns = workflow_template.columns

                                            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

                                            loop_new.run_until_complete(
                                                human_feedback_loop_executor._conversational_loop(
                                                    state, column, issue_data, project_config.github['org']
                                                )
                                            )

                                            loop_new.close()
                                        except Exception as e:
                                            logger.error(f"Failed to resume feedback loop: {e}")
                                            import traceback
                                            logger.error(traceback.format_exc())

                                    thread = threading.Thread(target=resume_feedback_loop, daemon=True)
                                    thread.start()
                                    logger.info(f"Conversational feedback loop monitoring resumed for issue #{issue_number}")

                                    # Return early - we've started the monitoring loop
                                    return agent

                                except Exception as e:
                                    logger.error(f"Failed to start conversational feedback loop thread: {e}")
                                    import traceback
                                    logger.error(traceback.format_exc())
                                    # If resume fails, allow normal startup below
                            else:
                                # No prior work found, will start fresh conversational loop below
                                logger.debug(f"No prior work found for conversational column, will start fresh loop")
                        else:
                            # For non-review, non-conversational columns in issues workspace
                            # Rely on execution state tracker (already checked above)
                            pass

                    # If already handled (work executed or resume thread started), don't start fresh work
                    if already_handled:
                        return None

                except Exception as e:
                    logger.warning(f"Could not check if issue was already processed: {e}")
                    import traceback
                    logger.warning(traceback.format_exc())
                    # Continue anyway if we can't check

            # Check column type and route appropriately
            # This happens AFTER the "already processed" check to prevent duplicate runs
            if column and hasattr(column, 'type'):
                if column.type == 'conversational':
                    logger.info(f"Starting conversational loop for issue #{issue_number} in {status}")
                    return self._start_conversational_loop_for_issue(
                        project_name, board_name, issue_number, status,
                        repository, project_config, pipeline_config,
                        workflow_template, column,
                        pipeline_run_id=pipeline_run.id
                    )
                elif column.type == 'review':
                    logger.info(f"Starting review cycle for issue #{issue_number} in {status}")
                    return self._start_review_cycle_for_issue(
                        project_name, board_name, issue_number, status,
                        repository, project_config, pipeline_config,
                        workflow_template, column
                    )

            if agent and agent != 'null':

                # Fetch full issue details from GitHub (use issue_data_early from above)
                issue_data = issue_data_early

                # Get the stage config from pipeline template for this column
                pipeline_template = self.config_manager.get_pipeline_template(pipeline_config.template)
                current_stage_config = None
                if column:
                    # Map column name to stage - column.stage_mapping should give us the stage name
                    stage_name = column.stage_mapping if hasattr(column, 'stage_mapping') else None
                    if stage_name and pipeline_template:
                        # Find the stage config in the pipeline template
                        for stage in pipeline_template.stages:
                            if stage.stage == stage_name:
                                current_stage_config = stage
                                break

                # Check if this is a repair_cycle stage and handle it specially
                if current_stage_config and getattr(current_stage_config, 'stage_type', None) == 'repair_cycle':
                    logger.info(f"Detected repair_cycle stage for issue #{issue_number} in {status}")
                    return self._start_repair_cycle_for_issue(
                        project_name, board_name, issue_number, status,
                        repository, project_config, pipeline_config,
                        workflow_template, column, current_stage_config
                    )

                # Fetch context from previous workflow stage (workspace-aware)
                previous_stage_context = self.get_previous_stage_context(
                    repository, issue_number, project_config.github['org'],
                    status, workflow_template,
                    workspace_type=workspace_type,
                    discussion_id=discussion_id,
                    pipeline_config=pipeline_config,
                    current_stage_config=current_stage_config,
                    project_name=project_name
                )

                # Pipeline run already created above, just use it
                logger.info(f"Creating task with pipeline run {pipeline_run.id} for issue #{issue_number}")

                # Create task for the agent
                task_context = {
                    'project': project_name,
                    'board': board_name,
                    'pipeline': pipeline_config.name,
                    'repository': repository,
                    'issue_number': issue_number,
                    'issue': issue_data,  # Include full issue details
                    'previous_stage_output': previous_stage_context,  # Include previous agent's work
                    'column': status,
                    'trigger': 'project_monitor',
                    'workspace_type': workspace_type,
                    'pipeline_run_id': pipeline_run.id,  # Include pipeline run ID
                    'timestamp': utc_isoformat()
                }

                # Add discussion_id if working in discussions
                if discussion_id:
                    task_context['discussion_id'] = discussion_id

                task = Task(
                    id=f"{agent}_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                    agent=agent,
                    project=project_name,
                    priority=TaskPriority.MEDIUM,
                    context=task_context,
                    created_at=utc_isoformat()
                )

                # Record execution start in work execution state FIRST
                # CRITICAL: Must happen before enqueue to prevent race condition
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_start(
                    issue_number=issue_number,
                    column=status,
                    agent=agent,
                    trigger_source='manual',  # Triggered from project monitor
                    project_name=project_name
                )

                # EMIT DECISION EVENT: Task queued
                self.decision_events.emit_task_queued(
                    agent=agent,
                    project=project_name,
                    issue_number=issue_number,
                    board=board_name,
                    priority='MEDIUM',
                    reason=f"Agent '{agent}' assigned to issue #{issue_number} in status '{status}'",
                    pipeline_run_id=pipeline_run.id
                )

                # Enqueue task LAST so workers find in_progress state
                self.task_queue.enqueue(task)

                logger.info(f"Created task for {agent} - Issue #{issue_number} moved to {status} on {board_name}")
                return agent
            else:
                # No agent for this column - end pipeline run if one exists
                logger.info(f"No agent assigned to column '{status}' in {board_name}")

                # End active pipeline run (issue has reached end of pipeline)
                ended = self.pipeline_run_manager.end_pipeline_run(
                    project=project_name,
                    issue_number=issue_number,
                    reason=f"Issue moved to column '{status}' with no agent"
                )
                if ended:
                    logger.info(f"Ended pipeline run for issue #{issue_number} (no agent in column '{status}')")

                # Release pipeline lock if held by this issue
                from services.pipeline_lock_manager import get_pipeline_lock_manager
                lock_manager = get_pipeline_lock_manager()
                lock = lock_manager.get_lock(project_name, board_name)

                if lock and lock.locked_by_issue == issue_number:
                    logger.info(f"Releasing pipeline lock for {project_name}/{board_name} (issue #{issue_number} moved to no-agent column '{status}')")
                    lock_manager.release_lock(project_name, board_name, issue_number)

                # Reset queue status so issue can be re-queued if moved back to trigger column
                from services.pipeline_queue_manager import get_pipeline_queue_manager
                pipeline_queue = get_pipeline_queue_manager(project_name, board_name)
                pipeline_queue.reset_issue_to_waiting(issue_number)

                # Clean up any active conversational loop state
                from services.human_feedback_loop import human_feedback_loop_executor
                human_feedback_loop_executor.cleanup_loop(
                    project_name=project_name,
                    issue_number=issue_number,
                    reason=f"Issue moved to no-agent column '{status}'"
                )

                return None

        except Exception as e:
            logger.error(f"Error triggering agent for status: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _release_pipeline_lock_and_process_next(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        exit_column: str,
        repository: str,
        workflow_template
    ):
        """
        Release pipeline lock when issue reaches exit column and process next waiting issue.

        Args:
            project_name: Project name
            board_name: Board name
            issue_number: Issue number that reached exit column
            exit_column: The exit column name
            repository: Repository name
            workflow_template: Workflow template with exit columns config
        """
        try:
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            from services.pipeline_queue_manager import get_pipeline_queue_manager

            lock_manager = get_pipeline_lock_manager()
            pipeline_queue = get_pipeline_queue_manager(project_name, board_name)

            # Get current lock
            lock = lock_manager.get_lock(project_name, board_name)

            # Safety check: Verify this issue actually holds the lock
            if lock and lock.locked_by_issue != issue_number:
                logger.warning(
                    f"Issue #{issue_number} reached exit column '{exit_column}' but doesn't hold lock "
                    f"(lock held by #{lock.locked_by_issue})"
                )
                return

            # Release the lock
            if lock:
                released = lock_manager.release_lock(project_name, board_name, issue_number)
                if released:
                    logger.info(
                        f"Released pipeline lock for {project_name}/{board_name} "
                        f"(issue #{issue_number} reached '{exit_column}')"
                    )
                else:
                    logger.warning(f"Failed to release lock for issue #{issue_number}")
                    return

            # Remove issue from queue (it has exited the pipeline)
            # It will be re-added if moved back to trigger column
            if pipeline_queue.is_issue_in_queue(issue_number):
                pipeline_queue.remove_issue_from_queue(issue_number)

            # End the pipeline run for this issue (it has exited the pipeline)
            ended = self.pipeline_run_manager.end_pipeline_run(
                project=project_name,
                issue_number=issue_number,
                reason=f"Issue reached exit column '{exit_column}'"
            )
            if ended:
                logger.info(f"Ended pipeline run for issue #{issue_number} (reached '{exit_column}')")

            # Process next waiting issue
            next_issue = pipeline_queue.get_next_waiting_issue()
            if next_issue:
                logger.info(
                    f"Processing next queued issue #{next_issue['issue_number']} "
                    f"for {project_name}/{board_name}"
                )

                # Acquire lock for next issue
                acquired, reason = lock_manager.try_acquire_lock(
                    project=project_name,
                    board=board_name,
                    issue_number=next_issue['issue_number']
                )

                if acquired:
                    # Mark as active in queue
                    pipeline_queue.mark_issue_active(next_issue['issue_number'])

                    # Get current column for the next issue from GitHub
                    # (May have moved since it was queued)
                    current_column = self.get_issue_column_sync(
                        project_name, board_name, next_issue['issue_number']
                    )

                    if current_column:
                        logger.info(
                            f"Triggering agent for next queued issue #{next_issue['issue_number']} "
                            f"in column '{current_column}'"
                        )
                        # Trigger agent for the waiting issue
                        self.trigger_agent_for_status(
                            project_name, board_name,
                            next_issue['issue_number'],
                            current_column,
                            repository
                        )
                    else:
                        logger.warning(
                            f"Could not determine current column for issue #{next_issue['issue_number']}, "
                            f"removing from queue"
                        )
                        pipeline_queue.remove_issue_from_queue(next_issue['issue_number'])
                        lock_manager.release_lock(project_name, board_name, next_issue['issue_number'])
                else:
                    logger.error(
                        f"Failed to acquire lock for next issue #{next_issue['issue_number']}: {reason}"
                    )
            else:
                logger.info(
                    f"No waiting issues in pipeline queue for {project_name}/{board_name}"
                )

            # CRITICAL: Check if PR should be marked ready after issue exits pipeline
            # This handles the case where all sub-issues complete after the last finalization
            import asyncio
            try:
                asyncio.run(
                    self._check_pr_ready_on_issue_exit(project_name, issue_number, exit_column)
                )
            except Exception as e:
                logger.error(
                    f"CRITICAL: Failed to check PR ready on issue exit for #{issue_number}: {e}",
                    exc_info=True
                )

        except Exception as e:
            logger.error(f"Error releasing pipeline lock and processing next issue: {e}")
            import traceback
            logger.error(traceback.format_exc())

    async def _check_pr_ready_on_issue_exit(
        self,
        project_name: str,
        issue_number: int,
        exit_column: str
    ):
        """
        Check if PR should be marked ready when an issue exits the pipeline.

        This handles the case where:
        1. Agent finishes work and calls finalize_workspace() BEFORE issue is closed
        2. finalize_workspace() checks sub-issue completion but some are still open
        3. Issue then gets moved to Done/Staged (exit column) and closes
        4. NOW all sub-issues might be complete, so we check again and mark PR ready

        Safety features:
        - Idempotent: Multiple concurrent calls won't cause duplicate PR marking
        - Defensive: Validates parent/PR existence at each step
        - Race-condition safe: Uses GitHub as source of truth

        Args:
            project_name: Project name
            issue_number: Issue number that just exited pipeline
            exit_column: The exit column name (Done, Staged, etc.)
        """
        try:
            from services.feature_branch_manager import feature_branch_manager
            from services.github_integration import GitHubIntegration

            # Step 1: Initialize GitHub integration
            project_config = self.config_manager.get_project_config(project_name)
            github = GitHubIntegration(project_config)

            # Step 2: Check if this issue has a parent (is it a sub-issue?)
            # FIX: Use correct async method get_parent_issue instead of non-existent _get_parent_issue_number
            parent_issue_number = await feature_branch_manager.get_parent_issue(
                github,
                issue_number,
                project=project_name
            )

            if not parent_issue_number:
                logger.debug(f"Issue #{issue_number} has no parent, skipping PR ready check")
                return

            logger.info(
                f"Issue #{issue_number} is sub-issue of #{parent_issue_number} and reached '{exit_column}'. "
                f"Checking if all sub-issues complete to mark PR ready..."
            )

            # Step 3: Get parent issue data
            parent_issue_data = await github.get_issue(parent_issue_number)
            if not parent_issue_data:
                logger.warning(f"Could not get parent issue #{parent_issue_number}, skipping PR ready check")
                return

            # Step 4: Get all sub-issues from parent (queries GitHub directly)
            actual_sub_issues = await feature_branch_manager._get_sub_issues_from_parent(
                github, parent_issue_data
            )

            if len(actual_sub_issues) == 0:
                logger.debug(f"Parent #{parent_issue_number} has no sub-issues, skipping PR ready check")
                return

            # Step 5: Check if ALL sub-issues are actually complete in GitHub
            all_complete = await feature_branch_manager._verify_all_sub_issues_complete(
                github, actual_sub_issues
            )

            if not all_complete:
                closed_count = len([s for s in actual_sub_issues if s.get('state') == 'closed'])
                logger.info(
                    f"Not all sub-issues complete for parent #{parent_issue_number} yet "
                    f"({closed_count}/{len(actual_sub_issues)} closed)"
                )
                return

            logger.info(
                f"✓ All {len(actual_sub_issues)} sub-issues complete for parent #{parent_issue_number}! "
                f"Checking PR status..."
            )

            # Step 6: Get feature branch state (queries git for branch name)
            # FIX: Use correct method name get_feature_branch_state instead of non-existent load_feature_branch_state
            feature_branch = feature_branch_manager.get_feature_branch_state(
                project_name, parent_issue_number
            )

            if not feature_branch:
                logger.warning(
                    f"No feature branch found for parent #{parent_issue_number}. "
                    f"Cannot mark PR ready without branch information."
                )
                return

            # Step 7: Find PR for this branch (queries GitHub)
            # FIX: get_feature_branch_state doesn't populate pr_number, so we need to query it
            pr_data = await github.find_pr_by_branch(feature_branch.branch_name)

            if not pr_data:
                logger.warning(
                    f"No PR found for branch '{feature_branch.branch_name}' (parent #{parent_issue_number}). "
                    f"PR may not have been created yet."
                )
                return

            pr_number = pr_data.get('number')
            is_draft = pr_data.get('isDraft', True)

            # Step 8: Check if PR is already ready (idempotent check for race conditions)
            if not is_draft:
                logger.debug(f"PR #{pr_number} is already marked ready, skipping")
                return

            logger.info(f"PR #{pr_number} is currently draft, marking as ready for review...")

            # Step 9: Mark PR as ready for review
            success = await github.mark_pr_ready(pr_number)

            if success:
                # Update cache with PR status (cache-only, no file persistence)
                feature_branch.pr_number = pr_number
                feature_branch.pr_status = "ready"
                feature_branch_manager.save_feature_branch_state(project_name, feature_branch)

                logger.info(
                    f"✓ Successfully marked PR #{pr_number} as ready for review "
                    f"(triggered by issue #{issue_number} completing)"
                )

                # Step 10: Post completion comment to parent issue
                pr_url = pr_data.get('url') or f"https://github.com/{project_config.github['org']}/{project_config.github['repo']}/pull/{pr_number}"
                await feature_branch_manager.post_feature_completion_comment(
                    github,
                    parent_issue_number,
                    pr_url
                )

                logger.info(f"✓ Posted completion comment to parent issue #{parent_issue_number}")
            else:
                logger.error(
                    f"✗ FAILED to mark PR #{pr_number} as ready for review. "
                    f"GitHub API call failed. Manual intervention required."
                )

                # Post warning comment to parent issue
                await github.add_comment(
                    parent_issue_number,
                    f"⚠️ **Warning**: All sub-issues have been completed, but the system failed to mark "
                    f"PR #{pr_number} as ready for review. Please manually mark it ready:\n\n"
                    f"```\ngh pr ready {pr_number}\n```"
                )

        except Exception as e:
            logger.error(f"Error checking PR ready on issue exit for #{issue_number}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def get_issue_column_sync(self, project_name: str, board_name: str, issue_number: int) -> Optional[str]:
        """
        Get the current column/status for a specific issue (synchronous version).

        Args:
            project_name: Project name
            board_name: Board name
            issue_number: GitHub issue number

        Returns:
            Column name (status) if found, None otherwise
        """
        try:
            from config.state_manager import state_manager

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find the board state
            project_state = state_manager.load_project_state(project_name)
            if not project_state:
                logger.error(f"No project state found for {project_name}")
                return None

            board_state = project_state.boards.get(board_name)
            if not board_state:
                logger.error(f"No board state found for {project_name}/{board_name}")
                return None

            # Query project items
            items = self.get_project_items(
                project_config.github['org'],
                board_state.project_number
            )

            # Find the specific issue
            for item in items:
                if item.issue_number == issue_number:
                    return item.status

            logger.debug(f"Issue #{issue_number} not found in {project_name}/{board_name}")
            return None

        except Exception as e:
            logger.error(f"Error getting column for issue #{issue_number}: {e}")
            return None

    def _start_conversational_loop_for_issue(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        pipeline_config,
        workflow_template,
        column,
        pipeline_run_id: Optional[str] = None
    ) -> Optional[str]:
        """Start a conversational loop (human feedback mode) for an issue"""
        try:
            import asyncio
            from services.human_feedback_loop import human_feedback_loop_executor

            # Get issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get workspace info
            workspace_type = pipeline_config.workspace
            from config.state_manager import state_manager
            discussion_id = None

            if workspace_type in ['discussions', 'hybrid']:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

                # CRITICAL FIX: If no discussion exists for discussions workspace, create one NOW
                # before starting the conversational loop. This prevents agents from posting
                # to the issue instead of the discussion thread.
                if not discussion_id:
                    logger.info(
                        f"No discussion exists for issue #{issue_number} in discussions workspace, "
                        f"creating one before starting conversational loop"
                    )
                    try:
                        self._create_discussion_from_issue(
                            project_name,
                            issue_number,
                            repository,
                            pipeline_config,
                            project_config
                        )
                        # Retrieve the newly created discussion ID
                        discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)
                        if discussion_id:
                            logger.info(f"Created discussion {discussion_id} for issue #{issue_number}")
                        else:
                            logger.error(f"Failed to retrieve discussion ID after creation for issue #{issue_number}")
                    except Exception as e:
                        logger.error(f"Failed to create discussion for issue #{issue_number}: {e}")
                        import traceback
                        logger.error(traceback.format_exc())

            # Get the stage config from pipeline template for this column
            pipeline_template_data = self.config_manager.get_pipeline_template(pipeline_config.template)
            current_stage_config = None
            if column:
                # Map column name to stage - column.stage_mapping should give us the stage name
                stage_name = column.stage_mapping if hasattr(column, 'stage_mapping') else None
                if stage_name and pipeline_template_data:
                    # Find the stage config in the pipeline template
                    for stage in pipeline_template_data.stages:
                        if stage.stage == stage_name:
                            current_stage_config = stage
                            break

            # Get previous stage output if available
            previous_stage_context = self.get_previous_stage_context(
                repository, issue_number, project_config.github['org'],
                status, workflow_template,
                workspace_type=workspace_type,
                discussion_id=discussion_id,
                pipeline_config=pipeline_config,
                current_stage_config=current_stage_config,
                project_name=project_name
            )

            # Start conversational loop in background thread
            logger.info(
                f"Starting conversational loop for {column.agent} on issue #{issue_number} "
                f"(workspace: {workspace_type})"
            )

            import threading

            def run_loop_in_thread():
                """Run the async loop in a background thread"""
                try:
                    # Create new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    next_column, success = loop.run_until_complete(
                        human_feedback_loop_executor.start_loop(
                            issue_number=issue_number,
                            repository=repository,
                            project_name=project_name,
                            board_name=board_name,
                            column=column,
                            issue_data=issue_data,
                            previous_stage_output=previous_stage_context,
                            org=project_config.github['org'],
                            workflow_columns=workflow_template.columns,
                            workspace_type=workspace_type,
                            discussion_id=discussion_id,
                            pipeline_run_id=pipeline_run_id
                        )
                    )

                    logger.info(
                        f"Conversational loop completed for issue #{issue_number}, "
                        f"success={success}, next_column={next_column}"
                    )

                    # Move card to next column if auto_advance is enabled and we have a next column
                    if success and next_column:
                        auto_advance = getattr(column, 'auto_advance_on_approval', False)
                        if auto_advance:
                            logger.info(f"Auto-advancing issue #{issue_number} to {next_column}")
                            try:
                                from services.pipeline_progression import PipelineProgression
                                progression_service = PipelineProgression(self.task_queue)
                                progression_service.move_issue_to_column(
                                    project_name=project_name,
                                    board_name=board_name,
                                    issue_number=issue_number,
                                    target_column=next_column,
                                    trigger='conversational_loop_completion'
                                )
                                logger.info(f"Successfully moved issue #{issue_number} to {next_column}")
                            except Exception as move_error:
                                logger.error(f"Failed to move issue to next column: {move_error}")
                                import traceback
                                logger.error(traceback.format_exc())
                        else:
                            logger.info(f"Auto-advance disabled for column {column.name}, not moving card")

                    loop.close()
                except Exception as e:
                    logger.error(f"Error in conversational loop thread: {e}")
                    import traceback
                    logger.error(traceback.format_exc())

            # Start in background thread
            thread = threading.Thread(target=run_loop_in_thread, daemon=True)
            thread.start()

            logger.info(f"Conversational loop thread started for issue #{issue_number}")

            return column.agent

        except Exception as e:
            logger.error(f"Conversational loop failed: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _start_review_cycle_for_issue(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        pipeline_config,
        workflow_template,
        column
    ) -> Optional[str]:
        """Start an automated review cycle for an issue in a review column (non-blocking)"""
        try:
            import asyncio
            import threading
            from services.review_cycle import review_cycle_executor

            # Get issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get previous stage context (maker's output)
            workspace_type = pipeline_config.workspace
            from config.state_manager import state_manager
            discussion_id = None

            if workspace_type in ['discussions', 'hybrid']:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

            # Get the stage config from pipeline template for this column
            pipeline_template_data = self.config_manager.get_pipeline_template(pipeline_config.template)
            current_stage_config = None
            if column:
                # Map column name to stage - column.stage_mapping should give us the stage name
                stage_name = column.stage_mapping if hasattr(column, 'stage_mapping') else None
                if stage_name and pipeline_template_data:
                    # Find the stage config in the pipeline template
                    for stage in pipeline_template_data.stages:
                        if stage.stage == stage_name:
                            current_stage_config = stage
                            break

            previous_stage_context = self.get_previous_stage_context(
                repository, issue_number, project_config.github['org'],
                status, workflow_template,
                workspace_type=workspace_type,
                discussion_id=discussion_id,
                pipeline_config=pipeline_config,
                current_stage_config=current_stage_config,
                project_name=project_name
            )

            if not previous_stage_context:
                logger.warning(f"No previous stage output found for issue #{issue_number} - cannot start review cycle")
                return None

            # Get or create pipeline run before starting the thread
            pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_data.get('title', f'Issue #{issue_number}'),
                issue_url=issue_data.get('url', ''),
                project=project_name,
                board=board_name
            )
            logger.debug(f"Using pipeline run {pipeline_run.id} for review cycle on issue #{issue_number}")

            # CRITICAL: Try to acquire pipeline lock for review cycle
            # Review cycles must hold locks just like regular agent execution to prevent
            # multiple issues from working on the same board simultaneously
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            lock_manager = get_pipeline_lock_manager()
            
            can_execute, reason = lock_manager.try_acquire_lock(
                project=project_name,
                board=board_name,
                issue_number=issue_number
            )
            
            if not can_execute:
                logger.warning(
                    f"Cannot start review cycle for issue #{issue_number}: {reason}. "
                    f"Another issue is currently working on this board."
                )
                # End the pipeline run we just created since we can't proceed
                self.pipeline_run_manager.end_pipeline_run(
                    project_name, issue_number, 
                    reason=f"Could not acquire lock: {reason}"
                )
                return None
            
            logger.info(
                f"Review cycle acquired pipeline lock for issue #{issue_number}. "
                f"Starting in background thread (reviewer: {column.agent}, maker: {column.maker_agent})"
            )

            def run_cycle_in_thread():
                """Run the review cycle in a background thread"""
                try:
                    # Create new event loop for this thread
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)

                    # Post initial comment (workspace-aware)
                    from services.github_integration import GitHubIntegration
                    github = GitHubIntegration(repo_owner=project_config.github['org'], repo_name=repository)

                    start_context = {
                        'issue_number': issue_number,
                        'repository': repository,
                        'workspace_type': workspace_type,
                        'discussion_id': discussion_id
                    }

                    loop.run_until_complete(
                        github.post_agent_output(
                            start_context,
                            f"""## 🔄 Starting Review Cycle

**Reviewer**: {column.agent.replace('_', ' ').title()}
**Maker**: {column.maker_agent.replace('_', ' ').title()}
**Max Iterations**: {column.max_iterations}

The automated maker-checker review cycle is now starting. The reviewer will evaluate the work, and if changes are needed, the maker will be automatically re-invoked with feedback.

---
_Review cycle initiated by Claude Code Orchestrator_
"""
                        )
                    )

                    # Create or update PR for code review if using git workflow
                    # Find the pipeline to check workspace type
                    pipeline = next(
                        (p for p in project_config.pipelines if p.board_name == board_name),
                        None
                    )
                    if pipeline and pipeline.workspace == 'issues':
                        from services.git_workflow_manager import git_workflow_manager
                        from services.project_workspace import workspace_manager

                        project_dir = workspace_manager.get_project_dir(project_name)

                        pr_result = loop.run_until_complete(
                            git_workflow_manager.create_or_update_pr(
                                project=project_name,
                                issue_number=issue_number,
                                project_dir=project_dir,
                                org=project_config.github['org'],
                                repo=project_config.github['repo'],
                                issue_title=issue_data.get('title', f'Issue #{issue_number}'),
                                issue_body=issue_data.get('body', ''),
                                draft=True  # Start as draft
                            )
                        )

                        if pr_result.get('success'):
                            logger.info(f"PR created/updated for issue #{issue_number}: {pr_result.get('pr_url')}")
                        else:
                            logger.warning(f"Failed to create PR: {pr_result.get('error')}")

                    # Execute review cycle
                    next_column, success = loop.run_until_complete(
                        review_cycle_executor.start_review_cycle(
                            issue_number=issue_number,
                            repository=repository,
                            project_name=project_name,
                            board_name=board_name,
                            column=column,
                            issue_data=issue_data,
                            previous_stage_output=previous_stage_context,
                            org=project_config.github['org'],
                            workflow_columns=workflow_template.columns,
                            workspace_type=workspace_type,
                            discussion_id=discussion_id,
                            pipeline_run_id=pipeline_run.id
                        )
                    )

                    logger.info(
                        f"Review cycle completed for issue #{issue_number}, "
                        f"success={success}, next_column={next_column}"
                    )

                    # Move card to next column if successful and next column specified
                    if success and next_column and next_column != status:
                        try:
                            logger.info(f"Moving issue #{issue_number} from {status} to {next_column}")

                            # Get the project and card IDs
                            project_state = state_manager.load_project_state(project_name)

                            # project_state is a GitHubProjectState object with boards attribute
                            board = project_state.boards.get(board_name) if project_state else None
                            project_id = board.project_id if board else None

                            if not project_id:
                                logger.error(f"No project ID found for {board_name}")
                            else:
                                # Find the target column
                                target_column = next((c for c in workflow_template.columns if c.name == next_column), None)
                                if not target_column:
                                    logger.error(f"Target column {next_column} not found in workflow")
                                else:
                                    # Move the card
                                    from services.pipeline_progression import PipelineProgression
                                    progression_service = PipelineProgression(self.task_queue)
                                    progression_service.move_issue_to_column(
                                        project_name=project_name,
                                        board_name=board_name,
                                        issue_number=issue_number,
                                        target_column=next_column,
                                        trigger='review_cycle_completion'
                                    )
                                    logger.info(f"Successfully moved issue #{issue_number} to {next_column}")
                        except Exception as move_error:
                            logger.error(f"Failed to move issue to next column: {move_error}")
                            import traceback
                            logger.error(traceback.format_exc())

                    loop.close()
                except Exception as e:
                    logger.error(f"Error in review cycle thread: {e}")
                    import traceback
                    logger.error(traceback.format_exc())
                finally:
                    # CRITICAL: Always release pipeline lock when review cycle completes
                    # This ensures the lock is released even if there's an exception
                    try:
                        from services.pipeline_lock_manager import get_pipeline_lock_manager
                        lock_mgr = get_pipeline_lock_manager()
                        lock_mgr.release_lock(project_name, board_name, issue_number)
                        logger.info(f"Released pipeline lock for issue #{issue_number} after review cycle completion")
                        
                        # CRITICAL: Process next waiting issue in queue after lock release
                        # This ensures queued issues are picked up when review cycle completes
                        try:
                            from services.pipeline_queue_manager import get_pipeline_queue_manager
                            from task_queue.task_manager import Task, TaskPriority
                            from datetime import datetime
                            import time

                            pipeline_queue = get_pipeline_queue_manager(project_name, board_name)
                            next_issue = pipeline_queue.get_next_waiting_issue()
                            
                            if next_issue:
                                logger.info(f"Attempting to acquire lock for next queued issue #{next_issue['issue_number']} after review cycle for #{issue_number} completed")
                                
                                # Try to acquire lock for next issue
                                acquired, acquire_reason = lock_mgr.try_acquire_lock(
                                    project=project_name,
                                    board=board_name,
                                    issue_number=next_issue['issue_number']
                                )
                                
                                if acquired:
                                    # CRITICAL: Mark issue active IMMEDIATELY after lock acquisition
                                    # This prevents monitoring loop from seeing "issue has lock" and creating duplicate task
                                    pipeline_queue.mark_issue_active(next_issue['issue_number'])
                                    logger.info(f"Successfully acquired lock for issue #{next_issue['issue_number']}")
                                    
                                    # CRITICAL: Actually dispatch the agent by creating a task
                                    # Not sufficient to just acquire lock - need to enqueue task
                                    # SAFETY: Track task_created for rollback if creation fails
                                    task_created = False
                                    try:
                                        workflow_template_obj = self.config_manager.get_workflow_template(pipeline_config.workflow)
                                        
                                        # SAFETY: Re-fetch issue from GitHub to verify it hasn't moved columns
                                        # The queue cache might be stale if user moved the issue
                                        import subprocess
                                        import json as json_module
                                        result = subprocess.run(
                                            ['gh', 'issue', 'view', str(next_issue['issue_number']), '--repo',
                                             f"{project_config.github['org']}/{project_config.github['repo']}", '--json', 'projectItems'],
                                            capture_output=True, text=True, check=True
                                        )
                                        issue_data_check = json_module.loads(result.stdout)
                                        
                                        # Find current column for this board
                                        actual_column = None
                                        for item in issue_data_check.get('projectItems', []):
                                            if item.get('project', {}).get('title') == board_name:
                                                actual_column = item.get('fieldValueByName', {}).get('name')
                                                break
                                        
                                        if not actual_column:
                                            raise Exception(f"Issue #{next_issue['issue_number']} not found on board '{board_name}'")
                                        
                                        # Get agent for ACTUAL current column (not cached column)
                                        agent = None
                                        for col in workflow_template_obj.columns:
                                            if col.name == actual_column:
                                                agent = col.agent
                                                break
                                        
                                        if agent and agent != 'null':
                                            # Create task for next issue with ACTUAL column (not cached)
                                            task_context = {
                                                'project': project_name,
                                                'board': board_name,
                                                'pipeline': pipeline_config.name,
                                                'repository': project_config.github['repo'],
                                                'issue_number': next_issue['issue_number'],
                                                'column': actual_column,  # Use verified actual column
                                                'trigger': 'review_cycle_completion_queue_processing',
                                                'timestamp': datetime.utcnow().isoformat() + 'Z'
                                            }
                                            
                                            from task_queue.task_queue_factory import get_task_queue
                                            task_queue = get_task_queue()
                                            
                                            task = Task(
                                                id=f"{agent}_{project_name}_{board_name}_{next_issue['issue_number']}_{int(time.time())}",
                                                agent=agent,
                                                project=project_name,
                                                priority=TaskPriority.MEDIUM,
                                                context=task_context,
                                                created_at=datetime.utcnow().isoformat() + 'Z'
                                            )
                                            
                                            task_queue.enqueue(task)
                                            task_created = True
                                            
                                            logger.info(
                                                f"Dispatched agent {agent} for next queued issue #{next_issue['issue_number']} "
                                                f"in column '{actual_column}'"
                                            )
                                        else:
                                            raise Exception(
                                                f"Next queued issue #{next_issue['issue_number']} in column '{actual_column}' "
                                                f"has no agent configured"
                                            )
                                    except Exception as dispatch_error:
                                        # CRITICAL: Rollback lock acquisition if task creation failed
                                        # Otherwise lock is held with no work happening (deadlock)
                                        if not task_created:
                                            logger.error(
                                                f"Task creation failed for issue #{next_issue['issue_number']}, "
                                                f"rolling back lock acquisition to prevent deadlock"
                                            )
                                            try:
                                                lock_mgr.release_lock(project_name, board_name, next_issue['issue_number'])
                                                logger.info(f"Rolled back lock for issue #{next_issue['issue_number']}")
                                            except Exception as rollback_error:
                                                logger.error(f"Failed to rollback lock: {rollback_error}")
                                        
                                        logger.error(f"Error dispatching agent for next issue: {dispatch_error}")
                                        import traceback
                                        logger.error(traceback.format_exc())
                                else:
                                    logger.info(
                                        f"Could not acquire lock for next issue #{next_issue['issue_number']}: {acquire_reason}"
                                    )
                            else:
                                logger.debug(f"No more issues waiting in queue for {project_name}/{board_name}")
                        except Exception as queue_error:
                            logger.error(f"Error processing next queued issue for {project_name}/{board_name}: {queue_error}")
                            import traceback
                            logger.error(traceback.format_exc())
                            
                    except Exception as lock_error:
                        logger.error(f"Failed to release pipeline lock for issue #{issue_number}: {lock_error}")

            # Start in background thread (non-blocking)
            thread = threading.Thread(target=run_cycle_in_thread, daemon=True)
            thread.start()

            logger.info(f"Review cycle thread started for issue #{issue_number}")

            return column.agent

        except Exception as e:
            logger.error(f"Error starting review cycle: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _monitor_repair_cycle_container(
        self,
        container_name: str,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        workflow_template,
        agent_name: str,
        pipeline_run_id: Optional[str] = None
    ):
        """
        Monitor repair cycle container completion and handle auto-advance.
        
        Runs in background thread, waits for container to finish, then:
        - Posts summary comment
        - Auto-advances if successful
        - Records execution outcome
        """
        import threading
        import subprocess
        import asyncio
        
        def monitor_thread():
            exit_code = None
            overall_success = False
            repair_result = None
            error_message = None
            pipeline_run_ended = False

            try:
                logger.info(f"Starting container monitor for {container_name}")
                
                # Wait for container to finish
                wait_cmd = ['docker', 'wait', container_name]
                result = subprocess.run(
                    wait_cmd,
                    capture_output=True,
                    text=True,
                    timeout=7200  # 2 hour max
                )
                
                exit_code = int(result.stdout.strip()) if result.stdout.strip() else 2
                logger.info(f"Container {container_name} exited with code {exit_code}")

                # Load result from Redis
                repair_result = None
                try:
                    repair_result = _load_repair_cycle_result_from_redis(
                        project_name, issue_number, pipeline_run_id
                    )
                    if repair_result:
                        logger.info(f"Loaded repair cycle result from Redis for {project_name}/#{issue_number}")
                    else:
                        logger.warning(f"No result found in Redis for {project_name}/#{issue_number}")
                        error_message = "Result not found in Redis (container may have failed to save result)"
                except Exception as e:
                    logger.error(f"Failed to load repair result from Redis: {e}")
                    error_message = f"Failed to load result from Redis: {e}"

                # Determine success
                overall_success = (exit_code == 0) and (
                    repair_result.get('overall_success', False) if repair_result else False
                )
                
                # Emit container completed event
                try:
                    from monitoring.observability import get_observability_manager
                    obs_manager = get_observability_manager()
                    obs_manager.emit_repair_cycle_container_completed(
                        project=project_name,
                        issue_number=issue_number,
                        container_name=container_name,
                        success=overall_success,
                        total_agent_calls=repair_result.get('total_agent_calls', 0) if repair_result else 0,
                        duration_seconds=repair_result.get('duration_seconds', 0.0) if repair_result else 0.0,
                        pipeline_run_id=pipeline_run_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit container completed event: {e}")
                
                # Post summary comment
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
                from services.github_integration import GitHubIntegration
                from config.state_manager import state_manager
                
                github = GitHubIntegration(
                    repo_owner=project_config.github['org'],
                    repo_name=repository
                )
                
                # Get workspace info - default to 'issues' for repair cycles
                # (Repair cycles always work with issues, not discussions)
                workspace_type = 'issues'
                discussion_id = None
                
                comment_context = {
                    'issue_number': issue_number,
                    'repository': repository,
                    'workspace_type': workspace_type,
                    'discussion_id': discussion_id
                }
                
                # Build summary
                if repair_result:
                    test_results = repair_result.get('test_results', [])
                    summary_lines = [
                        f"## ✅ Repair Cycle Complete" if overall_success else "## ❌ Repair Cycle Failed",
                        "",
                        f"**Container**: `{container_name}`",
                        f"**Exit Code**: {exit_code}",
                        f"**Total Agent Calls**: {repair_result.get('total_agent_calls', 0)}",
                        f"**Duration**: {repair_result.get('duration_seconds', 0):.1f}s",
                        ""
                    ]
                    
                    for test_result in test_results:
                        test_type = test_result.get('test_type', 'unknown')
                        passed = test_result.get('passed', False)
                        iterations = test_result.get('iterations', 0)
                        summary_lines.append(
                            f"- **{test_type}**: {'✅ PASSED' if passed else '❌ FAILED'} "
                            f"({iterations} iterations)"
                        )
                else:
                    summary_lines = [
                        f"## ❌ Repair Cycle Failed",
                        "",
                        f"**Container**: `{container_name}`",
                        f"**Exit Code**: {exit_code}",
                        f"**Error**: No result file found",
                        ""
                    ]
                
                summary_lines.append("")
                summary_lines.append("---")
                summary_lines.append("_Repair cycle executed by Claude Code Orchestrator (containerized)_")
                
                loop.run_until_complete(
                    github.post_agent_output(
                        comment_context,
                        "\n".join(summary_lines)
                    )
                )
                
                # NOTE: Execution outcome is recorded in finally block to ensure it happens even on error
                
                # Auto-commit changes if repair cycle succeeded (BEFORE auto-advance to ensure code is pushed first)
                if overall_success:
                    try:
                        logger.info(f"Auto-committing repair cycle changes for issue #{issue_number}")
                        from services.auto_commit import auto_commit_service
                        
                        commit_success = loop.run_until_complete(
                            auto_commit_service.commit_agent_changes(
                                project=project_name,
                                agent='repair_cycle',
                                task_id=f'repair_cycle_{issue_number}',
                                issue_number=issue_number,
                                custom_message=f"Complete repair cycle for issue #{issue_number}\n\nAutomated test-fix-validate cycle completed successfully.\nAll tests passing."
                            )
                        )
                        
                        if commit_success:
                            logger.info(f"Successfully committed repair cycle changes for issue #{issue_number}")
                        else:
                            logger.warning(f"No changes to commit for repair cycle on issue #{issue_number}")
                    except Exception as e:
                        logger.error(f"Failed to auto-commit repair cycle changes: {e}", exc_info=True)
                        # Don't fail the repair cycle if commit fails - changes are still in workspace
                
                # Auto-advance if successful (AFTER commit to ensure code is pushed before moving to next stage)
                if overall_success:
                    current_index = next(
                        (i for i, col in enumerate(workflow_template.columns) if col.name == status),
                        None
                    )
                    
                    if current_index is not None and current_index + 1 < len(workflow_template.columns):
                        next_column = workflow_template.columns[current_index + 1]
                        
                        logger.info(f"Auto-advancing issue #{issue_number} from {status} to {next_column.name}")
                        
                        from services.pipeline_progression import PipelineProgression
                        progression_service = PipelineProgression(self.task_queue)
                        moved = progression_service.move_issue_to_column(
                            project_name=project_name,
                            board_name=board_name,
                            issue_number=issue_number,
                            target_column=next_column.name,
                            trigger='repair_cycle_completion'
                        )
                        
                        if moved:
                            logger.info(f"Successfully moved issue #{issue_number} to {next_column.name}")
                        else:
                            logger.error(f"Failed to move issue #{issue_number} to {next_column.name}")
                            # Don't end pipeline run if move failed, so it can be retried or noticed
                            # But we already committed code... this is a tricky state.
                            # For now, we'll log error but still end run to avoid infinite loop of repair cycles
                            # (since repair cycle itself succeeded)
                            # Ideally we should have a "Move Failed" state or alert.

                # End pipeline run on success
                if pipeline_run_id:
                    try:
                        ended = self.pipeline_run_manager.end_pipeline_run(
                            project=project_name,
                            issue_number=issue_number,
                            reason="Repair cycle completed successfully"
                        )
                        if ended:
                            pipeline_run_ended = True
                            logger.info(f"Ended pipeline run {pipeline_run_id} for {project_name}/#{issue_number}")
                        else:
                            logger.warning(f"Pipeline run {pipeline_run_id} was already ended or not found")
                    except Exception as e:
                        logger.error(f"Failed to end pipeline run {pipeline_run_id}: {e}", exc_info=True)

                # Cleanup repair cycle state (only on success)
                if overall_success:
                    _cleanup_repair_cycle_state(project_name, issue_number, pipeline_run_id)
                
                # Cleanup container
                try:
                    subprocess.run(
                        ['docker', 'rm', container_name],
                        capture_output=True,
                        timeout=30
                    )
                    logger.info(f"Removed container {container_name}")
                except Exception as e:
                    logger.warning(f"Failed to remove container: {e}")
                
                # Clear Redis tracking
                try:
                    if self.task_queue.redis_client:
                        redis_key = f"repair_cycle:container:{project_name}:{issue_number}"
                        self.task_queue.redis_client.delete(redis_key)
                        logger.debug(f"Cleared Redis tracking key: {redis_key}")
                except Exception as e:
                    logger.warning(f"Failed to clear Redis tracking: {e}")
                
                loop.close()
                
            except subprocess.TimeoutExpired:
                logger.error(f"Container {container_name} timed out after 2 hours")
                error_message = "Container timed out after 2 hours"
                overall_success = False

                # Emit container killed event
                try:
                    from monitoring.observability import get_observability_manager
                    obs_manager = get_observability_manager()
                    obs_manager.emit_repair_cycle_container_killed(
                        project=project_name,
                        issue_number=issue_number,
                        container_name=container_name,
                        reason="Exceeded 2 hour time limit",
                        pipeline_run_id=pipeline_run_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit container killed event: {e}")

                # Emit container completed event (failure)
                try:
                    from monitoring.observability import get_observability_manager
                    obs_manager = get_observability_manager()
                    obs_manager.emit_repair_cycle_container_completed(
                        project=project_name,
                        issue_number=issue_number,
                        container_name=container_name,
                        success=False,
                        total_agent_calls=0,
                        duration_seconds=7200.0,  # 2 hours
                        pipeline_run_id=pipeline_run_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit container completed event: {e}")

                # End pipeline run
                try:
                    ended = self.pipeline_run_manager.end_pipeline_run(
                        project=project_name,
                        issue_number=issue_number,
                        reason="Container timeout after 2 hours"
                    )
                    if ended:
                        pipeline_run_ended = True
                        logger.info(f"Ended pipeline run for {project_name}/#{issue_number} due to timeout")
                except Exception as e:
                    logger.error(f"Failed to end pipeline run: {e}")

                # Kill container
                try:
                    subprocess.run(['docker', 'kill', container_name], timeout=30)
                except Exception as e:
                    logger.error(f"Failed to kill timed-out container: {e}")
            except Exception as e:
                logger.error(f"Error monitoring container: {e}", exc_info=True)
                error_message = f"Monitoring thread error: {str(e)}"
                overall_success = False
            finally:
                # CRITICAL: Always update execution state, even if monitoring thread crashes
                # This prevents stale 'in_progress' states that block future work
                try:
                    from services.work_execution_state import work_execution_tracker
                    # agent_name is now passed as a parameter to this function
                    # No need to extract from repair_result

                    outcome = 'success' if overall_success else 'failure'
                    
                    # If we never got an exit code, container failed during launch
                    if exit_code is None:
                        error_message = error_message or "Container failed to start or exited immediately"
                    
                    work_execution_tracker.record_execution_outcome(
                        issue_number=issue_number,
                        column=status,
                        agent=agent_name,
                        outcome=outcome,
                        project_name=project_name,
                        error=error_message
                    )
                    
                    logger.info(
                        f"Execution state updated for {project_name}/#{issue_number}: "
                        f"outcome={outcome}, exit_code={exit_code}"
                    )

                    # End pipeline run if failed (success case is handled in normal flow)
                    # Only end if not already ended (e.g., in timeout handler)
                    if not overall_success and not pipeline_run_ended:
                        try:
                            ended = self.pipeline_run_manager.end_pipeline_run(
                                project=project_name,
                                issue_number=issue_number,
                                reason=f"Repair cycle failed: {error_message or 'Unknown error'}"
                            )
                            if ended:
                                logger.info(f"Ended pipeline run for {project_name}/#{issue_number} due to failure")
                        except Exception as e:
                            logger.error(f"Failed to end pipeline run in finally block: {e}")

                    # CRITICAL: Release pipeline lock on failure
                    # On success, the lock will be released when auto-advancing to next column
                    # On failure, the issue stays in Testing, so we must explicitly release the lock
                    if not overall_success:
                        try:
                            from services.pipeline_lock_manager import get_pipeline_lock_manager
                            lock_manager = get_pipeline_lock_manager()
                            current_lock = lock_manager.get_lock(project_name, board_name)

                            if current_lock and current_lock.locked_by_issue == issue_number:
                                logger.info(
                                    f"Releasing pipeline lock for {project_name}/{board_name} "
                                    f"(repair cycle for issue #{issue_number} failed)"
                                )
                                lock_manager.release_lock(project_name, board_name, issue_number)
                            else:
                                logger.warning(
                                    f"Lock for {project_name}/{board_name} not held by issue #{issue_number} "
                                    f"during repair cycle failure cleanup"
                                )
                        except Exception as lock_error:
                            logger.error(
                                f"CRITICAL: Failed to release lock for {project_name}/{board_name} "
                                f"after repair cycle failure: {lock_error}", exc_info=True
                            )
                except Exception as state_error:
                    logger.error(
                        f"CRITICAL: Failed to update execution state for {project_name}/#{issue_number}: "
                        f"{state_error}", exc_info=True
                    )
        
        # Start monitor thread
        thread = threading.Thread(target=monitor_thread, daemon=True)
        thread.start()
        logger.info(f"Container monitor thread started for {container_name}")

    def _start_repair_cycle_for_issue(
        self,
        project_name: str,
        board_name: str,
        issue_number: int,
        status: str,
        repository: str,
        project_config,
        pipeline_config,
        workflow_template,
        column,
        stage_config
    ) -> Optional[str]:
        """Start an automated repair cycle (test-fix-validate) for an issue"""
        try:
            import asyncio
            import threading
            import subprocess
            from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig, RepairTestType

            # CRITICAL: Check if a repair cycle container is already running for this issue
            # This prevents duplicate containers when recovery reconnects to an existing container
            # and the normal monitoring loop also tries to start one
            if self.task_queue.redis_client:
                try:
                    redis_key = f"repair_cycle:container:{project_name}:{issue_number}"
                    existing_container_name = self.task_queue.redis_client.get(redis_key)

                    if existing_container_name:
                        # Redis says there's a container - verify it's actually running in Docker
                        result = subprocess.run(
                            ['docker', 'ps', '--filter', f'name={existing_container_name}',
                             '--format', '{{.Names}}'],
                            capture_output=True,
                            text=True,
                            timeout=5
                        )

                        if result.stdout.strip():
                            # Container is actually running - don't start a duplicate
                            logger.warning(
                                f"Repair cycle container already running for {project_name}/#{issue_number}: "
                                f"{existing_container_name}. Skipping duplicate launch."
                            )
                            return stage_config.default_agent
                        else:
                            # Orphaned Redis key - container not running, clean it up
                            logger.warning(
                                f"Found orphaned repair cycle Redis key for {project_name}/#{issue_number} "
                                f"(container {existing_container_name} not running). Cleaning up and proceeding."
                            )
                            self.task_queue.redis_client.delete(redis_key)
                except Exception as e:
                    logger.warning(f"Error checking for existing repair cycle container: {e}")
                    # Continue anyway - better to risk a duplicate than block work

            # CRITICAL: Check if pipeline is locked by ANOTHER repair cycle
            # Repair cycles can steal locks from Development, but not from other repair cycles
            # Only one repair cycle should run at a time per pipeline
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            lock_manager = get_pipeline_lock_manager()
            current_lock = lock_manager.get_lock(project_name, board_name)

            if current_lock and current_lock.locked_by_issue != issue_number:
                # Another issue holds the lock - check if it's also a repair cycle
                # by checking for repair cycle container in Redis
                if self.task_queue.redis_client:
                    other_redis_key = f"repair_cycle:container:{project_name}:{current_lock.locked_by_issue}"
                    other_container = self.task_queue.redis_client.get(other_redis_key)

                    if other_container:
                        # Another repair cycle is running - don't compete with it
                        logger.warning(
                            f"Pipeline locked by another repair cycle (issue #{current_lock.locked_by_issue}). "
                            f"Skipping repair cycle launch for issue #{issue_number} to prevent competition."
                        )
                        return None

            # Get issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get workspace info
            workspace_type = pipeline_config.workspace
            from config.state_manager import state_manager
            discussion_id = None

            if workspace_type in ['discussions', 'hybrid']:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)

            # Get previous stage context
            previous_stage_context = self.get_previous_stage_context(
                repository, issue_number, project_config.github['org'],
                status, workflow_template,
                workspace_type=workspace_type,
                discussion_id=discussion_id,
                pipeline_config=pipeline_config,
                current_stage_config=stage_config,
                project_name=project_name
            )

            # Load test configurations from project config
            testing_config = project_config.testing or {}
            test_configs = []
            
            for test_type_config in testing_config.get('types', []):
                test_type = RepairTestType(test_type_config['type'])
                test_configs.append(RepairTestRunConfig(
                    test_type=test_type,
                    timeout=test_type_config.get('timeout', 600),
                    max_iterations=test_type_config.get('max_iterations', 5),
                    review_warnings=test_type_config.get('review_warnings', True),
                    max_file_iterations=test_type_config.get('max_file_iterations', 3)
                ))

            if not test_configs:
                logger.warning(f"No test configurations found for project {project_name}")
                return None

            # Get global settings from stage config
            max_total_agent_calls = stage_config.max_total_agent_calls or 100
            checkpoint_interval = stage_config.checkpoint_interval or 5

            # Get or create pipeline run
            pipeline_run = self.pipeline_run_manager.get_or_create_pipeline_run(
                issue_number=issue_number,
                issue_title=issue_data.get('title', f'Issue #{issue_number}'),
                issue_url=issue_data.get('url', ''),
                project=project_name,
                board=board_name
            )
            logger.debug(f"Using pipeline run {pipeline_run.id} for repair cycle on issue #{issue_number}")

            # CRITICAL: Acquire pipeline lock before starting repair cycle
            # Note: We've already checked above that no OTHER repair cycle is running
            # Repair cycles have PRIORITY over Development items - they can steal the lock if needed
            # But repair cycles do NOT compete with each other (checked above)
            from services.pipeline_lock_manager import get_pipeline_lock_manager
            lock_manager = get_pipeline_lock_manager()

            current_lock = lock_manager.get_lock(project_name, board_name)

            if current_lock and current_lock.locked_by_issue != issue_number:
                # Lock held by another issue (non-repair-cycle) - steal it
                logger.warning(
                    f"Repair cycle for issue #{issue_number} is stealing pipeline lock from issue #{current_lock.locked_by_issue} "
                    f"(repair cycles have priority over {status})"
                )
                # Release the old lock
                lock_manager.release_lock(project_name, board_name, current_lock.locked_by_issue)
                # Acquire for this issue
                lock_manager._create_lock(project_name, board_name, issue_number)
            elif not current_lock:
                # No lock exists, acquire it
                logger.info(f"Repair cycle for issue #{issue_number} acquiring pipeline lock")
                lock_manager._create_lock(project_name, board_name, issue_number)
            else:
                # Already hold the lock (may have held it from Development stage)
                logger.debug(f"Repair cycle for issue #{issue_number} already holds pipeline lock")

            logger.info(
                f"Starting repair cycle for issue #{issue_number} in Docker container "
                f"(agent: {stage_config.default_agent}, test types: {[tc.test_type.value for tc in test_configs]})"
            )

            # Build context for stage execution
            from services.project_workspace import workspace_manager
            from monitoring.observability import get_observability_manager
            
            project_dir = workspace_manager.get_project_dir(project_name)
            obs = get_observability_manager()

            stage_context = {
                'project': project_name,
                'board': board_name,
                'pipeline': pipeline_config.name,
                'repository': repository,
                'issue_number': issue_number,
                'issue': issue_data,
                'previous_stage_output': previous_stage_context,
                'column': status,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'pipeline_run_id': pipeline_run.id,
                'project_dir': project_dir,
                'use_docker': True,
                'task_id': f"repair_cycle_{issue_number}_{pipeline_run.id}",
                'agent_name': stage_config.default_agent,
                'max_total_agent_calls': max_total_agent_calls,
                'checkpoint_interval': checkpoint_interval,
                'stage_name': status
            }

            # Prepare workspace branch for issues workspace (git checkout feature branch)
            branch_name = None
            if workspace_type == 'issues':
                try:
                    import asyncio
                    from services.feature_branch_manager import feature_branch_manager
                    from services.github_integration import GitHubIntegration
                    
                    github = GitHubIntegration(
                        repo_owner=project_config.github['org'],
                        repo_name=repository
                    )
                    
                    issue_title = issue_data.get('title', '')
                    
                    logger.info(
                        f"Preparing feature branch for repair cycle on issue #{issue_number}"
                    )
                    
                    # We're in a thread context, create and use new event loop
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                    try:
                        branch_name = loop.run_until_complete(
                            feature_branch_manager.prepare_feature_branch(
                                project=project_name,
                                issue_number=issue_number,
                                github_integration=github,
                                issue_title=issue_title
                            )
                        )
                    finally:
                        loop.close()
                    
                    logger.info(f"Checked out feature branch for repair cycle: {branch_name}")
                    stage_context['branch_name'] = branch_name
                    
                except Exception as e:
                    logger.error(f"Failed to prepare feature branch for repair cycle: {e}", exc_info=True)
                    # Don't fail the repair cycle if branch prep fails - it might still work
                    logger.warning("Continuing with repair cycle despite branch preparation failure")
            
            # Save context to JSON file
            try:
                context_file = _save_repair_cycle_context(
                    project_dir=project_dir,
                    context=stage_context,
                    test_configs=test_configs
                )
            except Exception as e:
                logger.error(f"Failed to save repair cycle context: {e}", exc_info=True)
                return None

            # Post initial comment (workspace-aware)
            try:
                from services.github_integration import GitHubIntegration
                github = GitHubIntegration(
                    repo_owner=project_config.github['org'],
                    repo_name=repository
                )

                start_context = {
                    'issue_number': issue_number,
                    'repository': repository,
                    'workspace_type': workspace_type,
                    'discussion_id': discussion_id
                }

                # Build comment with optional branch info
                branch_info = f"\n**Branch**: `{branch_name}`" if branch_name else ""
                
                comment_text = f"""## 🔧 Starting Repair Cycle (Testing)

**Agent**: {stage_config.default_agent.replace('_', ' ').title()}
**Test Types**: {', '.join([tc.test_type.value for tc in test_configs])}
**Max Iterations**: {max_total_agent_calls}
**Container**: Isolated Docker container{branch_info}

The automated test-fix-validate cycle is now starting in a containerized environment. Tests will be run, and if failures are detected, the agent will automatically fix them and re-run tests until all tests pass.

**Container Features**:
- ✅ Survives orchestrator restarts
- ✅ Automatic checkpointing every {checkpoint_interval} iterations
- ✅ Progress preserved on restart

---
_Repair cycle initiated by Claude Code Orchestrator_
"""

                # We're in a thread context (not async), so create and run a new event loop
                import asyncio
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    loop.run_until_complete(
                        github.post_agent_output(start_context, comment_text)
                    )
                finally:
                    loop.close()
            except Exception as e:
                logger.warning(f"Failed to post initial comment: {e}")

            # Record execution start in work execution state FIRST
            # CRITICAL: Must happen before launching container to prevent race condition
            from services.work_execution_state import work_execution_tracker
            work_execution_tracker.record_execution_start(
                issue_number=issue_number,
                column=status,
                agent=stage_config.default_agent,
                trigger_source='manual',
                project_name=project_name
            )

            # Launch Docker container
            container_name = _launch_repair_cycle_container(
                project_name=project_name,
                issue_number=issue_number,
                pipeline_run_id=pipeline_run.id,
                stage_name=status,
                context_file=context_file,
                project_dir=project_dir
            )

            if not container_name:
                logger.error(f"Failed to launch repair cycle container for issue #{issue_number}")
                return None

            # Register container in Redis
            try:
                if self.task_queue.redis_client:
                    _register_repair_cycle_container(
                        project_name=project_name,
                        issue_number=issue_number,
                        container_name=container_name,
                        redis_client=self.task_queue.redis_client
                    )
            except Exception as e:
                logger.warning(f"Failed to register container in Redis: {e}")

            logger.info(f"Repair cycle container started for issue #{issue_number}: {container_name}")

            # Start monitoring thread for container completion
            self._monitor_repair_cycle_container(
                container_name=container_name,
                project_name=project_name,
                board_name=board_name,
                issue_number=issue_number,
                status=status,
                repository=repository,
                project_config=project_config,
                workflow_template=workflow_template,
                agent_name=stage_config.default_agent,
                pipeline_run_id=pipeline_run.id
            )

            return stage_config.default_agent

        except Exception as e:
            logger.error(f"Error starting repair cycle: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None

    def _reconcile_active_runs(self):
        """
        Reconcile active pipeline runs with current board state.
        
        If an issue has an active pipeline run but is in an exit column (or a column with no agent),
        we should end the pipeline run.
        """
        logger.info("Reconciling active pipeline runs with board state...")
        
        try:
            for project_name in self.config_manager.list_visible_projects():
                project_config = self.config_manager.get_project_config(project_name)
                
                for pipeline in project_config.pipelines:
                    if not pipeline.active:
                        continue
                        
                    board_key = f"{project_name}_{pipeline.board_name}"
                    if board_key not in self.last_state:
                        continue
                        
                    current_items = self.last_state[board_key].values()
                    workflow_template = self.config_manager.get_workflow_template(pipeline.workflow)
                    
                    for item in current_items:
                        # Check if there is an active run
                        pipeline_run = self.pipeline_run_manager.get_active_pipeline_run(
                            project_name, item.issue_number
                        )
                        
                        if pipeline_run:
                            # Check if current column is an exit column or has no agent
                            column_config = next(
                                (c for c in workflow_template.columns if c.name == item.status),
                                None
                            )
                            
                            should_end = False
                            reason = ""
                            
                            if not column_config:
                                should_end = True
                                reason = f"Column '{item.status}' not found in workflow"
                            elif not column_config.agent or column_config.agent == 'null':
                                should_end = True
                                reason = f"Column '{item.status}' has no agent"
                            elif hasattr(workflow_template, 'pipeline_exit_columns') and \
                                 workflow_template.pipeline_exit_columns and \
                                 item.status in workflow_template.pipeline_exit_columns:
                                should_end = True
                                reason = f"Column '{item.status}' is an exit column"
                                
                            if should_end:
                                logger.info(
                                    f"Found active run {pipeline_run.id} for issue #{item.issue_number} "
                                    f"in exit/no-agent column '{item.status}'. Ending run."
                                )
                                self.pipeline_run_manager.end_pipeline_run(
                                    project_name, item.issue_number, reason
                                )
                                
                                # Also release lock if held
                                from services.pipeline_lock_manager import get_pipeline_lock_manager
                                lock_manager = get_pipeline_lock_manager()
                                lock = lock_manager.get_lock(project_name, pipeline.board_name)
                                if lock and lock.locked_by_issue == item.issue_number:
                                    lock_manager.release_lock(project_name, pipeline.board_name, item.issue_number)
                                    logger.info(f"Released lock for issue #{item.issue_number}")

            # CRITICAL WATCHDOG: Check for stale locks (locks without active runs/containers)
            # This prevents deadlocks when pipeline runs end due to errors but locks aren't released
            for project_name in self.config_manager.list_visible_projects():
                project_config = self.config_manager.get_project_config(project_name)
                
                for pipeline in project_config.pipelines:
                    if not pipeline.active:
                        continue
                    
                    # Check if this board has a lock
                    from services.pipeline_lock_manager import get_pipeline_lock_manager
                    lock_manager = get_pipeline_lock_manager()
                    lock = lock_manager.get_lock(project_name, pipeline.board_name)
                    
                    if lock and lock.lock_status == 'locked':
                        # Check if the locking issue has an active run
                        pipeline_run = self.pipeline_run_manager.get_active_pipeline_run(
                            project_name, lock.locked_by_issue
                        )
                        
                        if not pipeline_run:
                            # Stale lock! No active run but lock exists
                            logger.warning(
                                f"Found stale lock on {project_name}/{pipeline.board_name} "
                                f"held by issue #{lock.locked_by_issue} with no active pipeline run. "
                                f"Releasing lock..."
                            )
                            lock_manager.release_lock(project_name, pipeline.board_name, lock.locked_by_issue)
                            logger.info(f"Released stale lock for issue #{lock.locked_by_issue}")

        except Exception as e:
            logger.error(f"Error reconciling active runs: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _sort_items_by_board_position(self, items: list) -> list:
        """
        Sort items by board position to ensure correct processing order.

        Uses a hybrid approach:
        1. Column order (from workflow template)
        2. Issue number (ascending) within each column

        This ensures items are processed in board order:
        - Earlier columns processed first (Backlog → Development → Testing → Done)
        - Within a column, lower issue numbers (older issues) processed first

        Args:
            items: List of item_data dictionaries with 'change', 'project_name',
                   'board_name', 'column_config' keys

        Returns:
            Sorted list of items
        """
        if not items:
            return items

        # Build column order maps for each unique project/board
        column_order_maps = {}

        for item_data in items:
            project_name = item_data['project_name']
            board_name = item_data['board_name']
            board_key = f"{project_name}/{board_name}"

            if board_key not in column_order_maps:
                # Get workflow template to determine column order
                try:
                    project_config = self.config_manager.get_project_config(project_name)
                    pipeline = next(
                        (p for p in project_config.pipelines if p.board_name == board_name),
                        None
                    )

                    if pipeline:
                        workflow_template = self.config_manager.get_workflow_template(pipeline.workflow)
                        if workflow_template:
                            # Create column order map: column_name -> position
                            column_order_maps[board_key] = {
                                col.name: idx
                                for idx, col in enumerate(workflow_template.columns)
                            }
                            logger.debug(
                                f"Created column order map for {board_key}: "
                                f"{list(column_order_maps[board_key].keys())}"
                            )
                        else:
                            logger.warning(f"No workflow template found for {board_key}")
                            column_order_maps[board_key] = {}
                    else:
                        logger.warning(f"No pipeline config found for {board_key}")
                        column_order_maps[board_key] = {}

                except Exception as e:
                    logger.warning(f"Error building column order map for {board_key}: {e}")
                    column_order_maps[board_key] = {}

        # Sort items by (column_order, issue_number)
        def sort_key(item_data):
            board_key = f"{item_data['project_name']}/{item_data['board_name']}"
            column_name = item_data['change']['status']
            issue_number = item_data['change']['issue_number']

            # Get column order (default to 999 if column not found)
            column_order = column_order_maps.get(board_key, {}).get(column_name, 999)

            return (column_order, issue_number)

        sorted_items = sorted(items, key=sort_key)

        # Log the sorted order for debugging
        if sorted_items:
            logger.info(
                f"Sorted {len(sorted_items)} items by board position: " +
                ", ".join([
                    f"#{item['change']['issue_number']} ({item['change']['status']})"
                    for item in sorted_items
                ])
            )

        return sorted_items

    def _rescan_boards_for_stalled_items(self):
        """
        Rescan all boards after startup to dispatch agents for items already in action-required columns.
        
        This handles the case where:
        1. Orchestrator cleaned up stale pipeline runs during startup
        2. Items are sitting in columns that require agent action (Development, Testing, etc.)
        3. No active pipeline runs exist for these items
        
        We simulate an "item_added" event for each item in an action-required column
        that doesn't have an active pipeline run.
        """
        from config.state_manager import state_manager
        
        try:
            dispatched_count = 0

            # PHASE 1: Collect all stalled items and categorize by priority
            # Repair cycles (Testing) get priority over Development items
            repair_cycle_items = []  # High priority - already in progress
            other_items = []  # Lower priority - new work

            for project_name in self.config_manager.list_visible_projects():
                project_config = self.config_manager.get_project_config(project_name)
                project_state = state_manager.load_project_state(project_name)

                if not project_state:
                    continue

                for pipeline in project_config.pipelines:
                    if not pipeline.active:
                        continue

                    board_state = project_state.boards.get(pipeline.board_name)
                    if not board_state:
                        continue

                    # Get workflow template to check which columns need agents
                    workflow_template = self.config_manager.get_workflow_template(pipeline.workflow)

                    # Get pipeline template to check for repair_cycle stages
                    pipeline_template = self.config_manager.get_pipeline_template(pipeline.template)

                    # Get current items on the board
                    board_key = f"{project_name}_{pipeline.board_name}"
                    if board_key not in self.last_state:
                        continue

                    current_items = self.last_state[board_key].values()

                    for item in current_items:
                        # Find column config
                        column_config = next(
                            (c for c in workflow_template.columns if c.name == item.status),
                            None
                        )

                        if not column_config:
                            continue

                        # Check for missing discussions (e.g. Backlog items added while offline)
                        self._check_and_create_discussion(
                            project_name,
                            pipeline.board_name,
                            item.issue_number,
                            item.repository,
                            item.status
                        )

                        # Check if column requires agent action
                        has_agent = column_config.agent and column_config.agent != 'null'

                        if not has_agent:
                            continue

                        # Determine if this is a repair_cycle stage (need this early for container checks)
                        is_repair_cycle = False
                        if pipeline_template:
                            stage_name = column_config.stage_mapping if hasattr(column_config, 'stage_mapping') else None
                            if stage_name:
                                for stage in pipeline_template.stages:
                                    if stage.stage == stage_name and getattr(stage, 'stage_type', None) == 'repair_cycle':
                                        is_repair_cycle = True
                                        break

                        # Check if there's already an active pipeline run
                        has_active_run = self.pipeline_run_manager.get_active_pipeline_run(
                            project_name, item.issue_number
                        )

                        # Skip if active pipeline run exists
                        if has_active_run:
                            logger.debug(
                                f"Issue #{item.issue_number} in {item.status} already has active pipeline run - skipping"
                            )
                            continue

                        # Check pipeline lock status
                        from services.pipeline_lock_manager import get_pipeline_lock_manager
                        lock_manager = get_pipeline_lock_manager()
                        current_lock = lock_manager.get_lock(project_name, pipeline.board_name)

                        # Skip if pipeline is locked (by any issue)
                        if current_lock and current_lock.lock_status == 'locked':
                            if current_lock.locked_by_issue == item.issue_number:
                                # This issue holds the lock (may be re-triggering from recovery)
                                logger.debug(
                                    f"Issue #{item.issue_number} holds pipeline lock, likely being re-triggered by recovery, skipping"
                                )
                            else:
                                # Another issue holds the lock - CRITICAL: don't dispatch this issue
                                logger.debug(
                                    f"Issue #{item.issue_number} in {item.status} skipped - pipeline locked by issue #{current_lock.locked_by_issue}"
                                )
                            continue

                        # CRITICAL: Check if work has already been completed for this column/agent
                        # Prevents re-triggering agents when output already exists
                        has_existing_output = False
                        if not is_repair_cycle:  # Repair cycles should always run if no container exists
                            try:
                                # Check workspace for existing agent output
                                workspace_type = pipeline.workspace if hasattr(pipeline, 'workspace') else 'issues'

                                if workspace_type == 'discussions':
                                    # For discussions workspace, check if discussion has agent output
                                    from config.state_manager import state_manager
                                    discussion_id = state_manager.get_discussion_for_issue(project_name, item.issue_number)

                                    if discussion_id:
                                        # Check if discussion has comments from this agent
                                        from services.github_discussions import GitHubDiscussions
                                        discussions = GitHubDiscussions()
                                        comments = discussions.get_discussion_comments(
                                            project_config.github['org'],
                                            project_config.github['repo'],
                                            discussion_id
                                        )

                                        # Look for agent output in discussion
                                        agent_name = column_config.agent
                                        last_agent_idx = -1
                                        last_user_idx = -1
                                        
                                        for i, comment in enumerate(comments):
                                            body = comment.get('body', '')
                                            author = comment.get('author', {}).get('login')
                                            
                                            if author in ['orchestrator-bot', 'github-actions[bot]'] and f'_Processed by the {agent_name} agent_' in body:
                                                last_agent_idx = i
                                            elif author not in ['orchestrator-bot', 'github-actions[bot]']:
                                                last_user_idx = i
                                        
                                        if last_agent_idx != -1:
                                            if last_user_idx > last_agent_idx:
                                                has_existing_output = False
                                                logger.info(
                                                    f"Issue #{item.issue_number} in {item.status} has new user feedback after "
                                                    f"{agent_name} output in discussion {discussion_id} - triggering update"
                                                )
                                            else:
                                                has_existing_output = True
                                                logger.info(
                                                    f"Issue #{item.issue_number} in {item.status} already has output from "
                                                    f"{agent_name} in discussion {discussion_id} - skipping"
                                                )
                                else:
                                    # For issues workspace (default), check issue comments
                                    result = subprocess.run(
                                        ['gh', 'issue', 'view', str(item.issue_number), '--repo',
                                         f"{project_config.github['org']}/{item.repository}", '--json', 'comments'],
                                        capture_output=True, text=True, check=True
                                    )
                                    comments_data = json.loads(result.stdout)
                                    comments = comments_data.get('comments', [])
                                    
                                    agent_name = column_config.agent
                                    last_agent_idx = -1
                                    last_user_idx = -1
                                    
                                    for i, comment in enumerate(comments):
                                        body = comment.get('body', '')
                                        author = comment.get('author', {}).get('login')
                                        
                                        if f'_Processed by the {agent_name} agent_' in body:
                                            last_agent_idx = i
                                        elif author not in ['orchestrator-bot', 'github-actions[bot]']:
                                            last_user_idx = i
                                    
                                    if last_agent_idx != -1:
                                        if last_user_idx > last_agent_idx:
                                            has_existing_output = False
                                            logger.info(
                                                f"Issue #{item.issue_number} in {item.status} has new user feedback after "
                                                f"{agent_name} output in issue comments - triggering update"
                                            )
                                        else:
                                            has_existing_output = True
                                            logger.info(
                                                f"Issue #{item.issue_number} in {item.status} already has output from "
                                                f"{agent_name} in issue comments - skipping"
                                            )

                            except Exception as e:
                                logger.warning(f"Error checking for existing output on issue #{item.issue_number}: {e}")
                                # Continue anyway - better to re-run than skip valid work

                        if has_existing_output:
                            continue

                        # Build change object
                        change = {
                            'type': 'item_added',
                            'item': item,
                            'title': item.title,
                            'status': item.status,
                            'issue_number': item.issue_number,
                            'repository': item.repository
                        }

                        # Add metadata for processing
                        item_data = {
                            'change': change,
                            'project_name': project_name,
                            'board_name': pipeline.board_name,
                            'column_config': column_config,
                            'is_repair_cycle': is_repair_cycle
                        }

                        if is_repair_cycle:
                            repair_cycle_items.append(item_data)
                        else:
                            other_items.append(item_data)

            # PHASE 2: Sort and process repair cycles FIRST (they have priority and may steal locks)
            if repair_cycle_items:
                # Sort repair cycles by board position (column order + issue number)
                repair_cycle_items = self._sort_items_by_board_position(repair_cycle_items)
                logger.info(f"Processing {len(repair_cycle_items)} repair cycle items first (high priority)")
                for item_data in repair_cycle_items:
                    logger.info(
                        f"Dispatching agent for stalled item: {item_data['project_name']} issue #{item_data['change']['issue_number']} "
                        f"in column '{item_data['change']['status']}' (agent: {item_data['column_config'].agent}) [REPAIR CYCLE - HIGH PRIORITY]"
                    )
                    self.process_board_changes([item_data['change']], item_data['project_name'], item_data['board_name'])
                    dispatched_count += 1

            # PHASE 3: Sort and process other items (Development, etc.)
            if other_items:
                # Sort by board position (column order + issue number) to respect board ordering
                other_items = self._sort_items_by_board_position(other_items)
                logger.info(f"Processing {len(other_items)} other stalled items")
                for item_data in other_items:
                    logger.info(
                        f"Dispatching agent for stalled item: {item_data['project_name']} issue #{item_data['change']['issue_number']} "
                        f"in column '{item_data['change']['status']}' (agent: {item_data['column_config'].agent})"
                    )
                    self.process_board_changes([item_data['change']], item_data['project_name'], item_data['board_name'])
                    dispatched_count += 1
            
            if dispatched_count > 0:
                logger.info(f"Dispatched agents for {dispatched_count} stalled items after startup")
            else:
                logger.info("No stalled items found needing agent dispatch")
                
        except Exception as e:
            logger.error(f"Error during board rescan: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _check_and_process_waiting_issues_failsafe(self):
        """
        Failsafe mechanism to catch cases where waiting issues aren't being processed.

        This handles situations where a pipeline run ends but doesn't trigger the
        "process next" logic, leaving waiting issues stuck in the queue.

        The failsafe checks each pipeline for:
        1. Pipeline is unlocked (no active work)
        2. Waiting issues exist in queue
        3. If both true → try to start the next waiting issue

        Safety: Uses same lock acquisition as normal flow - prevents duplicate launches.
        Lock acquisition is atomic in Redis - only ONE process can acquire successfully.
        """
        from services.pipeline_lock_manager import get_pipeline_lock_manager
        from services.pipeline_queue_manager import get_pipeline_queue_manager

        try:
            for project_name in self.config_manager.list_visible_projects():
                project_config = self.config_manager.get_project_config(project_name)

                for pipeline in project_config.pipelines:
                    if not pipeline.active:
                        continue

                    try:
                        lock_manager = get_pipeline_lock_manager()
                        pipeline_queue = get_pipeline_queue_manager(
                            project_name, pipeline.board_name
                        )

                        # CRITICAL: Check if pipeline is unlocked
                        lock = lock_manager.get_lock(project_name, pipeline.board_name)
                        if lock and lock.lock_status == 'locked':
                            continue  # Pipeline busy, skip

                        # CRITICAL: Check if there are waiting issues
                        # This also syncs queue with GitHub (ensures up-to-date state)
                        next_issue = pipeline_queue.get_next_waiting_issue()
                        if not next_issue:
                            continue  # No waiting issues, skip

                        # We have: waiting issue + unlocked pipeline = should be processing!
                        logger.info(
                            f"⚡ FAILSAFE: Found waiting issue #{next_issue['issue_number']} "
                            f"for unlocked pipeline {project_name}/{pipeline.board_name} - attempting to process"
                        )

                        # CRITICAL: Try to acquire lock (atomic operation in Redis)
                        # If another process is processing this issue, acquisition will fail
                        acquired, reason = lock_manager.try_acquire_lock(
                            project=project_name,
                            board=pipeline.board_name,
                            issue_number=next_issue['issue_number']
                        )

                        if not acquired:
                            logger.debug(
                                f"FAILSAFE: Could not acquire lock for issue "
                                f"#{next_issue['issue_number']}: {reason} "
                                f"(likely being processed by another path)"
                            )
                            continue

                        # Mark as active in queue
                        pipeline_queue.mark_issue_active(next_issue['issue_number'])

                        # Get current column from GitHub (may have moved since queued)
                        current_column = self.get_issue_column_sync(
                            project_name,
                            pipeline.board_name,
                            next_issue['issue_number']
                        )

                        if current_column:
                            logger.info(
                                f"⚡ FAILSAFE: Triggering agent for issue "
                                f"#{next_issue['issue_number']} in column '{current_column}'"
                            )

                            # Trigger agent using normal flow
                            self.trigger_agent_for_status(
                                project_name,
                                pipeline.board_name,
                                next_issue['issue_number'],
                                current_column,
                                project_config.github['repo']
                            )
                        else:
                            # Issue not in any column - clean up
                            logger.warning(
                                f"FAILSAFE: Issue #{next_issue['issue_number']} not found "
                                f"in any column, removing from queue and releasing lock"
                            )
                            pipeline_queue.remove_issue_from_queue(next_issue['issue_number'])
                            lock_manager.release_lock(
                                project_name,
                                pipeline.board_name,
                                next_issue['issue_number']
                            )

                    except Exception as e:
                        logger.error(
                            f"Error in failsafe check for {project_name}/{pipeline.board_name}: {e}"
                        )
                        import traceback
                        logger.error(traceback.format_exc())
                        continue

        except Exception as e:
            logger.error(f"Error in queue processing failsafe: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def monitor_projects(self):
        """Main monitoring loop using new configuration system"""
        import sys
        import asyncio
        from config.state_manager import state_manager
        from services.github_api_client import get_github_client

        logger.info("Starting GitHub Projects v2 monitor...")
        sys.stdout.flush()

        # Resume any active review cycles from before restart
        logger.info("Checking for active review cycles to resume...")
        from services.review_cycle import review_cycle_executor

        for project_name in self.config_manager.list_visible_projects():
            project_config = self.config_manager.get_project_config(project_name)
            org = project_config.github['org']

            # Run async resume method
            asyncio.run(review_cycle_executor.resume_active_cycles(project_name, org))

        logger.info("Review cycle recovery complete")
        
        # Check GitHub API circuit breaker before attempting initialization queries
        github_client = get_github_client()
        if github_client.breaker.is_open():
            time_until = (github_client.breaker.reset_time - datetime.now()).total_seconds() if github_client.breaker.reset_time else None
            if time_until and time_until > 0:
                logger.warning(
                    f"⏸️  GitHub API circuit breaker is OPEN during initialization. "
                    f"Rate limit resets in {time_until:.0f}s. Skipping initialization queries..."
                )
            else:
                logger.warning(
                    f"⏸️  GitHub API circuit breaker is OPEN during initialization. "
                    f"Skipping initialization queries..."
                )
        else:
            # CRITICAL: Initialize last_state to avoid false "item_added" events on startup
            # Do an initial poll of all projects to populate last_state without processing changes
            logger.info("Initializing project state (initial poll without change detection)...")
            try:
                for project_name in self.config_manager.list_visible_projects():
                    # Check breaker again before each project (in case rate limit hit during loop)
                    if github_client.breaker.is_open():
                        logger.warning(
                            f"⏸️  GitHub API circuit breaker opened during initialization. "
                            f"Skipping remaining initialization queries..."
                        )
                        break
                    
                    project_config = self.config_manager.get_project_config(project_name)
                    project_state = state_manager.load_project_state(project_name)
                    
                    if not project_state:
                        logger.warning(f"No GitHub state found for project '{project_name}' during initialization")
                        continue
                    
                    for pipeline in project_config.pipelines:
                        if not pipeline.active:
                            continue
                        
                        board_state = project_state.boards.get(pipeline.board_name)
                        if not board_state:
                            logger.warning(f"No board state for '{pipeline.board_name}' in project '{project_name}'")
                            continue
                        
                        # Get current items and populate last_state WITHOUT processing changes
                        current_items = self.get_project_items(project_config.github['org'], board_state.project_number)
                        if current_items:
                            board_key = f"{project_name}_{pipeline.board_name}"
                            current_by_issue = {item.issue_number: item for item in current_items}
                            self.last_state[board_key] = current_by_issue
                            logger.info(f"Initialized state for {project_name}/{pipeline.board_name}: {len(current_items)} items")
            except Exception as e:
                logger.warning(f"Error during project state initialization: {e}")
                logger.info("Will use empty state, may detect false 'item_added' events on first poll")
        
        logger.info("Project state initialization complete, starting main monitoring loop...")

        # After cleanup and state initialization, reconcile active runs and rescan boards
        logger.info("Reconciling active runs and rescanning boards after startup...")
        self._reconcile_active_runs()
        self._rescan_boards_for_stalled_items()
        logger.info("Board rescan complete")

        # Signal that rescan is complete - workers can now safely start
        self.rescan_complete.set()
        logger.info("Startup rescan complete - worker pool can now process tasks")

        was_breaker_open = False

        while True:
            try:
                # Check Claude Code circuit breaker - if open, skip all monitoring
                from monitoring.claude_code_breaker import get_breaker
                from monitoring.claude_token_scheduler import get_scheduler
                from services.github_api_client import get_github_client
                import asyncio
                
                breaker = get_breaker()
                scheduler = get_scheduler()
                github_client = get_github_client()

                # Run token availability check/test
                try:
                    asyncio.run(scheduler.check_and_run_test())
                except Exception as e:
                    logger.error(f"Error running token scheduler: {e}", exc_info=True)

                # Check if GitHub API rate limit has reset and close breaker if so
                github_client.breaker.check_and_close()

                # Check GitHub API circuit breaker - if open, skip all monitoring
                if github_client.breaker.is_open():
                    # Log warning only once every 60 seconds to reduce noise
                    now = datetime.now()
                    if not hasattr(self, '_last_github_breaker_warning') or \
                       (now - self._last_github_breaker_warning).total_seconds() >= 60:
                        time_until = (github_client.breaker.reset_time - datetime.now()).total_seconds() if github_client.breaker.reset_time else None
                        if time_until and time_until > 0:
                            logger.warning(
                                f"⏸️  GitHub API circuit breaker is OPEN. "
                                f"Rate limit resets in {time_until:.0f}s. Pausing all monitoring..."
                            )
                        else:
                            logger.warning(
                                f"⏸️  GitHub API circuit breaker is OPEN. Pausing all monitoring..."
                            )
                        self._last_github_breaker_warning = now
                    time.sleep(5)  # Sleep briefly before checking again
                    continue
                
                if breaker and breaker.is_open():
                    was_breaker_open = True
                    # Log warning only once every 60 seconds to reduce noise
                    now = datetime.now()
                    if not hasattr(self, '_last_claude_breaker_warning') or \
                       (now - self._last_claude_breaker_warning).total_seconds() >= 60:
                        status = breaker.get_status()
                        time_until = status.get('time_until_reset')
                        if time_until and time_until > 0:
                            logger.warning(
                                f"⏸️  Claude Code circuit breaker is OPEN. "
                                f"Tokens reset in {time_until:.0f}s. Pausing all monitoring..."
                            )
                        else:
                            logger.warning(
                                f"⏸️  Claude Code circuit breaker is OPEN. Pausing all monitoring..."
                            )
                        self._last_claude_breaker_warning = now
                    time.sleep(5)  # Sleep briefly before checking again
                    continue
                
                if was_breaker_open:
                    logger.info("🟢 Claude Code circuit breaker recovered - rescanning for stalled items...")
                    self._rescan_boards_for_stalled_items()
                    was_breaker_open = False
                
                # Get all configured visible projects (exclude hidden/test projects)
                for project_name in self.config_manager.list_visible_projects():
                    project_config = self.config_manager.get_project_config(project_name)

                    # Get project state to find actual GitHub project numbers
                    project_state = state_manager.load_project_state(project_name)
                    if not project_state:
                        logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
                        logger.error("This indicates GitHub project management failed during reconciliation")
                        logger.error("Project monitoring cannot function without GitHub project state")
                        logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
                        exit(1)  # Fatal error - stop immediately

                    # Monitor each active board
                    for pipeline in project_config.pipelines:
                        if not pipeline.active:
                            continue

                        # Get board state
                        board_state = project_state.boards.get(pipeline.board_name)
                        if not board_state:
                            logger.error(f"FATAL: No GitHub state found for board '{pipeline.board_name}' in project '{project_name}'")
                            logger.error("This indicates GitHub project board creation failed during reconciliation")
                            logger.error("STOPPING PROJECT MONITOR: GitHub board management is broken")
                            exit(1)  # Fatal error - stop immediately

                        logger.debug(f"Checking {project_config.github['org']} project #{board_state.project_number} ({pipeline.board_name})...")

                        # Get current project items
                        current_items = self.get_project_items(project_config.github['org'], board_state.project_number)

                        if current_items:
                            # Detect changes (use board-specific key for state tracking)
                            board_key = f"{project_name}_{pipeline.board_name}"
                            changes = self.detect_changes(board_key, current_items)

                            if changes:
                                logger.info(f"Detected {len(changes)} changes in {project_name}/{pipeline.board_name}")
                                # Process changes with new system
                                self.process_board_changes(changes, project_name, pipeline.board_name)
                            else:
                                logger.debug(f"No changes in {project_name}/{pipeline.board_name}")

                            # Check all items for feedback comments (in issues)
                            for item in current_items:
                                self.check_for_feedback(
                                    project_name,
                                    pipeline.board_name,
                                    item.issue_number,
                                    item.repository
                                )
                        else:
                            logger.debug(f"No items found in {project_name}/{pipeline.board_name}")

                        # Monitor discussions if pipeline uses discussions workspace
                        if pipeline.workspace in ['discussions', 'hybrid']:
                            self.monitor_discussions(
                                project_name,
                                pipeline.board_name,
                                project_config.github['org'],
                                project_config.github['repo']
                            )

                # FAILSAFE: Check for waiting issues that haven't been processed
                # This catches edge cases where pipeline lock is released but next issue isn't triggered
                logger.debug("Running queue processing failsafe check...")
                self._check_and_process_waiting_issues_failsafe()

                logger.debug(f"Sleeping for {self.poll_interval} seconds...")
                time.sleep(self.poll_interval)

            except KeyboardInterrupt:
                logger.info("Project monitor stopped")
                break
            except Exception as e:
                logger.error(f"Error in monitoring loop: {e}")
                time.sleep(10)  # Wait before retrying

    def check_for_feedback(self, project_name: str, board_name: str, issue_number: int, repository: str):
        """
        DELETED: Old feedback manager - replaced by conversational_loop and review_cycle
        """
        logger.debug("Old feedback manager disabled")
        return

    def _check_for_feedback_OLD_DELETED(self, project_name: str, board_name: str, issue_number: int, repository: str):
        """Check if there are new feedback comments mentioning @orchestrator-bot"""
        try:
            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find the pipeline config for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            from services.github_integration import GitHubIntegration
            import asyncio

            # Create event loop if needed
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)

            github = GitHubIntegration(repo_owner=project_config.github['org'], repo_name=repository)

            # Get all comments to find which agent the user is responding to
            all_feedback_comments = loop.run_until_complete(
                github.get_feedback_comments(issue_number, repository, since_timestamp=None)
            )

            # Filter to unprocessed comments only
            new_feedback = []
            for comment in all_feedback_comments:
                if not self.feedback_manager.is_comment_processed(issue_number, comment['id']):
                    new_feedback.append(comment)

            if not new_feedback:
                return

            # Fetch all issue comments to find the most recent agent output before user feedback
            result = subprocess.run(
                ['gh', 'issue', 'view', str(issue_number), '--repo',
                 f"{project_config.github['org']}/{repository}", '--json', 'comments'],
                capture_output=True, text=True, check=True
            )
            all_comments_data = json.loads(result.stdout)
            all_comments = all_comments_data.get('comments', [])

            # For each new feedback comment, find which agent it's responding to
            from dateutil import parser as date_parser
            from datetime import timezone

            for feedback_comment in new_feedback:
                feedback_time = date_parser.parse(feedback_comment['created_at'])
                if feedback_time.tzinfo is None:
                    feedback_time = feedback_time.replace(tzinfo=timezone.utc)

                # Find the most recent agent comment before this feedback
                target_agent = None
                agent_comment_body = None
                most_recent_agent_time = None

                for comment in all_comments:
                    comment_time = date_parser.parse(comment.get('createdAt'))
                    if comment_time.tzinfo is None:
                        comment_time = comment_time.replace(tzinfo=timezone.utc)

                    # Only consider comments before the feedback
                    if comment_time >= feedback_time:
                        continue

                    # Check if this is an agent comment
                    body = comment.get('body', '')
                    for column in workflow_template.columns:
                        agent = column.agent
                        if agent and agent != 'null':
                            # Look for agent signature in comment
                            if f"_Processed by the {agent} agent_" in body:
                                # Track the most recent agent comment
                                if most_recent_agent_time is None or comment_time > most_recent_agent_time:
                                    target_agent = agent
                                    agent_comment_body = body
                                    most_recent_agent_time = comment_time
                                break

                if target_agent:
                    # If the target agent is a reviewer, feedback should go to the maker agent instead
                    final_target_agent = target_agent
                    if 'reviewer' in target_agent or 'review' in target_agent:
                        # Find the maker agent that this reviewer was reviewing
                        maker_agent = self._find_maker_for_reviewer(
                            target_agent, workflow_template, all_comments, most_recent_agent_time
                        )
                        if maker_agent:
                            logger.info(f"Routing feedback from {target_agent} review to maker agent {maker_agent}")
                            final_target_agent = maker_agent
                            # Get the maker's output as previous_output instead of reviewer's
                            for comment in reversed(all_comments):
                                if f"_Processed by the {maker_agent} agent_" in comment.get('body', ''):
                                    agent_comment_body = comment.get('body', '')
                                    break
                        else:
                            logger.warning(f"Could not find maker for reviewer {target_agent}, sending feedback to reviewer")

                    logger.info(f"Found feedback for {final_target_agent} on issue #{issue_number}")

                    # Create a feedback task for this specific agent
                    self.create_feedback_task(
                        project_name, board_name, issue_number,
                        repository, final_target_agent, [feedback_comment], project_config,
                        previous_output=agent_comment_body
                    )
                else:
                    logger.warning(f"Could not determine which agent to route feedback to for issue #{issue_number}")
                    # Mark as processed anyway to avoid repeated attempts
                    self.feedback_manager.mark_comment_processed(issue_number, feedback_comment['id'], project_name)

        except Exception as e:
            logger.error(f"Error checking for feedback: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _find_maker_for_reviewer(self, reviewer_agent, workflow_template, all_comments, reviewer_time):
        """Find the maker agent that a reviewer was reviewing"""
        from dateutil import parser as date_parser
        from datetime import timezone

        # Look backwards through comments before the reviewer's comment
        # to find the most recent non-reviewer agent
        most_recent_maker = None
        most_recent_maker_time = None

        for comment in all_comments:
            comment_time = date_parser.parse(comment.get('createdAt'))
            if comment_time.tzinfo is None:
                comment_time = comment_time.replace(tzinfo=timezone.utc)

            # Only consider comments before the reviewer
            if comment_time >= reviewer_time:
                continue

            # Check if this is an agent comment
            body = comment.get('body', '')
            for column in workflow_template.columns:
                agent = column.agent
                if agent and agent != 'null':
                    # Look for agent signature
                    if f"_Processed by the {agent} agent_" in body:
                        # Skip if it's also a reviewer
                        if 'reviewer' not in agent and 'review' not in agent:
                            # Track the most recent maker
                            if most_recent_maker_time is None or comment_time > most_recent_maker_time:
                                most_recent_maker = agent
                                most_recent_maker_time = comment_time
                        break

        return most_recent_maker

    def create_feedback_task(self, project_name: str, board_name: str, issue_number: int,
                            repository: str, agent: str, feedback_comments: List[Dict[str, Any]],
                            project_config, previous_output: str = None):
        """Create a task to handle feedback for an agent"""
        try:
            # Fetch full issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # DEFENSIVE: Check if issue is open before creating feedback task
            issue_state = issue_data.get('state', '').upper()
            if issue_state == 'CLOSED':
                logger.info(f"Skipping feedback task for issue #{issue_number}: issue is CLOSED")
                return

            # Prepare feedback context
            feedback_text = "\n\n".join([
                f"**Feedback from @{comment['author']} at {comment['created_at']}:**\n{comment['body']}"
                for comment in feedback_comments
            ])

            # Create task context with feedback
            task_context = {
                'project': project_name,
                'board': board_name,
                'pipeline': board_name,  # Simplified
                'repository': repository,
                'issue_number': issue_number,
                'issue': issue_data,
                'column': 'feedback',
                'trigger': 'feedback_loop',
                'feedback': {
                    'comments': feedback_comments,
                    'formatted_text': feedback_text
                },
                'previous_output': previous_output,  # Include agent's previous work
                'timestamp': utc_isoformat()
            }

            task = Task(
                id=f"{agent}_feedback_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                agent=agent,
                project=project_name,
                priority=TaskPriority.HIGH,  # Feedback gets high priority
                context=task_context,
                created_at=utc_isoformat()
            )

            self.task_queue.enqueue(task)

            # Mark comments as processed
            for comment in feedback_comments:
                self.feedback_manager.mark_comment_processed(issue_number, comment['id'], project_name)

            logger.info(f"Created feedback task for {agent} on issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to create feedback task: {e}")

    def process_board_changes(self, changes: List[Dict[str, Any]], project_name: str, board_name: str):
        """Process detected changes for a specific board"""
        for change in changes:
            logger.info(f"{change['type']}: #{change['issue_number']} - {change['title']}")

            if change['type'] == 'status_changed':
                logger.info(f"   Status: {change['old_status']} → {change['new_status']}")
                logger.info(f"   Board: {board_name}")

                # Record status change in work execution state (only if not programmatic)
                from services.work_execution_state import work_execution_tracker
                
                # Check if this was a recent programmatic change to avoid duplicate recording
                was_programmatic = work_execution_tracker.was_recent_programmatic_change(
                    project_name=project_name,
                    issue_number=change['issue_number'],
                    to_status=change['new_status'],
                    time_window_seconds=60
                )
                
                if not was_programmatic:
                    # Only record if this appears to be a manual status change
                    work_execution_tracker.record_status_change(
                        issue_number=change['issue_number'],
                        from_status=change['old_status'],
                        to_status=change['new_status'],
                        trigger='manual',  # Status changes from GitHub polling are manual
                        project_name=project_name
                    )
                else:
                    logger.debug(
                        f"Skipping duplicate status_change recording for #{change['issue_number']} "
                        f"({change['old_status']} → {change['new_status']}) - already recorded programmatically"
                    )

                # Check if this issue needs a discussion created BEFORE triggering agent
                # (Important for discussions workspaces)
                self._check_and_create_discussion(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['repository'],
                    change.get('new_status')  # Pass the new status for safety check
                )

                self.trigger_agent_for_status(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['new_status'],
                    change['repository']
                )
            elif change['type'] == 'item_added':
                logger.info(f"   Added to: {change['status']}")
                logger.info(f"   Board: {board_name}")

                # Check if this issue needs a discussion created
                self._check_and_create_discussion(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['repository'],
                    change.get('status')  # Pass the status for safety check
                )

                self.trigger_agent_for_status(
                    project_name,
                    board_name,
                    change['issue_number'],
                    change['status'],
                    change['repository']
                )

    def _check_and_create_discussion(self, project_name: str, board_name: str,
                                     issue_number: int, repository: str, status: Optional[str] = None):
        """Check if issue needs a discussion and create it if configured"""
        try:
            from config.state_manager import state_manager

            # Check if discussion already exists for this issue
            if state_manager.get_discussion_for_issue(project_name, issue_number):
                logger.debug(f"Discussion already exists for issue #{issue_number}")
                return

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find pipeline config
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Check if this pipeline uses discussions
            workspace = pipeline_config.workspace
            if workspace not in ['discussions', 'hybrid']:
                return

            # Check if auto-creation is enabled (default to True for discussion workspaces)
            auto_create = getattr(pipeline_config, 'auto_create_from_issues', True)
            if not auto_create:
                return

            # SAFETY LATCH: Only create discussions for items in Backlog column of Planning & Design board
            # This prevents erroneous discussion creation when sub-issues are linked to parent issues
            if status and status.lower() != 'backlog':
                logger.debug(f"Skipping discussion creation for issue #{issue_number} - not in Backlog column (current: {status})")
                return

            logger.info(f"Creating discussion for issue #{issue_number} (workspace: {workspace})")

            # Create the discussion
            self._create_discussion_from_issue(
                project_name,
                issue_number,
                repository,
                pipeline_config,
                project_config
            )

        except Exception as e:
            logger.error(f"Error checking/creating discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _create_discussion_from_issue(self, project_name: str, issue_number: int,
                                     repository: str, pipeline_config, project_config):
        """Auto-create discussion from issue"""
        try:
            # Fetch issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # Get discussion category
            # Use first discussion stage if available, otherwise 'initial'
            stage = (pipeline_config.discussion_stages[0]
                    if hasattr(pipeline_config, 'discussion_stages') and pipeline_config.discussion_stages
                    else 'initial')
            workspace_type, category_id = self.workspace_router.determine_workspace(
                project_name,
                pipeline_config.board_name,
                stage
            )

            if not category_id:
                logger.warning(f"Could not determine discussion category for {project_name}/{pipeline_config.board_name} (GitHub App not configured)")
                return

            # Get repository ID for GraphQL
            repo_id = self.discussions.get_repository_id(
                project_config.github['org'],
                repository
            )

            if not repo_id:
                logger.error(f"Could not get repository ID for {project_config.github['org']}/{repository}")
                return

            # Format discussion title
            title_prefix = getattr(pipeline_config, 'discussion_title_prefix', 'Requirements: ')
            discussion_title = f"{title_prefix}{issue_data['title']}"

            # Format discussion body
            discussion_body = self._format_discussion_from_issue(issue_data, issue_number)

            # Create discussion
            discussion_data = self.discussions.create_discussion(
                owner=project_config.github['org'],
                repo=repository,
                repository_id=repo_id,
                category_id=category_id,
                title=discussion_title,
                body=discussion_body
            )

            if not discussion_data:
                logger.error(f"Failed to create discussion for issue #{issue_number}")
                return

            # Extract discussion details from response
            discussion_id = discussion_data['id']
            discussion_number = discussion_data['number']
            discussion_url = discussion_data['url']

            logger.info(f"Created discussion #{discussion_number} (ID: {discussion_id}) for issue #{issue_number}")

            # Store link in state
            from config.state_manager import state_manager
            state_manager.link_issue_to_discussion(project_name, issue_number, discussion_id)

            # Add comment to issue linking to discussion
            comment_body = f"""📋 Requirements analysis moved to Discussion #{discussion_number}

This issue will be updated with final requirements when ready for implementation.

_Link: {discussion_url}_"""

            subprocess.run(
                ['gh', 'issue', 'comment', str(issue_number),
                 '--repo', f"{project_config.github['org']}/{repository}",
                 '--body', comment_body],
                capture_output=True, text=True, check=True
            )

            logger.info(f"Added link comment to issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to create discussion from issue: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _format_discussion_from_issue(self, issue_data: Dict[str, Any], issue_number: int) -> str:
        """Format discussion body from issue data"""
        labels = ', '.join([label['name'] for label in issue_data.get('labels', [])])
        author = issue_data.get('author', {}).get('login', 'unknown')

        return f"""# Requirements Analysis

Auto-created from Issue #{issue_number}

## User Request

{issue_data.get('body', '_No description provided_')}

---

**Labels**: {labels if labels else '_None_'}
**Requested by**: @{author}

---

The orchestrator will analyze this request and develop detailed requirements.
When complete, Issue #{issue_number} will be updated with final requirements.
"""

    def finalize_requirements_to_issue(self, project_name: str, board_name: str,
                                      issue_number: int, repository: str,
                                      discussion_id: Optional[str] = None):
        """
        Extract final requirements from discussion and update issue body.
        Called when requirements are approved or at transition stage.
        """
        try:
            from config.state_manager import state_manager

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Get discussion ID from state if not provided
            if not discussion_id:
                discussion_id = state_manager.get_discussion_for_issue(project_name, issue_number)
                if not discussion_id:
                    logger.error(f"No discussion found for issue #{issue_number}")
                    return

            logger.info(f"Finalizing requirements from discussion to issue #{issue_number}")

            # Get full discussion with all comments
            discussion = self.discussions.get_discussion(discussion_id)
            if not discussion:
                logger.error(f"Could not retrieve discussion {discussion_id}")
                return

            # Extract requirements from agent comments
            requirements = self._extract_requirements_from_discussion(discussion_id, project_config, repository)

            if not requirements:
                logger.warning(f"No requirements found in discussion for issue #{issue_number}")
                return

            # Format new issue body
            discussion_number = discussion.get('number', '?')
            discussion_url = discussion.get('url', '')
            new_issue_body = self._format_finalized_requirements(
                discussion_number,
                discussion_url,
                requirements
            )

            # Update issue body
            result = subprocess.run(
                ['gh', 'issue', 'edit', str(issue_number),
                 '--repo', f"{project_config.github['org']}/{repository}",
                 '--body', new_issue_body],
                capture_output=True, text=True, check=True
            )

            logger.info(f"Updated issue #{issue_number} with finalized requirements")

            # Add "ready-for-implementation" label
            subprocess.run(
                ['gh', 'issue', 'edit', str(issue_number),
                 '--repo', f"{project_config.github['org']}/{repository}",
                 '--add-label', 'ready-for-implementation'],
                capture_output=True, text=True
            )

            # Post completion comment to discussion
            completion_comment = f"""✅ Requirements finalized and posted to Issue #{issue_number}

The issue has been updated with the final requirements from this discussion.
Moving to implementation phase.

[View Issue →]({discussion.get('url', '').replace(f'/discussions/{discussion_number}', f'/issues/{issue_number}')})"""

            self.discussions.add_discussion_comment(discussion_id, completion_comment)

            logger.info(f"Finalization complete for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to finalize requirements to issue: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _extract_requirements_from_discussion(self, discussion_id: str,
                                             project_config, repository: str) -> Dict[str, Any]:
        """
        Extract structured requirements from discussion comments.
        Looks for outputs from business_analyst, architect, and other agents.
        """
        try:
            # Get full discussion with comments
            org = project_config.github['org']

            # Use GraphQL to get discussion with all comments
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                    }
                  }
                }
              }
            }
            """

            from services.github_app import github_app
            result = github_app.graphql_request(query, {'discussionId': discussion_id})

            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion comments for {discussion_id}")
                return {}

            comments = result['node']['comments']['nodes']

            # Extract agent outputs
            requirements = {
                'executive_summary': '',
                'functional': [],
                'non_functional': [],
                'user_stories': [],
                'architecture': '',
                'acceptance_criteria': []
            }

            # Find the most recent business analyst output
            ba_output = None
            architect_output = None

            for comment in reversed(comments):
                body = comment.get('body', '')
                author = comment.get('author', {}).get('login', '')

                # Look for agent signatures
                if '_Processed by the business_analyst agent_' in body:
                    if not ba_output:
                        ba_output = body
                elif '_Processed by the software_architect agent_' in body:
                    if not architect_output:
                        architect_output = body

            # Parse business analyst output
            if ba_output:
                requirements['executive_summary'] = self._extract_section(ba_output, 'Executive Summary', 'Functional Requirements')
                requirements['functional'] = self._extract_list_items(ba_output, 'Functional Requirements')
                requirements['non_functional'] = self._extract_list_items(ba_output, 'Non-Functional Requirements')
                requirements['user_stories'] = self._extract_user_stories(ba_output)
                requirements['acceptance_criteria'] = self._extract_list_items(ba_output, 'Acceptance Criteria')

            # Parse architect output
            if architect_output:
                requirements['architecture'] = self._extract_section(architect_output, 'Architecture Overview', 'Component Design')

            return requirements

        except Exception as e:
            logger.error(f"Error extracting requirements from discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return {}

    def _extract_section(self, text: str, start_marker: str, end_marker: str) -> str:
        """Extract text between two section markers"""
        try:
            start = text.find(f"## {start_marker}")
            if start == -1:
                start = text.find(f"### {start_marker}")
            if start == -1:
                return ""

            end = text.find(f"## {end_marker}", start + 1)
            if end == -1:
                end = text.find(f"### {end_marker}", start + 1)
            if end == -1:
                end = len(text)

            section = text[start:end].strip()
            # Remove the header line
            lines = section.split('\n', 1)
            return lines[1].strip() if len(lines) > 1 else ""
        except Exception:
            return ""

    def _extract_list_items(self, text: str, section_name: str) -> List[str]:
        """Extract bullet point list items from a section"""
        try:
            section_text = self._extract_section(text, section_name, '---')
            if not section_text:
                return []

            items = []
            for line in section_text.split('\n'):
                line = line.strip()
                if line.startswith('- ') or line.startswith('* '):
                    items.append(line[2:].strip())
                elif line.startswith('• '):
                    items.append(line[2:].strip())
            return items
        except Exception:
            return []

    def _extract_user_stories(self, text: str) -> List[str]:
        """Extract user stories from business analyst output"""
        try:
            stories = []
            # Look for "As a..." patterns
            lines = text.split('\n')
            current_story = []

            for line in lines:
                line = line.strip()
                if line.startswith('**As a') or line.startswith('As a'):
                    if current_story:
                        stories.append(' '.join(current_story))
                    current_story = [line]
                elif current_story and (line.startswith('I want') or line.startswith('So that')):
                    current_story.append(line)
                elif current_story and not line:
                    stories.append(' '.join(current_story))
                    current_story = []

            if current_story:
                stories.append(' '.join(current_story))

            return stories
        except Exception:
            return []

    def _format_finalized_requirements(self, discussion_number: int, discussion_url: str,
                                      requirements: Dict[str, Any]) -> str:
        """Format the finalized requirements for issue body"""
        parts = []

        # Executive summary
        if requirements.get('executive_summary'):
            parts.append(requirements['executive_summary'])
            parts.append('')

        # Background section
        parts.append('## Background')
        parts.append(f'Full requirements analysis available in [Discussion #{discussion_number}]({discussion_url})')
        parts.append('')

        # Functional requirements
        if requirements.get('functional'):
            parts.append('## Functional Requirements')
            for item in requirements['functional']:
                parts.append(f'- {item}')
            parts.append('')

        # Non-functional requirements
        if requirements.get('non_functional'):
            parts.append('## Non-Functional Requirements')
            for item in requirements['non_functional']:
                parts.append(f'- {item}')
            parts.append('')

        # User stories
        if requirements.get('user_stories'):
            parts.append('## User Stories')
            for story in requirements['user_stories']:
                parts.append(f'- {story}')
            parts.append('')

        # Architecture notes
        if requirements.get('architecture'):
            parts.append('## Architecture Notes')
            parts.append(requirements['architecture'])
            parts.append('')

        # Acceptance criteria
        if requirements.get('acceptance_criteria'):
            parts.append('## Acceptance Criteria')
            for item in requirements['acceptance_criteria']:
                parts.append(f'- {item}')
            parts.append('')

        # Footer
        parts.append('---')
        parts.append(f'📋 Requirements finalized from [Discussion #{discussion_number}]({discussion_url})')
        parts.append('Ready for implementation.')
        parts.append('---')

        return '\n'.join(parts)

    def monitor_discussions(self, project_name: str, board_name: str, org: str, repo: str):
        """Monitor discussions for activity and feedback"""
        try:
            from config.state_manager import state_manager

            # Get project state to find linked discussions
            project_state = state_manager.load_project_state(project_name)
            if not project_state or not project_state.issue_discussion_links:
                logger.debug(f"No discussions linked for {project_name}")
                return

            # Get recent discussions (updated in last poll interval * 2)
            from datetime import datetime, timedelta, timezone
            since = datetime.now(timezone.utc) - timedelta(seconds=self.poll_interval * 2)

            # Check each linked discussion for new activity
            for issue_number, discussion_id in list(project_state.issue_discussion_links.items()):
                try:
                    # Get discussion details
                    discussion = self.discussions.get_discussion(discussion_id)
                    if not discussion:
                        # Discussion was deleted - remove from state
                        from config.state_manager import state_manager
                        logger.info(f"Discussion {discussion_id} for issue #{issue_number} no longer exists, removing from state")
                        state_manager.unlink_issue_discussion(project_name, int(issue_number))
                        continue

                    # Check if discussion has been updated recently
                    updated_at = datetime.fromisoformat(discussion['updatedAt'].replace('Z', '+00:00'))
                    if updated_at < since:
                        continue  # No recent activity

                    logger.debug(f"Checking discussion #{discussion.get('number')} for new activity")

                    # Check for feedback in discussion comments
                    self.check_for_feedback_in_discussion(
                        project_name,
                        board_name,
                        discussion_id,
                        issue_number,
                        repo
                    )

                except Exception as e:
                    logger.error(f"Error monitoring discussion {discussion_id}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error in monitor_discussions: {e}")
            import traceback
            logger.error(traceback.format_exc())


    def get_full_thread_history(self, all_comments: List[Dict], parent_comment_id: str) -> List[Dict]:
        """
        Extract complete thread history for conversational context

        Args:
            all_comments: All comments from the discussion (with replies)
            parent_comment_id: The ID of the parent comment to extract thread from

        Returns:
            List of messages in chronological order with role, author, body, timestamp
        """
        thread_history = []

        # Find the parent comment
        for comment in all_comments:
            if comment['id'] == parent_comment_id:
                # Add parent comment (the agent's initial output or previous reply)
                author_login = comment.get('author', {}).get('login', 'unknown')
                is_bot = 'bot' in author_login.lower()

                thread_history.append({
                    'role': 'agent' if is_bot else 'user',
                    'author': author_login,
                    'body': comment.get('body', ''),
                    'timestamp': comment.get('createdAt'),
                    'is_agent': is_bot
                })

                # Add all replies in chronological order
                for reply in comment.get('replies', {}).get('nodes', []):
                    reply_author = reply.get('author', {})
                    reply_author_login = reply_author.get('login', '') if reply_author else 'unknown'
                    reply_is_bot = 'bot' in reply_author_login.lower()

                    thread_history.append({
                        'role': 'agent' if reply_is_bot else 'user',
                        'author': reply_author_login,
                        'body': reply.get('body', ''),
                        'timestamp': reply.get('createdAt'),
                        'is_agent': reply_is_bot
                    })

                break

        return thread_history

    def check_for_feedback_in_discussion(self, project_name: str, board_name: str,
                                         discussion_id: str, issue_number: int, repository: str):
        """
        Check if there's an escalated review cycle waiting for human feedback on this discussion.
        If so, check for new human comments and resume the cycle.

        Note: Regular feedback for conversational columns is handled by human_feedback_loop.
        This method only checks for escalated review cycles.
        """
        try:
            from services.review_cycle import review_cycle_executor
            from services.github_app import github_app
            from dateutil import parser as date_parser
            from datetime import datetime

            # Check if there's an escalated cycle for this issue (in-memory)
            if issue_number not in review_cycle_executor.active_cycles:
                return

            cycle_state = review_cycle_executor.active_cycles[issue_number]
            if cycle_state.status != 'awaiting_human_feedback':
                return

            # There's an escalated cycle waiting for feedback on this discussion
            logger.debug(f"Checking for human feedback on escalated cycle for issue #{issue_number}")

            # Get project config for org
            project_config = self.config_manager.get_project_config(project_name)
            org = project_config.github['org']

            # Query for recent comments
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  comments(last: 20) {
                    nodes {
                      id
                      author {
                        login
                      }
                      body
                      createdAt
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})

            if result and 'node' in result and result['node']:
                comments = result['node']['comments']['nodes']

                # Get escalation time
                escalation_time = datetime.fromisoformat(cycle_state.escalation_time)
                if escalation_time.tzinfo:
                    escalation_time = escalation_time.replace(tzinfo=None)

                # Look for human feedback after escalation
                human_feedback = None
                for comment in reversed(comments):  # Most recent first
                    author = comment['author']['login']
                    created_at = date_parser.parse(comment['createdAt'])

                    # Convert to naive datetime
                    if created_at.tzinfo:
                        created_at = created_at.replace(tzinfo=None)

                    # Check if this is a human comment after escalation
                    if author != 'orchestrator-bot' and created_at > escalation_time:
                        human_feedback = comment['body']
                        logger.info(
                            f"Human feedback detected for escalated cycle #{issue_number} "
                            f"from {author}, resuming review cycle..."
                        )
                        break

                if human_feedback:
                    # Human feedback detected! Resume the review cycle in background thread
                    import asyncio
                    import threading

                    def resume_cycle_in_thread():
                        """Resume review cycle in background thread (non-blocking)"""
                        try:
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)

                            loop.run_until_complete(
                                review_cycle_executor.resume_review_cycle_with_feedback(
                                    cycle_state=cycle_state,
                                    human_feedback=human_feedback,
                                    org=org
                                )
                            )
                            loop.close()

                            logger.info(f"Review cycle #{issue_number} resumed successfully")
                        except Exception as e:
                            logger.error(f"Error resuming review cycle #{issue_number}: {e}")
                            import traceback
                            logger.error(traceback.format_exc())

                    # Start in background thread
                    thread = threading.Thread(target=resume_cycle_in_thread, daemon=True)
                    thread.start()

                    logger.info(f"Review cycle resume thread started for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Error checking escalated cycle for issue #{issue_number}: {e}")
            import traceback
            logger.error(traceback.format_exc())

        # OLD CODE BELOW (disabled)
        """Check discussion comments for user feedback mentioning @orchestrator-bot"""
        try:
            from config.state_manager import state_manager
            from dateutil import parser as date_parser
            from datetime import timezone

            # Get project config
            project_config = self.config_manager.get_project_config(project_name)

            # Find the pipeline config for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board_name:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Get workflow template
            workflow_template = self.config_manager.get_workflow_template(pipeline_config.workflow)

            # Get discussion with all comments and replies
            from services.github_app import github_app
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                        ... on User {
                          login
                        }
                        ... on Bot {
                          login
                        }
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})
            if not result or 'node' not in result:
                return

            all_comments = result['node']['comments']['nodes']

            # Find new feedback comments/replies (mentioning @orchestrator-bot from non-bot users)
            new_feedback = []

            for comment in all_comments:
                # Check top-level comment
                comment_id = comment['id']
                body = comment.get('body', '')
                author = comment.get('author', {})
                author_login = author.get('login', '') if author else ''

                # Skip bot comments
                if 'bot' not in author_login.lower():
                    # Check if this comment mentions the bot
                    if '@orchestrator-bot' in body:
                        # Check if we've already processed this comment
                        if not self.feedback_manager.is_comment_processed(issue_number, comment_id):
                            new_feedback.append({
                                'id': comment_id,
                                'body': body,
                                'author': author_login,
                                'created_at': comment['createdAt'],
                                'parent_comment_id': None,  # Top-level comment
                                'is_reply': False
                            })

                # Check replies to this comment
                for reply in comment.get('replies', {}).get('nodes', []):
                    reply_id = reply['id']
                    reply_body = reply.get('body', '')
                    reply_author = reply.get('author', {})
                    reply_author_login = reply_author.get('login', '') if reply_author else ''

                    # Skip bot replies
                    if 'bot' in reply_author_login.lower():
                        continue

                    # Check if this reply mentions the bot
                    if '@orchestrator-bot' not in reply_body:
                        continue

                    # Check if we've already processed this reply
                    if self.feedback_manager.is_comment_processed(issue_number, reply_id):
                        continue

                    new_feedback.append({
                        'id': reply_id,
                        'body': reply_body,
                        'author': reply_author_login,
                        'created_at': reply['createdAt'],
                        'parent_comment_id': comment_id,  # This is a reply to a comment
                        'is_reply': True
                    })

            if not new_feedback:
                return

            # For each feedback comment/reply, determine which agent to route to
            for feedback_comment in new_feedback:
                feedback_time = date_parser.parse(feedback_comment['created_at'])
                if feedback_time.tzinfo is None:
                    feedback_time = feedback_time.replace(tzinfo=timezone.utc)

                target_agent = None
                agent_comment_body = None
                agent_comment_id = None
                most_recent_agent_time = None

                # If this is a reply to a comment, check if the parent is an agent comment
                if feedback_comment['is_reply'] and feedback_comment['parent_comment_id']:
                    parent_id = feedback_comment['parent_comment_id']

                    # Find the parent comment
                    for comment in all_comments:
                        if comment['id'] == parent_id:
                            # Check if parent is an agent comment
                            body = comment.get('body', '')
                            for column in workflow_template.columns:
                                agent = column.agent
                                if agent and agent != 'null':
                                    if f"_Processed by the {agent} agent_" in body:
                                        target_agent = agent
                                        agent_comment_body = body
                                        agent_comment_id = parent_id
                                        logger.info(f"Reply is threaded to {agent} agent comment")
                                        break
                            break

                # If no agent found (not a reply or parent wasn't agent), use chronological search
                if not target_agent:
                    for comment in all_comments:
                        comment_time = date_parser.parse(comment.get('createdAt'))
                        if comment_time.tzinfo is None:
                            comment_time = comment_time.replace(tzinfo=timezone.utc)

                        # Only consider comments before the feedback
                        if comment_time >= feedback_time:
                            continue

                        # Check if this is an agent comment
                        body = comment.get('body', '')
                        for column in workflow_template.columns:
                            agent = column.agent
                            if agent and agent != 'null':
                                # Look for agent signature in comment
                                if f"_Processed by the {agent} agent_" in body:
                                    # Track the most recent agent comment
                                    if most_recent_agent_time is None or comment_time > most_recent_agent_time:
                                        target_agent = agent
                                        agent_comment_body = body
                                        agent_comment_id = comment['id']
                                        most_recent_agent_time = comment_time
                                    break

                if target_agent:
                    # If the target agent is a reviewer, feedback should go to the maker agent instead
                    final_target_agent = target_agent
                    if 'reviewer' in target_agent or 'review' in target_agent:
                        # Find the maker agent that this reviewer was reviewing
                        maker_agent = self._find_maker_for_reviewer_in_discussion(
                            target_agent, workflow_template, all_comments, most_recent_agent_time
                        )
                        if maker_agent:
                            logger.info(f"Routing feedback from {target_agent} review to maker agent {maker_agent}")
                            final_target_agent = maker_agent
                            # Get the maker's output as previous_output instead of reviewer's
                            for comment in reversed(all_comments):
                                if f"_Processed by the {maker_agent} agent_" in comment.get('body', ''):
                                    agent_comment_body = comment.get('body', '')
                                    break
                        else:
                            logger.warning(f"Could not find maker for reviewer {target_agent}, sending feedback to reviewer")

                    logger.info(f"Found feedback for {final_target_agent} in discussion for issue #{issue_number}")

                    # Extract full thread history if this is a threaded reply
                    thread_history = []
                    conversation_mode = None

                    if feedback_comment['is_reply'] and feedback_comment['parent_comment_id']:
                        thread_history = self.get_full_thread_history(
                            all_comments,
                            feedback_comment['parent_comment_id']
                        )
                        conversation_mode = 'threaded'
                        logger.info(f"Extracted thread history with {len(thread_history)} messages for conversational mode")

                    # Create a feedback task for this specific agent
                    self.create_feedback_task_for_discussion(
                        project_name, board_name, issue_number,
                        repository, final_target_agent, [feedback_comment], project_config,
                        discussion_id, previous_output=agent_comment_body,
                        reply_to_comment_id=agent_comment_id,  # For threaded replies
                        thread_history=thread_history,  # For conversational mode
                        conversation_mode=conversation_mode  # Signal conversational mode
                    )
                else:
                    logger.warning(f"Could not determine which agent to route feedback to in discussion for issue #{issue_number}")
                    # Mark as processed anyway to avoid repeated attempts
                    self.feedback_manager.mark_comment_processed(issue_number, feedback_comment['id'], project_name)

        except Exception as e:
            logger.error(f"Error checking for feedback in discussion: {e}")
            import traceback
            logger.error(traceback.format_exc())

    def _find_maker_for_reviewer_in_discussion(self, reviewer_agent, workflow_template, all_comments, reviewer_time):
        """Find the maker agent that a reviewer was reviewing (for discussions)"""
        from dateutil import parser as date_parser
        from datetime import timezone

        # Look backwards through comments before the reviewer's comment
        # to find the most recent non-reviewer agent
        most_recent_maker = None
        most_recent_maker_time = None

        for comment in all_comments:
            comment_time = date_parser.parse(comment.get('createdAt'))
            if comment_time.tzinfo is None:
                comment_time = comment_time.replace(tzinfo=timezone.utc)

            # Only consider comments before the reviewer
            if comment_time >= reviewer_time:
                continue

            # Check if this is an agent comment
            body = comment.get('body', '')
            for column in workflow_template.columns:
                agent = column.agent
                if agent and agent != 'null':
                    # Look for agent signature
                    if f"_Processed by the {agent} agent_" in body:
                        # Skip if it's also a reviewer
                        if 'reviewer' not in agent and 'review' not in agent:
                            # Track the most recent maker
                            if most_recent_maker_time is None or comment_time > most_recent_maker_time:
                                most_recent_maker = agent
                                most_recent_maker_time = comment_time
                        break

        return most_recent_maker

    def create_feedback_task_for_discussion(self, project_name: str, board_name: str, issue_number: int,
                                           repository: str, agent: str, feedback_comments: List[Dict[str, Any]],
                                           project_config, discussion_id: str, previous_output: str = None,
                                           reply_to_comment_id: str = None, thread_history: List[Dict] = None,
                                           conversation_mode: str = None):
        """Create a task to handle feedback for an agent (from discussion)"""
        try:
            # Fetch full issue details
            issue_data = self.get_issue_details(repository, issue_number, project_config.github['org'])

            # DEFENSIVE: Check if issue is open before creating feedback task
            issue_state = issue_data.get('state', '').upper()
            if issue_state == 'CLOSED':
                logger.info(f"Skipping feedback task for issue #{issue_number}: issue is CLOSED")
                return

            # Prepare feedback context
            feedback_text = "\n\n".join([
                f"**Feedback from @{comment['author']} at {comment['created_at']}:**\n{comment['body']}"
                for comment in feedback_comments
            ])

            # Create task context with feedback and discussion info
            task_context = {
                'project': project_name,
                'board': board_name,
                'pipeline': board_name,
                'repository': repository,
                'issue_number': issue_number,
                'issue': issue_data,
                'column': 'feedback',
                'trigger': 'feedback_loop',
                'workspace_type': 'discussions',
                'discussion_id': discussion_id,
                'reply_to_comment_id': reply_to_comment_id,  # For threaded replies
                'conversation_mode': conversation_mode,  # 'threaded' for conversational mode
                'thread_history': thread_history or [],  # Full conversation history
                'feedback': {
                    'comments': feedback_comments,
                    'formatted_text': feedback_text
                },
                'previous_output': previous_output,
                'timestamp': utc_isoformat()
            }

            from task_queue.task_manager import Task, TaskPriority
            task = Task(
                id=f"{agent}_feedback_{project_name}_{board_name}_{issue_number}_{int(time.time())}",
                agent=agent,
                project=project_name,
                priority=TaskPriority.HIGH,
                context=task_context,
                created_at=utc_isoformat()
            )

            self.task_queue.enqueue(task)

            # Mark comments as processed
            for comment in feedback_comments:
                self.feedback_manager.mark_comment_processed(issue_number, comment['id'], project_name)

            logger.info(f"Created feedback task for {agent} on discussion for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to create feedback task for discussion: {e}")

if __name__ == "__main__":
    # Initialize task queue and start monitoring
    task_queue = TaskQueue()
    monitor = ProjectMonitor(task_queue)
    monitor.monitor_projects()
"""
Agent Container Recovery Service

Handles recovery of orphaned agent containers and repair cycle containers after orchestrator restart.
Checks for running Docker containers and attempts to reconnect or clean them up.
"""

import logging
import subprocess
import json
import redis
from typing import List, Dict, Optional, Tuple
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)


class AgentContainerRecovery:
    """Manages recovery of agent containers after orchestrator restart"""
    
    def __init__(self, redis_client: Optional[redis.Redis] = None):
        """
        Initialize container recovery service
        
        Args:
            redis_client: Redis client for tracking
        """
        if redis_client:
            self.redis = redis_client
        else:
            self.redis = redis.Redis(
                host='redis',
                port=6379,
                decode_responses=True
            )
    
    def get_running_agent_containers(self) -> List[Dict[str, str]]:
        """
        Get list of running agent containers from Docker
        
        Returns:
            List of container info dicts with name, status, created_at
        """
        try:
            # List all running containers with name starting with claude-agent-
            result = subprocess.run(
                ['docker', 'ps', '--filter', 'name=claude-agent-', '--format', '{{json .}}'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to list Docker containers: {result.stderr}")
                return []
            
            containers = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        container_data = json.loads(line)
                        containers.append({
                            'name': container_data.get('Names', ''),
                            'id': container_data.get('ID', ''),
                            'status': container_data.get('Status', ''),
                            'created_at': container_data.get('CreatedAt', ''),
                            'image': container_data.get('Image', '')
                        })
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse container JSON: {e}")
            
            return containers
            
        except Exception as e:
            logger.error(f"Error getting running containers: {e}")
            return []
    
    def parse_container_name(self, container_name: str) -> Optional[Dict[str, str]]:
        """
        Parse agent container name to extract metadata

        ACTUAL container name format: claude-agent-{project}-{task_id}
        Where task_id is: {task_id_prefix}_{agent}_{timestamp}

        For example:
        - claude-agent-what_am_i_watching-review_cycle_senior_software_engineer_1762082586
        - claude-agent-project_name-task_business_analyst_1234567890

        The task_id always ends with a numeric timestamp and contains underscores (not hyphens).
        The project may contain hyphens (e.g., "my-project-name").

        Args:
            container_name: Container name

        Returns:
            Dict with project, agent, task_id components, or None if invalid format
        """
        if not container_name.startswith('claude-agent-'):
            return None

        # Remove prefix: claude-agent-
        remainder = container_name.replace('claude-agent-', '', 1)

        # Find the LAST hyphen - everything after it is the task_id
        # The task_id contains underscores, not hyphens, so the last hyphen separates project from task_id
        last_hyphen_idx = remainder.rfind('-')

        if last_hyphen_idx == -1:
            logger.warning(f"No hyphen found in container name after prefix: {container_name}")
            return None

        project = remainder[:last_hyphen_idx]
        task_id = remainder[last_hyphen_idx + 1:]

        # Validate task_id format: should have at least 2 underscores and end with numeric timestamp
        task_parts = task_id.split('_')

        if len(task_parts) < 3:
            logger.warning(f"Task ID has too few parts (expected at least 3): {task_id}")
            return None

        # Last part should be numeric timestamp
        if not task_parts[-1].isdigit():
            logger.warning(f"Task ID does not end with numeric timestamp: {task_id}")
            return None

        # Extract agent name (everything except the last part which is timestamp)
        # The agent name might be multiple words joined by underscores (e.g., "senior_software_engineer")
        # Format is: {task_id_prefix}_{agent_parts...}_{timestamp}
        timestamp = task_parts[-1]

        # The agent is typically the last word(s) before timestamp
        # We can't reliably determine where task_id_prefix ends and agent begins
        # So we'll extract what we can and use the full task_id
        agent = '_'.join(task_parts[:-1])  # Everything except timestamp

        return {
            'agent': agent,
            'project': project,
            'task_id': task_id,
            'timestamp': timestamp,
            'container_name': container_name
        }
    
    def check_execution_history(self, project: str, issue_number: int) -> Optional[Dict[str, any]]:
        """
        Check execution history for an issue

        Args:
            project: Project name
            issue_number: Issue number

        Returns:
            Most recent in_progress execution record, or None
        """
        try:
            from services.work_execution_state import work_execution_tracker

            logger.debug(f"Checking execution history for {project} issue #{issue_number}")

            # Load execution state using the proper API
            state = work_execution_tracker.load_state(project, issue_number)

            logger.debug(f"Loaded state for {project}/#{issue_number}: {len(state.get('execution_history', []))} executions")

            # Find most recent in_progress execution
            executions = state.get('execution_history', [])
            for execution in reversed(executions):
                logger.debug(f"Checking execution: outcome={execution.get('outcome')}, agent={execution.get('agent')}")
                if execution.get('outcome') == 'in_progress':
                    logger.info(f"Found in_progress execution for {project}/#{issue_number}: {execution}")
                    return execution

            logger.warning(f"No in_progress execution found for {project}/#{issue_number}")
            return None

        except Exception as e:
            logger.error(f"Error checking execution history for {project}/#{issue_number}: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def kill_container(self, container_name: str, container_id: str):
        """Kill a Docker container"""
        try:
            subprocess.run(
                ['docker', 'kill', container_id],
                capture_output=True,
                timeout=10
            )
            logger.info(f"Killed container {container_name}")
        except Exception as e:
            logger.error(f"Failed to kill container {container_name}: {e}")
    
    def cleanup_execution_state(self, project: str, issue_number: int, agent: str, reason: str):
        """
        Mark execution state as failed due to orchestrator restart
        
        Args:
            project: Project name
            issue_number: Issue number
            agent: Agent name
            reason: Reason for cleanup (e.g., "orchestrator_restart")
        """
        try:
            from services.work_execution_state import work_execution_tracker
            
            work_execution_tracker.record_execution_outcome(
                issue_number=issue_number,
                column='unknown',  # We don't know which column
                agent=agent,
                outcome='failed',
                project_name=project,
                error=reason
            )
            
            logger.info(f"Cleaned up execution state for {project} issue #{issue_number} agent {agent}: {reason}")
            
        except Exception as e:
            logger.error(f"Failed to cleanup execution state: {e}")
    
    def recover_or_cleanup_containers(self) -> Tuple[int, int, int]:
        """
        Main recovery function - checks all running containers and decides whether to keep or kill
        
        Returns:
            Tuple of (recovered_count, killed_count, error_count)
        """
        logger.info("Starting agent container recovery process")
        
        running_containers = self.get_running_agent_containers()
        
        if not running_containers:
            logger.info("No running agent containers found")
            return (0, 0, 0)
        
        logger.info(f"Found {len(running_containers)} running agent containers")
        
        recovered = 0
        killed = 0
        errors = 0
        
        for container in running_containers:
            container_name = container['name']
            container_id = container['id']

            try:
                # Skip agent containers that are part of repair cycles
                # These are managed by the repair cycle container itself
                if 'repair_' in container_name or 'repair-' in container_name:
                    logger.debug(
                        f"Skipping {container_name} - managed by repair cycle container"
                    )
                    continue

                # Parse container name
                metadata = self.parse_container_name(container_name)
                if not metadata:
                    logger.warning(f"Could not parse container name: {container_name}, killing it")
                    self.kill_container(container_name, container_id)
                    killed += 1
                    continue

                project = metadata['project']
                agent = metadata['agent']
                task_id = metadata['task_id']

                # Try to get additional metadata from Redis (if still available after restart)
                redis_key = f'agent:container:{container_name}'
                issue_number = None

                try:
                    redis_data = self.redis.hgetall(redis_key)
                    if redis_data and 'issue_number' in redis_data:
                        issue_number_str = redis_data.get('issue_number', '')
                        if issue_number_str and issue_number_str != 'unknown':
                            issue_number = int(issue_number_str)
                            logger.info(f"Found issue number {issue_number} in Redis for container {container_name}")
                except Exception as e:
                    logger.debug(f"Could not get Redis data for container: {e}")

                # If we have issue_number, validate against execution history
                if issue_number:
                    execution = self.check_execution_history(project, issue_number)

                    if not execution:
                        logger.warning(
                            f"Container {container_name} has no in_progress execution history, killing it"
                        )
                        self.kill_container(container_name, container_id)
                        # Clean up execution state to prevent deadlock
                        self.cleanup_execution_state(
                            project=project,
                            issue_number=issue_number,
                            agent=agent,
                            reason="Container killed during recovery: no execution history found"
                        )
                        killed += 1
                        continue

                    # Check if execution matches the container
                    # Note: execution['agent'] might have full underscores while our parsed agent is simplified
                    # So we check if parsed agent is contained in execution agent
                    exec_agent = execution.get('agent', '')
                    if exec_agent and exec_agent not in agent and agent not in exec_agent:
                        logger.warning(
                            f"Container {container_name} agent mismatch (container: {agent}, history: {exec_agent}), killing it"
                        )
                        self.kill_container(container_name, container_id)
                        self.cleanup_execution_state(project, issue_number, agent, "agent_mismatch")
                        killed += 1
                        continue
                else:
                    logger.info(
                        f"Container {container_name} has no issue number in Redis, "
                        f"will re-register without validation"
                    )
                
                # Check container age - if older than 2 hours, kill it
                # Docker CreatedAt format: "2025-10-17 16:27:28 +0000 UTC"
                created_str = container['created_at']
                try:
                    # Parse the timestamp
                    created_dt = datetime.strptime(created_str[:19], '%Y-%m-%d %H:%M:%S')
                    age = datetime.utcnow() - created_dt
                    
                    if age > timedelta(hours=2):
                        logger.warning(
                            f"Container {container_name} is too old ({age.total_seconds()/3600:.1f}h), killing it"
                        )
                        self.kill_container(container_name, container_id)
                        if issue_number:
                            self.cleanup_execution_state(project, issue_number, agent, "container_timeout")
                        killed += 1
                        continue
                except Exception as e:
                    logger.warning(f"Could not parse container age: {e}")
                
                # Container looks valid - reconnect to it by re-registering in Redis
                logger.info(
                    f"Container {container_name} appears valid, re-registering for tracking"
                )

                # Re-register container in Redis
                container_info = {
                    'container_name': container_name,
                    'agent': agent,
                    'project': project,
                    'task_id': task_id,
                    'started_at': datetime.utcnow().isoformat(),
                    'recovered': 'true'
                }

                # Add optional fields if available
                if issue_number:
                    container_info['issue_number'] = str(issue_number)

                # Get started_at from execution history if available
                if issue_number:
                    execution = self.check_execution_history(project, issue_number)
                    if execution and 'timestamp' in execution:
                        container_info['started_at'] = execution['timestamp']

                self.redis.hset(f'agent:container:{container_name}', mapping=container_info)
                self.redis.expire(f'agent:container:{container_name}', 7200)
                
                logger.info(f"✓ Recovered container: {container_name}")
                recovered += 1
                
            except Exception as e:
                logger.error(f"Error processing container {container_name}: {e}")
                errors += 1
        
        logger.info(
            f"Container recovery complete: {recovered} recovered, {killed} killed, {errors} errors"
        )
        
        return (recovered, killed, errors)
    
    def get_running_repair_cycle_containers(self) -> List[Dict[str, str]]:
        """
        Get list of running repair cycle containers from Docker
        
        Returns:
            List of container info dicts with name, status, created_at
        """
        try:
            # List all running containers with name starting with repair-cycle-
            result = subprocess.run(
                ['docker', 'ps', '--filter', 'name=repair-cycle-', '--format', '{{json .}}'],
                capture_output=True,
                text=True,
                timeout=10
            )
            
            if result.returncode != 0:
                logger.error(f"Failed to list repair cycle containers: {result.stderr}")
                return []
            
            containers = []
            for line in result.stdout.strip().split('\n'):
                if line:
                    try:
                        container_data = json.loads(line)
                        containers.append({
                            'name': container_data.get('Names', ''),
                            'id': container_data.get('ID', ''),
                            'status': container_data.get('Status', ''),
                            'created_at': container_data.get('CreatedAt', ''),
                            'image': container_data.get('Image', '')
                        })
                    except json.JSONDecodeError as e:
                        logger.warning(f"Failed to parse container JSON: {e}")
            
            return containers
            
        except Exception as e:
            logger.error(f"Error getting running repair cycle containers: {e}")
            return []
    
    def parse_repair_cycle_container_name(self, container_name: str) -> Optional[Dict[str, str]]:
        """
        Parse repair cycle container name to extract metadata
        
        Container name format: repair-cycle-{project}-{issue_number}-{run_id[:8]}
        After sanitization: repair-cycle-{project}-{issue}-{run_id}
        
        Args:
            container_name: Container name
            
        Returns:
            Dict with project, issue_number, run_id, or None if invalid format
        """
        if not container_name.startswith('repair-cycle-'):
            return None
        
        # Remove prefix: repair-cycle-
        remainder = container_name.replace('repair-cycle-', '', 1)
        
        # Split on hyphens
        parts = remainder.split('-')
        
        # Need at least 3 parts: project, issue, run_id
        if len(parts) < 3:
            logger.warning(f"Invalid repair cycle container name format: {container_name}")
            return None
        
        # Last part is run_id
        run_id = parts[-1]
        # Second to last is issue_number
        issue_number = parts[-2]
        # Everything else is project name (may contain hyphens)
        project = '-'.join(parts[:-2])
        
        # Validate issue_number is numeric
        if not issue_number.isdigit():
            logger.warning(f"Invalid issue number in container name: {container_name}")
            return None
        
        return {
            'project': project,
            'issue_number': issue_number,
            'run_id': run_id,
            'container_name': container_name
        }
    
    def check_repair_cycle_checkpoint(self, project: str, issue_number: int = None) -> Optional[Dict[str, any]]:
        """
        Check repair cycle checkpoint file to determine progress
        
        Args:
            project: Project name
            issue_number: Issue number (if known, enables new path lookup)
            
        Returns:
            Checkpoint data if exists, None otherwise
        """
        try:
            # Try new location first (in orchestrator_data)
            if issue_number is not None:
                orchestrator_data_dir = Path("/workspace/clauditoreum/orchestrator_data/repair_cycles")
                checkpoint_file = orchestrator_data_dir / project / str(issue_number) / "checkpoint.json"
                
                if checkpoint_file.exists():
                    with open(checkpoint_file, 'r') as f:
                        checkpoint = json.load(f)
                    
                    # Parse checkpoint time
                    checkpoint_time_str = checkpoint.get('checkpoint_time')
                    if checkpoint_time_str:
                        try:
                            # Format: 2025-10-17T12:34:56.789012Z
                            checkpoint_time = datetime.strptime(
                                checkpoint_time_str.rstrip('Z'),
                                '%Y-%m-%dT%H:%M:%S.%f'
                            )
                            checkpoint['checkpoint_age_seconds'] = (
                                datetime.utcnow() - checkpoint_time
                            ).total_seconds()
                        except Exception as e:
                            logger.warning(f"Could not parse checkpoint time: {e}")
                    
                    return checkpoint
            
            # Fallback: try old location in project directory (for backward compatibility)
            from services.project_workspace import workspace_manager
            project_dir = workspace_manager.get_project_dir(project)
            checkpoint_file = Path(project_dir) / ".repair_cycle_checkpoint.json"
            
            if not checkpoint_file.exists():
                return None
            
            with open(checkpoint_file, 'r') as f:
                checkpoint = json.load(f)
            
            # Parse checkpoint time
            checkpoint_time_str = checkpoint.get('checkpoint_time')
            if checkpoint_time_str:
                try:
                    # Format: 2025-10-17T12:34:56.789012Z
                    checkpoint_time = datetime.strptime(
                        checkpoint_time_str.rstrip('Z'),
                        '%Y-%m-%dT%H:%M:%S.%f'
                    )
                    checkpoint['checkpoint_age_seconds'] = (
                        datetime.utcnow() - checkpoint_time
                    ).total_seconds()
                except Exception as e:
                    logger.warning(f"Could not parse checkpoint time: {e}")
            
            return checkpoint
            
        except Exception as e:
            logger.error(f"Error checking repair cycle checkpoint: {e}")
            return None
    
    def check_repair_cycle_result(self, project: str, issue_number: int = None) -> Optional[Dict[str, any]]:
        """
        Check repair cycle result file to see if container completed
        
        Args:
            project: Project name
            issue_number: Issue number (if known, enables new path lookup)
            
        Returns:
            Result data if exists, None otherwise
        """
        try:
            # Try new location first (in orchestrator_data)
            if issue_number is not None:
                orchestrator_data_dir = Path("/workspace/clauditoreum/orchestrator_data/repair_cycles")
                result_file = orchestrator_data_dir / project / str(issue_number) / "result.json"
                
                if result_file.exists():
                    with open(result_file, 'r') as f:
                        result = json.load(f)
                    return result
            
            # Fallback: try old location in project directory (for backward compatibility)
            from services.project_workspace import workspace_manager
            project_dir = workspace_manager.get_project_dir(project)
            result_file = Path(project_dir) / ".repair_cycle_result.json"
            
            if not result_file.exists():
                return None
            
            with open(result_file, 'r') as f:
                result = json.load(f)
            
            return result
            
        except Exception as e:
            logger.error(f"Error checking repair cycle result: {e}")
            return None
    
    def reconnect_repair_cycle_container(
        self,
        container_name: str,
        project: str,
        issue_number: int,
        run_id: str,
        checkpoint: Optional[Dict[str, any]] = None
    ):
        """
        Reconnect to a running repair cycle container
        
        Re-registers in Redis and restarts monitoring thread
        
        Args:
            container_name: Container name
            project: Project name
            issue_number: Issue number
            run_id: Pipeline run ID
            checkpoint: Checkpoint data (optional)
        """
        try:
            # Re-register in Redis
            redis_key = f"repair_cycle:container:{project}:{issue_number}"
            self.redis.setex(redis_key, 7200, container_name)
            
            logger.info(
                f"Re-registered repair cycle container in Redis: {redis_key} -> {container_name}"
            )
            
            # Restart monitoring thread
            # Import here to avoid circular dependency
            from services.project_monitor import ProjectMonitor
            from config.manager import ConfigManager
            from task_queue.task_manager import TaskQueue
            
            # Get project config
            config_manager = ConfigManager()
            project_config = config_manager.get_project_config(project)
            
            if not project_config:
                logger.warning(f"Could not find project config for {project}")
                return
            
            # Find workflow template
            # Try dev pipeline first
            pipeline_config = project_config.pipeline.get('dev')
            if not pipeline_config:
                logger.warning(f"No dev pipeline found for {project}")
                return
            
            workflow_name = pipeline_config.get('workflow')
            workflow_template = config_manager.get_workflow_template(workflow_name)
            
            if not workflow_template:
                logger.warning(f"Could not find workflow template {workflow_name}")
                return
            
            # Get board name from state
            from config.state_manager import state_manager
            github_state = state_manager.get_github_state(project)
            board_name = github_state.get('board_name', project)
            
            # Determine status from checkpoint or assume "Testing"
            status = checkpoint.get('stage_name', 'Testing') if checkpoint else 'Testing'
            
            # Get repository
            repository = project_config.github.get('repo', project)
            
            # Create monitor instance (without starting main loop)
            task_queue = TaskQueue()  # Dummy queue
            monitor = ProjectMonitor(task_queue, config_manager)
            
            # Restart monitoring thread
            monitor._monitor_repair_cycle_container(
                container_name=container_name,
                project_name=project,
                board_name=board_name,
                issue_number=issue_number,
                status=status,
                repository=repository,
                project_config=project_config,
                workflow_template=workflow_template
            )
            
            logger.info(f"✓ Reconnected to repair cycle container: {container_name}")
            
        except Exception as e:
            logger.error(f"Failed to reconnect to repair cycle container: {e}", exc_info=True)
    
    def recover_or_cleanup_repair_cycle_containers(self) -> Tuple[int, int, int]:
        """
        Recover or cleanup repair cycle containers
        
        Returns:
            Tuple of (recovered_count, killed_count, error_count)
        """
        logger.info("Starting repair cycle container recovery process")
        
        running_containers = self.get_running_repair_cycle_containers()
        
        if not running_containers:
            logger.info("No running repair cycle containers found")
            return (0, 0, 0)
        
        logger.info(f"Found {len(running_containers)} running repair cycle containers")
        
        recovered = 0
        killed = 0
        errors = 0
        
        for container in running_containers:
            container_name = container['name']
            container_id = container['id']
            
            try:
                # Parse container name
                metadata = self.parse_repair_cycle_container_name(container_name)
                if not metadata:
                    logger.warning(f"Could not parse repair cycle container name: {container_name}, killing it")
                    self.kill_container(container_name, container_id)
                    killed += 1
                    continue
                
                project = metadata['project']
                issue_number = int(metadata['issue_number'])
                run_id = metadata['run_id']
                
                # Check for result file (container may have finished)
                result = self.check_repair_cycle_result(project, issue_number)
                if result:
                    logger.info(
                        f"Container {container_name} has completed (result file exists), "
                        f"success={result.get('overall_success')}"
                    )
                    # Container finished but orchestrator restarted before processing result
                    # Process the result now to complete the workflow
                    try:
                        self._process_completed_repair_cycle(
                            container_name, container_id, project, issue_number, result
                        )
                        logger.info(f"Successfully processed completed repair cycle for {project}/#{issue_number}")
                        recovered += 1
                    except Exception as e:
                        logger.error(f"Failed to process completed repair cycle: {e}", exc_info=True)
                        errors += 1
                    continue
                
                # Check checkpoint to see if making progress
                checkpoint = self.check_repair_cycle_checkpoint(project, issue_number)
                
                if not checkpoint:
                    logger.warning(
                        f"Container {container_name} has no checkpoint yet, may be starting up"
                    )
                    # Give it benefit of doubt if container is young
                    created_str = container['created_at']
                    try:
                        created_dt = datetime.strptime(created_str[:19], '%Y-%m-%d %H:%M:%S')
                        age = datetime.utcnow() - created_dt

                        if age < timedelta(minutes=60):
                            logger.info(f"Container is young ({age.total_seconds()/60:.1f}min), keeping it")
                            # Don't reconnect yet, just leave it running
                            continue
                        else:
                            logger.warning(f"Container is old ({age.total_seconds()/60:.1f}min) with no checkpoint, killing it")
                            self.kill_repair_cycle_container_with_event(
                                container_name, container_id, project, issue_number,
                                f"No checkpoint after {age.total_seconds()/60:.1f} minutes"
                            )
                            killed += 1
                            continue
                    except Exception as e:
                        logger.warning(f"Could not parse container age: {e}, killing it")
                        self.kill_repair_cycle_container_with_event(
                            container_name, container_id, project, issue_number,
                            f"Could not parse container age: {e}"
                        )
                        killed += 1
                        continue
                
                # Check checkpoint age - if checkpoint is stale (>60 min old), container might be stuck
                checkpoint_age = checkpoint.get('checkpoint_age_seconds', 0)
                if checkpoint_age > 3600:  # 60 minutes
                    logger.warning(
                        f"Container {container_name} checkpoint is stale "
                        f"({checkpoint_age/60:.1f}min old), container may be stuck"
                    )
                    # Check container age too
                    created_str = container['created_at']
                    try:
                        created_dt = datetime.strptime(created_str[:19], '%Y-%m-%d %H:%M:%S')
                        age = datetime.utcnow() - created_dt
                        
                        if age > timedelta(hours=2):
                            logger.warning(
                                f"Container is over 2 hours old ({age.total_seconds()/3600:.1f}h), killing it"
                            )
                            self.kill_repair_cycle_container_with_event(
                                container_name, container_id, project, issue_number,
                                f"Stale checkpoint ({checkpoint_age/60:.1f}min old) and container over 2 hours old"
                            )
                            killed += 1
                            continue
                    except Exception:
                        pass
                
                # Checkpoint exists and is recent - container is making progress
                iteration = checkpoint.get('iteration', 0)
                test_type = checkpoint.get('test_type', 'unknown')
                agent_calls = checkpoint.get('agent_call_count', 0)
                
                logger.info(
                    f"Container {container_name} is making progress: "
                    f"{test_type} iteration {iteration}, {agent_calls} agent calls, "
                    f"checkpoint {checkpoint_age:.0f}s old"
                )
                
                # Reconnect to container
                self.reconnect_repair_cycle_container(
                    container_name=container_name,
                    project=project,
                    issue_number=issue_number,
                    run_id=run_id,
                    checkpoint=checkpoint
                )
                
                # Emit recovery event
                try:
                    from monitoring.observability import get_observability_manager
                    obs_manager = get_observability_manager()
                    obs_manager.emit_repair_cycle_container_recovered(
                        project=project,
                        issue_number=issue_number,
                        container_name=container_name,
                        checkpoint=checkpoint,
                        pipeline_run_id=run_id
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit container recovered event: {e}")
                
                recovered += 1
                
            except Exception as e:
                logger.error(f"Error processing repair cycle container {container_name}: {e}", exc_info=True)
                errors += 1
        
        logger.info(
            f"Repair cycle container recovery complete: {recovered} recovered, {killed} killed, {errors} errors"
        )
        
        # Write recovery metrics to Elasticsearch
        try:
            from monitoring.timestamp_utils import utc_now
            from elasticsearch import Elasticsearch
            
            es = Elasticsearch(['http://elasticsearch:9200'])
            timestamp = utc_now()
            index_name = f"repair-cycle-recovery-{timestamp.strftime('%Y.%m.%d')}"
            
            doc = {
                'timestamp': timestamp.isoformat(),
                'containers_recovered': recovered,
                'containers_killed': killed,
                'containers_with_errors': errors,
                'total_containers_found': len(running_containers),
                'recovery_success_rate': recovered / len(running_containers) if running_containers else 0.0,
                'stale_rate': killed / len(running_containers) if running_containers else 0.0
            }
            
            es.index(index=index_name, document=doc)
            logger.debug(f"Indexed recovery metrics to {index_name}")
        except Exception as e:
            logger.warning(f"Failed to index recovery metrics to Elasticsearch: {e}")
        
        return (recovered, killed, errors)

    def _process_completed_repair_cycle(self, container_name: str, container_id: str,
                                       project: str, issue_number: int, result: Dict) -> None:
        """
        Process a completed repair cycle that finished while orchestrator was restarting.

        This handles:
        1. Loading context
        2. Posting summary to GitHub
        3. Auto-committing changes if successful
        4. Auto-advancing ticket if successful
        5. Cleaning up container

        Args:
            container_name: Container name
            container_id: Container ID
            project: Project name
            issue_number: Issue number
            result: Result dict from result.json
        """
        import asyncio
        from pathlib import Path

        logger.info(f"Processing completed repair cycle for {project}/#{issue_number}")

        # Load context file
        context_file = Path(f"/workspace/clauditoreum/orchestrator_data/repair_cycles/{project}/{issue_number}/context.json")
        if not context_file.exists():
            logger.error(f"Context file not found: {context_file}")
            raise FileNotFoundError(f"Context file not found: {context_file}")

        with open(context_file, 'r') as f:
            context = json.load(f)

        board_name = context['board']
        repository = context['repository']
        column = context['column']
        pipeline_run_id = context.get('pipeline_run_id')

        # Get success status
        overall_success = result.get('overall_success', False)

        # Load project config to get org
        from config.manager import ConfigManager
        config_manager = ConfigManager()
        project_config = config_manager.get_project_config(project)

        # Post summary comment to GitHub
        from services.github_integration import GitHubIntegration
        github = GitHubIntegration(
            repo_owner=project_config.github['org'],
            repo_name=repository
        )

        # Build summary
        test_results = result.get('test_results', [])
        summary_lines = [
            f"## ✅ Repair Cycle Complete" if overall_success else "## ❌ Repair Cycle Failed",
            "",
            f"**Container**: `{container_name}`",
            f"**Total Agent Calls**: {result.get('total_agent_calls', 0)}",
            f"**Duration**: {result.get('duration_seconds', 0):.1f}s",
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

        summary_lines.append("")
        summary_lines.append("---")
        summary_lines.append("_Repair cycle recovered after orchestrator restart_")

        # Post comment
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        comment_context = {
            'issue_number': issue_number,
            'repository': repository,
            'workspace_type': context.get('workspace_type', 'issues'),
            'discussion_id': context.get('discussion_id')
        }

        loop.run_until_complete(
            github.post_agent_output(comment_context, "\n".join(summary_lines))
        )

        # Auto-commit if successful
        if overall_success:
            try:
                logger.info(f"Auto-committing repair cycle changes for issue #{issue_number}")
                from services.auto_commit import auto_commit_service

                commit_success = loop.run_until_complete(
                    auto_commit_service.commit_agent_changes(
                        project=project,
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

        # Auto-advance if successful
        if overall_success:
            try:
                # Get workflow columns
                workflow_name = None

                # Find the pipeline for this board
                for pipeline in project_config.pipelines:
                    if pipeline.board_name == board_name:
                        workflow_name = pipeline.workflow
                        break

                if workflow_name:
                    workflow_template = config_manager.get_workflow_template(workflow_name)
                    current_index = next(
                        (i for i, col in enumerate(workflow_template.columns) if col.name == column),
                        None
                    )

                    if current_index is not None and current_index + 1 < len(workflow_template.columns):
                        next_column = workflow_template.columns[current_index + 1]

                        logger.info(f"Auto-advancing issue #{issue_number} from {column} to {next_column.name}")

                        from services.pipeline_progression import PipelineProgression
                        from task_queue.task_manager import TaskManager

                        task_queue = TaskManager()
                        progression_service = PipelineProgression(task_queue)
                        progression_service.move_issue_to_column(
                            project_name=project,
                            board_name=board_name,
                            issue_number=issue_number,
                            target_column=next_column.name,
                            trigger='repair_cycle_completion'
                        )
                        logger.info(f"Successfully moved issue #{issue_number} to {next_column.name}")
            except Exception as e:
                logger.error(f"Failed to auto-advance issue: {e}", exc_info=True)

        # Clean up container
        try:
            subprocess.run(['docker', 'rm', '-f', container_id], capture_output=True, timeout=30)
            logger.info(f"Removed container {container_name}")
        except Exception as e:
            logger.warning(f"Failed to remove container: {e}")

        # Clear Redis tracking
        try:
            if self.redis:
                redis_key = f"repair_cycle:container:{project}:{issue_number}"
                self.redis.delete(redis_key)
                logger.debug(f"Cleared Redis tracking key: {redis_key}")
        except Exception as e:
            logger.warning(f"Failed to clear Redis tracking: {e}")

        # Emit completion event
        try:
            from monitoring.observability import get_observability_manager
            obs_manager = get_observability_manager()
            obs_manager.emit_repair_cycle_container_completed(
                project=project,
                issue_number=issue_number,
                container_name=container_name,
                success=overall_success,
                total_agent_calls=result.get('total_agent_calls', 0),
                duration_seconds=result.get('duration_seconds', 0.0),
                pipeline_run_id=pipeline_run_id
            )
        except Exception as e:
            logger.warning(f"Failed to emit container completed event: {e}")

        loop.close()
        logger.info(f"Completed processing of repair cycle for {project}/#{issue_number}")

    def kill_repair_cycle_container_with_event(self, container_name: str, container_id: str,
                                               project: str, issue_number: int, reason: str):
        """Kill a repair cycle container and emit event"""
        self.kill_container(container_name, container_id)
        
        # Emit killed event
        try:
            from monitoring.observability import get_observability_manager
            obs_manager = get_observability_manager()
            obs_manager.emit_repair_cycle_container_killed(
                project=project,
                issue_number=issue_number,
                container_name=container_name,
                reason=reason
            )
        except Exception as e:
            logger.warning(f"Failed to emit container killed event: {e}")


def get_agent_container_recovery() -> AgentContainerRecovery:
    """Get singleton instance of agent container recovery"""
    if not hasattr(get_agent_container_recovery, '_instance'):
        get_agent_container_recovery._instance = AgentContainerRecovery()
    return get_agent_container_recovery._instance

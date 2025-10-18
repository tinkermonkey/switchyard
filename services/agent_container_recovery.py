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
        
        Container name format: claude-agent-{project}-{task_id}
        After sanitization: claude-agent-{project}-{agent}_{project}_{board}_{issue_number}_{timestamp}
        
        Note: The hyphen before task_id is important - we need to find the LAST hyphen
        before the task_id starts (task_id contains underscores, not hyphens)
        
        Args:
            container_name: Container name
            
        Returns:
            Dict with project, task_id components, or None if invalid format
        """
        if not container_name.startswith('claude-agent-'):
            return None
        
        # Remove prefix: claude-agent-
        remainder = container_name.replace('claude-agent-', '', 1)
        
        # Find the last hyphen - everything after it should be the task_id
        # Task ID format: {agent}_{project}_{board}_{issue_number}_{timestamp}
        # Board names might have hyphens (e.g., "SDLC-Execution")
        # But task_id always has underscores between components
        
        # Split on hyphens
        parts = remainder.split('-')
        
        # Find where task_id starts by checking for the pattern agent_project_board_issue_timestamp
        # We know:
        # - Last component should be timestamp (numeric)
        # - Second to last should be issue_number (numeric)
        # - Task ID has exactly 5 underscore-separated parts
        
        task_id = None
        project_with_hyphen = None
        
        for i in range(len(parts) - 1, -1, -1):
            # Check if this part matches task_id pattern
            test_task_id = parts[i]
            underscore_parts = test_task_id.split('_')
            
            # Need exactly 5 parts for valid task_id
            if len(underscore_parts) == 5:
                # Last part should be numeric (timestamp)
                if underscore_parts[-1].isdigit() and underscore_parts[-2].isdigit():
                    task_id = test_task_id
                    # Project is everything before this part
                    project_with_hyphen = '-'.join(parts[:i])
                    break
        
        if not task_id:
            logger.warning(f"Could not find valid task_id in container name: {container_name}")
            return None
        
        # Parse task_id components
        task_id_parts = task_id.split('_')
        
        return {
            'agent': task_id_parts[0],
            'project': task_id_parts[1],
            'board': task_id_parts[2],
            'issue_number': task_id_parts[3],
            'timestamp': task_id_parts[4],
            'task_id': task_id,
            'container_name': container_name,
            'project_with_hyphen': project_with_hyphen  # For debugging
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
            
            # Load execution history
            history_file = work_execution_tracker._get_history_file(project, issue_number)
            if not history_file.exists():
                return None
            
            import yaml
            with open(history_file, 'r') as f:
                history_data = yaml.safe_load(f)
            
            # Find most recent in_progress execution
            executions = history_data.get('execution_history', [])
            for execution in reversed(executions):
                if execution.get('outcome') == 'in_progress':
                    return execution
            
            return None
            
        except Exception as e:
            logger.error(f"Error checking execution history: {e}")
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
                # Parse container name
                metadata = self.parse_container_name(container_name)
                if not metadata:
                    logger.warning(f"Could not parse container name: {container_name}, killing it")
                    self.kill_container(container_name, container_id)
                    killed += 1
                    continue
                
                project = metadata['project']
                issue_number = int(metadata['issue_number'])
                agent = metadata['agent']
                
                # Check if there's an in_progress execution for this issue
                execution = self.check_execution_history(project, issue_number)
                
                if not execution:
                    logger.warning(
                        f"Container {container_name} has no in_progress execution history, killing it"
                    )
                    self.kill_container(container_name, container_id)
                    killed += 1
                    continue
                
                # Check if execution matches the container
                if execution.get('agent') != agent:
                    logger.warning(
                        f"Container {container_name} agent mismatch (container: {agent}, history: {execution.get('agent')}), killing it"
                    )
                    self.kill_container(container_name, container_id)
                    self.cleanup_execution_state(project, issue_number, agent, "agent_mismatch")
                    killed += 1
                    continue
                
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
                    'task_id': metadata['task_id'],
                    'started_at': execution.get('timestamp', datetime.utcnow().isoformat()),
                    'issue_number': str(issue_number),
                    'recovered': 'true'
                }
                
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
                    # Container finished but wasn't cleaned up
                    # Kill it and let normal completion flow handle it
                    self.kill_repair_cycle_container_with_event(
                        container_name, container_id, project, issue_number,
                        "Container already finished (result file exists)"
                    )
                    killed += 1
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
                        
                        if age < timedelta(minutes=10):
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
                
                # Check checkpoint age - if checkpoint is stale (>10 min old), container might be stuck
                checkpoint_age = checkpoint.get('checkpoint_age_seconds', 0)
                if checkpoint_age > 600:  # 10 minutes
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

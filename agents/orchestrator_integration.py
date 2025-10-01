"""
Agent Orchestrator Integration Layer

This module provides the integration between the main orchestrator and the agent system,
replacing the legacy agent_stages.py with a proper factory-based approach.
"""

from typing import Dict, Any
from datetime import datetime
from pipeline.base import PipelineStage
from pipeline.orchestrator import SequentialPipeline
from state_management.manager import StateManager
from agents import AGENT_REGISTRY, get_agent_class


async def validate_task_can_run(task, logger) -> Dict[str, Any]:
    """
    Validate if a task can run based on agent requirements

    Args:
        task: Task object
        logger: Logger instance

    Returns:
        Dict with 'can_run' (bool), 'reason' (str), 'needs_dev_setup' (bool)
    """
    from config.manager import config_manager
    from services.dev_container_state import dev_container_state, DevContainerStatus

    # Get agent configuration
    agent_config = config_manager.get_project_agent_config(task.project, task.agent)
    requires_dev_container = getattr(agent_config, 'requires_dev_container', False)

    if not requires_dev_container:
        # Agent doesn't need dev container, can always run
        return {'can_run': True, 'reason': 'No dev container required'}

    # Check dev container status
    status = dev_container_state.get_status(task.project)

    if status == DevContainerStatus.VERIFIED:
        return {'can_run': True, 'reason': 'Dev container verified'}
    elif status == DevContainerStatus.IN_PROGRESS:
        return {
            'can_run': False,
            'reason': 'Dev container setup in progress',
            'needs_dev_setup': False
        }
    elif status == DevContainerStatus.BLOCKED:
        return {
            'can_run': False,
            'reason': 'Dev container setup blocked - manual intervention required',
            'needs_dev_setup': False
        }
    else:  # UNVERIFIED
        return {
            'can_run': False,
            'reason': 'Dev container not verified',
            'needs_dev_setup': True
        }


async def queue_dev_environment_setup(project: str, logger):
    """
    Queue a dev_environment_setup task for a project

    Args:
        project: Project name
        logger: Logger instance
    """
    from task_queue.task_manager import Task, TaskPriority, TaskQueue
    from datetime import datetime

    logger.info(f"Auto-queuing dev_environment_setup task for {project}")

    task_queue = TaskQueue(use_redis=True)

    task = Task(
        id=f"auto_dev_env_setup_{project}_{int(datetime.now().timestamp())}",
        agent="dev_environment_setup",
        project=project,
        priority=TaskPriority.HIGH,
        context={
            'issue': {
                'title': f'Development environment setup for {project}',
                'body': 'Auto-triggered: Agent requires dev container but it is not verified',
                'number': 0
            },
            'issue_number': 0,
            'board': 'system',
            'repository': project,
            'automated_setup': True,
            'auto_triggered': True,
            'use_docker': False  # Run locally in orchestrator environment
        },
        created_at=datetime.now().isoformat()
    )

    task_queue.enqueue(task)
    logger.info(f"Auto-queued dev_environment_setup task: {task.id}")


class AgentStage(PipelineStage):
    """Generic pipeline stage that wraps any agent"""

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None):
        super().__init__(agent_name, agent_config=agent_config)
        self.agent_class = get_agent_class(agent_name)
        if not self.agent_class:
            raise ValueError(f"Unknown agent: {agent_name}")

        self.agent_instance = self.agent_class(agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the wrapped agent"""
        return await self.agent_instance.execute(context)


def create_agent_pipeline(agent_names: list, state_manager: StateManager) -> SequentialPipeline:
    """Create a pipeline from a list of agent names"""
    stages = []

    for agent_name in agent_names:
        stage = AgentStage(agent_name)
        stages.append(stage)

    return SequentialPipeline(stages, state_manager)


def create_single_agent_pipeline(agent_name: str, state_manager: StateManager) -> SequentialPipeline:
    """Create a pipeline with a single agent"""
    return create_agent_pipeline([agent_name], state_manager)


async def process_task_integrated(task, state_manager, logger):
    """
    Process task using the new agent architecture

    This function maintains compatibility with the legacy interface while using
    the new agent system.
    """
    from datetime import datetime
    from config.manager import config_manager
    from pipeline.factory import PipelineFactory
    from monitoring.observability import get_observability_manager
    from services.project_workspace import workspace_manager
    import time
    import json

    obs = get_observability_manager()

    # Emit task received event
    obs.emit_task_received(task.agent, task.id, task.project, task.context)

    # Manage branch for dev and full-sdlc pipelines
    task_context = task.context
    board_name = task_context.get('board', '')
    issue_number = task_context.get('issue_number')

    # For development pipelines, create/switch to feature branch
    if issue_number and board_name in ['development', 'full-sdlc']:
        logger.info(f"Creating/switching to feature branch for issue #{issue_number}")
        branch_name = workspace_manager.create_feature_branch(task.project, issue_number)
        if branch_name:
            logger.info(f"Working on branch: {branch_name}")
        else:
            logger.log_warning(f"Failed to create feature branch for issue #{issue_number}")

    # Create stream callback that publishes to Redis
    # The observability server will pick it up and forward to websockets
    def stream_callback(event):
        """Publish Claude stream events to Redis for websocket forwarding and history"""
        try:
            if obs and obs.enabled:
                import time
                event_data = {
                    'agent': task.agent,
                    'task_id': task.id,
                    'project': task.project,
                    'timestamp': event.get('timestamp') or time.time(),
                    'event': event
                }
                event_json = json.dumps(event_data)

                # Publish to pub/sub for real-time delivery
                obs.redis.publish('orchestrator:claude_stream', event_json)

                # Also add to Redis Stream for history (with automatic trimming)
                claude_stream_key = "orchestrator:claude_logs_stream"
                obs.redis.xadd(
                    claude_stream_key,
                    {'log': event_json},
                    maxlen=500,  # Keep last 500 log entries
                    approximate=True
                )

                # Set 2-hour TTL on the stream
                obs.redis.expire(claude_stream_key, 7200)
        except Exception as e:
            logger.log_error(f"Error publishing stream event: {e}")

    # Convert Task object to pipeline context
    pipeline_context = {
        'pipeline_id': f"pipeline_{task.id}_{datetime.now().timestamp()}",
        'task_id': task.id,
        'agent': task.agent,
        'project': task.project,
        'context': task.context,
        'work_dir': f"./projects/{task.project}",
        'completed_work': [],
        'decisions': [],
        'metrics': {},
        'validation': {},
        'state_manager': state_manager,
        'observability': obs,  # Pass observability manager to agents
        'stream_callback': stream_callback,  # Pass stream callback for live logs
        'use_docker': task.context.get('use_docker', True)  # Respect task context, default to True for project isolation
    }

    # Validate task can run (check dev container requirements)
    validation_result = await validate_task_can_run(task, logger)
    if not validation_result['can_run']:
        logger.log_warning(f"Task {task.id} blocked: {validation_result['reason']}")
        # Queue dev_environment_setup task if needed
        if validation_result.get('needs_dev_setup'):
            await queue_dev_environment_setup(task.project, logger)
        raise Exception(f"Task blocked: {validation_result['reason']}")

    # Create pipeline using the factory (which loads agent config including MCP servers)
    factory = PipelineFactory(config_manager)
    agent_stage = factory.create_agent(task.agent, task.project)

    # Emit agent initialized event
    agent_config = agent_stage.agent_config or {}
    obs.emit_agent_initialized(task.agent, task.id, task.project, agent_config)

    # Add claude_model from agent config to pipeline context for claude_integration
    if 'claude_model' in agent_config:
        pipeline_context['claude_model'] = agent_config['claude_model']
        logger.info(f"Using configured model for {task.agent}: {agent_config['claude_model']}")

    pipeline = SequentialPipeline([agent_stage], state_manager)

    start_time = time.time()

    try:
        logger.info(f"Starting pipeline execution for task {task.id} with agent {task.agent}")
        result = await pipeline.execute(pipeline_context)

        duration_ms = (time.time() - start_time) * 1000
        obs.emit_agent_completed(task.agent, task.id, task.project, duration_ms, True)

        logger.info(f"Pipeline completed for task {task.id}")

        # Auto-commit changes if agent makes code changes
        makes_code_changes = getattr(agent_config, 'makes_code_changes', False)
        if makes_code_changes:
            logger.info(f"Agent {task.agent} makes code changes, attempting auto-commit")
            from services.auto_commit import auto_commit_service

            issue_number = task_context.get('issue_number')
            commit_success = await auto_commit_service.commit_agent_changes(
                project=task.project,
                agent=task.agent,
                task_id=task.id,
                issue_number=issue_number
            )

            if commit_success:
                logger.info(f"Successfully auto-committed changes for task {task.id}")
            else:
                logger.log_warning(f"Failed to auto-commit changes for task {task.id}")

        return result

    except Exception as e:
        duration_ms = (time.time() - start_time) * 1000
        obs.emit_agent_completed(task.agent, task.id, task.project, duration_ms, False, str(e))

        logger.log_error(f"Pipeline execution failed for task {task.id}: {e}")
        raise


# Legacy function aliases for backward compatibility
async def business_analyst_agent(context: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy compatibility function"""
    agent_class = get_agent_class("business_analyst")
    agent = agent_class()
    return await agent.execute(context)


async def code_reviewer_agent(context: Dict[str, Any]) -> Dict[str, Any]:
    """Legacy compatibility function"""
    agent_class = get_agent_class("code_reviewer")
    agent = agent_class()
    return await agent.execute(context)


# Export the main integration function
__all__ = [
    'process_task_integrated',
    'business_analyst_agent',
    'code_reviewer_agent',
    'create_agent_pipeline',
    'create_single_agent_pipeline',
    'AgentStage'
]
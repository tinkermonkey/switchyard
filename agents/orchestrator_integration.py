"""
Agent Orchestrator Integration Layer

This module provides the integration between the main orchestrator and the agent system,
replacing the legacy agent_stages.py with a proper factory-based approach.
"""

import uuid
from typing import Dict, Any
from datetime import datetime
from pipeline.base import PipelineStage
from pipeline.orchestrator import SequentialPipeline
from state_management.manager import StateManager
from agents import AGENT_REGISTRY, get_agent_class
from services.circuit_breaker import CircuitBreaker


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
        return {'can_run': True, 'reason': 'Dev container verified and ready'}
    elif status == DevContainerStatus.IN_PROGRESS:
        return {
            'can_run': False,
            'reason': f"Dev container setup currently in progress for '{task.project}'",
            'needs_dev_setup': False
        }
    elif status == DevContainerStatus.BLOCKED:
        return {
            'can_run': False,
            'reason': f"Dev container setup is blocked for '{task.project}'. Check state/dev_containers/{task.project}.yaml for error details",
            'needs_dev_setup': False
        }
    else:  # UNVERIFIED
        return {
            'can_run': False,
            'reason': f"Dev container not yet verified for project '{task.project}'",
            'needs_dev_setup': True
        }


async def queue_dev_environment_setup(project: str, logger):
    """
    Queue a dev_environment_setup task for a project.

    Idempotent: skips queuing if setup is already IN_PROGRESS.
    Sets status to IN_PROGRESS before enqueuing to prevent races.

    Args:
        project: Project name
        logger: Logger instance
    """
    from task_queue.task_manager import Task, TaskPriority, TaskQueue
    from services.dev_container_state import dev_container_state, DevContainerStatus

    # Check if setup is already in progress - avoid duplicate queuing
    current_status = dev_container_state.get_status(project)
    if current_status == DevContainerStatus.IN_PROGRESS:
        logger.info(f"Dev environment setup already in progress for {project}, skipping duplicate queue")
        return

    # Mark as in-progress BEFORE queuing to prevent races
    dev_container_state.set_status(
        project,
        DevContainerStatus.IN_PROGRESS,
        image_name=f"{project}-agent:latest"
    )
    logger.info(f"Set dev container status to IN_PROGRESS for {project}")

    try:
        logger.info(f"Auto-queuing dev_environment_setup task for {project}")

        task_queue = TaskQueue(use_redis=True)

        task = Task(
            id=str(uuid.uuid4()),
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
                'project': project,
                'repository': project,
                'automated_setup': True,
                'auto_triggered': True,
                'skip_workspace_prep': True,  # System task — no feature branch needed
                'use_docker': False  # Run locally in orchestrator environment
            },
            created_at=datetime.now().isoformat()
        )

        task_queue.enqueue(task)
        logger.info(f"Auto-queued dev_environment_setup task: {task.id}")
    except Exception as e:
        # Roll back status to prevent permanent stuck IN_PROGRESS state
        logger.error(
            f"Failed to enqueue dev_environment_setup for {project}: {e}. "
            f"Rolling back status from IN_PROGRESS to UNVERIFIED to allow retry."
        )
        dev_container_state.set_status(
            project,
            DevContainerStatus.UNVERIFIED,
            error_message=f"Enqueue failed: {e}"
        )
        raise


class AgentStage(PipelineStage):
    """Generic pipeline stage that wraps any agent"""

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None):
        # Check for custom circuit breaker config
        circuit_breaker = None
        if agent_config and 'agent_config' in agent_config:
            # agent_config['agent_config'] is the AgentConfig object from ConfigManager
            real_config = agent_config['agent_config']
            if hasattr(real_config, 'circuit_breaker_config') and real_config.circuit_breaker_config:
                cb_config = real_config.circuit_breaker_config
                circuit_breaker = CircuitBreaker(
                    name=agent_name,
                    failure_threshold=cb_config.get('failure_threshold', 3),
                    recovery_timeout=cb_config.get('recovery_timeout', 30),
                    success_threshold=cb_config.get('success_threshold', 2)
                )

        super().__init__(agent_name, circuit_breaker=circuit_breaker, agent_config=agent_config)
        self.agent_class = get_agent_class(agent_name)
        if not self.agent_class:
            raise ValueError(f"Unknown agent: {agent_name}")

        self.agent_instance = self.agent_class(agent_config)

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute the wrapped agent"""
        return await self.agent_instance.execute(context)


def create_stage_from_config(stage_config, project_name: str) -> PipelineStage:
    """
    Create a pipeline stage from configuration.

    Determines whether to create a standard AgentStage, RepairCycleStage, or PRReviewStage
    based on the stage_type field.

    Args:
        stage_config: PipelineStage configuration from config manager
        project_name: Project name for loading test configurations

    Returns:
        Instantiated PipelineStage (AgentStage, RepairCycleStage, or PRReviewStage)
    """
    from config.manager import config_manager
    from pipeline.repair_cycle import RepairCycleStage, RepairTestRunConfig
    from pipeline.pr_review_stage import PRReviewStage
    import logging

    logger = logging.getLogger(__name__)

    # Check if this is a specialized pipeline stage
    if hasattr(stage_config, 'stage_type') and stage_config.stage_type:

        if stage_config.stage_type == 'repair_cycle':
            # Load testing configuration from project
            project_config = config_manager.get_project_config(project_name)
            testing_config = project_config.testing or {}

            # Build RepairTestRunConfig list from project config
            test_configs = []
            for test_type_config in testing_config.get('types', []):
                test_type = test_type_config['type']
                test_configs.append(RepairTestRunConfig(
                    test_type=test_type,
                    timeout=test_type_config.get('timeout', 600),
                    max_iterations=test_type_config.get('max_iterations', 5),
                    review_warnings=test_type_config.get('review_warnings', True),
                    max_file_iterations=test_type_config.get('max_file_iterations', 3)
                ))

            # Get global settings
            max_total_agent_calls = stage_config.max_total_agent_calls or 100
            checkpoint_interval = stage_config.checkpoint_interval or 5

            # Create RepairCycleStage
            return RepairCycleStage(
                name=stage_config.name,
                test_configs=test_configs,
                agent_name=stage_config.default_agent,
                max_total_agent_calls=max_total_agent_calls,
                checkpoint_interval=checkpoint_interval
            )

        elif stage_config.stage_type == 'pr_review':
            # Create PRReviewStage
            logger.info(f"Creating PRReviewStage for {stage_config.name}")
            return PRReviewStage(
                name=stage_config.name,
                pr_review_agent=stage_config.default_agent,  # pr_code_reviewer
                requirements_verifier_agent="requirements_verifier"
            )

        else:
            logger.warning(f"Unknown stage_type: {stage_config.stage_type}")
            # Fall through to standard agent stage

    # Standard agent stage
    agent_config = config_manager.get_project_agent_config(
        project_name,
        stage_config.default_agent
    )
    return AgentStage(stage_config.default_agent, agent_config)


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
    Process task from task queue with branch management and auto-commit support.

    This is the entry point for task queue-based agent execution.
    Uses centralized AgentExecutor for consistent observability.
    """
    from services.project_workspace import workspace_manager
    from services.agent_executor import get_agent_executor
    from config.manager import config_manager
    from monitoring.observability import get_observability_manager
    from monitoring.decision_events import DecisionEventEmitter
    
    # Initialize decision observability
    obs = get_observability_manager()
    decision_events = DecisionEventEmitter(obs)

    task_context = task.context
    board_name = task_context.get('board', '')
    issue_number = task_context.get('issue_number')

    # For development pipelines, create/switch to feature branch
    # Check if this pipeline uses issue-based workflow (has workspace type 'issues')
    try:
        project_config = config_manager.get_project_config(task.project)
        # Find the pipeline with matching board_name
        pipeline = next(
            (p for p in project_config.pipelines if p.board_name == board_name),
            None
        )
        uses_git_workflow = pipeline and pipeline.workspace == 'issues'
    except Exception as e:
        logger.warning(f"Could not determine workspace type for board {board_name}: {e}")
        uses_git_workflow = False

    # Feature branch management handled by AgentExecutor's FeatureBranchManager
    # This provides hierarchical parent/sub-issue branch support

    # Extract pipeline_run_id for event tracking
    pipeline_run_id = None
    if hasattr(task, 'context') and task.context:
        pipeline_run_id = task.context.get('pipeline_run_id')

    # If not in task context, try to look it up
    if not pipeline_run_id and issue_number:
        try:
            from services.pipeline_run import get_pipeline_run_manager
            prm = get_pipeline_run_manager()
            active_run = prm.get_active_pipeline_run(task.project, issue_number)
            if active_run:
                pipeline_run_id = active_run.id
        except Exception:
            pass  # pipeline_run_id remains None

    # Validate task can run (check dev container requirements)
    validation_result = await validate_task_can_run(task, logger)
    if not validation_result['can_run']:
        logger.log_warning(f"Task {task.id} blocked: {validation_result['reason']}")

        # Build user-friendly error message
        base_message = validation_result['reason']
        if validation_result.get('needs_dev_setup'):
            user_message = (
                f"{base_message}. Agent '{task.agent}' requires a Docker development environment. "
                f"The system will automatically setup the environment and retry this task."
            )
        else:
            user_message = (
                f"{base_message}. Agent '{task.agent}' cannot execute until this is resolved. "
                f"Please check the project configuration or wait for setup to complete."
            )

        # EMIT DECISION EVENT: Error encountered
        decision_events.emit_error_decision(
            error_type='TaskValidationError',
            error_message=user_message,
            context={
                'task_id': task.id,
                'agent': task.agent,
                'issue_number': issue_number,
                'board': board_name,
                'requires_dev_container': True
            },
            recovery_action='queue_dev_environment_setup' if validation_result.get('needs_dev_setup') else 'block_task',
            success=validation_result.get('needs_dev_setup', False),
            project=task.project,
            pipeline_run_id=pipeline_run_id
        )
        
        # Queue dev_environment_setup task if needed
        if validation_result.get('needs_dev_setup'):
            try:
                await queue_dev_environment_setup(task.project, logger)

                # EMIT DECISION EVENT: Recovery successful
                recovery_message = (
                    f"Development environment setup has been queued for project '{task.project}'. "
                    f"Task will be retried automatically once the environment is ready."
                )
                decision_events.emit_error_decision(
                    error_type='TaskValidationError',
                    error_message=recovery_message,
                    context={
                        'task_id': task.id,
                        'agent': task.agent,
                        'issue_number': issue_number,
                        'board': board_name,
                        'auto_queued': True
                    },
                    recovery_action='queue_dev_environment_setup',
                    success=True,
                    project=task.project,
                    pipeline_run_id=pipeline_run_id
                )
            except Exception as queue_error:
                logger.error(
                    f"Failed to queue dev environment setup for {task.project}: {queue_error}. "
                    f"Task will be blocked until setup is manually triggered."
                )
                decision_events.emit_error_decision(
                    error_type='DevSetupQueueFailure',
                    error_message=f"Failed to auto-queue dev environment setup: {queue_error}",
                    context={
                        'task_id': task.id,
                        'agent': task.agent,
                        'issue_number': issue_number,
                        'board': board_name
                    },
                    recovery_action='manual_intervention_required',
                    success=False,
                    project=task.project,
                    pipeline_run_id=pipeline_run_id
                )

        from agents.non_retryable import NonRetryableAgentError
        raise NonRetryableAgentError(f"Task blocked: {user_message}")

    # Record execution start in work execution state
    if 'issue_number' in task_context and 'column' in task_context:
        from services.work_execution_state import work_execution_tracker
        work_execution_tracker.record_execution_start(
            issue_number=task_context['issue_number'],
            column=task_context['column'],
            agent=task.agent,
            trigger_source='task_queue',
            project_name=task.project
        )
        logger.info(
            f"Recorded execution start for {task.agent} on {task.project}/#{task_context['issue_number']} "
            f"in column {task_context['column']} (trigger: task_queue)"
        )

    # Execute agent using centralized executor
    executor = get_agent_executor()
    result = await executor.execute_agent(
        agent_name=task.agent,
        project_name=task.project,
        task_context=task.context,
        execution_type="task_queue"
    )

    # Auto-commit handled by FeatureBranchManager in AgentExecutor
    # This ensures commits are properly associated with parent/sub-issue branches

    # Auto-advance to next column if configured
    # CRITICAL: Skip auto-advancement if agent made manual progression
    manual_progression_made = result.get('manual_progression_made', False)
    if manual_progression_made:
        logger.info(
            f"Skipping auto-advancement for issue #{issue_number}: "
            f"agent made manual progression during execution"
        )

    current_column_name = task_context.get('column')
    if current_column_name and issue_number and not manual_progression_made:
        try:
            # Get workflow configuration
            from config.state_manager import state_manager

            project_config = config_manager.get_project_config(task.project)
            pipeline = next(
                (p for p in project_config.pipelines if p.board_name == board_name),
                None
            )

            if pipeline:
                workflow_template = config_manager.get_workflow_template(pipeline.workflow)

                # Find current column
                current_column = next(
                    (c for c in workflow_template.columns if c.name == current_column_name),
                    None
                )

                # Check if auto-advance is enabled
                if current_column and getattr(current_column, 'auto_advance_on_approval', False):
                    # Find next column
                    current_index = workflow_template.columns.index(current_column)
                    if current_index + 1 < len(workflow_template.columns):
                        next_column = workflow_template.columns[current_index + 1]

                        logger.info(
                            f"Auto-advancing issue #{issue_number} from {current_column_name} to {next_column.name}"
                        )

                        # Move the card
                        from services.pipeline_progression import PipelineProgression
                        from task_queue.task_manager import TaskQueue

                        task_queue = TaskQueue()
                        progression_service = PipelineProgression(task_queue)

                        moved = progression_service.move_issue_to_column(
                            project_name=task.project,
                            board_name=board_name,
                            issue_number=issue_number,
                            target_column=next_column.name,
                            trigger='agent_auto_advance'
                        )

                        if moved:
                            logger.info(
                                f"Successfully auto-advanced issue #{issue_number} to {next_column.name}"
                            )
                        else:
                            logger.warning(
                                f"Failed to auto-advance issue #{issue_number} to {next_column.name}"
                            )
        except Exception as e:
            logger.error(f"Error during auto-advancement: {e}")
            import traceback
            logger.error(traceback.format_exc())
            
            # EMIT DECISION EVENT: Error during auto-advancement
            decision_events.emit_error_decision(
                error_type='AutoAdvancementError',
                error_message=str(e),
                context={
                    'task_id': task.id,
                    'agent': task.agent,
                    'issue_number': issue_number,
                    'board': board_name,
                    'current_column': current_column_name
                },
                recovery_action='log_and_continue',
                success=False,
                project=task.project,
                pipeline_run_id=pipeline_run_id
            )

    return result


# Export the main integration function
__all__ = [
    'process_task_integrated',
    'create_agent_pipeline',
    'create_single_agent_pipeline',
    'AgentStage'
]
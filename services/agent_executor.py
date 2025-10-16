"""
Centralized Agent Execution Service

This is the ONLY way to execute agents in the orchestrator.
All agent executions MUST go through this service to ensure:
- Observability events are always emitted
- Claude logs are always streamed to Redis
- Consistent context structure across all execution paths
"""

import logging
import time
import json
from typing import Dict, Any, Optional
from datetime import datetime, timezone
from pathlib import Path
from monitoring.timestamp_utils import utc_now, utc_isoformat
from monitoring.observability import get_observability_manager
from pipeline.factory import PipelineFactory
from config.manager import config_manager
from services.github_integration import GitHubIntegration, AgentCommentFormatter

logger = logging.getLogger(__name__)


class AgentExecutor:
    """
    Centralized service for executing agents with guaranteed observability.
    """

    def __init__(self):
        self.obs = get_observability_manager()
        self.factory = PipelineFactory(config_manager)
        self.github = GitHubIntegration()

    async def execute_agent(
        self,
        agent_name: str,
        project_name: str,
        task_context: Dict[str, Any],
        task_id_prefix: str = "task"
    ) -> Any:
        """
        Execute an agent with full observability support.

        Args:
            agent_name: Name of the agent to execute (e.g., 'business_analyst')
            project_name: Name of the project
            task_context: The task context (issue data, discussion data, etc.)
            task_id_prefix: Prefix for generating task ID (e.g., 'review_cycle', 'conversational')

        Returns:
            Agent execution result
        """
        # Generate unique task ID
        task_id = f"{task_id_prefix}_{agent_name}_{int(utc_now().timestamp())}"

        logger.info(f"Executing agent {agent_name} for project {project_name} (task_id: {task_id})")

        # Emit task received event
        self.obs.emit_task_received(agent_name, task_id, project_name, task_context)

        # Create stream callback for live Claude Code output
        stream_callback = self._create_stream_callback(agent_name, task_id, project_name)

        # Build execution context with ALL required fields
        execution_context = self._build_execution_context(
            agent_name=agent_name,
            project_name=project_name,
            task_id=task_id,
            task_context=task_context,
            stream_callback=stream_callback
        )

        # Create agent instance
        agent_stage = self.factory.create_agent(agent_name, project_name)

        # Prepare workspace using abstraction layer
        workspace_context = None
        branch_name = None
        if 'issue_number' in task_context:
            try:
                from services.workspace import WorkspaceContextFactory
                from services.github_integration import GitHubIntegration

                # Get project config to determine repo info
                project_config = config_manager.get_project_config(project_name)
                if project_config and hasattr(project_config, 'github'):
                    repo_owner = project_config.github.get('org')
                    repo_name = project_config.github.get('repo')
                    if repo_owner and repo_name:
                        gh_integration = GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)

                        # Add agent_name to task_context for hybrid workspace routing
                        # This allows hybrid workspaces to determine if git operations are needed
                        task_context['agent_name'] = agent_name

                        # Create workspace context based on type
                        workspace_type = task_context.get('workspace_type', 'issues')
                        workspace_context = WorkspaceContextFactory.create(
                            workspace_type=workspace_type,
                            project=project_name,
                            issue_number=task_context['issue_number'],
                            task_context=task_context,
                            github_integration=gh_integration
                        )

                        # Prepare workspace (git branch OR discussion context)
                        prep_result = await workspace_context.prepare_execution()
                        task_context.update(prep_result)
                        
                        # Extract branch name if available
                        branch_name = prep_result.get('branch_name')

                        logger.info(
                            f"Prepared {workspace_type} workspace: {prep_result.get('branch_name', prep_result.get('discussion_id'))}"
                        )

            except Exception as e:
                logger.warning(f"Failed to prepare workspace: {e}")
                # Continue execution even if workspace preparation fails
        
        # Generate container name if agent will use Docker
        # This matches the naming logic in docker_runner.py:206-207
        container_name = None
        if execution_context.get('use_docker', True):
            from claude.docker_runner import DockerAgentRunner
            raw_container_name = f"claude-agent-{project_name}-{task_id}"
            container_name = DockerAgentRunner._sanitize_container_name(raw_container_name)
            logger.info(f"Generated container name for UI tracking: {container_name}")

        # Extract pipeline_run_id from task_context for event tracking
        pipeline_run_id = task_context.get('pipeline_run_id')

        # Emit agent initialized event (after workspace prep to include branch_name and container_name)
        agent_config = agent_stage.agent_config or {}
        self.obs.emit_agent_initialized(
            agent_name, task_id, project_name, agent_config, branch_name, container_name, pipeline_run_id
        )

        # Mark dev container as in_progress when setup agent starts
        if agent_name == 'dev_environment_setup':
            from services.dev_container_state import dev_container_state, DevContainerStatus
            dev_container_state.set_status(
                project_name,
                DevContainerStatus.IN_PROGRESS,
                image_name=f"{project_name}-agent:latest"
            )
            logger.info(f"Marked {project_name} dev container as IN_PROGRESS")

        # Execute agent
        start_time = time.time()
        try:
            result = await agent_stage.execute(execution_context)

            # Extract output from result for event emission
            output_text = None
            if isinstance(result, dict):
                # Try to get markdown output or raw analysis result
                output_text = result.get('markdown_analysis') or result.get('raw_analysis_result')
                
                # If not found at top level, try nested in context
                if not output_text and 'context' in result:
                    ctx = result['context']
                    output_text = ctx.get('markdown_analysis') or ctx.get('raw_analysis_result')

            duration_ms = (time.time() - start_time) * 1000
            self.obs.emit_agent_completed(
                agent_name, task_id, project_name, duration_ms, True, None, pipeline_run_id, output_text
            )

            # If dev_environment_setup completed successfully, queue verifier
            if agent_name == 'dev_environment_setup':
                await self._queue_environment_verifier(project_name, task_context)

            # Post agent output to GitHub (centralized posting)
            await self._post_agent_output_to_github(agent_name, task_context, result)

            # Finalize workspace using abstraction layer
            if workspace_context:
                try:
                    commit_message = f"Complete work for issue #{task_context['issue_number']}\n\nAgent: {agent_name}\nTask: {task_id}"

                    finalize_result = await workspace_context.finalize_execution(
                        result=result,
                        commit_message=commit_message
                    )

                    if finalize_result.get('success'):
                        logger.info(f"Finalized workspace: {finalize_result}")
                    else:
                        logger.warning(f"Workspace finalization had issues: {finalize_result}")

                except Exception as e:
                    logger.warning(f"Failed to finalize workspace: {e}")
                    # Continue execution even if finalization fails

            # Record successful execution outcome
            if 'issue_number' in task_context and 'column' in task_context:
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_outcome(
                    issue_number=task_context['issue_number'],
                    column=task_context['column'],
                    agent=agent_name,
                    outcome='success',
                    project_name=project_name
                )

            logger.info(f"Agent {agent_name} completed successfully (duration: {duration_ms:.0f}ms)")
            return result

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000
            self.obs.emit_agent_completed(
                agent_name, task_id, project_name, duration_ms, False, str(e), pipeline_run_id, None
            )

            # Record failed execution outcome
            if 'issue_number' in task_context and 'column' in task_context:
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.record_execution_outcome(
                    issue_number=task_context['issue_number'],
                    column=task_context['column'],
                    agent=agent_name,
                    outcome='failure',
                    project_name=project_name,
                    error=str(e)
                )

            logger.error(f"Agent {agent_name} failed after {duration_ms:.0f}ms: {e}")
            raise

    def _create_stream_callback(self, agent_name: str, task_id: str, project_name: str):
        """Create callback for streaming Claude Code output to Redis"""
        def stream_callback(event):
            """Publish Claude stream events to Redis for websocket forwarding and history"""
            try:
                if self.obs and self.obs.enabled:
                    event_data = {
                        'agent': agent_name,
                        'task_id': task_id,
                        'project': project_name,
                        'timestamp': event.get('timestamp') or time.time(),
                        'event': event
                    }
                    event_json = json.dumps(event_data)

                    # Publish to pub/sub for real-time delivery
                    self.obs.redis.publish('orchestrator:claude_stream', event_json)

                    # Also add to Redis Stream for history (with automatic trimming)
                    claude_stream_key = "orchestrator:claude_logs_stream"
                    self.obs.redis.xadd(
                        claude_stream_key,
                        {'log': event_json},
                        maxlen=500,
                        approximate=True
                    )

                    # Set 2-hour TTL on the stream
                    self.obs.redis.expire(claude_stream_key, 7200)
            except Exception as e:
                logger.error(f"Error publishing stream event: {e}")

        return stream_callback

    def _build_execution_context(
        self,
        agent_name: str,
        project_name: str,
        task_id: str,
        task_context: Dict[str, Any],
        stream_callback
    ) -> Dict[str, Any]:
        """Build standardized execution context for agent"""
        from state_management.manager import StateManager
        from services.project_workspace import workspace_manager

        # Create state manager
        state_manager = StateManager(Path("orchestrator_data/state"))

        # Get project directory from workspace manager
        project_dir = workspace_manager.get_project_dir(project_name)

        # Build context with ALL required fields for agents
        context = {
            'pipeline_id': f"pipeline_{task_id}_{utc_now().timestamp()}",
            'task_id': task_id,
            'agent': agent_name,
            'project': project_name,
            'context': task_context,  # Nest task context here
            'work_dir': str(project_dir),  # Use absolute path from workspace manager
            'completed_work': [],
            'decisions': [],
            'metrics': {},
            'validation': {},
            'state_manager': state_manager,
            'observability': self.obs,  # REQUIRED: Observability manager
            'stream_callback': stream_callback,  # REQUIRED: Live Claude logs
            'use_docker': task_context.get('use_docker', True)
        }

        # Add claude_model and use_docker from agent config if available
        agent_config = config_manager.get_project_agent_config(project_name, agent_name)
        if hasattr(agent_config, 'model'):
            context['claude_model'] = agent_config.model

        # Override use_docker if agent explicitly requires or forbids Docker
        if hasattr(agent_config, 'requires_docker'):
            # Agent config takes precedence over task context
            context['use_docker'] = agent_config.requires_docker
            logger.info(f"Agent {agent_name} requires_docker={agent_config.requires_docker}, overriding task context")

        return context

    async def _post_agent_output_to_github(
        self,
        agent_name: str,
        task_context: Dict[str, Any],
        result: Dict[str, Any]
    ):
        """
        Post agent output to GitHub (issues or discussions based on workspace_type).

        This centralizes GitHub posting logic that was previously duplicated across all agents.
        """
        # Check if there's an issue to post to
        if 'issue_number' not in task_context:
            logger.debug(f"No issue_number in task context, skipping GitHub post for {agent_name}")
            return

        issue_number = task_context['issue_number']
        workspace_type = task_context.get('workspace_type', 'issues')

        # Extract markdown output from result (different agents use different keys)
        markdown_output = self._extract_markdown_output(agent_name, result)

        if not markdown_output:
            logger.warning(f"No markdown output found for {agent_name}, skipping GitHub post")
            return

        # Format the comment
        comment = AgentCommentFormatter.format_agent_completion(
            agent_name=agent_name,
            output=markdown_output,
            summary_stats={},  # Could extract from result if needed
            next_steps=None
        )

        try:
            # Get reply_to_id for threaded conversations
            reply_to_id = task_context.get('reply_to_comment_id')

            # Post to GitHub (workspace-aware: issues or discussions)
            post_result = await self.github.post_agent_output(
                task_context,
                comment,
                reply_to_id=reply_to_id
            )

            if post_result.get('success'):
                logger.info(f"Posted {agent_name} output to GitHub (workspace: {workspace_type}, issue: #{issue_number})")

                # Track comment timestamp for feedback loop
                from services.feedback_manager import FeedbackManager
                feedback_manager = FeedbackManager()
                feedback_manager.set_last_agent_comment_time(
                    issue_number,
                    agent_name,
                    utc_isoformat()
                )
            else:
                logger.error(f"Failed to post {agent_name} output to GitHub: {post_result.get('error')}")

        except Exception as e:
            logger.error(f"Error posting {agent_name} output to GitHub: {e}", exc_info=True)
            # Don't fail the agent execution if posting fails

    def _extract_markdown_output(self, agent_name: str, result: Dict[str, Any]) -> Optional[str]:
        """
        Extract markdown output from agent result.

        Different agents store their output in different keys:
        - markdown_analysis (business_analyst, idea_researcher)
        - markdown_review (reviewers)
        - markdown_design (software_architect)
        - etc.

        This method tries common patterns to find the output.
        """
        # Common output keys to try
        output_keys = [
            'markdown_analysis',
            'markdown_review',
            'markdown_design',
            'markdown_plan',
            'markdown_test_plan',
            'markdown_documentation',
            'markdown_output',
            'raw_analysis_result',
            'output',  # Used by dev_environment_verifier
            'verification_result',  # Used by dev_environment_verifier
        ]

        for key in output_keys:
            if key in result:
                output = result[key]
                if output and isinstance(output, str):
                    return output

        # Fallback: check if there's a dict with a 'full_markdown' key
        for value in result.values():
            if isinstance(value, dict) and 'full_markdown' in value:
                return value['full_markdown']

        logger.warning(f"Could not find markdown output for {agent_name} in keys: {list(result.keys())}")
        return None

    async def _queue_environment_verifier(
        self,
        project_name: str,
        task_context: Dict[str, Any]
    ):
        """
        Queue a dev_environment_verifier task after setup completes.

        Args:
            project_name: Name of the project
            task_context: Context from the setup task
        """
        try:
            from task_queue.task_manager import Task, TaskPriority, TaskQueue
            from datetime import datetime

            logger.info(f"Queuing dev_environment_verifier task for {project_name}")

            task_queue = TaskQueue(use_redis=True)

            # Create verifier task with reference to setup output
            task = Task(
                id=f"auto_dev_env_verify_{project_name}_{int(utc_now().timestamp())}",
                agent="dev_environment_verifier",
                project=project_name,
                priority=TaskPriority.HIGH,
                context={
                    'issue': task_context.get('issue', {
                        'title': f'Verify development environment for {project_name}',
                        'body': 'Auto-triggered: Verify Docker image after setup completion',
                        'number': 0
                    }),
                    'issue_number': task_context.get('issue_number', 0),
                    'board': task_context.get('board', 'system'),
                    'repository': project_name,
                    'automated_setup': True,
                    'auto_triggered': True,
                    'use_docker': False,  # Verifier also runs locally
                    'previous_stage_output': 'Setup agent completed successfully'
                },
                created_at=utc_isoformat()
            )

            task_queue.enqueue(task)
            logger.info(f"Queued dev_environment_verifier task: {task.id}")

        except Exception as e:
            logger.error(f"Failed to queue dev_environment_verifier for {project_name}: {e}")
            # Don't fail the setup agent if verifier queueing fails


# Global singleton instance
_agent_executor: Optional[AgentExecutor] = None

def get_agent_executor() -> AgentExecutor:
    """Get the global AgentExecutor instance"""
    global _agent_executor
    if _agent_executor is None:
        _agent_executor = AgentExecutor()
    return _agent_executor

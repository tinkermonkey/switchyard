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
import asyncio
import uuid
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
        # Don't initialize GitHubIntegration here - create it per-execution with proper repo context

    async def execute_agent(
        self,
        agent_name: str,
        project_name: str,
        task_context: Dict[str, Any],
        execution_type: str = "standard"
    ) -> Any:
        """
        Execute an agent with full observability support.

        IMPORTANT: Callers MUST call work_execution_tracker.record_execution_start()
        BEFORE calling this method, if the task_context contains 'issue_number' and 'column'.
        This ensures proper execution state tracking and audit trails.

        See examples in:
        - services/project_monitor.py (lines 1804, 3105)
        - services/pipeline_progression.py (line 390)
        - services/human_feedback_loop.py (in _execute_agent method)
        - services/review_cycle.py (in _execute_agent_directly method)

        Args:
            agent_name: Name of the agent to execute (e.g., 'business_analyst')
            project_name: Name of the project
            task_context: The task context (issue data, discussion data, etc.)
            execution_type: Classification of this execution (e.g., 'review_cycle', 'conversational', 'repair_test')

        Returns:
            Agent execution result
        """
        # Generate opaque UUID task ID
        task_id = str(uuid.uuid4())

        # Store execution_type in task_context for downstream propagation
        # (Docker labels, observability events, Redis tracking)
        task_context['execution_type'] = execution_type

        logger.info(f"Executing agent {agent_name} for project {project_name} (task_id: {task_id})")

        # Stamp task_id onto the in_progress execution record so restart
        # recovery can match this exact execution's Redis result.
        # Best-effort: failure here must not prevent agent execution.
        if 'issue_number' in task_context:
            try:
                from services.work_execution_state import work_execution_tracker
                work_execution_tracker.stamp_execution_task_id(
                    project_name, task_context['issue_number'],
                    agent_name, task_context.get('column', 'unknown'), task_id
                )
            except (IOError, OSError) as stamp_err:
                logger.warning(
                    f"Failed to stamp task_id on execution for "
                    f"{project_name}/#{task_context['issue_number']}: {stamp_err}. "
                    f"Recovery will fall back to wildcard scan if needed."
                )
            except Exception as stamp_err:
                logger.error(
                    f"Unexpected error stamping task_id on execution for "
                    f"{project_name}/#{task_context['issue_number']}: {stamp_err}. "
                    f"This may indicate a bug in stamp_execution_task_id.",
                    exc_info=True
                )

        # Emit task received event
        self.obs.emit_task_received(agent_name, task_id, project_name, task_context,
                                    execution_type=execution_type)

        # Extract pipeline_run_id from task_context for event tracking
        pipeline_run_id = task_context.get('pipeline_run_id')
        logger.info(f"[DIAGNOSTIC] Extracted pipeline_run_id from task_context: {pipeline_run_id} (type: {type(pipeline_run_id)})")
        logger.info(f"[DIAGNOSTIC] Full task_context keys: {list(task_context.keys())}")

        # Build execution context with ALL required fields
        # NOTE: Stream callback removed - docker-claude-wrapper.py handles all Claude log streaming
        execution_context = self._build_execution_context(
            agent_name=agent_name,
            project_name=project_name,
            task_id=task_id,
            task_context=task_context
        )

        # Create agent instance
        agent_stage = self.factory.create_agent(agent_name, project_name)

        # Prepare workspace using abstraction layer
        # Skip if we're in a repair cycle (workspace already prepared by parent execution)
        workspace_context = None
        branch_name = None
        skip_workspace_prep = task_context.get('skip_workspace_prep', False)

        logger.info(
            f"🔍 WORKSPACE PREP DEBUG: agent={agent_name}, "
            f"skip_workspace_prep={skip_workspace_prep}, "
            f"has_issue_number={'issue_number' in task_context}, "
            f"issue_number={task_context.get('issue_number', 'N/A')}"
        )

        if skip_workspace_prep:
            logger.info(f"Skipping workspace preparation (skip_workspace_prep=True) for {agent_name}")
            # Extract branch_name from context if available
            branch_name = task_context.get('branch_name')
        elif 'issue_number' in task_context:
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
                        logger.info(
                            f"🔍 WORKSPACE PREP DEBUG: Creating {workspace_type} workspace context for issue #{task_context['issue_number']}"
                        )

                        workspace_context = WorkspaceContextFactory.create(
                            workspace_type=workspace_type,
                            project=project_name,
                            issue_number=task_context['issue_number'],
                            task_context=task_context,
                            github_integration=gh_integration
                        )

                        logger.info(
                            f"🔍 WORKSPACE PREP DEBUG: workspace_context created, type={type(workspace_context).__name__}, "
                            f"supports_git={getattr(workspace_context, 'supports_git_operations', 'N/A')}"
                        )

                        # Prepare workspace (git branch OR discussion context)
                        logger.info(f"🔍 WORKSPACE PREP DEBUG: Calling prepare_execution() on {workspace_type} workspace")
                        prep_result = await workspace_context.prepare_execution()
                        logger.info(f"🔍 WORKSPACE PREP DEBUG: prepare_execution() returned: {prep_result}")
                        
                        if prep_result is None:
                            logger.error(f"Workspace prepare_execution returned None for {workspace_type} workspace")
                            raise ValueError(f"Workspace preparation failed: prepare_execution returned None")
                        
                        task_context.update(prep_result)
                        
                        # Extract branch name if available
                        branch_name = prep_result.get('branch_name')

                        logger.info(
                            f"Prepared {workspace_type} workspace: {prep_result.get('branch_name', prep_result.get('discussion_id'))}"
                        )

                        # CRITICAL: Verify the correct branch is checked out for git-based workspaces
                        # This prevents commits to wrong branches (e.g., committing to main instead of feature branch)
                        if workspace_context.supports_git_operations and branch_name:
                            from services.feature_branch_manager import feature_branch_manager
                            import subprocess
                            project_dir = f"/workspace/{project_name}"

                            try:
                                actual_branch = await feature_branch_manager.get_current_branch(project_dir)

                                if actual_branch != branch_name:
                                    error_msg = (
                                        f"Branch verification failed for issue #{task_context.get('issue_number')}: "
                                        f"expected branch '{branch_name}', but repository is on '{actual_branch}'. "
                                        f"This indicates workspace preparation did not complete successfully. "
                                        f"Cannot continue safely to prevent commits to wrong branch."
                                    )
                                    logger.error(error_msg)
                                    from agents.non_retryable import NonRetryableAgentError
                                    raise NonRetryableAgentError(error_msg)

                                logger.info(f"Branch verification passed: confirmed on '{actual_branch}'")

                            except RuntimeError as runtime_error:
                                # Re-raise our own branch mismatch errors
                                raise
                            except subprocess.CalledProcessError as git_error:
                                # Git command failed (e.g., not a git repo, directory doesn't exist)
                                # This is common in test environments - log warning but don't halt
                                logger.warning(
                                    f"Could not verify git branch (git command failed): {git_error}. "
                                    f"This may indicate a test environment or missing repository. "
                                    f"Continuing with caution."
                                )
                            except Exception as branch_check_error:
                                # Other unexpected errors during branch checking
                                logger.warning(
                                    f"Branch verification encountered unexpected error: {branch_check_error}. "
                                    f"Continuing with caution.",
                                    exc_info=True
                                )

            except Exception as e:
                from agents.non_retryable import NonRetryableAgentError
                if isinstance(e, NonRetryableAgentError):
                    raise

                logger.error(
                    f"🔍 WORKSPACE PREP DEBUG: Exception during workspace preparation: {e}\n"
                    f"  workspace_context={'present' if workspace_context else 'NONE'}\n"
                    f"  Exception type: {type(e).__name__}",
                    exc_info=True
                )

                # Check if this workspace requires git operations
                # For git-based workspaces, workspace prep failures are CRITICAL
                # For non-git workspaces (discussions), we can continue with a warning
                if workspace_context is not None and hasattr(workspace_context, 'supports_git_operations'):
                    if workspace_context.supports_git_operations:
                        logger.error(
                            f"Failed to prepare git-based workspace: {e}. "
                            f"Halting execution to prevent commits to wrong branch.",
                            exc_info=True
                        )

                        # Emit error event if this is part of a review cycle
                        if pipeline_run_id and 'issue_number' in task_context:
                            try:
                                from monitoring.decision_events import DecisionEventEmitter
                                decision_emitter = DecisionEventEmitter(self.obs)

                                decision_emitter.emit_error_decision(
                                    error_type="workspace_preparation_git_failure",
                                    error_message=str(e),
                                    context={
                                        'agent': agent_name,
                                        'project': project_name,
                                        'issue_number': task_context.get('issue_number'),
                                        'branch_name': task_context.get('branch_name'),
                                        'workspace_type': task_context.get('workspace_type', 'issues')
                                    },
                                    recovery_action="Agent execution halted to prevent commits to wrong branch",
                                    success=False,
                                    project=project_name,
                                    pipeline_run_id=pipeline_run_id
                                )
                            except Exception as emit_error:
                                logger.error(f"Failed to emit workspace prep error event: {emit_error}", exc_info=True)

                        raise RuntimeError(f"Git workspace preparation failed: {e}") from e

                # For other cases (no workspace context yet, or non-git workspace), log warning and continue
                # This preserves backward compatibility for agents that don't need git operations
                logger.warning(f"Failed to prepare workspace: {e}", exc_info=True)
                # Continue execution even if workspace preparation fails for non-critical cases
        
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
        # This returns the agent_execution_id for tracking this specific execution
        agent_config = agent_stage.agent_config or {}
        agent_execution_id = self.obs.emit_agent_initialized(
            agent_name, task_id, project_name, agent_config, branch_name, container_name, pipeline_run_id,
            execution_type=execution_type
        )
        
        logger.info(f"Agent execution started with ID: {agent_execution_id}")

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
        
        # Get retry configuration
        agent_config = agent_stage.agent_config or {}
        if isinstance(agent_config, dict):
            retries = agent_config.get('retries', 2)
        else:
            retries = getattr(agent_config, 'retries', 2)
            
        max_attempts = 1 + retries
        attempt = 0
        
        try:
            # Pre-flight validation: reject executions with empty issue descriptions.
            # An empty body means the task was created with bad data and every agent
            # prompt will silently fall back to "No description", producing garbage
            # output.  Fail immediately — before any Docker container is launched —
            # so the pipeline lock is retained and the problem surfaces for human review.
            issue = task_context.get('issue', {})
            if issue:
                issue_body = issue.get('body')
                if not issue_body or not str(issue_body).strip():
                    from agents.non_retryable import NonRetryableAgentError
                    raise NonRetryableAgentError(
                        f"Agent {agent_name} cannot execute: issue "
                        f"#{issue.get('number', '?')} ('{issue.get('title', 'unknown')}') "
                        f"has an empty description. A non-empty issue body is required for "
                        f"agents to produce meaningful output. Pipeline halted — resolve the "
                        f"issue description and re-trigger."
                    )

            while attempt < max_attempts:
                attempt += 1
                try:
                    if attempt > 1:
                        logger.info(f"Retry attempt {attempt}/{max_attempts} for agent {agent_name}")

                    # Check cancellation signal BEFORE circuit breaker — ensures cancelled
                    # work never touches the circuit breaker at all
                    if 'issue_number' in task_context:
                        from services.cancellation import get_cancellation_signal, CancellationError
                        if get_cancellation_signal().is_cancelled(project_name, task_context['issue_number']):
                            raise CancellationError(
                                f"Work cancelled for {project_name}/#{task_context['issue_number']}"
                            )

                    # Check Claude Code circuit breaker before attempting execution
                    # If it's open, we should not attempt execution or count failures against the agent
                    from monitoring.claude_code_breaker import get_breaker
                    claude_breaker = get_breaker()
                    if claude_breaker.is_open():
                        reset_time = claude_breaker.reset_time
                        if reset_time:
                            from datetime import datetime, timezone
                            time_until = (reset_time - datetime.now(timezone.utc)).total_seconds()
                            logger.warning(
                                f"⏸️  Claude Code circuit breaker is OPEN. Agent {agent_name} execution paused. "
                                f"Tokens reset in {time_until:.0f}s at {reset_time.strftime('%I:%M %p')}"
                            )
                            # Raise a specific exception that indicates this is a systemic issue, not an agent failure
                            raise Exception(
                                f"Claude Code circuit breaker is OPEN. Resets at {reset_time.strftime('%I:%M %p')}. "
                                f"This is a systemic token limit issue, not an agent failure."
                            )
                        else:
                            logger.warning(f"⏸️  Claude Code circuit breaker is OPEN. Agent {agent_name} execution paused.")
                            raise Exception(
                                "Claude Code circuit breaker is OPEN. Awaiting token reset. "
                                "This is a systemic token limit issue, not an agent failure."
                            )

                    # Use run_with_circuit_breaker instead of direct execute
                    result = await agent_stage.run_with_circuit_breaker(execution_context)

                    # If successful, break the retry loop
                    break

                except Exception as e:
                    # CancellationError: deliberate stop — never retry, never trip circuit breaker
                    from services.cancellation import CancellationError
                    if isinstance(e, CancellationError):
                        logger.info(f"Agent {agent_name} cancelled: {e}")
                        raise

                    # NonRetryableAgentError: permanent failure — skip retries
                    from agents.non_retryable import NonRetryableAgentError
                    if isinstance(e, NonRetryableAgentError):
                        logger.warning(f"Agent {agent_name} hit non-retryable error: {e}")
                        raise

                    # Check if this is a Claude Code breaker failure (systemic issue)
                    error_message = str(e)
                    is_claude_breaker_failure = "Claude Code circuit breaker is OPEN" in error_message

                    if is_claude_breaker_failure:
                        # This is a systemic issue (token limits), not an agent failure
                        # Don't retry - all agents will fail until breaker closes
                        # Don't count against agent's circuit breaker
                        logger.error(
                            f"Agent {agent_name} blocked by Claude Code circuit breaker. "
                            f"No retries will be attempted (all would fail until tokens reset)."
                        )
                        # Re-raise to be caught by outer block, which will emit agent_failed event
                        raise e

                    # Check if we should retry (for normal agent failures)
                    if attempt < max_attempts:
                        logger.warning(f"Agent execution failed (attempt {attempt}/{max_attempts}): {e}")
                        # Wait before retry (longer backoff to allow circuit breaker recovery: 15s, 30s, 60s)
                        # Circuit breaker recovery timeout is 30s, so first retry happens after breaker opens,
                        # second retry happens after breaker transitions to HALF_OPEN
                        wait_time = 15 * attempt
                        logger.info(f"Waiting {wait_time}s before retry (allows circuit breaker recovery)...")
                        await asyncio.sleep(wait_time)
                    else:
                        # Out of retries, re-raise to be caught by outer block
                        raise e

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
                agent_name, task_id, project_name, duration_ms, True, None, pipeline_run_id, output_text, agent_execution_id,
                execution_type=execution_type
            )

            # If dev_environment_setup completed successfully, queue verifier
            if agent_name == 'dev_environment_setup':
                await self._queue_environment_verifier(project_name, task_context)

            # Post agent output to GitHub — skipped when docker_runner already handled it
            # via _complete_agent_execution (which reads workspace routing from durable stores).
            if not result.get('output_posted'):
                await self._post_agent_output_to_github(agent_name, task_context, result)

            logger.info(
                f"🔍 FINALIZATION DEBUG: workspace_context={'present' if workspace_context else 'NONE'}, "
                f"workspace_type={workspace_context.__class__.__name__ if workspace_context else 'N/A'}, "
                f"issue_number={task_context.get('issue_number', 'N/A')}"
            )

            # Finalize workspace using abstraction layer
            if workspace_context:
                logger.info(f"🔍 FINALIZATION DEBUG: Entering workspace finalization block")
                try:
                    commit_message = (
                        f"Complete work for issue #{task_context['issue_number']}\n\n"
                        f"Agent: {agent_name}\n"
                        f"Task: {task_id}\n\n"
                        f"Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>\n"
                        f"[orchestrator-commit]"
                    )

                    finalize_result = await workspace_context.finalize_execution(
                        result=result,
                        commit_message=commit_message
                    )

                    if finalize_result.get('success'):
                        logger.info(f"✅ Finalized workspace: {finalize_result}")
                    else:
                        # Finalization returned failure - log details and check for uncommitted changes
                        logger.warning(
                            f"⚠️ Workspace finalization reported issues: {finalize_result}\n"
                            f"  Error: {finalize_result.get('error', 'Unknown')}\n"
                            f"  Checking for uncommitted changes via failsafe..."
                        )

                        # Run failsafe check to handle any uncommitted changes
                        if 'issue_number' in task_context:
                            await self._failsafe_commit_check(
                                project_name=project_name,
                                agent_name=agent_name,
                                task_context=task_context,
                                task_id=task_id
                            )

                except Exception as e:
                    logger.error(
                        f"❌ FINALIZATION DEBUG: Exception during workspace finalization: {e}\n"
                        f"  Exception type: {type(e).__name__}",
                        exc_info=True
                    )

                    # Run failsafe check even on exception to handle uncommitted changes
                    if 'issue_number' in task_context:
                        logger.info("Running failsafe commit check after finalization exception...")
                        try:
                            await self._failsafe_commit_check(
                                project_name=project_name,
                                agent_name=agent_name,
                                task_context=task_context,
                                task_id=task_id
                            )
                        except Exception as failsafe_error:
                            logger.error(
                                f"❌ Failsafe commit check also failed: {failsafe_error}",
                                exc_info=True
                            )
                    # Continue execution even if finalization fails
            else:
                # CRITICAL FAILSAFE: workspace_context is None, but agent may have made changes
                # This happens when:
                # 1. Workspace preparation was skipped (repair cycles)
                # 2. Workspace preparation failed but agent still ran
                # 3. Agent doesn't have an issue_number (unlikely)
                logger.warning(
                    f"⚠️ FAILSAFE: workspace_context is None for {agent_name}, "
                    f"but agent completed. Checking for uncommitted changes..."
                )

                # Check if there are any uncommitted changes in the workspace
                if 'issue_number' in task_context:
                    await self._failsafe_commit_check(
                        project_name=project_name,
                        agent_name=agent_name,
                        task_context=task_context,
                        task_id=task_id
                    )
                else:
                    logger.info("No issue_number in task_context - skipping failsafe commit check")

            # PR-ready marking is handled by two idempotent checks:
            # 1. feature_branch_manager.finalize_workspace() - immediate check during finalization
            # 2. project_monitor._check_pr_ready_on_issue_exit() - delayed check when issue exits to Done/Staged
            # Both query GitHub as source of truth and provide redundant coverage

            # Record successful execution outcome
            # CRITICAL: Always try to record outcome to prevent stuck "in_progress" states
            # Note: docker_runner also records outcome (for early recording before result processing)
            # We check if it's already recorded to avoid double-recording errors
            if 'issue_number' in task_context:
                from services.work_execution_state import work_execution_tracker
                column = task_context.get('column', 'unknown')

                # Warn if column is missing (shouldn't happen in normal flow)
                if column == 'unknown':
                    logger.warning(
                        f"Recording execution outcome without column for issue #{task_context['issue_number']} "
                        f"(agent={agent_name}, project={project_name}). This may indicate a bug in task creation."
                    )

                # Check if docker_runner already recorded the outcome for the CURRENT execution.
                # Walk history backwards: if we hit an in_progress entry for this agent/column,
                # that's our execution and it hasn't been recorded yet. Only skip if a terminal
                # outcome appears BEFORE (i.e. more recent than) any in_progress entry.
                state = work_execution_tracker.load_state(project_name, task_context['issue_number'])
                already_recorded = False

                for execution in reversed(state.get('execution_history', [])):
                    if execution.get('column') != column or execution.get('agent') != agent_name:
                        continue
                    if execution.get('outcome') == 'in_progress':
                        # Found our current execution — it hasn't been recorded yet
                        break
                    if execution.get('outcome') in ['success', 'failure', 'cancelled', 'blocked']:
                        # Terminal outcome already recorded for this execution
                        already_recorded = True
                        logger.debug(
                            f"Execution outcome already recorded by docker_runner for "
                            f"{project_name}/#{task_context['issue_number']} {agent_name} in {column}"
                        )
                        break

                if not already_recorded:
                    work_execution_tracker.record_execution_outcome(
                        issue_number=task_context['issue_number'],
                        column=column,
                        agent=agent_name,
                        outcome='success',
                        project_name=project_name
                    )
            else:
                # Log warning if we can't record outcome due to missing context
                logger.warning(
                    f"Cannot record execution outcome for {agent_name}: missing issue_number in task_context. "
                    f"This execution will not be tracked in work execution state. task_id={task_id}"
                )

            logger.info(f"Agent {agent_name} completed successfully (duration: {duration_ms:.0f}ms)")
            return result

        except Exception as e:
            duration_ms = (time.time() - start_time) * 1000

            # CancellationError: deliberate stop — cancel_issue_work() already recorded outcome
            from services.cancellation import CancellationError
            if isinstance(e, CancellationError):
                logger.info(f"Agent {agent_name} cancelled after {duration_ms:.0f}ms: {e}")
                raise

            error_message = str(e)
            is_claude_breaker_failure = "Claude Code circuit breaker is OPEN" in error_message

            if is_claude_breaker_failure:
                # This is a systemic issue (token limits), not an agent failure
                # Don't emit agent_failed - emit a paused/blocked event instead
                logger.warning(
                    f"Agent {agent_name} blocked by Claude Code circuit breaker after {duration_ms:.0f}ms. "
                    f"Not counting as agent failure. Pipeline will resume when tokens reset."
                )

                # Record execution outcome as 'blocked' to enable automatic recovery
                # Bug fix: Previously left in 'in_progress' which caused stuck issues
                if 'issue_number' in task_context:
                    from services.work_execution_state import work_execution_tracker
                    column = task_context.get('column', 'unknown')

                    work_execution_tracker.record_execution_outcome(
                        issue_number=task_context['issue_number'],
                        column=column,
                        agent=agent_name,
                        outcome='blocked',
                        project_name=project_name,
                        error=error_message
                    )
                    logger.info(
                        f"Recorded 'blocked' outcome for issue #{task_context['issue_number']} "
                        f"to enable automatic retry when circuit breaker closes"
                    )
                else:
                    logger.warning(
                        f"Cannot record blocked outcome for {agent_name}: missing issue_number in task_context"
                    )
            else:
                # Normal agent failure - emit event and record outcome
                self.obs.emit_agent_completed(
                    agent_name, task_id, project_name, duration_ms, False, str(e), pipeline_run_id, None, agent_execution_id,
                    execution_type=execution_type
                )

                # Record failed execution outcome
                # CRITICAL: Always try to record outcome to prevent stuck "in_progress" states
                # Note: docker_runner also records outcome (for early recording before result processing)
                # We check if it's already recorded to avoid double-recording errors
                if 'issue_number' in task_context:
                    from services.work_execution_state import work_execution_tracker
                    column = task_context.get('column', 'unknown')

                    # Warn if column is missing (shouldn't happen in normal flow)
                    if column == 'unknown':
                        logger.warning(
                            f"Recording execution outcome without column for issue #{task_context['issue_number']} "
                            f"(agent={agent_name}, project={project_name}). This may indicate a bug in task creation."
                        )

                    # Check if docker_runner already recorded the outcome for the CURRENT execution.
                    # Walk history backwards: if we hit an in_progress entry for this agent/column,
                    # that's our execution and it hasn't been recorded yet.
                    state = work_execution_tracker.load_state(project_name, task_context['issue_number'])
                    already_recorded = False

                    for execution in reversed(state.get('execution_history', [])):
                        if execution.get('column') != column or execution.get('agent') != agent_name:
                            continue
                        if execution.get('outcome') == 'in_progress':
                            break
                        if execution.get('outcome') in ['success', 'failure', 'cancelled', 'blocked']:
                            already_recorded = True
                            logger.debug(
                                f"Execution outcome already recorded by docker_runner for "
                                f"{project_name}/#{task_context['issue_number']} {agent_name} in {column}"
                            )
                            break

                    if not already_recorded:
                        work_execution_tracker.record_execution_outcome(
                            issue_number=task_context['issue_number'],
                            column=column,
                            agent=agent_name,
                            outcome='failure',
                            project_name=project_name,
                            error=str(e)
                        )
                else:
                    # Log warning if we can't record outcome due to missing context
                    logger.warning(
                        f"Cannot record execution outcome for {agent_name}: missing issue_number in task_context. "
                        f"This execution will not be tracked in work execution state. task_id={task_id}, error={str(e)}"
                    )

                logger.error(f"Agent {agent_name} failed after {duration_ms:.0f}ms: {e}")

                # Reset dev container state on setup failure so it can be retried.
                # Only for actual failures — circuit breaker blocks are temporary and
                # the agent never ran, so the state should remain IN_PROGRESS.
                if agent_name == 'dev_environment_setup':
                    try:
                        from services.dev_container_state import dev_container_state, DevContainerStatus
                        dev_container_state.set_status(
                            project_name, DevContainerStatus.UNVERIFIED,
                            error_message=f"Setup failed: {str(e)[:200]}"
                        )
                        logger.info(
                            f"Reset dev container state to UNVERIFIED for {project_name} after setup failure"
                        )
                    except Exception as state_err:
                        logger.error(f"Failed to reset dev container state for {project_name}: {state_err}")

            raise

    def _build_execution_context(
        self,
        agent_name: str,
        project_name: str,
        task_id: str,
        task_context: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Build standardized execution context for agent"""
        from state_management.manager import StateManager
        from services.project_workspace import workspace_manager

        # Create state manager
        state_manager = StateManager(Path("orchestrator_data/state"))

        # Get project directory from workspace manager
        project_dir = workspace_manager.get_project_dir(project_name)

        # Build context with ALL required fields for agents
        # NOTE: stream_callback removed - docker-claude-wrapper.py handles all Claude log streaming
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
        repository = task_context.get('repository')
        
        # Get repo owner/org from project config
        project_name = task_context.get('project')
        if not project_name:
            logger.warning(f"No project name in task context, skipping GitHub post for {agent_name}")
            return
            
        try:
            project_config = config_manager.get_project_config(project_name)
            repo_owner = project_config.github.get('org') if project_config and hasattr(project_config, 'github') else None
            
            if not repository:
                repository = project_config.github.get('repo') if project_config and hasattr(project_config, 'github') else None
                
            if not repo_owner or not repository:
                logger.warning(f"Cannot determine repo owner/name for project {project_name}, skipping GitHub post")
                return
                
        except Exception as e:
            logger.warning(f"Error getting project config for {project_name}: {e}")
            return

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
            # Create GitHubIntegration with proper repo context
            github = GitHubIntegration(repo_owner=repo_owner, repo_name=repository)
            
            # Get reply_to_id for threaded conversations
            reply_to_id = task_context.get('reply_to_comment_id')
            
            if reply_to_id:
                logger.info(f"Posting threaded reply to comment {reply_to_id}")
            else:
                logger.info("Posting top-level comment (no reply_to_comment_id found)")

            # Post to GitHub (workspace-aware: issues or discussions)
            post_result = await github.post_agent_output(
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

    async def _failsafe_commit_check(
        self,
        project_name: str,
        agent_name: str,
        task_context: Dict[str, Any],
        task_id: str
    ):
        """
        Failsafe: Check for uncommitted changes when workspace_context is None.

        This handles scenarios where:
        - Claude Code partially stages files but doesn't commit
        - Agent makes changes but workspace finalization doesn't run
        - There's a mix of staged and unstaged changes

        Args:
            project_name: Name of the project
            agent_name: Name of the agent that ran
            task_context: Task context containing issue info
            task_id: Task identifier
        """
        import subprocess
        import glob

        try:
            project_dir = f"/workspace/{project_name}"
            issue_number = task_context.get('issue_number')

            logger.info(f"🔍 FAILSAFE: Checking git status in {project_dir}")

            # Get git status
            status_result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if status_result.returncode != 0:
                logger.error(f"❌ FAILSAFE: git status failed: {status_result.stderr}")
                return

            status_output = status_result.stdout.strip()

            if not status_output:
                logger.info("✅ FAILSAFE: No uncommitted changes found - workspace is clean")
                return

            # Parse git status output
            # Format: "XY filename" where X=staged, Y=unstaged
            # A  = added to index (staged)
            #  M = modified in working tree (unstaged)
            # M  = modified in index (staged)
            # ?? = untracked

            staged_files = []
            unstaged_files = []
            untracked_files = []

            for line in status_output.split('\n'):
                if not line:
                    continue

                status_code = line[:2]
                filename = line[3:]

                # Check staged status (first character)
                if status_code[0] in ('A', 'M', 'D', 'R', 'C'):
                    staged_files.append(filename)

                # Check unstaged status (second character)
                if status_code[1] in ('M', 'D'):
                    unstaged_files.append(filename)

                # Check untracked
                if status_code == '??':
                    untracked_files.append(filename)

            logger.warning(
                f"⚠️ FAILSAFE: Found uncommitted changes:\n"
                f"  Staged: {len(staged_files)} files\n"
                f"  Unstaged: {len(unstaged_files)} files\n"
                f"  Untracked: {len(untracked_files)} files"
            )

            # Clean up prompt files FIRST (critical to prevent them being committed)
            try:
                prompt_files = glob.glob(f"{project_dir}/.claude_prompt_*.txt")
                for prompt_file in prompt_files:
                    try:
                        import os
                        os.remove(prompt_file)
                        logger.info(f"🔍 FAILSAFE: Removed prompt file: {os.path.basename(prompt_file)}")
                    except Exception as e:
                        logger.warning(f"Failed to remove prompt file {prompt_file}: {e}")
            except Exception as e:
                logger.warning(f"Error during failsafe prompt file cleanup: {e}")

            # Decision logic based on what we found
            if staged_files and not unstaged_files:
                # Only staged files - safe to commit
                logger.info("🔍 FAILSAFE: Only staged files found - proceeding with commit")
                await self._failsafe_commit_staged(
                    project_dir, project_name, issue_number, agent_name, task_id, staged_files
                )

            elif unstaged_files and not staged_files:
                # Only unstaged files - stage them and commit
                logger.info("🔍 FAILSAFE: Only unstaged files found - staging and committing")
                await self._failsafe_stage_and_commit(
                    project_dir, project_name, issue_number, agent_name, task_id, unstaged_files
                )

            elif staged_files and unstaged_files:
                # MIXED STATE - this is the problematic scenario
                logger.warning(
                    f"⚠️ FAILSAFE: MIXED STATE detected - both staged and unstaged changes\n"
                    f"  This usually happens when Claude Code partially stages files.\n"
                    f"  Staged: {staged_files}\n"
                    f"  Unstaged: {unstaged_files}"
                )
                # Stage everything and commit
                await self._failsafe_stage_and_commit(
                    project_dir, project_name, issue_number, agent_name, task_id,
                    staged_files + unstaged_files
                )

            elif untracked_files:
                # Only untracked files - these might be artifacts, log cautiously
                logger.info(f"🔍 FAILSAFE: Only untracked files: {untracked_files}")
                # Don't commit untracked files automatically - might be build artifacts

        except Exception as e:
            logger.error(f"❌ FAILSAFE: Exception during commit check: {e}", exc_info=True)

    async def _failsafe_commit_staged(
        self,
        project_dir: str,
        project_name: str,
        issue_number: int,
        agent_name: str,
        task_id: str,
        staged_files: list
    ):
        """Commit already-staged files"""
        import subprocess

        try:
            commit_message = (
                f"Complete work for issue #{issue_number}\n\n"
                f"Agent: {agent_name}\n"
                f"Task: {task_id}\n\n"
                f"Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>\n"
                f"[orchestrator-commit]\n\n"
                f"(Failsafe commit: staged files)"
            )

            logger.info(f"🔍 FAILSAFE: Committing {len(staged_files)} staged files")

            # Skip pre-commit hooks for failsafe commits (same as normal orchestrator commits)
            commit_result = subprocess.run(
                ['git', 'commit', '-m', commit_message, '--no-verify'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if commit_result.returncode == 0:
                logger.info(f"✅ FAILSAFE: Successfully committed staged changes")

                # Try to push
                await self._failsafe_push(project_dir, project_name, issue_number)
            else:
                logger.error(f"❌ FAILSAFE: Commit failed: {commit_result.stderr}")

        except Exception as e:
            logger.error(f"❌ FAILSAFE: Exception during commit: {e}", exc_info=True)

    async def _failsafe_stage_and_commit(
        self,
        project_dir: str,
        project_name: str,
        issue_number: int,
        agent_name: str,
        task_id: str,
        all_files: list
    ):
        """Stage all changes and commit"""
        import subprocess

        try:
            logger.info(f"🔍 FAILSAFE: Staging {len(all_files)} files")

            # Stage all changes
            add_result = subprocess.run(
                ['git', 'add', '-A'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if add_result.returncode != 0:
                logger.error(f"❌ FAILSAFE: git add failed: {add_result.stderr}")
                return

            logger.info(f"✅ FAILSAFE: Staged all changes")

            # Commit
            commit_message = (
                f"Complete work for issue #{issue_number}\n\n"
                f"Agent: {agent_name}\n"
                f"Task: {task_id}\n\n"
                f"Co-Authored-By: Claude Sonnet 4.5 <noreply@anthropic.com>\n"
                f"[orchestrator-commit]\n\n"
                f"(Failsafe commit: auto-staged all changes)"
            )

            # Skip pre-commit hooks for failsafe commits (same as normal orchestrator commits)
            commit_result = subprocess.run(
                ['git', 'commit', '-m', commit_message, '--no-verify'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if commit_result.returncode == 0:
                logger.info(f"✅ FAILSAFE: Successfully committed all changes")

                # Try to push
                await self._failsafe_push(project_dir, project_name, issue_number)
            else:
                logger.error(f"❌ FAILSAFE: Commit failed: {commit_result.stderr}")

        except Exception as e:
            logger.error(f"❌ FAILSAFE: Exception during stage and commit: {e}", exc_info=True)

    async def _failsafe_push(
        self,
        project_dir: str,
        project_name: str,
        issue_number: int
    ):
        """Try to push committed changes"""
        import subprocess

        try:
            from services.git_workflow_manager import git_workflow_manager

            # Get current branch
            branch_result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            if branch_result.returncode != 0:
                logger.error(f"❌ FAILSAFE: Could not determine current branch: {branch_result.stderr}")
                return

            branch_name = branch_result.stdout.strip()
            logger.info(f"🔍 FAILSAFE: Pushing to branch: {branch_name}")

            # Push
            push_result = subprocess.run(
                ['git', 'push', 'origin', branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if push_result.returncode == 0:
                logger.info(f"✅ FAILSAFE: Successfully pushed to origin/{branch_name}")
            else:
                logger.warning(f"⚠️ FAILSAFE: Push failed (non-critical): {push_result.stderr}")

        except Exception as e:
            logger.warning(f"⚠️ FAILSAFE: Exception during push (non-critical): {e}")

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

            # Try to find the column for dev_environment_verifier
            verifier_column = 'Verification'  # Default
            try:
                project_config = config_manager.get_project_config(project_name)
                board_name = task_context.get('board')
                if board_name and board_name != 'system':
                    pipeline = next((p for p in project_config.pipelines if p.board_name == board_name), None)
                    if pipeline:
                        workflow = config_manager.get_workflow_template(pipeline.workflow)
                        for col in workflow.columns:
                            if col.agent == 'dev_environment_verifier':
                                verifier_column = col.name
                                break
            except Exception:
                pass

            # Create verifier task with reference to setup output
            task = Task(
                id=str(uuid.uuid4()),
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
                    'column': verifier_column,  # Pass the column so auto-advance works
                    'project': project_name,
                    'repository': project_name,
                    'automated_setup': True,
                    'auto_triggered': True,
                    'skip_workspace_prep': True,  # Verifier checks Docker image, no issue branch needed
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

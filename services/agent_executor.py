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
from monitoring.timestamp_utils import utc_now, utc_isoformat
from monitoring.observability import get_observability_manager
from pipeline.factory import PipelineFactory
from config.manager import config_manager
from services.github_integration import GitHubIntegration, AgentCommentFormatter

logger = logging.getLogger(__name__)

try:
    from monitoring.claude_code_breaker import ClaudeCodeRateLimitError, get_breaker as get_claude_code_breaker
except ImportError:
    class ClaudeCodeRateLimitError(Exception):  # type: ignore[assignment]
        pass
    def get_claude_code_breaker():  # type: ignore[misc]
        return None


def _release_lock_instruction(project_name: str, board_name: str, issue_number: int) -> str:
    """Shared recovery-instruction snippet for the GitHub comments below. The
    pipeline lock is durably marked retained-due-to-failure once these paths
    call end_pipeline_run(outcome="failed") — moving the card alone no longer
    re-triggers anything; a human must run this command."""
    return (
        f"`python scripts/release_lock.py --project {project_name} "
        f"--board \"{board_name}\" --issue {issue_number}` to release the pipeline "
        f"lock and allow a fresh attempt"
    )


def _build_branch_error_comment(
    e, project_name: str, board_name: str, issue_number: int, marked_successfully: bool = True
) -> str:
    """Build a GitHub comment body for StaleBranchError or BranchPullFailedError.

    marked_successfully should reflect whether mark_failed() actually durably
    retained the lock — when False, the comment must not claim the lock is
    retained, since that would itself be a silent-failure regression."""
    from services.feature_branch_manager import StaleBranchError
    release_instruction = _release_lock_instruction(project_name, board_name, issue_number)
    lock_status_line = (
        f"_Pipeline lock retained — no new work will start on this issue until "
        f"the lock is released._"
        if marked_successfully else
        f"_⚠️ The pipeline lock could NOT be durably marked retained (both Redis "
        f"and YAML writes failed) — this issue may be silently re-dispatched. "
        f"Please investigate immediately._"
    )
    if isinstance(e, StaleBranchError):
        return (
            f"## Branch Rebase Required\n\n"
            f"The feature branch `{e.branch_name}` is **{e.commits_behind} commits behind main** "
            f"and cannot proceed without a rebase.\n\n"
            f"**Rebase command:**\n"
            f"```bash\n"
            f"git checkout {e.branch_name}\n"
            f"git fetch origin\n"
            f"git rebase origin/main\n"
            f"# Resolve any conflicts\n"
            f"git push --force-with-lease\n"
            f"```\n\n"
            f"After rebasing, run {release_instruction}.\n\n"
            f"{lock_status_line}"
        )
    else:
        return (
            f"## Branch Creation Blocked\n\n"
            f"Failed to pull the latest `main` before creating the feature branch. "
            f"The local checkout may be out of date or there may be a network/auth issue.\n\n"
            f"**To diagnose:**\n"
            f"1. Check `docker-compose logs orchestrator` for git errors\n"
            f"2. Verify network/SSH access to the repository\n"
            f"3. Once resolved, run {release_instruction}\n\n"
            f"{lock_status_line}"
        )


def _build_workspace_prep_error_comment(
    e, project_name: str, board_name: str, issue_number: int, marked_successfully: bool = True
) -> str:
    """
    Build a GitHub comment body for the catch-all git-based workspace preparation
    failure (workspace_preparation_git_failure) — e.g. a stash that can't be
    written because the shared workspace already has unresolved/unmerged paths.

    Unlike StaleBranchError/BranchPullFailedError, this failure doesn't carry
    structured detail about which files or branch are involved, so the comment
    points at the shared checkout in general rather than a specific remediation
    command.

    marked_successfully should reflect whether mark_failed() actually durably
    retained the lock — see _build_branch_error_comment for the same contract.
    """
    release_instruction = _release_lock_instruction(project_name, board_name, issue_number)
    lock_status_line = (
        f"_Pipeline lock retained — no new work will start on this project's board "
        f"until the lock is released._"
        if marked_successfully else
        f"_⚠️ The pipeline lock could NOT be durably marked retained (both Redis "
        f"and YAML writes failed) — this project's board may be silently "
        f"re-dispatched. Please investigate immediately._"
    )
    return (
        f"## Workspace Preparation Failed\n\n"
        f"Git workspace preparation failed and the agent was halted before making "
        f"any changes, to avoid committing to the wrong branch:\n\n"
        f"```\n{str(e)[:1500]}\n```\n\n"
        f"This usually means the project's shared git checkout is stuck in an "
        f"unresolved state (e.g. an incomplete merge or conflicted files) — "
        f"retrying will fail identically, including for any other issue that "
        f"touches this workspace.\n\n"
        f"**To diagnose and repair:**\n"
        f"1. `docker-compose exec orchestrator bash -c \"cd /workspace/<project> && git status\"`\n"
        f"2. Resolve or abort any unresolved merge (`git merge --abort`) and/or "
        f"discard local state (`git reset --hard origin/<branch>`)\n"
        f"3. Once the workspace is clean, run {release_instruction}\n\n"
        f"{lock_status_line}"
    )


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

        # Extract pipeline_run_id from task_context for event tracking
        pipeline_run_id = task_context.get('pipeline_run_id')
        logger.info(f"[DIAGNOSTIC] Extracted pipeline_run_id from task_context: {pipeline_run_id} (type: {type(pipeline_run_id)})")
        logger.info(f"[DIAGNOSTIC] Full task_context keys: {list(task_context.keys())}")

        # Emit task received event
        self.obs.emit_task_received(agent_name, task_id, project_name, task_context,
                                    execution_type=execution_type,
                                    pipeline_run_id=pipeline_run_id)

        # Resolve this run's git branch and isolated epic worktree so the
        # container-mount directory built below (via _build_execution_context) is
        # the epic's isolated worktree instead of the shared base clone. If a
        # caller already resolved this earlier in the same dispatch (e.g. a
        # repair cycle stashed 'epic_id' / 'branch_name' into task_context before
        # calling us with skip_workspace_prep=True), reuse it rather than
        # re-resolving.
        #
        # Two independent gates used to live here (issue #46): a 'discussions'-
        # only allowlist calling FeatureBranchManager.resolve_epic_id()/
        # resolve_epic_branch_name() directly, which explicitly excluded
        # 'issues'/'hybrid' because IssuesWorkspaceContext/HybridWorkspaceContext's
        # prepare_execution() independently resolved and checked out its OWN
        # branch on the shared base clone -- a second, uncoordinated resolution
        # that could disagree with this one and collide (git refuses to check out
        # a branch already checked out in another worktree). #122 (WI-C of #119)
        # removes that second resolution entirely: both workspace-context classes
        # now read the SAME PipelineRunManager.resolve_workspace() result this
        # block produces, via the pipeline_run threaded into
        # WorkspaceContextFactory.create() below. There is nothing left to
        # collide with, so 'issues'/'hybrid' resolution is unconditional here too
        # -- no per-project opt-in, unlike 'discussions' below (#52's
        # worktree_isolation_enabled flag was only ever a rollout control for the
        # allowlist this replaces, not a requirement of resolve_workspace() itself).
        workspace_type_for_epic_gate = task_context.get('workspace_type', 'issues')
        epic_id = task_context.get('epic_id')
        epic_branch_name = task_context.get('branch_name')
        pipeline_run_for_workspace = None

        if (
            epic_id is None
            and 'issue_number' in task_context
            and not task_context.get('skip_workspace_prep', False)
            and workspace_type_for_epic_gate in ('issues', 'hybrid')
        ):
            project_config_for_workspace = config_manager.get_project_config(project_name)
            repo_owner = None
            repo_name = None
            if project_config_for_workspace and hasattr(project_config_for_workspace, 'github'):
                repo_owner = project_config_for_workspace.github.get('org')
                repo_name = project_config_for_workspace.github.get('repo')

            if pipeline_run_id and repo_owner and repo_name:
                from services.github_integration import GitHubIntegration
                from services.pipeline_run import get_pipeline_run_manager

                pipeline_run_manager_for_workspace = get_pipeline_run_manager()
                pipeline_run_for_workspace = pipeline_run_manager_for_workspace.get_pipeline_run(pipeline_run_id)

                if pipeline_run_for_workspace is None:
                    # NOT a fallback: pipeline_run_for_workspace stays None, which
                    # WorkspaceContextFactory/IssuesWorkspaceContext.prepare_execution()
                    # (issue #122/WI-C) treats as a hard ValueError, not a silent
                    # base-clone degradation -- a silent fallback here would
                    # reintroduce exactly the per-caller resolution inconsistency
                    # #119 exists to eliminate. If this warning fires in practice,
                    # that ValueError is the actual, intended outcome.
                    logger.warning(
                        f"No pipeline run found for pipeline_run_id={pipeline_run_id!r} "
                        f"({project_name} issue #{task_context.get('issue_number')}) -- "
                        "cannot resolve an isolated workspace. This dispatch will fail "
                        "when the workspace context requires a resolved pipeline run."
                    )
                else:
                    gh_integration_for_workspace = GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)
                    # Not swallowed -- resolve_workspace()'s own documented
                    # contract requires ValueError/RuntimeError to reach whatever
                    # generic failure handling this dispatch already has, this
                    # codebase's established uniform retry/escalation pattern,
                    # not special-casing errors that merely look permanent.
                    # BUT this block runs well before this method's own big
                    # try/except (which is what actually calls
                    # work_execution_tracker.record_execution_outcome() on
                    # failure) -- unlike project_monitor.py's repair-cycle call
                    # site, where the equivalent resolve_workspace() call already
                    # sits inside that method's own enclosing try/except. Without
                    # this explicit record here (code review finding, issue
                    # #124), a resolve_workspace() failure at this point would
                    # propagate out of execute_agent() with the record_execution_
                    # start() "in_progress" entry never paired with a terminal
                    # outcome -- the exact "escalation never fires" bug class
                    # fixed by 76e7adc for the repair-cycle path, reproduced here
                    # for ordinary dispatch.
                    try:
                        pipeline_run_for_workspace = await pipeline_run_manager_for_workspace.resolve_workspace(
                            pipeline_run_for_workspace, gh_integration_for_workspace, workspace_type_for_epic_gate
                        )
                    except Exception as resolve_err:
                        if 'issue_number' in task_context:
                            from services.work_execution_state import work_execution_tracker
                            try:
                                work_execution_tracker.record_execution_outcome(
                                    issue_number=task_context['issue_number'],
                                    column=task_context.get('column', 'unknown'),
                                    agent=agent_name,
                                    outcome='failure',
                                    project_name=project_name,
                                    error=f"Workspace resolution failed: {resolve_err}"
                                )
                            except Exception as record_err:
                                logger.warning(
                                    f"Failed to record workspace-resolution failure outcome for "
                                    f"{project_name}/#{task_context['issue_number']}: {record_err}"
                                )
                        raise
                    epic_id = pipeline_run_for_workspace.epic_id
                    epic_branch_name = pipeline_run_for_workspace.branch_name
                    task_context['epic_id'] = epic_id
                    task_context['project_dir'] = pipeline_run_for_workspace.project_dir
            else:
                # NOT a fallback -- see the comment above on the pipeline_run_for_workspace
                # is None branch; the same "this dispatch will fail" outcome applies here.
                logger.warning(
                    f"Cannot resolve an isolated workspace for {project_name} "
                    f"issue #{task_context.get('issue_number')} (workspace_type="
                    f"{workspace_type_for_epic_gate!r}): missing pipeline_run_id or repo "
                    "org/repo. This dispatch will fail when the workspace context "
                    "requires a resolved pipeline run."
                )

        elif (
            epic_id is None
            and 'issue_number' in task_context
            and not task_context.get('skip_workspace_prep', False)
            and workspace_type_for_epic_gate == 'discussions'
        ):
            # This workspace-type allowlist is necessary but not sufficient (#52): even
            # for 'discussions' dispatch, isolation only actually happens for a project
            # that has opted in via ProjectConfig.worktree_isolation_enabled (checked
            # further down, once project_config_for_epic is loaded) -- so this doesn't
            # roll out isolation to every planning_design project at once, only to
            # whichever project(s) explicitly enable it. Unlike 'issues'/'hybrid' above,
            # 'discussions' is verified git-free (supports_git_operations=False, no call
            # to prepare_feature_branch/resolve_workspace from DiscussionsWorkspaceContext)
            # and was never at risk of the collision #122 fixed for 'issues'/'hybrid' --
            # it keeps its own pre-existing, best-effort resolution here rather than
            # moving onto resolve_workspace() (which is scoped to 'issues'/'hybrid' only;
            # see its own docstring).
            try:
                project_config_for_epic = config_manager.get_project_config(project_name)
                # Per-project opt-in (#52): the workspace_type allowlist above says
                # this TYPE of dispatch is safe to isolate, but isolation still only
                # actually happens for a project that has explicitly opted in --
                # defaults to False so every project keeps today's shared-base-clone
                # behavior until a chosen pilot project sets this in its config.
                if not getattr(project_config_for_epic, 'worktree_isolation_enabled', False):
                    project_config_for_epic = None
                repo_owner = None
                repo_name = None
                if project_config_for_epic and hasattr(project_config_for_epic, 'github'):
                    repo_owner = project_config_for_epic.github.get('org')
                    repo_name = project_config_for_epic.github.get('repo')
                if repo_owner and repo_name:
                    from services.github_integration import GitHubIntegration
                    from services.feature_branch_manager import feature_branch_manager
                    gh_integration_for_epic = GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)
                    epic_id = await feature_branch_manager.resolve_epic_id(
                        gh_integration_for_epic, task_context['issue_number'], project=project_name
                    )
                    # resolve_epic_branch_name is sync (a plain read-only git query on a
                    # cache miss) -- run it off the event loop thread so it can't stall
                    # other coroutines scheduled on this loop.
                    resolved_branch = await asyncio.to_thread(
                        feature_branch_manager.resolve_epic_branch_name, project_name, epic_id
                    )
                    epic_branch_name = resolved_branch or feature_branch_manager.create_feature_branch_name(int(epic_id), "")
                    task_context['epic_id'] = epic_id
            except Exception as epic_resolve_err:
                logger.warning(
                    f"Could not resolve epic worktree target for {project_name} "
                    f"issue #{task_context.get('issue_number')}: {epic_resolve_err}. "
                    f"Falling back to the shared base clone for this execution."
                )
                epic_id = None
                epic_branch_name = None

        # Build execution context with ALL required fields
        # NOTE: Stream callback removed - docker-claude-wrapper.py handles all Claude log streaming
        execution_context = self._build_execution_context(
            agent_name=agent_name,
            project_name=project_name,
            task_id=task_id,
            task_context=task_context,
            epic_id=epic_id,
            branch_name=epic_branch_name
        )

        self._apply_frozen_session_resume(execution_context, task_context, project_name, agent_name)

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
                            github_integration=gh_integration,
                            # The same PipelineRun this method's epic-resolution
                            # block (above) already resolved via
                            # resolve_workspace() -- IssuesWorkspaceContext/
                            # HybridWorkspaceContext read branch_name/project_dir
                            # directly off of it (#122). Idempotent to call
                            # resolve_workspace() again against it, which those
                            # classes' prepare_execution() does.
                            pipeline_run=pipeline_run_for_workspace
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
                            # The directory the agent actually works in -- the shared
                            # base clone for workspace types that stay there, or the
                            # resolved isolated epic worktree for 'issues'/'hybrid'
                            # (issue #122/WI-C review finding: this was hardcoded to
                            # the shared base clone, which is no longer where 'issues'/
                            # 'hybrid' dispatch's branch actually lives -- verifying
                            # against it there would fail this check on every such
                            # dispatch, halting execution that was actually prepared
                            # correctly).
                            project_dir = str(workspace_context.get_working_directory())

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
                from services.feature_branch_manager import StaleBranchError, BranchPullFailedError

                # Stale branch and pull failures require human intervention:
                # end the pipeline run with retain_lock=True and post a GitHub comment,
                # then re-raise as NonRetryableAgentError so the caller does not retry.
                if isinstance(e, (StaleBranchError, BranchPullFailedError)):
                    issue_number = task_context.get('issue_number')
                    logger.error(
                        f"Branch error during workspace preparation for {project_name} "
                        f"issue #{issue_number}: {e}"
                    )

                    # Mark the run failed FIRST (before posting the comment) — via
                    # the shared mark_failed() entry point, not a bare
                    # end_pipeline_run(outcome="failed") whose return was
                    # previously discarded — so the comment can accurately say
                    # whether the lock is actually retained rather than
                    # unconditionally claiming so (see mark_failed's docstring:
                    # posting "the lock is retained" when it isn't would itself
                    # be a silent-failure regression).
                    board_name = task_context.get('board') or '<board_name>'
                    marked_ok = True
                    if issue_number:
                        try:
                            from services.pipeline_run import get_pipeline_run_manager
                            marked_ok = get_pipeline_run_manager().mark_failed(
                                project=project_name,
                                board=board_name,
                                issue_number=issue_number,
                                reason=str(e),
                            )
                            if not marked_ok:
                                logger.critical(
                                    f"Pipeline lock for {project_name}/#{issue_number} could NOT "
                                    f"be durably marked failed after a branch error — this issue "
                                    f"may be silently re-dispatched."
                                )
                        except Exception as end_err:
                            marked_ok = False
                            logger.error(
                                f"Failed to end pipeline run after branch error for "
                                f"{project_name} issue #{issue_number}: {end_err}"
                            )

                    # Post GitHub comment so the issue has visible context
                    if issue_number:
                        try:
                            from services.github_integration import GitHubIntegration
                            project_config = config_manager.get_project_config(project_name)
                            github = GitHubIntegration(
                                repo_owner=project_config.github['org'],
                                repo_name=project_config.github['repo']
                            )
                            comment = _build_branch_error_comment(
                                e, project_name, board_name, issue_number, marked_successfully=marked_ok
                            )
                            await github.post_comment(issue_number, comment, pipeline_run_id=pipeline_run_id)
                        except Exception as comment_err:
                            logger.error(
                                f"Failed to post branch error comment to issue #{issue_number}: {comment_err}"
                            )

                    raise NonRetryableAgentError(str(e)) from e

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
                        issue_number = task_context.get('issue_number')
                        logger.error(
                            f"Failed to prepare git-based workspace: {e}. "
                            f"Halting execution and retaining the pipeline lock — this is "
                            f"treated as a broken shared workspace, not a transient agent "
                            f"failure, because it will fail identically for every other "
                            f"issue that touches this project's checkout until repaired.",
                            exc_info=True
                        )

                        # Emit error event if this is part of a review cycle
                        if pipeline_run_id and issue_number:
                            try:
                                from monitoring.decision_events import DecisionEventEmitter
                                decision_emitter = DecisionEventEmitter(self.obs)

                                decision_emitter.emit_error_decision(
                                    error_type="workspace_preparation_git_failure",
                                    error_message=str(e),
                                    context={
                                        'agent': agent_name,
                                        'project': project_name,
                                        'issue_number': issue_number,
                                        'branch_name': task_context.get('branch_name'),
                                        'workspace_type': task_context.get('workspace_type', 'issues')
                                    },
                                    recovery_action="Agent execution halted; pipeline lock retained pending manual git repair",
                                    success=False,
                                    project=project_name,
                                    pipeline_run_id=pipeline_run_id
                                )
                            except Exception as emit_error:
                                logger.error(f"Failed to emit workspace prep error event: {emit_error}", exc_info=True)

                        # Same treatment as StaleBranchError/BranchPullFailedError above:
                        # this is not a per-issue-retryable failure — the underlying git
                        # workspace is broken and every subsequent issue that touches it
                        # will hit the exact same error. Post a comment, retain the
                        # pipeline lock so nothing else is dispatched against this
                        # project's board, and raise non-retryably so neither the
                        # in-process retry loop nor the zombie watchdog's auto-retry
                        # accounting kicks in and silently releases the lock again.
                        if issue_number:
                            board_name = task_context.get('board') or '<board_name>'
                            marked_ok = True
                            try:
                                from services.pipeline_run import get_pipeline_run_manager
                                marked_ok = get_pipeline_run_manager().mark_failed(
                                    project=project_name,
                                    board=board_name,
                                    issue_number=issue_number,
                                    reason=f"Git workspace preparation failed: {e}",
                                )
                                if not marked_ok:
                                    logger.critical(
                                        f"Pipeline lock for {project_name}/#{issue_number} could NOT "
                                        f"be durably marked failed after a workspace prep error — "
                                        f"this issue may be silently re-dispatched."
                                    )
                            except Exception as end_err:
                                marked_ok = False
                                logger.error(
                                    f"Failed to end pipeline run after workspace prep error for "
                                    f"{project_name} issue #{issue_number}: {end_err}"
                                )

                            try:
                                from services.github_integration import GitHubIntegration
                                project_config = config_manager.get_project_config(project_name)
                                github = GitHubIntegration(
                                    repo_owner=project_config.github['org'],
                                    repo_name=project_config.github['repo']
                                )
                                comment = _build_workspace_prep_error_comment(
                                    e, project_name, board_name, issue_number, marked_successfully=marked_ok
                                )
                                await github.post_comment(issue_number, comment, pipeline_run_id=pipeline_run_id)
                            except Exception as comment_err:
                                logger.error(
                                    f"Failed to post workspace prep error comment to issue "
                                    f"#{issue_number}: {comment_err}"
                                )

                        raise NonRetryableAgentError(f"Git workspace preparation failed: {e}") from e

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
        cycle_stack = task_context.get('cycle_stack')

        # Collect pipeline context files for observability (best-effort)
        _context_files = None
        _context_dir = task_context.get('pipeline_context_dir')
        if _context_dir:
            try:
                from services.pipeline_context_writer import PipelineContextWriter as _PCW
                _writer = _PCW.from_existing(_context_dir)
                if _writer.exists():
                    _context_files = _writer.list_files()
            except Exception:
                pass

        agent_execution_id = self.obs.emit_agent_initialized(
            agent_name, task_id, project_name, agent_config, branch_name, container_name, pipeline_run_id,
            execution_type=execution_type,
            cycle_stack=cycle_stack,
            context_files=_context_files
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
                    claude_breaker = get_claude_code_breaker()
                    if claude_breaker and claude_breaker.is_open():
                        reset_time = claude_breaker.reset_time
                        if reset_time:
                            from datetime import datetime, timezone
                            time_until = (reset_time - datetime.now(timezone.utc)).total_seconds()
                            logger.warning(
                                f"⏸️  Claude Code circuit breaker is OPEN. Agent {agent_name} execution paused. "
                                f"Tokens reset in {time_until:.0f}s at {reset_time.strftime('%I:%M %p')}"
                            )
                            raise ClaudeCodeRateLimitError(
                                f"Claude Code circuit breaker is OPEN. Resets at {reset_time.strftime('%I:%M %p')}. "
                                f"This is a systemic token limit issue, not an agent failure."
                            )
                        else:
                            logger.warning(f"⏸️  Claude Code circuit breaker is OPEN. Agent {agent_name} execution paused.")
                            raise ClaudeCodeRateLimitError(
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

                    # ClaudeCodeRateLimitError: systemic token limit — trip breaker if not open, never retry
                    if isinstance(e, ClaudeCodeRateLimitError):
                        claude_breaker = get_claude_code_breaker()
                        if claude_breaker and not claude_breaker.is_open():
                            claude_breaker.trip()
                        raise

                    # Check if this is a Claude Code breaker failure (systemic issue)
                    error_message = str(e)
                    is_claude_breaker_failure = (
                        "Claude Code circuit breaker is OPEN" in error_message
                    )

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
            if isinstance(result, dict):
                output_text, output_unexpected = self._extract_markdown_output(agent_name, result)
            else:
                output_text, output_unexpected = None, True

            duration_ms = (time.time() - start_time) * 1000
            self.obs.emit_agent_completed(
                agent_name, task_id, project_name, duration_ms, True, None, pipeline_run_id, output_text, agent_execution_id,
                execution_type=execution_type
            )

            if output_unexpected and output_text is not None:
                from monitoring.observability import EventType
                self.obs.emit(EventType.AGENT_OUTPUT_FORMAT_UNEXPECTED, agent_name, task_id, project_name, {
                    'result_keys': list(result.keys()) if isinstance(result, dict) else [],
                    'output_length': len(output_text),
                    'message': f"Agent {agent_name} output was extracted via fallback, not a known output key",
                }, pipeline_run_id, execution_type=execution_type)

            # If dev_environment_setup completed successfully, queue verifier,
            # passing along its actual output so the verifier can scrutinize the
            # real narrative (e.g. "no change needed" claims) rather than a stand-in.
            if agent_name == 'dev_environment_setup':
                await self._queue_environment_verifier(project_name, task_context, setup_output=output_text)

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
                    from services.git_workflow_manager import PushFailedError
                    if isinstance(e, PushFailedError):
                        # Push was rejected — agent did real work but it cannot reach origin.
                        # Block the pipeline: retain the lock and fail visibly so a human
                        # can inspect, force-push or reset, and re-queue the issue.
                        issue_number = task_context.get('issue_number')
                        logger.error(
                            f"❌ PUSH FAILURE for {project_name} issue #{issue_number}: {e}"
                        )

                        if issue_number:
                            board_name = task_context.get('board') or '<board_name>'
                            marked_ok = True
                            try:
                                from services.pipeline_run import get_pipeline_run_manager
                                marked_ok = get_pipeline_run_manager().mark_failed(
                                    project=project_name,
                                    board=board_name,
                                    issue_number=issue_number,
                                    reason=str(e),
                                )
                                if not marked_ok:
                                    logger.critical(
                                        f"Pipeline lock for {project_name}/#{issue_number} could NOT "
                                        f"be durably marked failed after a push failure — this issue "
                                        f"may be silently re-dispatched."
                                    )
                            except Exception as end_err:
                                marked_ok = False
                                logger.error(f"Failed to end pipeline run after push failure: {end_err}")

                            lock_status_line = (
                                f"_Pipeline lock retained — no further automated work will run on "
                                f"this issue until the lock is released._"
                                if marked_ok else
                                f"_⚠️ The pipeline lock could NOT be durably marked retained (both "
                                f"Redis and YAML writes failed) — this issue may be silently "
                                f"re-dispatched. Please investigate immediately._"
                            )
                            try:
                                from services.github_integration import GitHubIntegration
                                project_config = config_manager.get_project_config(project_name)
                                github = GitHubIntegration(
                                    repo_owner=project_config.github['org'],
                                    repo_name=project_config.github['repo']
                                )
                                await github.post_comment(
                                    issue_number,
                                    f"## ❌ Push Failed — Pipeline Blocked\n\n"
                                    f"The agent completed its work and committed changes locally, "
                                    f"but the push to `origin` was rejected.\n\n"
                                    f"**Reason:** {e}\n\n"
                                    f"**To recover:**\n"
                                    f"1. Inspect the local commits: `git log origin/{task_context.get('branch_name', '<branch>')}..HEAD`\n"
                                    f"2. Force-push if the changes are correct: `git push --force-with-lease`\n"
                                    f"3. Or reset and let the pipeline retry: `git reset --hard origin/<branch>`\n"
                                    f"4. Run `python scripts/release_lock.py --project {project_name} "
                                    f"--board \"{board_name}\" --issue {issue_number}` "
                                    f"to release the pipeline lock (moving the card alone no longer "
                                    f"re-triggers anything) once ready to continue — see below for "
                                    f"whether it is actually durably retained.\n\n"
                                    f"{lock_status_line}",
                                    pipeline_run_id=pipeline_run_id
                                )
                            except Exception as comment_err:
                                logger.error(f"Failed to post push-failure comment: {comment_err}")

                        from agents.non_retryable import NonRetryableAgentError
                        raise NonRetryableAgentError(str(e)) from e

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
                            from services.git_workflow_manager import PushFailedError
                            if isinstance(failsafe_error, PushFailedError):
                                raise  # Re-raise into outer PushFailedError handler
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

                # Check if there are any uncommitted changes in the workspace.
                # Skip for repair_test agents — they run tests and return JSON; any
                # workspace writes are test artifacts, not code changes to commit.
                if execution_type == "repair_test":
                    logger.info("Skipping failsafe commit check for repair_test execution type")
                elif 'issue_number' in task_context:
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
                    if execution.get('outcome') in ['success', 'failure', 'cancelled', 'frozen']:
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

            # CancellationError: deliberate stop. cancel_issue_work() records outcome
            # when it's the source, but end_pipeline_run() does NOT — so defensively
            # record 'cancelled' here to prevent stuck in_progress entries.
            from services.cancellation import CancellationError
            if isinstance(e, CancellationError):
                logger.info(f"Agent {agent_name} cancelled after {duration_ms:.0f}ms: {e}")
                if 'issue_number' in task_context:
                    try:
                        from services.work_execution_state import work_execution_tracker
                        column = task_context.get('column', 'unknown')
                        work_execution_tracker.record_execution_outcome(
                            issue_number=task_context['issue_number'],
                            column=column,
                            agent=agent_name,
                            outcome='cancelled',
                            project_name=project_name,
                            error=str(e)
                        )
                    except Exception as cleanup_err:
                        logger.warning(
                            f"Failed to record cancelled outcome for {agent_name}: {cleanup_err}"
                        )
                raise

            error_message = str(e)
            is_claude_breaker_failure = (
                isinstance(e, ClaudeCodeRateLimitError) or
                "Claude Code circuit breaker is OPEN" in error_message
            )

            if is_claude_breaker_failure:
                # This is a systemic issue (token limits), not an agent failure
                # Don't emit agent_failed - emit a paused/frozen event instead
                logger.warning(
                    f"Agent {agent_name} frozen by Claude Code circuit breaker after {duration_ms:.0f}ms. "
                    f"Not counting as agent failure. Pipeline will resume when tokens reset."
                )

                # Piggyback the captured Claude Code session_id (if any) onto this same
                # write — see docker_runner.py's _rate_limit_signal capture and
                # ClaudeCodeRateLimitError.claude_session_id. Only meaningful if the
                # rejected call had already made real progress (prior_progress=True);
                # the active-resume step decides whether to actually use it.
                claude_session_id = getattr(e, 'claude_session_id', None) if getattr(e, 'prior_progress', False) else None

                # Record execution outcome as 'frozen' to enable automatic recovery
                # Bug fix: Previously left in 'in_progress' which caused stuck issues
                if 'issue_number' in task_context:
                    from services.work_execution_state import work_execution_tracker
                    column = task_context.get('column', 'unknown')

                    work_execution_tracker.record_execution_outcome(
                        issue_number=task_context['issue_number'],
                        column=column,
                        agent=agent_name,
                        outcome='frozen',
                        project_name=project_name,
                        error=error_message,
                        claude_session_id=claude_session_id
                    )
                    logger.info(
                        f"Recorded 'frozen' outcome for issue #{task_context['issue_number']} "
                        f"to enable automatic retry when circuit breaker closes"
                        + (f" (captured resumable session {claude_session_id})" if claude_session_id else "")
                    )
                else:
                    logger.warning(
                        f"Cannot record frozen outcome for {agent_name}: missing issue_number in task_context"
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
                        if execution.get('outcome') in ['success', 'failure', 'cancelled', 'frozen']:
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

    def _apply_frozen_session_resume(
        self,
        execution_context: Dict[str, Any],
        task_context: Dict[str, Any],
        project_name: str,
        agent_name: str
    ):
        """
        Frozen-session resume fork: if the immediately-preceding execution for
        this exact (project, issue, column, agent) was frozen by the Claude Code
        breaker and captured a session_id with evidence of prior progress,
        resume that session with a short continuation prompt instead of
        rebuilding the stage's normal prompt from scratch. The common case —
        first-turn rejections with nothing accumulated — has no captured
        session_id and this is a no-op. Respects a caller-supplied direct_prompt
        (e.g. pr_review_stage's Phase 2 verification calls) by never overriding
        it. claude_session_id must be set on execution_context itself (not just
        task_context) — that's the top-level dict docker_runner.py's --resume
        wiring actually reads.

        Mutates execution_context and task_context in place.
        """
        if 'issue_number' not in task_context or task_context.get('direct_prompt'):
            return

        try:
            from services.work_execution_state import work_execution_tracker
            resumable_session_id = work_execution_tracker.get_resumable_frozen_session(
                project_name,
                task_context['issue_number'],
                task_context.get('column', 'unknown'),
                agent_name
            )
            if resumable_session_id:
                execution_context['claude_session_id'] = resumable_session_id
                task_context['direct_prompt'] = (
                    "Please continue exactly where you left off. Your previous turn "
                    "was interrupted by a Claude Code usage limit before it could "
                    "finish — pick up the task from where you stopped. "
                    "IMPORTANT: your original task instructions required your final "
                    "response to end with a specific status marker section (e.g. a "
                    "'### Status' heading followed by a bold decision keyword such as "
                    "'**APPROVED**' or '**BLOCKED**') for automated parsing. That "
                    "requirement still applies to this continuation — your final "
                    "response in this turn MUST still include that exact marker "
                    "section, formatted exactly as your original instructions "
                    "specified, even though this is a resumed session."
                )
                logger.info(
                    f"Resuming Claude Code session {resumable_session_id} for "
                    f"{agent_name} on {project_name}/#{task_context['issue_number']} "
                    f"(captured from a frozen execution with prior progress)"
                )
        except Exception as e:
            logger.warning(f"Could not check for a resumable frozen session: {e}")

    def _build_execution_context(
        self,
        agent_name: str,
        project_name: str,
        task_id: str,
        task_context: Dict[str, Any],
        epic_id: Optional[str] = None,
        branch_name: Optional[str] = None
    ) -> Dict[str, Any]:
        """Build standardized execution context for agent.

        epic_id/branch_name (resolved by the caller -- see execute_agent) scope
        the returned work_dir to the epic's isolated git worktree instead of the
        shared base clone. Omitting epic_id preserves the pre-worktree-isolation
        behavior exactly (plain base-clone path, no side effects).
        """
        from pathlib import Path
        from services.project_workspace import workspace_manager

        # If the caller already resolved a concrete directory (e.g. a repair
        # cycle running in its own separate container, handed the epic worktree
        # path its originating orchestrator process already created/reused via
        # task_context['project_dir']), reuse it as-is rather than re-deriving
        # via epic_id here. Re-deriving would run against a *fresh*, empty
        # in-process worktree cache (ProjectWorkspaceManager's cache is
        # process-local) and could attempt to `git worktree add` a worktree that
        # already exists on disk -- which is not idempotent and would raise.
        existing_project_dir = task_context.get('project_dir')
        if existing_project_dir:
            project_dir = Path(existing_project_dir)
        else:
            # Get project directory from workspace manager -- an isolated
            # per-epic worktree when epic_id is known, otherwise the shared base
            # clone.
            project_dir = workspace_manager.get_project_dir(
                project_name, epic_id=epic_id, branch_name=branch_name
            )

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

        # Pass the hard timeout into context so docker_runner can enforce it
        if hasattr(agent_config, 'timeout') and agent_config.timeout:
            context['agent_hard_timeout'] = agent_config.timeout

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
        markdown_output, _ = self._extract_markdown_output(agent_name, result)

        if not markdown_output:
            logger.warning(f"No markdown output found for {agent_name}, skipping GitHub post")
            return

        # Persist a durable local copy unconditionally, before attempting the GitHub
        # post. GitHub writes can fail transiently (rate limits, open circuit breaker)
        # — without this, a failed post here silently strands any downstream stage
        # that depends on finding this stage's output (see
        # PipelineContextWriter.find_latest_output_for_issue, used as a fallback by
        # ProjectMonitor._get_issue_context when the GitHub comment scrape is empty).
        pipeline_run_id = task_context.get('pipeline_run_id')
        if pipeline_run_id:
            try:
                from services.pipeline_context_writer import PipelineContextWriter
                PipelineContextWriter.setup(issue_number, pipeline_run_id).write_stage_output(
                    agent_name, markdown_output
                )
            except Exception as e:
                logger.warning(
                    f"Could not persist local stage-output fallback for {agent_name} "
                    f"on issue #{issue_number}: {e}"
                )

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

            # Post to GitHub (workspace-aware: issues or discussions), with a short
            # bounded retry for transient failures. Does not retry against an open
            # rate-limit circuit breaker — its reset window is typically minutes to
            # an hour away (see GitHubBreaker.trip), so retrying immediately would
            # just fail again and hold up finalization for no benefit; the durable
            # local copy written above is the fallback of record for that case.
            post_result = await github.post_agent_output(
                task_context,
                comment,
                reply_to_id=reply_to_id
            )

            attempt = 1
            while (
                not post_result.get('success')
                and attempt < 3
                and 'circuit breaker open' not in str(post_result.get('error', '')).lower()
            ):
                backoff_seconds = 3 * attempt
                logger.warning(
                    f"Retrying {agent_name} GitHub post for issue #{issue_number} "
                    f"in {backoff_seconds}s (attempt {attempt + 1}/3): {post_result.get('error')}"
                )
                await asyncio.sleep(backoff_seconds)
                post_result = await github.post_agent_output(
                    task_context,
                    comment,
                    reply_to_id=reply_to_id
                )
                attempt += 1

            if post_result.get('success'):
                logger.info(f"Posted {agent_name} output to GitHub (workspace: {workspace_type}, issue: #{issue_number})")

                # Track comment timestamp for feedback loop
                from services.feedback_manager import FeedbackManager
                feedback_manager = FeedbackManager()
                feedback_manager.set_last_agent_comment_time(
                    issue_number,
                    agent_name,
                    utc_isoformat(),
                    project=project_name
                )
            else:
                logger.error(
                    f"Failed to post {agent_name} output to GitHub for issue #{issue_number} "
                    f"after {attempt} attempt(s): {post_result.get('error')}. "
                    f"A durable local copy was persisted for downstream stages to fall back on "
                    f"(see PipelineContextWriter.find_latest_output_for_issue)."
                )

        except Exception as e:
            logger.error(f"Error posting {agent_name} output to GitHub: {e}", exc_info=True)
            # Don't fail the agent execution if posting fails

    # Markdown indicators: headings, bold, lists, code blocks, links
    _MARKDOWN_INDICATORS = ('# ', '## ', '### ', '**', '- ', '* ', '```', '[')

    # Keys that are part of the execution context infrastructure, not agent output.
    # Fallback tiers skip these to avoid picking up prompts, config, or metadata.
    _CONTEXT_KEYS = frozenset({
        'pipeline_id', 'task_id', 'agent', 'project', 'context',
        'work_dir', 'completed_work', 'decisions', 'metrics', 'validation',
        'observability', 'use_docker', 'claude_model',
        'agent_config', 'mcp_servers', 'claude_session_id', 'output_posted',
        'agent_hard_timeout', 'branch_name', 'container_name',
    })

    def _extract_markdown_output(self, agent_name: str, result: Dict[str, Any]) -> tuple:
        """
        Extract markdown output from agent result.

        All agents should store output under 'agent_output'. Legacy keys
        (markdown_analysis, markdown_review, etc.) are checked as fallbacks
        for in-flight executions during rollout.

        Returns (output_text, unexpected) where unexpected is True if the
        output was found via fallback rather than a known key.
        """
        # --- Tier 1: Known output keys ---
        output_keys = [
            'agent_output',           # Standard key (all agents)
            # Legacy keys (fallback for in-flight executions)
            'markdown_analysis',
            'markdown_review',
            'markdown_design',
            'markdown_plan',
            'markdown_test_plan',
            'markdown_documentation',
            'markdown_output',
            'raw_analysis_result',
            'raw_review_result',
            'output',
            'verification_result',
        ]

        for key in output_keys:
            if key in result:
                output = result[key]
                if output and isinstance(output, str):
                    return output, False

        # Check for nested full_markdown dicts (MakerAgent pattern)
        for key, value in result.items():
            if key in self._CONTEXT_KEYS:
                continue
            if isinstance(value, dict) and 'full_markdown' in value:
                fm = value['full_markdown']
                if fm and isinstance(fm, str):
                    return fm, False

        # --- Tier 2: Scan for any string value containing markdown ---
        for key, value in result.items():
            if key in self._CONTEXT_KEYS:
                continue
            if isinstance(value, str) and len(value) > 20:
                if any(indicator in value for indicator in self._MARKDOWN_INDICATORS):
                    logger.warning(
                        f"Agent {agent_name}: output found via markdown scan in key '{key}' "
                        f"(not a known output key). Available keys: {list(result.keys())}"
                    )
                    return value, True

        # --- Tier 3: Scan for JSON-serializable content, render as markdown ---
        for key, value in result.items():
            if key in self._CONTEXT_KEYS:
                continue
            if isinstance(value, (dict, list)):
                try:
                    rendered = f"```json\n{json.dumps(value, indent=2, default=str)}\n```"
                    logger.warning(
                        f"Agent {agent_name}: output found via JSON scan in key '{key}' "
                        f"(not a known output key). Available keys: {list(result.keys())}"
                    )
                    return rendered, True
                except (TypeError, ValueError):
                    continue

        # --- Tier 4: Use longest raw string value as-is ---
        longest_str = None
        longest_key = None
        for key, value in result.items():
            if key in self._CONTEXT_KEYS:
                continue
            if isinstance(value, str) and len(value) > 0:
                if longest_str is None or len(value) > len(longest_str):
                    longest_str = value
                    longest_key = key

        if longest_str:
            logger.warning(
                f"Agent {agent_name}: no markdown or JSON output found, using raw string "
                f"from key '{longest_key}' ({len(longest_str)} chars). Available keys: {list(result.keys())}"
            )
            return longest_str, True

        logger.warning(f"Could not find any output for {agent_name} in keys: {list(result.keys())}")
        return None, True

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
        from services.project_workspace import workspace_manager

        try:
            # Resolve the SAME directory the agent actually ran in. Read it
            # straight off task_context['project_dir'] -- set by execute_agent()'s
            # epic-resolution block (PipelineRunManager.resolve_workspace(), issue
            # #122) and/or by workspace_context.prepare_execution()'s prep_result
            # -- rather than independently re-deriving it via get_project_dir(
            # epic_id=...) (issue #123, WI-D of #119): the latter reaches the same
            # directory today (get_or_create_epic_worktree() is idempotent), but
            # is a second, divergence-prone resolution path for a value already
            # known. Falls back to epic_id/branch_name-based resolution only when
            # project_dir was never set on task_context (e.g. a 'discussions'
            # dispatch, or a genuinely unresolved workspace) -- checking the base
            # clone there while the agent actually wrote to an isolated worktree
            # would find nothing dirty and silently skip real uncommitted work.
            existing_project_dir = task_context.get('project_dir')
            if existing_project_dir:
                project_dir = str(existing_project_dir)
            else:
                project_dir = str(workspace_manager.get_project_dir(
                    project_name,
                    epic_id=task_context.get('epic_id'),
                    branch_name=task_context.get('branch_name'),
                ))
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
            from services.git_workflow_manager import PushFailedError
            if isinstance(e, PushFailedError):
                raise  # Propagate — finalization exception handler will block the pipeline
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
            from services.git_workflow_manager import PushFailedError
            if isinstance(e, PushFailedError):
                raise  # Propagate — finalization exception handler will block the pipeline
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
            from services.git_workflow_manager import PushFailedError
            if isinstance(e, PushFailedError):
                raise  # Propagate — finalization exception handler will block the pipeline
            logger.error(f"❌ FAILSAFE: Exception during stage and commit: {e}", exc_info=True)

    async def _failsafe_push(
        self,
        project_dir: str,
        project_name: str,
        issue_number: int
    ):
        """Try to push committed changes. Raises PushFailedError on failure."""
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

            # Delegates to push_branch which retries transient failures and raises
            # PushFailedError for permanent failures (non-fast-forward etc.)
            await git_workflow_manager.push_branch(project_dir, branch_name)
            logger.info(f"✅ FAILSAFE: Successfully pushed to origin/{branch_name}")

        except Exception as e:
            from services.git_workflow_manager import PushFailedError
            if isinstance(e, PushFailedError):
                raise  # Propagate — finalization exception handler will block the pipeline
            logger.warning(f"⚠️ FAILSAFE: Exception during push (non-critical): {e}")

    async def _queue_environment_verifier(
        self,
        project_name: str,
        task_context: Dict[str, Any],
        setup_output: Optional[str] = None
    ):
        """
        Queue a dev_environment_verifier task after setup completes.

        Args:
            project_name: Name of the project
            task_context: Context from the setup task
            setup_output: The setup agent's actual markdown output (extracted via
                _extract_markdown_output), passed through as previous_stage_output so
                the verifier reviews what the setup agent really said rather than a
                placeholder. Falls back to a generic note if unavailable.
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
            verifier_context = {
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
                'previous_stage_output': setup_output if setup_output else 'Setup agent output was not captured — its outcome is unknown.'
            }
            # Propagate pipeline_run_id from setup task so verifier events are
            # visible to the repair cycle's stall-detection query.
            if task_context.get('pipeline_run_id'):
                verifier_context['pipeline_run_id'] = task_context['pipeline_run_id']

            task = Task(
                id=str(uuid.uuid4()),
                agent="dev_environment_verifier",
                project=project_name,
                priority=TaskPriority.HIGH,
                context=verifier_context,
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

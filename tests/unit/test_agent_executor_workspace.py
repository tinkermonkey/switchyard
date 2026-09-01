"""
Tests for AgentExecutor with workspace abstraction integration.

These tests verify that AgentExecutor correctly uses workspace contexts.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def agent_executor():
    """Create an AgentExecutor instance with required mocks"""
    from services.agent_executor import AgentExecutor
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestAgentExecutorWorkspaceIntegration:
    """Test AgentExecutor integration with workspace contexts"""

    @pytest.mark.asyncio
    async def test_creates_workspace_context_for_issues(self, agent_executor):
        """AgentExecutor should create workspace context for issue-based work"""
        task_context = {
            'issue_number': 123,
            'workspace_type': 'issues',
            'issue_title': 'Test'
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/test', 'work_dir': '/workspace/test'}
            )
            mock_workspace.finalize_execution = AsyncMock(
                return_value={'success': True}
            )
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            # Execute
            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # Verify workspace context was created
            mock_factory.create.assert_called_once()
            call_kwargs = mock_factory.create.call_args[1]
            assert call_kwargs['workspace_type'] == 'issues'
            assert call_kwargs['project'] == 'test-project'
            assert call_kwargs['issue_number'] == 123

            # Verify workspace methods were called
            mock_workspace.prepare_execution.assert_called_once()
            mock_workspace.finalize_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_creates_workspace_context_for_discussions(self, agent_executor):
        """AgentExecutor should create workspace context for discussion-based work"""
        task_context = {
            'issue_number': 88,
            'workspace_type': 'discussions',
            'discussion_id': 'D_test123'
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'discussion_id': 'D_test123', 'work_dir': '/tmp/discussions/test'}
            )
            mock_workspace.finalize_execution = AsyncMock(
                return_value={'success': True, 'message': 'No finalization needed'}
            )
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            # Execute
            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

            # Verify workspace context was created with discussions type
            mock_factory.create.assert_called_once()
            call_kwargs = mock_factory.create.call_args[1]
            assert call_kwargs['workspace_type'] == 'discussions'

            # Verify workspace methods were called
            mock_workspace.prepare_execution.assert_called_once()
            mock_workspace.finalize_execution.assert_called_once()

    @pytest.mark.asyncio
    async def test_skips_workspace_without_issue_number(self, agent_executor):
        """AgentExecutor should skip workspace context if no issue_number"""
        task_context = {
            'task_type': 'adhoc'
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            # Execute
            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # Verify workspace context was NOT created
            mock_factory.create.assert_not_called()

    @pytest.mark.asyncio
    async def test_workspace_preparation_failure_continues(self, agent_executor):
        """AgentExecutor should continue if workspace preparation fails"""
        task_context = {
            'issue_number': 123,
            'workspace_type': 'issues'
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            # Workspace preparation fails
            mock_factory.create.side_effect = Exception("Preparation failed")

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            # Execute - should not raise
            result = await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # Verify agent still executed
            assert result['status'] == 'success'

    @pytest.mark.asyncio
    async def test_workspace_finalization_failure_continues(self, agent_executor):
        """AgentExecutor should continue if workspace finalization fails"""
        task_context = {
            'issue_number': 123,
            'workspace_type': 'issues'
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/test'}
            )
            # Finalization fails
            mock_workspace.finalize_execution = AsyncMock(
                side_effect=Exception("Finalization failed")
            )
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            # Execute - should not raise
            result = await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # Verify execution completed despite finalization failure
            assert result['status'] == 'success'

    @pytest.mark.asyncio
    async def test_task_context_updated_with_workspace_prep_result(self, agent_executor):
        """Task context should be updated with workspace preparation results"""
        task_context = {
            'issue_number': 123,
            'workspace_type': 'issues'
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={
                    'branch_name': 'feature/issue-123-test',
                    'work_dir': '/workspace/test-project'
                }
            )
            mock_workspace.finalize_execution = AsyncMock(
                return_value={'success': True}
            )
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            # Execute
            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # Verify task_context was updated with prep results
            assert task_context['branch_name'] == 'feature/issue-123-test'
            assert task_context['work_dir'] == '/workspace/test-project'

    @pytest.mark.asyncio
    async def test_git_workspace_prep_failure_retains_lock_and_comments(self, agent_executor):
        """
        A generic failure preparing a git-based workspace (e.g. the shared
        checkout is stuck mid-merge and `git stash` can't write the index) must
        be treated as a broken shared workspace, not a transient agent failure:
        it should post a GitHub comment, end the pipeline run with the lock
        retained, and raise NonRetryableAgentError so nothing retries and the
        zombie watchdog never gets a chance to auto-release the lock back into
        the same broken workspace.

        Regression test for the 2026-08-11 documentation_robotics incident:
        an unresolved merge conflict left the shared workspace poisoned, and
        because this path only raised a bare RuntimeError (no retain_lock, no
        comment), the zombie watchdog auto-released the lock for every
        subsequent issue on the board, each repeating the identical failure
        for ~an hour before being reaped.
        """
        from agents.non_retryable import NonRetryableAgentError

        task_context = {
            'issue_number': 793,
            'workspace_type': 'issues',
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch('services.github_integration.GitHubIntegration') as mock_github_cls, \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_get_prm, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            mock_workspace = MagicMock()
            mock_workspace.supports_git_operations = True
            mock_workspace.prepare_execution = AsyncMock(
                side_effect=Exception("Failed to stash changes: error: could not write index\n")
            )
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            mock_github = MagicMock()
            mock_github.post_comment = AsyncMock()
            mock_github_cls.return_value = mock_github

            mock_prm = MagicMock()
            mock_get_prm.return_value = mock_prm

            with pytest.raises(NonRetryableAgentError):
                await agent_executor.execute_agent(
                    agent_name='senior_software_engineer',
                    project_name='documentation_robotics',
                    task_context=task_context
                )

            # Pipeline run must be marked failed via the shared mark_failed()
            # entry point (not a bare end_pipeline_run(outcome="failed") whose
            # return was previously discarded) -- no auto-retry, no
            # next-queued-issue dispatch into the same broken workspace.
            mock_prm.mark_failed.assert_called_once()
            _, mark_kwargs = mock_prm.mark_failed.call_args
            assert mark_kwargs['issue_number'] == 793
            assert mark_kwargs['project'] == 'documentation_robotics'

            # A human-visible comment must be posted so this doesn't fail silently.
            mock_github.post_comment.assert_called_once()
            comment_args, _ = mock_github.post_comment.call_args
            assert comment_args[0] == 793
            assert 'Workspace Preparation Failed' in comment_args[1]
            # The default MagicMock() return of mark_failed() is truthy, so this
            # run exercises the "successfully retained" branch — see the
            # companion test below for the marked_successfully=False branch.
            assert 'Pipeline lock retained' in comment_args[1]
            assert 'could NOT be durably marked' not in comment_args[1]

    @pytest.mark.asyncio
    async def test_git_workspace_prep_failure_comment_warns_when_mark_failed_fails(self, agent_executor):
        """The other half of test_git_workspace_prep_failure_retains_lock_and_comments:
        when mark_failed() itself reports it could NOT durably retain the lock
        (both Redis and YAML writes failed), the posted comment must say so —
        not unconditionally claim "the lock is retained", which would itself be
        a silent-failure regression (a human reading it would believe the
        board is protected when it may not be)."""
        task_context = {
            'issue_number': 793,
            'workspace_type': 'issues',
        }

        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch('services.github_integration.GitHubIntegration') as mock_github_cls, \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_get_prm, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            mock_workspace = MagicMock()
            mock_workspace.supports_git_operations = True
            mock_workspace.prepare_execution = AsyncMock(
                side_effect=Exception("Failed to stash changes: error: could not write index\n")
            )
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            mock_github = MagicMock()
            mock_github.post_comment = AsyncMock()
            mock_github_cls.return_value = mock_github

            mock_prm = MagicMock()
            mock_prm.mark_failed.return_value = False  # both Redis and YAML writes failed
            mock_get_prm.return_value = mock_prm

            from agents.non_retryable import NonRetryableAgentError
            with pytest.raises(NonRetryableAgentError):
                await agent_executor.execute_agent(
                    agent_name='senior_software_engineer',
                    project_name='documentation_robotics',
                    task_context=task_context
                )

            comment_args, _ = mock_github.post_comment.call_args
            assert 'could NOT be durably marked retained' in comment_args[1]
            assert 'Pipeline lock retained' not in comment_args[1]


class TestBuildExecutionContextEpicWorktree:
    """Issue #46: _build_execution_context's work_dir must resolve to the
    epic's isolated git worktree (via workspace_manager.get_project_dir's
    epic_id/branch_name kwargs) instead of the shared base clone, while
    staying byte-for-byte identical to the pre-migration behavior when no
    epic_id is supplied.
    """

    def test_defaults_to_shared_base_clone_without_epic_id(self, agent_executor):
        """Omitting epic_id must preserve the pre-worktree-isolation behavior
        exactly: a plain base-clone path, no epic/branch kwargs threaded."""
        with patch('services.project_workspace.workspace_manager.get_project_dir',
                   return_value=Path('/workspace/test-project')) as mock_get_dir, \
             patch('services.agent_executor.config_manager') as mock_config:
            mock_config.get_project_agent_config.return_value = MagicMock()

            context = agent_executor._build_execution_context(
                agent_name='test_agent',
                project_name='test-project',
                task_id='task-1',
                task_context={'issue_number': 100},
            )

            mock_get_dir.assert_called_once_with('test-project', epic_id=None, branch_name=None)
            assert context['work_dir'] == '/workspace/test-project'

    def test_scopes_work_dir_to_the_epic_worktree_when_epic_id_given(self, agent_executor):
        with patch('services.project_workspace.workspace_manager.get_project_dir',
                   return_value=Path('/workspace/.orchestrator/worktrees/test-project/42')) as mock_get_dir, \
             patch('services.agent_executor.config_manager') as mock_config:
            mock_config.get_project_agent_config.return_value = MagicMock()

            context = agent_executor._build_execution_context(
                agent_name='test_agent',
                project_name='test-project',
                task_id='task-1',
                task_context={'issue_number': 101},
                epic_id='42',
                branch_name='feature/issue-42-epic',
            )

            mock_get_dir.assert_called_once_with(
                'test-project', epic_id='42', branch_name='feature/issue-42-epic'
            )
            assert context['work_dir'] == '/workspace/.orchestrator/worktrees/test-project/42'

    def test_reuses_an_already_resolved_project_dir_without_rederiving(self, agent_executor):
        """A caller running in its own separate process (a repair cycle
        container) hands us the concrete directory its originating
        orchestrator process already created/reused via
        task_context['project_dir'] -- re-deriving via epic_id here would run
        against a fresh, empty in-process worktree cache and could try to
        `git worktree add` a worktree that already exists on disk."""
        with patch('services.project_workspace.workspace_manager.get_project_dir') as mock_get_dir, \
             patch('services.agent_executor.config_manager') as mock_config:
            mock_config.get_project_agent_config.return_value = MagicMock()

            task_context = {
                'issue_number': 100,
                'project_dir': '/workspace/.orchestrator/worktrees/test-project/42',
            }
            context = agent_executor._build_execution_context(
                agent_name='test_agent',
                project_name='test-project',
                task_id='task-1',
                task_context=task_context,
                epic_id='42',
                branch_name='feature/issue-42-epic',
            )

            mock_get_dir.assert_not_called()
            assert context['work_dir'] == '/workspace/.orchestrator/worktrees/test-project/42'


class TestExecuteAgentEpicResolution:
    """Issue #46: execute_agent must resolve the epic (parent issue for
    sdlc_execution sub-issues, or the issue's own number for
    planning_design/standalone dispatch) and thread it into
    _build_execution_context, so the directory it builds is the epic's
    isolated worktree.
    """

    async def _run(self, agent_executor, task_context, parent_issue):
        """Drive execute_agent for real with everything except the epic
        resolution chain and _build_execution_context mocked out, capturing
        the epic_id/branch_name _build_execution_context was actually called
        with."""
        captured = {}

        def fake_build_execution_context(agent_name, project_name, task_id, task_context,
                                          epic_id=None, branch_name=None):
            captured['epic_id'] = epic_id
            captured['branch_name'] = branch_name
            return {'work_dir': '/fake', 'use_docker': True, 'context': task_context}

        mock_project_config = MagicMock()
        mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        with patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor, '_build_execution_context',
                          side_effect=fake_build_execution_context), \
             patch.object(agent_executor, '_apply_frozen_session_resume'), \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock), \
             patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('services.feature_branch_manager.feature_branch_manager.resolve_epic_id',
                   new_callable=AsyncMock) as mock_resolve_epic_id, \
             patch('services.feature_branch_manager.feature_branch_manager.resolve_epic_branch_name',
                   return_value=None), \
             patch('services.feature_branch_manager.feature_branch_manager.create_feature_branch_name',
                   return_value='feature/issue-generated'), \
             patch('services.feature_branch_manager.feature_branch_manager.get_current_branch',
                   new_callable=AsyncMock, return_value='feature/issue-42-shared'):

            mock_config.get_project_config.return_value = mock_project_config

            async def fake_resolve_epic_id(gh, issue_number, project=None):
                return str(parent_issue) if parent_issue else str(issue_number)
            mock_resolve_epic_id.side_effect = fake_resolve_epic_id

            mock_workspace = MagicMock()
            mock_workspace.supports_git_operations = True
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/issue-42-shared', 'work_dir': '/workspace/test-project'}
            )
            mock_workspace.finalize_execution = AsyncMock(return_value={'success': True})
            mock_factory.create.return_value = mock_workspace

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context,
            )

        return captured

    @pytest.mark.asyncio
    async def test_issues_workspace_type_does_not_resolve_epic_id(self, agent_executor):
        """A sub-issue dispatch through the DEFAULT 'issues' workspace type must NOT
        resolve/pass an epic_id -- IssuesWorkspaceContext.prepare_execution() ->
        FeatureBranchManager.prepare_feature_branch() still checks out its own
        (independently-resolved) branch directly on the shared base clone, which
        would conflict with a worktree already holding that same branch checked
        out. Epic worktree isolation for this workspace type is deferred to #48;
        until then it must keep exactly its pre-migration base-clone behavior."""
        task_context = {'issue_number': 100, 'workspace_type': 'issues'}

        captured = await self._run(agent_executor, task_context, parent_issue=42)

        assert captured['epic_id'] is None
        assert 'epic_id' not in task_context

    @pytest.mark.asyncio
    async def test_hybrid_workspace_type_does_not_resolve_epic_id(self, agent_executor):
        """HybridWorkspaceContext also calls prepare_feature_branch() on the base
        clone (same conflict risk as 'issues') -- must be excluded from epic
        worktree resolution for the same reason."""
        task_context = {'issue_number': 150, 'workspace_type': 'hybrid'}

        captured = await self._run(agent_executor, task_context, parent_issue=42)

        assert captured['epic_id'] is None
        assert 'epic_id' not in task_context

    @pytest.mark.asyncio
    async def test_planning_design_style_dispatch_falls_back_to_its_own_number(self, agent_executor):
        """A board item dispatched directly as an epic through the 'discussions'
        workspace type (verified git-free -- supports_git_operations=False, no
        call to prepare_feature_branch, so no checkout-conflict risk) must scope
        the worktree to its own issue number when no parent is found."""
        task_context = {'issue_number': 200, 'workspace_type': 'discussions'}

        captured = await self._run(agent_executor, task_context, parent_issue=None)

        assert captured['epic_id'] == '200'

    @pytest.mark.asyncio
    async def test_skip_workspace_prep_reuses_a_pre_resolved_epic_id(self, agent_executor):
        """A repair cycle's own internal execute_agent calls
        (skip_workspace_prep=True) already have 'epic_id'/'project_dir'
        threaded in from the stage_context their originating dispatch built
        -- this must be reused as-is, not re-resolved (which would run a
        fresh GitHub lookup and, for project_dir, risk re-deriving via a
        worktree cache that's empty in this process)."""
        task_context = {
            'issue_number': 100,
            'workspace_type': 'issues',
            'skip_workspace_prep': True,
            'epic_id': '77',
            'branch_name': 'feature/issue-77-shared',
            'project_dir': '/workspace/.orchestrator/worktrees/test-project/77',
        }

        # parent_issue=42 here would be WRONG if reached -- proves resolution
        # was skipped entirely rather than happening to agree.
        captured = await self._run(agent_executor, task_context, parent_issue=42)

        assert captured['epic_id'] == '77'
        assert captured['branch_name'] == 'feature/issue-77-shared'

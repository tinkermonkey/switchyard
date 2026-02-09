"""
Test suite for workspace context abstraction layer.

These tests document and verify the current behavior of workspace-specific
operations. They must pass BEFORE refactoring and AFTER refactoring to
ensure backward compatibility.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path


@pytest.fixture
def agent_executor():
    """Create an AgentExecutor instance with required mocks"""
    from services.agent_executor import AgentExecutor
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestIssuesWorkspaceContext:
    """Test issues workspace behavior - git operations enabled"""

    @pytest.mark.asyncio
    async def test_issues_workspace_prepares_feature_branch(self, agent_executor):
        """Issues workspace should prepare git feature branch"""
        task_context = {
            'issue_number': 123,
            'issue_title': 'Test feature',
            'workspace_type': 'issues'  # KEY: issues workspace
        }

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value="feature/issue-123-test"
            )

            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_project_agent_config.return_value = {}

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            # This should NOT raise an error
            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # Verify feature branch was prepared
            mock_fbm.prepare_feature_branch.assert_called_once()
            call_kwargs = mock_fbm.prepare_feature_branch.call_args[1]
            assert call_kwargs['project'] == 'test-project'
            assert call_kwargs['issue_number'] == 123

    @pytest.mark.asyncio
    async def test_issues_workspace_finalizes_feature_branch(self):
        """Issues workspace should finalize git operations (commit/push/PR)"""
        from services.agent_executor import AgentExecutor

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.GitHubIntegration') as mock_gh, \
             patch('services.agent_executor.config_manager') as mock_config:

            # Setup mocks
            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value="feature/issue-123-test"
            )
            mock_fbm.finalize_feature_branch_work = AsyncMock(
                return_value={'success': True, 'pr_url': 'https://github.com/org/repo/pull/1'}
            )

            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_project_agent_config.return_value = {}

            executor = AgentExecutor()

            task_context = {
                'issue_number': 123,
                'workspace_type': 'issues'
            }

            with patch.object(executor, 'factory') as mock_factory, \
                 patch.object(executor, '_build_execution_context') as mock_build, \
                 patch.object(executor, '_post_agent_output_to_github') as mock_post:

                mock_agent = MagicMock()
                mock_agent.execute = AsyncMock(return_value={'status': 'success'})
                mock_factory.create_agent.return_value = mock_agent
                mock_build.return_value = {}

                await executor.execute_agent(
                    agent_name='test_agent',
                    project_name='test-project',
                    task_context=task_context,
                    task_id_prefix='test-123'
                )

                # Verify finalization was called
                mock_fbm.finalize_feature_branch_work.assert_called_once()
                call_kwargs = mock_fbm.finalize_feature_branch_work.call_args[1]
                assert call_kwargs['project'] == 'test-project'
                assert call_kwargs['issue_number'] == 123
                assert 'commit_message' in call_kwargs

    @pytest.mark.asyncio
    async def test_issues_workspace_uses_git_directory(self):
        """Issues workspace should use actual git repository directory"""
        from services.agent_executor import AgentExecutor

        with patch('services.project_workspace.workspace_manager') as mock_workspace:
            mock_workspace.get_project_dir.return_value = Path('/workspace/test-project')

            executor = AgentExecutor()

            task_context = {
                'issue_number': 123,
                'workspace_type': 'issues'
            }

            with patch.object(executor, 'factory') as mock_factory, \
                 patch.object(executor, '_post_agent_output_to_github') as mock_post, \
                 patch('services.feature_branch_manager.feature_branch_manager'), \
                 patch('services.agent_executor.config_manager') as mock_config:

                mock_config.get_project_config.return_value = MagicMock(github={'org': 'test', 'repo': 'test'})
                mock_config.get_project_agent_config.return_value = {}

                mock_agent = MagicMock()
                mock_agent.execute = AsyncMock(return_value={'status': 'success'})
                mock_agent.agent_config = {}
                mock_factory.create_agent.return_value = mock_agent

                await executor.execute_agent(
                    agent_name='test_agent',
                    project_name='test-project',
                    task_context=task_context,
                    task_id_prefix='test-123'
                )

                # Verify workspace manager was called for project directory
                mock_workspace.get_project_dir.assert_called_with('test-project')


class TestDiscussionsWorkspaceContext:
    """Test discussions workspace behavior - NO git operations"""

    @pytest.mark.asyncio
    async def test_discussions_workspace_skips_feature_branch_prepare(self):
        """Discussions workspace should NOT prepare git feature branch"""
        from services.agent_executor import AgentExecutor

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('config.manager.config_manager') as mock_config:

            mock_fbm.prepare_feature_branch = AsyncMock()

            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            executor = AgentExecutor()

            task_context = {
                'issue_number': 88,
                'workspace_type': 'discussions',  # KEY: discussions workspace
                'discussion_id': 'D_kwDOPH6wk84AiUip'
            }

            with patch.object(executor, 'factory') as mock_factory, \
                 patch.object(executor, '_build_execution_context') as mock_build, \
                 patch.object(executor, '_post_agent_output_to_github') as mock_post:

                mock_agent = MagicMock()
                mock_agent.execute = AsyncMock(return_value={'status': 'success'})
                mock_factory.create_agent.return_value = mock_agent
                mock_build.return_value = {}

                await executor.execute_agent(
                    agent_name='business_analyst',
                    project_name='test-project',
                    task_context=task_context,
                    task_id_prefix='test-88'
                )

                # Verify feature branch preparation was NOT called
                mock_fbm.prepare_feature_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_discussions_workspace_skips_feature_branch_finalize(self):
        """Discussions workspace should NOT finalize git operations"""
        from services.agent_executor import AgentExecutor

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('config.manager.config_manager') as mock_config:

            mock_fbm.finalize_feature_branch_work = AsyncMock()

            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config

            executor = AgentExecutor()

            task_context = {
                'issue_number': 88,
                'workspace_type': 'discussions',
                'discussion_id': 'D_kwDOPH6wk84AiUip'
            }

            with patch.object(executor, 'factory') as mock_factory, \
                 patch.object(executor, '_build_execution_context') as mock_build, \
                 patch.object(executor, '_post_agent_output_to_github') as mock_post:

                mock_agent = MagicMock()
                mock_agent.execute = AsyncMock(return_value={'status': 'success'})
                mock_factory.create_agent.return_value = mock_agent
                mock_build.return_value = {}

                await executor.execute_agent(
                    agent_name='business_analyst',
                    project_name='test-project',
                    task_context=task_context,
                    task_id_prefix='test-88'
                )

                # Verify finalization was NOT called (the bug we're fixing!)
                mock_fbm.finalize_feature_branch_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_discussions_workspace_posts_to_discussion(self):
        """Discussions workspace should post to GitHub Discussions"""
        from services.agent_executor import AgentExecutor

        executor = AgentExecutor()

        task_context = {
            'issue_number': 88,
            'workspace_type': 'discussions',
            'discussion_id': 'D_kwDOPH6wk84AiUip'
        }

        result = {
            'status': 'success',
            'markdown_analysis': 'Test markdown output for discussion posting'
        }

        # Patch the executor's github instance directly
        with patch.object(executor, 'github') as mock_gh:
            mock_gh.post_agent_output = AsyncMock(
                return_value={'success': True}
            )

            await executor._post_agent_output_to_github(
                agent_name='business_analyst',
                task_context=task_context,
                result=result
            )

            # Verify output was posted
            mock_gh.post_agent_output.assert_called_once()
            call_args_positional = mock_gh.post_agent_output.call_args[0]
            call_args_keyword = mock_gh.post_agent_output.call_args[1]
            
            # Check that the method was called with task_context and formatted output
            assert len(call_args_positional) == 2
            
            # First argument should be task_context
            task_context_arg = call_args_positional[0]
            assert task_context_arg['issue_number'] == 88
            assert task_context_arg['discussion_id'] == 'D_kwDOPH6wk84AiUip'
            assert task_context_arg['workspace_type'] == 'discussions'
            
            # Second argument should be the formatted markdown output
            output_arg = call_args_positional[1]
            assert 'Test markdown output for discussion posting' in output_arg
            assert 'business_analyst agent' in output_arg


class TestWorkspaceContextBehaviorEquivalence:
    """
    These tests verify that workspace contexts provide equivalent behavior
    to the current conditional-based implementation.
    """

    @pytest.mark.asyncio
    async def test_both_workspaces_execute_agent(self):
        """Both workspace types should successfully execute agents"""
        from services.agent_executor import AgentExecutor

        executor = AgentExecutor()

        for workspace_type in ['issues', 'discussions']:
            task_context = {
                'issue_number': 123,
                'workspace_type': workspace_type
            }

            if workspace_type == 'discussions':
                task_context['discussion_id'] = 'D_test123'

            with patch.object(executor, 'factory') as mock_factory, \
                 patch.object(executor, '_build_execution_context') as mock_build, \
                 patch.object(executor, '_post_agent_output_to_github') as mock_post, \
                 patch('services.feature_branch_manager.feature_branch_manager'), \
                 patch('config.manager.config_manager'):

                mock_agent = MagicMock()
                mock_agent.execute = AsyncMock(return_value={'status': 'success'})
                mock_factory.create_agent.return_value = mock_agent
                mock_build.return_value = {}

                # Should not raise any errors
                result = await executor.execute_agent(
                    agent_name='test_agent',
                    project_name='test-project',
                    task_context=task_context,
                    task_id_prefix=f'test-{workspace_type}'
                )

                assert result['status'] == 'success'

    @pytest.mark.asyncio
    async def test_workspace_type_determines_git_operations(self):
        """Git operations should only happen for issues workspace"""
        from services.agent_executor import AgentExecutor

        git_operation_count = {}

        for workspace_type in ['issues', 'discussions']:
            with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
                 patch('services.agent_executor.config_manager') as mock_config:

                mock_fbm.prepare_feature_branch = AsyncMock(
                    return_value="feature/test"
                )
                mock_fbm.finalize_feature_branch_work = AsyncMock(
                    return_value={'success': True}
                )

                mock_project_config = MagicMock()
                mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
                mock_config.get_project_config.return_value = mock_project_config
                mock_config.get_project_agent_config.return_value = {}

                executor = AgentExecutor()

                task_context = {
                    'issue_number': 123,
                    'workspace_type': workspace_type
                }

                if workspace_type == 'discussions':
                    task_context['discussion_id'] = 'D_test'

                with patch.object(executor, 'factory') as mock_factory, \
                     patch.object(executor, '_post_agent_output_to_github') as mock_post:

                    mock_agent = MagicMock()
                    mock_agent.execute = AsyncMock(return_value={'status': 'success'})
                    mock_agent.agent_config = {}
                    mock_factory.create_agent.return_value = mock_agent

                    await executor.execute_agent(
                        agent_name='test_agent',
                        project_name='test-project',
                        task_context=task_context,
                        task_id_prefix=f'test-{workspace_type}'
                    )

                    prepare_called = mock_fbm.prepare_feature_branch.call_count
                    finalize_called = mock_fbm.finalize_feature_branch_work.call_count

                    git_operation_count[workspace_type] = {
                        'prepare': prepare_called,
                        'finalize': finalize_called
                    }

        # Verify git operations only happen for issues
        assert git_operation_count['issues']['prepare'] == 1
        assert git_operation_count['issues']['finalize'] == 1
        assert git_operation_count['discussions']['prepare'] == 0
        assert git_operation_count['discussions']['finalize'] == 0


class TestFeatureBranchManagerHandlesStandalone:
    """Test that feature branch manager handles standalone issues gracefully"""

    @pytest.mark.asyncio
    async def test_finalize_handles_standalone_issue(self):
        """Feature branch manager should handle issues without parent tracking"""
        from services.feature_branch_manager import FeatureBranchManager
        from services.git_workflow_manager import GitWorkflowManager

        fbm = FeatureBranchManager(workspace_root='/tmp/test')

        with patch.object(fbm, 'get_feature_branch_for_issue', return_value=None), \
             patch.object(fbm, 'git_add_all', new_callable=AsyncMock), \
             patch.object(fbm, 'git_commit', new_callable=AsyncMock), \
             patch.object(fbm, 'git_push', new_callable=AsyncMock), \
             patch('services.git_workflow_manager.git_workflow_manager') as mock_gwm:

            mock_gwm.get_current_branch = AsyncMock(return_value='feature/issue-88-standalone')

            mock_gh = MagicMock()

            result = await fbm.finalize_feature_branch_work(
                project='test-project',
                issue_number=88,
                commit_message='Test commit',
                github_integration=mock_gh
            )

            # Should succeed even without feature branch state
            assert result['success'] == True
            assert result['standalone'] == True
            assert result['branch_name'] == 'feature/issue-88-standalone'

            # Should still do git operations
            fbm.git_add_all.assert_called_once()
            fbm.git_commit.assert_called_once()
            fbm.git_push.assert_called_once()

    @pytest.mark.asyncio
    async def test_finalize_handles_tracked_issue(self):
        """Feature branch manager should handle tracked sub-issues normally"""
        from services.feature_branch_manager import FeatureBranchManager, FeatureBranch

        fbm = FeatureBranchManager(workspace_root='/tmp/test')

        # Create mock feature branch
        mock_fb = FeatureBranch(
            parent_issue=50,
            branch_name='feature/issue-50-parent',
            created_at='2025-10-07T00:00:00'
        )

        with patch.object(fbm, 'get_feature_branch_for_issue', return_value=mock_fb), \
             patch.object(fbm, 'git_add_all', new_callable=AsyncMock), \
             patch.object(fbm, 'git_commit', new_callable=AsyncMock), \
             patch.object(fbm, 'git_push', new_callable=AsyncMock), \
             patch.object(fbm, 'mark_sub_issue_complete'), \
             patch.object(fbm, 'check_all_sub_issues_complete', return_value=False), \
             patch.object(fbm, 'create_or_update_feature_pr', new_callable=AsyncMock,
                         return_value={'pr_url': 'https://github.com/test/repo/pull/1'}):

            mock_gh = MagicMock()

            result = await fbm.finalize_feature_branch_work(
                project='test-project',
                issue_number=51,
                commit_message='Test commit',
                github_integration=mock_gh
            )

            # Should succeed with normal flow
            assert result['success'] == True
            assert 'standalone' not in result
            assert result['branch_name'] == 'feature/issue-50-parent'

            # Should update tracking
            fbm.mark_sub_issue_complete.assert_called_once()

"""
Simplified test suite documenting workspace-specific behavior.

These tests MUST pass before and after the workspace abstraction refactor.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture
def agent_executor():
    """Create AgentExecutor with mocked dependencies"""
    from services.agent_executor import AgentExecutor
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestWorkspaceGitOperations:
    """Test that git operations only happen in issues workspace"""

    @pytest.mark.asyncio
    async def test_issues_workspace_prepares_branch(self, agent_executor):
        """Issues workspace MUST prepare git feature branch"""
        task_context = {
            'issue_number': 123,
            'workspace_type': 'issues'
        }

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_fbm.prepare_feature_branch = AsyncMock(return_value="feature/test")
            mock_fbm.finalize_feature_branch_work = AsyncMock(return_value={'success': True})

            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test', 'repo': 'test'}
            )
            mock_config.get_project_agent_config.return_value = {}

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # REQUIREMENT: prepare_feature_branch MUST be called for issues
            assert mock_fbm.prepare_feature_branch.call_count == 1

    @pytest.mark.asyncio
    async def test_discussions_workspace_skips_branch_prepare(self, agent_executor):
        """Discussions workspace MUST NOT prepare git feature branch"""
        task_context = {
            'issue_number': 88,
            'workspace_type': 'discussions',
            'discussion_id': 'D_test123'
        }

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_fbm.prepare_feature_branch = AsyncMock()

            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test', 'repo': 'test'}
            )
            mock_config.get_project_agent_config.return_value = {}

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

            # REQUIREMENT: prepare_feature_branch MUST NOT be called for discussions
            assert mock_fbm.prepare_feature_branch.call_count == 0

    @pytest.mark.asyncio
    async def test_issues_workspace_finalizes_branch(self, agent_executor):
        """Issues workspace MUST finalize git operations"""
        task_context = {
            'issue_number': 123,
            'workspace_type': 'issues'
        }

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_fbm.prepare_feature_branch = AsyncMock(return_value="feature/test")
            mock_fbm.finalize_feature_branch_work = AsyncMock(return_value={'success': True})

            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test', 'repo': 'test'}
            )
            mock_config.get_project_agent_config.return_value = {}

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )

            # REQUIREMENT: finalize_feature_branch_work MUST be called for issues
            assert mock_fbm.finalize_feature_branch_work.call_count == 1

    @pytest.mark.asyncio
    async def test_discussions_workspace_skips_branch_finalize(self, agent_executor):
        """Discussions workspace MUST NOT finalize git operations"""
        task_context = {
            'issue_number': 88,
            'workspace_type': 'discussions',
            'discussion_id': 'D_test123'
        }

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_fbm.finalize_feature_branch_work = AsyncMock()

            mock_config.get_project_config.return_value = MagicMock(
                github={'org': 'test', 'repo': 'test'}
            )
            mock_config.get_project_agent_config.return_value = {}

            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent

            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

            # REQUIREMENT: finalize_feature_branch_work MUST NOT be called for discussions
            assert mock_fbm.finalize_feature_branch_work.call_count == 0


class TestFeatureBranchManagerStandalone:
    """Test that feature branch manager handles standalone issues"""

    @pytest.mark.asyncio
    async def test_finalize_succeeds_without_feature_branch_state(self):
        """MUST handle issues without parent tracking (standalone issues)"""
        from services.feature_branch_manager import FeatureBranchManager

        fbm = FeatureBranchManager(workspace_root='/tmp/test')

        with patch.object(fbm, 'get_feature_branch_for_issue', return_value=None), \
             patch.object(fbm, 'git_add_all', new_callable=AsyncMock), \
             patch.object(fbm, 'git_commit', new_callable=AsyncMock), \
             patch.object(fbm, 'git_push', new_callable=AsyncMock), \
             patch('services.git_workflow_manager.git_workflow_manager') as mock_gwm:

            mock_gwm.get_current_branch = AsyncMock(return_value='feature/issue-88-test')
            mock_gh = MagicMock()

            result = await fbm.finalize_feature_branch_work(
                project='test-project',
                issue_number=88,
                commit_message='Test commit',
                github_integration=mock_gh
            )

            # REQUIREMENT: MUST succeed (not return {'success': False})
            assert result['success'] == True
            assert result['standalone'] == True

            # REQUIREMENT: MUST still do git operations
            fbm.git_add_all.assert_called_once()
            fbm.git_commit.assert_called_once()
            fbm.git_push.assert_called_once()

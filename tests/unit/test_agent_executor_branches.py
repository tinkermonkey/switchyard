"""
Unit tests for agent executor integration with feature branches
"""

import pytest
from unittest.mock import AsyncMock, patch, MagicMock
from datetime import datetime

from services.agent_executor import AgentExecutor


@pytest.fixture
def agent_executor():
    """Create an AgentExecutor instance"""
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestFeatureBranchPreparation:
    """Test feature branch preparation before agent execution"""

    @pytest.mark.asyncio
    async def test_prepare_branch_with_issue_number(self, agent_executor):
        """Test that feature branch is prepared when issue_number present"""
        task_context = {
            'issue_number': 51,
            'issue_title': 'Login form UI',
            'repository': 'test-org/test-repo'
        }

        # Mock dependencies
        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value="feature/issue-50-user-auth"
            )

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            # Execute agent
            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

            # Verify prepare_feature_branch was called
            mock_fbm.prepare_feature_branch.assert_called_once()
            call_kwargs = mock_fbm.prepare_feature_branch.call_args[1]
            assert call_kwargs['project'] == 'test-project'
            assert call_kwargs['issue_number'] == 51
            assert call_kwargs['issue_title'] == 'Login form UI'

            # Verify branch name added to context
            assert task_context['branch_name'] == "feature/issue-50-user-auth"

    @pytest.mark.asyncio
    async def test_skip_branch_prep_no_issue_number(self, agent_executor):
        """Test that branch prep is skipped when no issue_number"""
        task_context = {
            'task_type': 'adhoc'
        }

        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_fbm.prepare_feature_branch = AsyncMock()

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            # Execute agent
            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

            # Verify prepare_feature_branch was NOT called
            mock_fbm.prepare_feature_branch.assert_not_called()

    @pytest.mark.asyncio
    async def test_branch_prep_failure_continues_execution(self, agent_executor):
        """Test that agent execution continues even if branch prep fails"""
        task_context = {
            'issue_number': 51,
            'issue_title': 'Login form UI'
        }

        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            # Branch prep fails
            mock_fbm.prepare_feature_branch = AsyncMock(
                side_effect=Exception("GitHub API error")
            )

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            # Execute agent - should not raise
            result = await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

            # Agent should still execute
            mock_agent.execute.assert_called_once()
            assert result['status'] == 'success'


class TestFeatureBranchFinalization:
    """Test feature branch finalization after agent execution"""

    @pytest.mark.asyncio
    async def test_finalize_branch_after_success(self, agent_executor):
        """Test that feature branch is finalized after successful execution"""
        task_context = {
            'issue_number': 51,
            'issue_title': 'Login form UI'
        }

        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor, '_post_agent_output_to_github') as mock_post, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value="feature/issue-50-user-auth"
            )
            mock_fbm.finalize_feature_branch_work = AsyncMock(
                return_value={
                    'success': True,
                    'pr_url': 'https://github.com/org/repo/pull/123',
                    'all_complete': False
                }
            )

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            mock_post.return_value = None

            # Execute agent
            await agent_executor.execute_agent(
                agent_name='senior_software_engineer',
                project_name='test-project',
                task_context=task_context
            )

            # Verify finalize was called
            mock_fbm.finalize_feature_branch_work.assert_called_once()
            call_kwargs = mock_fbm.finalize_feature_branch_work.call_args[1]
            assert call_kwargs['project'] == 'test-project'
            assert call_kwargs['issue_number'] == 51
            assert 'senior_software_engineer' in call_kwargs['commit_message']

    @pytest.mark.asyncio
    async def test_skip_finalize_no_branch_name(self, agent_executor):
        """Test that finalization is skipped if no branch was prepared"""
        task_context = {
            'issue_number': 51
        }

        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor, '_post_agent_output_to_github') as mock_post, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks - no branch preparation
            mock_fbm.prepare_feature_branch = AsyncMock(side_effect=Exception("No parent"))
            mock_fbm.finalize_feature_branch_work = AsyncMock()

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            mock_post.return_value = None

            # Execute agent
            await agent_executor.execute_agent(
                agent_name='senior_software_engineer',
                project_name='test-project',
                task_context=task_context
            )

            # Verify finalize was NOT called (no branch_name)
            mock_fbm.finalize_feature_branch_work.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_failure_does_not_fail_execution(self, agent_executor):
        """Test that finalization failure doesn't fail the agent execution"""
        task_context = {
            'issue_number': 51,
            'issue_title': 'Login form UI'
        }

        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor, '_post_agent_output_to_github') as mock_post, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value="feature/issue-50-user-auth"
            )
            # Finalization fails
            mock_fbm.finalize_feature_branch_work = AsyncMock(
                side_effect=Exception("Git push failed")
            )

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            mock_post.return_value = None

            # Execute agent - should not raise
            result = await agent_executor.execute_agent(
                agent_name='senior_software_engineer',
                project_name='test-project',
                task_context=task_context
            )

            # Execution should still be successful
            assert result['status'] == 'success'


class TestCommitMessages:
    """Test commit message generation"""

    @pytest.mark.asyncio
    async def test_commit_message_includes_agent_and_task(self, agent_executor):
        """Test that commit messages include agent name and task ID"""
        task_context = {
            'issue_number': 51,
            'issue_title': 'Login form UI'
        }

        with patch('services.agent_executor.feature_branch_manager') as mock_fbm, \
             patch('services.agent_executor.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor, '_post_agent_output_to_github') as mock_post, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.repository = "test-org/test-repo"
            mock_config.get_project_config.return_value = mock_project_config

            mock_fbm.prepare_feature_branch = AsyncMock(return_value="feature/issue-50-auth")
            mock_fbm.finalize_feature_branch_work = AsyncMock(return_value={'success': True})

            # Mock agent
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            mock_post.return_value = None

            # Execute
            await agent_executor.execute_agent(
                agent_name='code_reviewer',
                project_name='test-project',
                task_context=task_context
            )

            # Verify commit message
            call_kwargs = mock_fbm.finalize_feature_branch_work.call_args[1]
            commit_msg = call_kwargs['commit_message']

            assert 'issue #51' in commit_msg
            assert 'code_reviewer' in commit_msg
            assert 'task' in commit_msg.lower()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

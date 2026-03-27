"""
Tests for AgentExecutor with workspace abstraction integration.

These tests verify that AgentExecutor correctly uses workspace contexts.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

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

"""
Test that pipeline_run_id is properly propagated through agent execution.

This test suite ensures that when agents execute as part of a pipeline run,
the pipeline_run_id is correctly passed through to all observability events.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, AsyncMock, patch, call
from services.agent_executor import AgentExecutor
from monitoring.observability import ObservabilityManager, EventType


@pytest.fixture
def agent_executor():
    """Create an AgentExecutor instance with required mocks"""
    from services.agent_executor import AgentExecutor
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class TestPipelineRunIdPropagation:
    """Test suite for pipeline_run_id propagation through the execution stack"""
    
    @pytest.mark.asyncio
    async def test_agent_initialized_receives_pipeline_run_id(self, agent_executor):
        """Test that emit_agent_initialized is called with pipeline_run_id from task_context"""
        task_context = {
            'issue_number': 42,
            'pipeline_run_id': 'test-pipeline-run-123',
            'project': 'test-project',
            'board': 'Development',
            'column': 'In Progress'
        }
        
        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):
            
            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config
            
            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/issue-42', 'work_dir': '/workspace/test'}
            )
            mock_workspace.finalize_execution = AsyncMock(
                return_value={'success': True}
            )
            mock_factory.create.return_value = mock_workspace
            
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {'model': 'claude-sonnet-4.5', 'timeout': 3600}
            mock_create.return_value = mock_agent
            
            # Execute
            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )
            
            # Verify emit_agent_initialized was called with pipeline_run_id
            agent_executor.obs.emit_agent_initialized.assert_called_once()
            call_args = agent_executor.obs.emit_agent_initialized.call_args
            
            # Check positional and keyword arguments
            assert call_args[0][0] == 'test_agent'  # agent_name
            assert call_args[0][2] == 'test-project'  # project_name
            
            # Verify pipeline_run_id is passed (either as positional or keyword arg)
            if len(call_args[0]) >= 7:
                # If passed as positional arg
                assert call_args[0][6] == 'test-pipeline-run-123'
            else:
                # If passed as keyword arg
                assert call_args[1].get('pipeline_run_id') == 'test-pipeline-run-123'
    
    @pytest.mark.asyncio
    async def test_agent_completed_receives_pipeline_run_id_on_success(self, agent_executor):
        """Test that emit_agent_completed is called with pipeline_run_id on successful execution"""
        task_context = {
            'issue_number': 42,
            'pipeline_run_id': 'test-pipeline-run-456',
            'project': 'test-project',
            'board': 'Development',
            'column': 'In Progress'
        }
        
        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):
            
            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config
            
            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/issue-42', 'work_dir': '/workspace/test'}
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
            
            # Verify emit_agent_completed was called with pipeline_run_id
            agent_executor.obs.emit_agent_completed.assert_called_once()
            call_args = agent_executor.obs.emit_agent_completed.call_args
            
            # Check that success=True and pipeline_run_id is passed
            assert call_args[0][4] is True  # success parameter
            
            # Verify pipeline_run_id is passed
            if len(call_args[0]) >= 7:
                # If passed as positional arg
                assert call_args[0][6] == 'test-pipeline-run-456'
            else:
                # If passed as keyword arg
                assert call_args[1].get('pipeline_run_id') == 'test-pipeline-run-456'
    
    @pytest.mark.asyncio
    async def test_agent_completed_receives_pipeline_run_id_on_failure(self, agent_executor):
        """Test that emit_agent_completed is called with pipeline_run_id on failed execution"""
        task_context = {
            'issue_number': 42,
            'pipeline_run_id': 'test-pipeline-run-789',
            'project': 'test-project',
            'board': 'Development',
            'column': 'In Progress'
        }
        
        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):
            
            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config
            
            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/issue-42', 'work_dir': '/workspace/test'}
            )
            mock_factory.create.return_value = mock_workspace
            
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(side_effect=Exception("Agent execution failed"))
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent
            
            # Execute (should raise exception)
            with pytest.raises(Exception, match="Agent execution failed"):
                await agent_executor.execute_agent(
                    agent_name='test_agent',
                    project_name='test-project',
                    task_context=task_context
                )
            
            # Verify emit_agent_completed was called with pipeline_run_id even on failure
            agent_executor.obs.emit_agent_completed.assert_called_once()
            call_args = agent_executor.obs.emit_agent_completed.call_args
            
            # Check that success=False
            assert call_args[0][4] is False  # success parameter
            
            # Verify pipeline_run_id is passed
            if len(call_args[0]) >= 7:
                # If passed as positional arg
                assert call_args[0][6] == 'test-pipeline-run-789'
            else:
                # If passed as keyword arg
                assert call_args[1].get('pipeline_run_id') == 'test-pipeline-run-789'
    
    @pytest.mark.asyncio
    async def test_agent_execution_without_pipeline_run_id(self, agent_executor):
        """Test that agent execution works when pipeline_run_id is not present (backward compatibility)"""
        task_context = {
            'issue_number': 42,
            # No pipeline_run_id
            'project': 'test-project',
            'board': 'Development',
            'column': 'In Progress'
        }
        
        with patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('config.manager.config_manager') as mock_config, \
             patch.object(agent_executor.factory, 'create_agent') as mock_create, \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock):
            
            # Setup mocks
            mock_project_config = MagicMock()
            mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_config.get_project_config.return_value = mock_project_config
            
            mock_workspace = MagicMock()
            mock_workspace.prepare_execution = AsyncMock(
                return_value={'branch_name': 'feature/issue-42', 'work_dir': '/workspace/test'}
            )
            mock_workspace.finalize_execution = AsyncMock(
                return_value={'success': True}
            )
            mock_factory.create.return_value = mock_workspace
            
            mock_agent = MagicMock()
            mock_agent.execute = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create.return_value = mock_agent
            
            # Execute (should not raise exception)
            await agent_executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context
            )
            
            # Verify methods were called (but pipeline_run_id should be None)
            agent_executor.obs.emit_agent_initialized.assert_called_once()
            agent_executor.obs.emit_agent_completed.assert_called_once()
            
            # Verify pipeline_run_id is None or not set
            init_call_args = agent_executor.obs.emit_agent_initialized.call_args
            if len(init_call_args[0]) >= 7:
                assert init_call_args[0][6] is None
            else:
                assert init_call_args[1].get('pipeline_run_id') is None


class TestObservabilityHelperMethods:
    """Test that observability helper methods properly handle pipeline_run_id"""
    
    @pytest.fixture
    def obs_manager(self):
        """Create observability manager with mocked dependencies"""
        with patch('monitoring.observability.redis.Redis'), \
             patch('monitoring.observability.Elasticsearch'):
            obs = ObservabilityManager(enabled=True)
            obs.redis = MagicMock()
            obs.es = MagicMock()
            return obs
    
    def test_emit_agent_initialized_with_pipeline_run_id(self, obs_manager):
        """Test that emit_agent_initialized passes pipeline_run_id to emit()"""
        with patch.object(obs_manager, 'emit') as mock_emit:
            obs_manager.emit_agent_initialized(
                agent="test_agent",
                task_id="task_123",
                project="test-project",
                config={'model': 'claude-sonnet-4.5'},
                branch_name="feature/test",
                container_name="claude-agent-test",
                pipeline_run_id="run-123"
            )
            
            # Verify emit was called with pipeline_run_id
            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            
            # Check pipeline_run_id parameter (last parameter)
            assert call_args[0][-1] == "run-123" or call_args[1].get('pipeline_run_id') == "run-123"
    
    def test_emit_agent_completed_with_pipeline_run_id(self, obs_manager):
        """Test that emit_agent_completed passes pipeline_run_id to emit()"""
        with patch.object(obs_manager, 'emit') as mock_emit:
            obs_manager.emit_agent_completed(
                agent="test_agent",
                task_id="task_123",
                project="test-project",
                duration_ms=5000.0,
                success=True,
                error=None,
                pipeline_run_id="run-456"
            )
            
            # Verify emit was called with pipeline_run_id
            mock_emit.assert_called_once()
            call_args = mock_emit.call_args
            
            # Check pipeline_run_id parameter (last parameter)
            assert call_args[0][-1] == "run-456" or call_args[1].get('pipeline_run_id') == "run-456"
    
    def test_emit_agent_initialized_without_pipeline_run_id(self, obs_manager):
        """Test backward compatibility: emit_agent_initialized works without pipeline_run_id"""
        with patch.object(obs_manager, 'emit') as mock_emit:
            obs_manager.emit_agent_initialized(
                agent="test_agent",
                task_id="task_123",
                project="test-project",
                config={'model': 'claude-sonnet-4.5'}
            )
            
            # Should not raise exception
            mock_emit.assert_called_once()
    
    def test_emit_agent_completed_without_pipeline_run_id(self, obs_manager):
        """Test backward compatibility: emit_agent_completed works without pipeline_run_id"""
        with patch.object(obs_manager, 'emit') as mock_emit:
            obs_manager.emit_agent_completed(
                agent="test_agent",
                task_id="task_123",
                project="test-project",
                duration_ms=5000.0,
                success=True
            )
            
            # Should not raise exception
            mock_emit.assert_called_once()

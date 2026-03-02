"""
Unit tests for orchestrator agent routing

Tests the agent selection and routing logic without actually executing agents.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, patch, AsyncMock
from tests.unit.orchestrator.mocks import MockGitHubAPI, MockAgentExecutor
from tests.unit.orchestrator.conftest import create_test_issue, configure_agent_results
from tests.unit.orchestrator.mocks.mock_agents import success_result


class TestAgentRouting:
    """Test agent selection and routing logic"""
    
    def test_trigger_agent_for_status_routes_to_correct_agent(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that correct agent is selected based on status"""
        # Setup: Create issue in Requirements status
        create_test_issue(mock_github, 100, 'Requirements')
        
        # Create project monitor with mocks
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            # Mock pipeline run manager
            mock_run = Mock()
            mock_run.id = 'run-123'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            
            # Mock get_issue_details to return the issue
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Trigger agent for Requirements status
            result = monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=100,
                status='Requirements',
                repository='test-repo'
            )
            
            # Assert: Agent routing decision event was emitted
            assert mock_observability[1].emit_agent_routing_decision.called
            call_args = mock_observability[1].emit_agent_routing_decision.call_args[1]
            assert call_args['selected_agent'] == 'business_analyst'
            assert call_args['current_status'] == 'Requirements'
    
    def test_skip_agent_for_closed_issue(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that closed issues don't trigger agents"""
        # Setup: Create closed issue
        create_test_issue(mock_github, 101, 'Requirements', state='CLOSED')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Trigger agent
            result = monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=101,
                status='Requirements',
                repository='test-repo'
            )
            
            # Assert: No agent triggered
            assert result is None
    
    def test_no_agent_for_done_status(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that Done status (no agent) doesn't trigger anything"""
        # Setup: Create issue in Done status
        create_test_issue(mock_github, 102, 'Done')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            mock_run = Mock()
            mock_run.id = 'run-123'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Trigger agent
            result = monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=102,
                status='Done',
                repository='test-repo'
            )
            
            # Assert: No agent triggered (Done has agent=None)
            assert result is None or result == 'null'
    
    def test_skip_duplicate_tasks(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that duplicate tasks for same issue+agent are not created"""
        # Setup: Create issue
        create_test_issue(mock_github, 103, 'Requirements')
        
        # Mock existing task in queue
        existing_task = Mock()
        existing_task.agent = 'business_analyst'
        existing_task.context = {
            'issue_number': 103,
            'project': 'test-project',
            'board': 'dev'
        }
        mock_task_queue.get_pending_tasks.return_value = [existing_task]
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            mock_run = Mock()
            mock_run.id = 'run-123'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Trigger agent
            result = monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=103,
                status='Requirements',
                repository='test-repo'
            )
            
            # Assert: No new task created
            assert result is None
    
    def test_different_statuses_route_to_different_agents(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that different statuses route to different agents"""
        # Setup: Create issues in different statuses
        create_test_issue(mock_github, 200, 'Requirements')
        create_test_issue(mock_github, 201, 'Design')
        create_test_issue(mock_github, 202, 'Development')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            mock_run = Mock()
            mock_run.id = 'run-123'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Clear mock call history
            mock_observability[1].reset_mock()
            
            # Trigger agents for different statuses
            monitor.trigger_agent_for_status('test-project', 'dev', 200, 'Requirements', 'test-repo')
            monitor.trigger_agent_for_status('test-project', 'dev', 201, 'Design', 'test-repo')
            monitor.trigger_agent_for_status('test-project', 'dev', 202, 'Development', 'test-repo')
            
            # Assert: Different agents selected
            calls = mock_observability[1].emit_agent_routing_decision.call_args_list
            assert len(calls) == 3
            
            # Extract selected agents from calls
            agents_selected = [call[1]['selected_agent'] for call in calls]
            assert 'business_analyst' in agents_selected  # Requirements
            assert 'software_architect' in agents_selected  # Design
            assert 'senior_software_engineer' in agents_selected  # Development


class TestAgentRoutingWithWorkspaceTypes:
    """Test agent routing with different workspace types"""
    
    def test_issues_workspace_routing(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test routing in issues workspace"""
        create_test_issue(mock_github, 300, 'Requirements')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            mock_run = Mock()
            mock_run.id = 'run-123'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Trigger agent
            monitor.trigger_agent_for_status('test-project', 'dev', 300, 'Requirements', 'test-repo')
            
            # Assert: Workspace type is 'issues'
            call_args = mock_observability[1].emit_agent_routing_decision.call_args[1]
            assert call_args['workspace_type'] == 'issues'


class TestPipelineRunTracking:
    """Test pipeline run ID tracking for event correlation"""
    
    def test_pipeline_run_created_for_issue(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that pipeline run is created and tracked"""
        create_test_issue(mock_github, 400, 'Requirements')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            # Mock pipeline run
            mock_run = Mock()
            mock_run.id = 'pipeline-run-400'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Trigger agent
            monitor.trigger_agent_for_status('test-project', 'dev', 400, 'Requirements', 'test-repo')
            
            # Assert: Pipeline run was created
            assert mock_pipeline_mgr.return_value.get_or_create_pipeline_run.called
            
            # Assert: Pipeline run ID passed to decision event
            call_args = mock_observability[1].emit_agent_routing_decision.call_args[1]
            assert call_args['pipeline_run_id'] == 'pipeline-run-400'

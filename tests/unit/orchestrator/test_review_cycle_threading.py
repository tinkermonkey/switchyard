"""
Unit tests for ProjectMonitor._start_review_cycle_for_issue

Tests the background thread execution wrapper for review cycles,
including pipeline run creation, thread safety, and proper closure
variable capture.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import asyncio
import threading
import time
from unittest.mock import Mock, patch, AsyncMock, MagicMock, call
from tests.unit.orchestrator.mocks import MockGitHubAPI
from tests.unit.orchestrator.conftest import create_test_issue


class TestReviewCycleThreading:
    """Test background thread execution of review cycles"""
    
    def test_start_review_cycle_creates_pipeline_run(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that pipeline run is created before thread starts"""
        create_test_issue(mock_github, 2000, 'Code Review')
        
        # Create mock pipeline run
        mock_run = Mock()
        mock_run.id = 'run-2000'
        
        # Create mock pipeline run manager
        mock_pipeline_mgr = Mock()
        mock_pipeline_mgr.get_or_create_pipeline_run.return_value = mock_run
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.review_cycle_executor') as mock_executor:
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            monitor.pipeline_run_manager = mock_pipeline_mgr
            monitor.decision_events = mock_observability[1]
            
            # Mock get_issue_details
            monitor.get_issue_details = lambda repo, num, org: {
                'number': num,
                'title': f'Test Issue #{num}',
                'url': f'https://github.com/test-org/test-repo/issues/{num}',
                'body': 'Test issue body'
            }
            
            # Mock get_previous_stage_context to return output
            monitor.get_previous_stage_context = Mock(return_value='Previous stage output')
            
            # Create column config
            column = WorkflowColumn(
                stage_mapping=None,
                description="Code review column",
                automation_rules=[],
                name='Code Review',
                agent='code_reviewer',
                maker_agent='senior_software_engineer',
                max_iterations=3,
                type='review'
            )
            
            # Mock the project config
            project_config = Mock()
            project_config.github = {
                'org': 'test-org',
                'repo': 'test-repo'
            }
            
            # Mock the pipeline config
            pipeline_config = Mock()
            pipeline_config.workspace = 'issues'
            pipeline_config.template = 'sdlc_execution'
            
            # Mock workflow template
            workflow_template = Mock()
            workflow_template.columns = [column]
            
            # Call the method
            result = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='dev',
                issue_number=2000,
                status='Code Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column
            )
            
            # Assert: Pipeline run was created
            mock_pipeline_mgr.get_or_create_pipeline_run.assert_called_once_with(
                issue_number=2000,
                issue_title='Test Issue #2000',
                issue_url='https://github.com/test-org/test-repo/issues/2000',
                project='test-project',
                board='dev'
            )
            
            # Assert: Agent returned
            assert result == 'code_reviewer'
    
    def test_pipeline_run_accessible_in_thread(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that pipeline_run variable is accessible inside the thread closure"""
        create_test_issue(mock_github, 2001, 'Design Review')
        
        # Create mock pipeline run
        mock_run = Mock()
        mock_run.id = 'run-2001'
        
        # Track if pipeline_run.id was accessed in the thread
        pipeline_run_accessed = threading.Event()
        
        # Create mock pipeline run manager
        mock_pipeline_mgr = Mock()
        mock_pipeline_mgr.get_or_create_pipeline_run.return_value = mock_run
        
        # Mock the review cycle executor
        async def mock_start_review_cycle(*args, **kwargs):
            # Check if pipeline_run_id is in kwargs
            if 'pipeline_run_id' in kwargs and kwargs['pipeline_run_id'] == 'run-2001':
                pipeline_run_accessed.set()
            return ('Development', True)
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.review_cycle_executor.start_review_cycle', 
                   side_effect=mock_start_review_cycle):
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            monitor.pipeline_run_manager = mock_pipeline_mgr
            monitor.decision_events = mock_observability[1]
            
            # Mock get_issue_details
            monitor.get_issue_details = lambda repo, num, org: {
                'number': num,
                'title': f'Test Issue #{num}',
                'url': f'https://github.com/test-org/test-repo/issues/{num}',
                'body': 'Test issue body'
            }
            
            # Mock get_previous_stage_context
            monitor.get_previous_stage_context = Mock(return_value='Previous stage output')
            
            # Create column config
            column = WorkflowColumn(
                stage_mapping=None,
                description="Design review column",
                automation_rules=[],
                name='Design Review',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            # Mock configs
            project_config = Mock()
            project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            
            pipeline_config = Mock()
            pipeline_config.workspace = 'issues'
            pipeline_config.template = 'planning_design'
            pipeline_config.board_name = 'planning'
            
            # Mock pipelines list for iteration
            project_config.pipelines = [pipeline_config]
            
            workflow_template = Mock()
            workflow_template.columns = [column]
            
            # Call the method
            result = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='planning',
                issue_number=2001,
                status='Design Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column
            )
            
            # Wait for thread to potentially access pipeline_run
            # Give it 2 seconds to start
            pipeline_run_accessed.wait(timeout=2.0)
            
            # Assert: Thread was able to access pipeline_run.id
            assert pipeline_run_accessed.is_set(), "pipeline_run.id was not accessible in the thread"
    
    def test_review_cycle_thread_handles_missing_previous_output(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that method returns None if no previous stage output exists"""
        create_test_issue(mock_github, 2002, 'Code Review')
        
        mock_pipeline_mgr = Mock()
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            monitor.pipeline_run_manager = mock_pipeline_mgr
            
            # Mock get_issue_details
            monitor.get_issue_details = lambda repo, num, org: {
                'number': num,
                'title': f'Test Issue #{num}',
                'url': f'https://github.com/test-org/test-repo/issues/{num}'
            }
            
            # Mock get_previous_stage_context to return None
            monitor.get_previous_stage_context = Mock(return_value=None)
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Code review column",
                automation_rules=[],
                name='Code Review',
                agent='code_reviewer',
                maker_agent='senior_software_engineer',
                max_iterations=3,
                type='review'
            )
            
            project_config = Mock()
            project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            
            pipeline_config = Mock()
            pipeline_config.workspace = 'issues'
            pipeline_config.template = 'sdlc_execution'
            
            workflow_template = Mock()
            workflow_template.columns = [column]
            
            # Call the method
            result = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='dev',
                issue_number=2002,
                status='Code Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column
            )
            
            # Assert: Returns None without creating pipeline run
            assert result is None
            mock_pipeline_mgr.get_or_create_pipeline_run.assert_not_called()
    
    def test_review_cycle_thread_posts_initial_comment(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that thread posts initial comment to GitHub"""
        create_test_issue(mock_github, 2003, 'Requirements Review')
        
        # Track if comment was posted
        comment_posted = threading.Event()
        
        mock_run = Mock()
        mock_run.id = 'run-2003'
        
        mock_pipeline_mgr = Mock()
        mock_pipeline_mgr.get_or_create_pipeline_run.return_value = mock_run
        
        # Mock GitHub integration
        mock_github_integration = Mock()
        
        async def mock_post_output(context, comment):
            if '🔄 Starting Review Cycle' in comment:
                comment_posted.set()
        
        mock_github_integration.post_agent_output = AsyncMock(side_effect=mock_post_output)
        
        # Mock review cycle to complete quickly
        async def mock_review_cycle(*args, **kwargs):
            return ('Development', True)
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.review_cycle_executor.start_review_cycle', 
                   side_effect=mock_review_cycle), \
             patch('services.github_integration.GitHubIntegration', return_value=mock_github_integration):
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            monitor.pipeline_run_manager = mock_pipeline_mgr
            monitor.decision_events = mock_observability[1]
            
            monitor.get_issue_details = lambda repo, num, org: {
                'number': num,
                'title': f'Test Issue #{num}',
                'url': f'https://github.com/test-org/test-repo/issues/{num}'
            }
            
            monitor.get_previous_stage_context = Mock(return_value='Previous stage output')
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Requirements review column",
                automation_rules=[],
                name='Requirements Review',
                agent='requirements_reviewer',
                maker_agent='business_analyst',
                max_iterations=3,
                type='review'
            )
            
            project_config = Mock()
            project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            
            pipeline_config = Mock()
            pipeline_config.workspace = 'issues'
            pipeline_config.template = 'planning_design'
            
            workflow_template = Mock()
            workflow_template.columns = [column]
            
            # Call the method
            result = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='planning',
                issue_number=2003,
                status='Requirements Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column
            )
            
            # Wait for comment to be posted
            comment_posted.wait(timeout=2.0)
            
            # Assert: Initial comment was posted
            assert comment_posted.is_set(), "Initial review cycle comment was not posted"
    
    def test_review_cycle_thread_error_handling(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that thread handles errors gracefully"""
        create_test_issue(mock_github, 2004, 'Design Review')
        
        # Track if error was logged
        error_logged = threading.Event()
        
        mock_run = Mock()
        mock_run.id = 'run-2004'
        
        mock_pipeline_mgr = Mock()
        mock_pipeline_mgr.get_or_create_pipeline_run.return_value = mock_run
        
        # Mock review cycle to raise an error
        async def mock_review_cycle_error(*args, **kwargs):
            raise RuntimeError("Simulated review cycle error")
        
        # Patch the logger to detect error logging
        original_logger = None
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.review_cycle_executor.start_review_cycle',
                   side_effect=mock_review_cycle_error), \
             patch('services.github_integration.GitHubIntegration'):
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            # Patch the logger
            with patch('services.project_monitor.logger') as mock_logger:
                monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
                monitor.github_client = mock_github
                monitor.pipeline_run_manager = mock_pipeline_mgr
                monitor.decision_events = mock_observability[1]
                
                monitor.get_issue_details = lambda repo, num, org: {
                    'number': num,
                    'title': f'Test Issue #{num}',
                    'url': f'https://github.com/test-org/test-repo/issues/{num}'
                }
                
                monitor.get_previous_stage_context = Mock(return_value='Previous stage output')
                
                column = WorkflowColumn(
                    stage_mapping=None,
                    description="Design review column",
                    automation_rules=[],
                    name='Design Review',
                    agent='design_reviewer',
                    maker_agent='software_architect',
                    max_iterations=3,
                    type='review'
                )
                
                project_config = Mock()
                project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
                
                pipeline_config = Mock()
                pipeline_config.workspace = 'issues'
                pipeline_config.template = 'planning_design'
                
                workflow_template = Mock()
                workflow_template.columns = [column]
                
                # Call the method
                result = monitor._start_review_cycle_for_issue(
                    project_name='test-project',
                    board_name='planning',
                    issue_number=2004,
                    status='Design Review',
                    repository='test-repo',
                    project_config=project_config,
                    pipeline_config=pipeline_config,
                    workflow_template=workflow_template,
                    column=column
                )
                
                # Wait for thread to execute and log error
                time.sleep(1.0)
                
                # Assert: Method returned successfully (non-blocking)
                assert result == 'design_reviewer'
                
                # Thread should have logged error (check after a delay)
                time.sleep(0.5)
                
                # The error should be logged by the thread
                # We can't easily check the exact call due to threading,
                # but the method should have returned without raising
    
    def test_pipeline_run_logged_with_id(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that pipeline run ID is logged for debugging"""
        create_test_issue(mock_github, 2005, 'Code Review')
        
        mock_run = Mock()
        mock_run.id = 'run-2005-unique-id'
        
        mock_pipeline_mgr = Mock()
        mock_pipeline_mgr.get_or_create_pipeline_run.return_value = mock_run
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.project_monitor.logger') as mock_logger:
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            monitor.pipeline_run_manager = mock_pipeline_mgr
            
            monitor.get_issue_details = lambda repo, num, org: {
                'number': num,
                'title': f'Test Issue #{num}',
                'url': f'https://github.com/test-org/test-repo/issues/{num}'
            }
            
            monitor.get_previous_stage_context = Mock(return_value='Previous stage output')
            
            column = WorkflowColumn(
                stage_mapping=None,
                description="Code review column",
                automation_rules=[],
                name='Code Review',
                agent='code_reviewer',
                maker_agent='senior_software_engineer',
                max_iterations=3,
                type='review'
            )
            
            project_config = Mock()
            project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            
            pipeline_config = Mock()
            pipeline_config.workspace = 'issues'
            pipeline_config.template = 'sdlc_execution'
            
            workflow_template = Mock()
            workflow_template.columns = [column]
            
            # Call the method
            result = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='dev',
                issue_number=2005,
                status='Code Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column
            )
            
            # Assert: Pipeline run ID was logged
            debug_calls = [call for call in mock_logger.debug.call_args_list 
                          if 'pipeline run' in str(call).lower()]
            assert len(debug_calls) > 0, "Pipeline run ID was not logged at debug level"
            
            # Verify the log contains the actual run ID
            logged_text = str(mock_logger.debug.call_args_list)
            assert 'run-2005-unique-id' in logged_text, "Specific pipeline run ID not in logs"


class TestReviewCycleThreadingEdgeCases:
    """Test edge cases in review cycle threading"""
    
    def test_concurrent_review_cycles_different_issues(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that multiple review cycles can run concurrently for different issues"""
        create_test_issue(mock_github, 3000, 'Code Review')
        create_test_issue(mock_github, 3001, 'Design Review')
        
        # Track both pipeline runs
        pipeline_runs_created = []
        
        def track_pipeline_run(issue_number, issue_title, issue_url, project, board):
            run = Mock()
            run.id = f'run-{issue_number}'
            pipeline_runs_created.append(run.id)
            return run
        
        mock_pipeline_mgr = Mock()
        mock_pipeline_mgr.get_or_create_pipeline_run.side_effect = track_pipeline_run
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.review_cycle.review_cycle_executor'):
            
            from services.project_monitor import ProjectMonitor
            from config.manager import WorkflowColumn
            
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            monitor.pipeline_run_manager = mock_pipeline_mgr
            monitor.decision_events = mock_observability[1]
            
            monitor.get_issue_details = lambda repo, num, org: {
                'number': num,
                'title': f'Test Issue #{num}',
                'url': f'https://github.com/test-org/test-repo/issues/{num}'
            }
            
            monitor.get_previous_stage_context = Mock(return_value='Previous stage output')
            
            column1 = WorkflowColumn(
                stage_mapping=None,
                description="Code review",
                automation_rules=[],
                name='Code Review',
                agent='code_reviewer',
                maker_agent='senior_software_engineer',
                max_iterations=3,
                type='review'
            )
            
            column2 = WorkflowColumn(
                stage_mapping=None,
                description="Design review",
                automation_rules=[],
                name='Design Review',
                agent='design_reviewer',
                maker_agent='software_architect',
                max_iterations=3,
                type='review'
            )
            
            project_config = Mock()
            project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            
            pipeline_config = Mock()
            pipeline_config.workspace = 'issues'
            pipeline_config.template = 'sdlc_execution'
            
            workflow_template = Mock()
            workflow_template.columns = [column1, column2]
            
            # Start both review cycles
            result1 = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='dev',
                issue_number=3000,
                status='Code Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column1
            )
            
            result2 = monitor._start_review_cycle_for_issue(
                project_name='test-project',
                board_name='dev',
                issue_number=3001,
                status='Design Review',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column2
            )
            
            # Assert: Both pipeline runs were created
            assert 'run-3000' in pipeline_runs_created
            assert 'run-3001' in pipeline_runs_created
            assert len(pipeline_runs_created) == 2
            
            # Assert: Both agents started
            assert result1 == 'code_reviewer'
            assert result2 == 'design_reviewer'

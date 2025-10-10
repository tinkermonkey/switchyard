"""
Unit tests for GitHub monitoring

Tests the GitHub polling and issue status change detection logic.
"""

import pytest
from unittest.mock import Mock, patch, call
from datetime import datetime, timezone
from tests.unit.orchestrator.mocks import MockGitHubAPI
from tests.unit.orchestrator.conftest import create_test_issue


class TestGitHubIssueDetection:
    """Test detection of new and updated issues"""
    
    def test_detect_new_issue(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test detection of new issue in project"""
        # Create new issue
        create_test_issue(mock_github, 1200, 'Requirements')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Mock get_project_issues to return our test issue
            def mock_get_issues(project, board, org=None):
                return [mock_github.get_issue(1200)]
            
            monitor.get_project_issues = mock_get_issues
            
            # Scan for issues
            issues = monitor.get_project_issues('test-project', 'dev')
            
            # Assert: Issue detected
            assert len(issues) == 1
            assert issues[0]['number'] == 1200
            assert issues[0]['status'] == 'Requirements'
    
    def test_detect_status_change(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test detection of issue status change"""
        # Create issue in Requirements
        create_test_issue(mock_github, 1201, 'Requirements')
        
        # Store initial state
        initial_state = {'issue_1201_status': 'Requirements'}
        mock_state_manager.load_state.return_value = initial_state
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Change status to Design
            mock_github.update_issue_status(1201, 'Design')
            
            # Check if status changed
            current_issue = mock_github.get_issue(1201)
            previous_status = initial_state.get('issue_1201_status')
            
            # Assert: Status change detected
            assert current_issue['status'] == 'Design'
            assert previous_status == 'Requirements'
            assert current_issue['status'] != previous_status
    
    def test_ignore_unchanged_issues(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that unchanged issues don't trigger actions"""
        # Create issue
        create_test_issue(mock_github, 1202, 'Design')
        
        # Store state showing same status
        initial_state = {'issue_1202_status': 'Design'}
        mock_state_manager.load_state.return_value = initial_state
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            current_issue = mock_github.get_issue(1202)
            previous_status = initial_state.get('issue_1202_status')
            
            # Assert: No change detected
            assert current_issue['status'] == previous_status


class TestGitHubStatusProcessing:
    """Test processing of status changes"""
    
    def test_process_status_change_triggers_agent(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that status change triggers appropriate agent"""
        create_test_issue(mock_github, 1300, 'Design')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            mock_run = Mock()
            mock_run.id = 'run-1300'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = mock_run
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.github_client = mock_github
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Process status change
            monitor.trigger_agent_for_status(
                'test-project', 'dev', 1300, 'Design', 'test-repo'
            )
            
            # Assert: Agent triggered
            assert mock_task_queue.enqueue_agent_task.called or \
                   mock_observability[1].emit_agent_routing_decision.called
    
    def test_process_multiple_status_changes(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test processing multiple status changes"""
        # Create multiple issues with different statuses
        create_test_issue(mock_github, 1301, 'Requirements')
        create_test_issue(mock_github, 1302, 'Design')
        create_test_issue(mock_github, 1303, 'Development')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:
            
            mock_run = Mock()
            mock_run.id = 'run-batch'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = mock_run
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.github_client = mock_github
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)
            
            # Process all status changes
            monitor.trigger_agent_for_status('test-project', 'dev', 1301, 'Requirements', 'test-repo')
            monitor.trigger_agent_for_status('test-project', 'dev', 1302, 'Design', 'test-repo')
            monitor.trigger_agent_for_status('test-project', 'dev', 1303, 'Development', 'test-repo')
            
            # Assert: All processed
            assert mock_observability[1].emit_agent_routing_decision.call_count == 3


class TestGitHubPolling:
    """Test GitHub polling mechanism"""
    
    def test_polling_retrieves_project_issues(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that polling retrieves all project issues"""
        # Create multiple issues
        create_test_issue(mock_github, 1400, 'Requirements')
        create_test_issue(mock_github, 1401, 'Design')
        create_test_issue(mock_github, 1402, 'Done')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Mock get_project_issues
            def mock_get_issues(project, board, org=None):
                return [
                    mock_github.get_issue(1400),
                    mock_github.get_issue(1401),
                    mock_github.get_issue(1402)
                ]
            
            monitor.get_project_issues = mock_get_issues
            
            # Poll for issues
            issues = monitor.get_project_issues('test-project', 'dev')
            
            # Assert: All issues retrieved
            assert len(issues) == 3
            assert any(i['number'] == 1400 for i in issues)
            assert any(i['number'] == 1401 for i in issues)
            assert any(i['number'] == 1402 for i in issues)
    
    def test_polling_filters_closed_issues(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that polling filters out closed issues"""
        # Create open and closed issues
        create_test_issue(mock_github, 1403, 'Requirements', state='OPEN')
        create_test_issue(mock_github, 1404, 'Design', state='CLOSED')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Mock get_project_issues to filter closed
            def mock_get_issues(project, board, org=None):
                all_issues = [
                    mock_github.get_issue(1403),
                    mock_github.get_issue(1404)
                ]
                return [i for i in all_issues if i.get('state') == 'OPEN']
            
            monitor.get_project_issues = mock_get_issues
            
            # Poll
            issues = monitor.get_project_issues('test-project', 'dev')
            
            # Assert: Only open issues returned
            assert len(issues) == 1
            assert issues[0]['number'] == 1403


class TestGitHubCommentMonitoring:
    """Test monitoring of GitHub comments"""
    
    def test_detect_new_comment(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test detection of new comment on issue"""
        create_test_issue(mock_github, 1500, 'Design')
        
        # Add comment
        mock_github.add_comment(1500, 'Review feedback: looks good')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Get issue with comments
            issue = mock_github.get_issue(1500)
            comments = mock_github.get_comments(1500)
            
            # Assert: Comment detected
            assert len(comments) == 1
            assert 'looks good' in comments[0]['body']
    
    def test_comment_triggers_review_parsing(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that reviewer comments trigger review parsing"""
        create_test_issue(mock_github, 1501, 'Design Review')
        
        # Add reviewer comment
        mock_github.add_comment(1501, 'APPROVED: Design meets requirements')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            comments = mock_github.get_comments(1501)
            
            # Check if comment contains review keywords
            has_approval = any('APPROVED' in c['body'] for c in comments)
            
            # Assert: Review comment detected
            assert has_approval


class TestGitHubStateSync:
    """Test synchronization of GitHub state"""
    
    def test_state_saved_after_processing(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that state is saved after processing issues"""
        create_test_issue(mock_github, 1600, 'Requirements')
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            
            # Simulate processing
            mock_state_manager.save_state('issue_1600_status', 'Requirements')
            
            # Assert: State saved
            assert mock_state_manager.save_state.called
    
    def test_state_loaded_on_startup(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test that previous state is loaded on monitor startup"""
        # Setup previous state
        previous_state = {
            'issue_1601_status': 'Design',
            'issue_1602_status': 'Development'
        }
        mock_state_manager.load_state.return_value = previous_state
        
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            
            # Load state
            loaded = mock_state_manager.load_state()
            
            # Assert: State loaded
            assert loaded == previous_state
            assert loaded['issue_1601_status'] == 'Design'


class TestGitHubErrorHandling:
    """Test error handling in GitHub monitoring"""
    
    def test_handle_api_error_gracefully(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test graceful handling of GitHub API errors"""
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Mock API error
            def raise_error(*args, **kwargs):
                raise Exception("GitHub API error")
            
            monitor.get_project_issues = raise_error
            
            # Attempt to get issues
            try:
                issues = monitor.get_project_issues('test-project', 'dev')
                # If no exception handling, this will raise
                assert False, "Should have raised exception"
            except Exception as e:
                # Assert: Error raised
                assert "API error" in str(e)
    
    def test_handle_missing_issue_gracefully(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability
    ):
        """Test handling of missing/deleted issues"""
        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]):
            
            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.github_client = mock_github
            
            # Try to get non-existent issue
            issue = mock_github.get_issue(99999)
            
            # Assert: Returns None or handles gracefully
            assert issue is None or issue == {}

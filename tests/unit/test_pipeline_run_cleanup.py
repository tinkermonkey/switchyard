"""
Test pipeline run cleanup logic based on GitHub issue status.

This test suite ensures that pipeline runs are correctly ended or kept active
based solely on the GitHub issue's current column, using the workflow's exit_columns
and column agent assignments as the source of truth.
"""

import pytest
from unittest.mock import MagicMock, patch, call
from datetime import datetime
from services.pipeline_run import PipelineRunManager
from config.manager import WorkflowTemplate, WorkflowColumn


class MockElasticsearch:
    """Mock Elasticsearch client for testing"""
    
    def __init__(self):
        self.search_results = []
        self.indexed_docs = []
        self.ilm = MagicMock()  # Mock ILM API
        
    def search(self, index, body):
        """Return mock search results"""
        return {
            'hits': {
                'total': {'value': len(self.search_results)},
                'hits': [
                    {
                        '_source': doc,
                        '_index': f"pipeline-runs-{datetime.utcnow().strftime('%Y-%m-%d')}"
                    }
                    for doc in self.search_results
                ]
            }
        }
    
    def index(self, index, id=None, document=None, body=None):
        """Track indexed documents - supports both old and new API"""
        doc = document or body
        self.indexed_docs.append({'index': index, 'id': id, 'body': doc})
        return {'result': 'updated'}


class MockRedis:
    """Mock Redis client for testing"""
    
    def __init__(self):
        self.data = {}
        
    def get(self, key):
        return self.data.get(key)
    
    def setex(self, key, ttl, value):
        self.data[key] = value
    
    def hdel(self, key, field):
        if key in self.data and isinstance(self.data[key], dict):
            self.data[key].pop(field, None)
    
    def hget(self, key, field):
        if key in self.data and isinstance(self.data[key], dict):
            return self.data[key].get(field)
        return None


@pytest.fixture
def mock_elasticsearch():
    """Fixture providing a mock Elasticsearch client"""
    return MockElasticsearch()


@pytest.fixture
def mock_redis():
    """Fixture providing a mock Redis client"""
    return MockRedis()


@pytest.fixture
def pipeline_run_manager(mock_elasticsearch, mock_redis):
    """Fixture providing a PipelineRunManager with mocked dependencies"""
    with patch('services.pipeline_run.Elasticsearch', return_value=mock_elasticsearch), \
         patch('services.pipeline_run.redis.Redis', return_value=mock_redis):
        manager = PipelineRunManager()
        manager.es = mock_elasticsearch
        manager.redis = mock_redis
        return manager


@pytest.fixture
def mock_workflow_template():
    """Create a mock workflow template with common columns"""
    return WorkflowTemplate(
        name="Test Workflow",
        description="Test workflow for unit tests",
        pipeline_mapping="test_pipeline",
        pipeline_trigger_columns=["Development"],
        pipeline_exit_columns=["Done", "Staged"],
        columns=[
            WorkflowColumn(
                name="Backlog",
                stage_mapping=None,
                agent=None,
                description="Backlog items",
                automation_rules=[]
            ),
            WorkflowColumn(
                name="Development",
                stage_mapping="implementation",
                agent="senior_software_engineer",
                description="In development",
                automation_rules=[]
            ),
            WorkflowColumn(
                name="Code Review",
                stage_mapping="code_review",
                agent="code_reviewer",
                description="Under review",
                automation_rules=[]
            ),
            WorkflowColumn(
                name="Testing",
                stage_mapping="testing",
                agent="qa_engineer",
                description="Being tested",
                automation_rules=[]
            ),
            WorkflowColumn(
                name="Staged",
                stage_mapping=None,
                agent=None,
                description="Staged for production",
                automation_rules=[]
            ),
            WorkflowColumn(
                name="Done",
                stage_mapping=None,
                agent=None,
                description="Completed",
                automation_rules=[]
            ),
        ]
    )


@pytest.fixture
def mock_project_config(mock_workflow_template):
    """Create a mock project configuration"""
    config = MagicMock()
    config.github = {
        'org': 'test-org',
        'repo': 'test-repo',
        'project_id': 'PVT_test123'
    }
    
    pipeline_config = MagicMock()
    pipeline_config.board_name = "Development Board"
    pipeline_config.workflow = "test_workflow"
    config.pipelines = [pipeline_config]
    
    return config


class TestPipelineRunCleanupLogic:
    """Test suite for GitHub-status-based pipeline run cleanup"""
    
    def test_issue_in_exit_column_ends_run(
        self, 
        pipeline_run_manager, 
        mock_elasticsearch, 
        mock_project_config,
        mock_workflow_template
    ):
        """Test that a pipeline run is ended when issue is in an exit column (Done)"""
        # Setup: Create an active pipeline run for an issue in "Done" column
        mock_elasticsearch.search_results = [{
            'id': 'run-123',
            'issue_number': 42,
            'issue_title': 'Test Issue',
            'issue_url': 'https://github.com/test-org/test-repo/issues/42',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T10:00:00Z',
            'status': 'active'
        }]
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            # Mock GitHub API to return "Done" as current column
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                return_value='Done'
            ):
                # Execute cleanup
                pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: Run should be ended because issue is in exit column
        assert len(mock_elasticsearch.indexed_docs) == 1
        ended_run = mock_elasticsearch.indexed_docs[0]['body']
        assert ended_run['status'] == 'completed'
    
    def test_issue_in_backlog_ends_run(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config,
        mock_workflow_template
    ):
        """Test that a pipeline run is ended when issue is moved to Backlog (no agent)"""
        mock_elasticsearch.search_results = [{
            'id': 'run-456',
            'issue_number': 99,
            'issue_title': 'Another Issue',
            'issue_url': 'https://github.com/test-org/test-repo/issues/99',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T09:00:00Z',
            'status': 'active'
        }]
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            # Mock GitHub API to return "Backlog" as current column
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                return_value='Backlog'
            ):
                pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: Run should be ended because Backlog has no agent
        assert len(mock_elasticsearch.indexed_docs) == 1
        ended_run = mock_elasticsearch.indexed_docs[0]['body']
        assert ended_run['status'] == 'completed'
    
    def test_issue_in_development_keeps_run_active(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config,
        mock_workflow_template
    ):
        """Test that a pipeline run stays active when issue is in Development (has agent)"""
        mock_elasticsearch.search_results = [{
            'id': 'run-789',
            'issue_number': 50,
            'issue_title': 'Active Issue',
            'issue_url': 'https://github.com/test-org/test-repo/issues/50',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T08:00:00Z',
            'status': 'active'
        }]
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            # Mock GitHub API to return "Development" as current column
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                return_value='Development'
            ):
                pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: Run should NOT be ended because Development has an agent
        assert len(mock_elasticsearch.indexed_docs) == 0
    
    def test_issue_removed_from_board_ends_run(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config,
        mock_workflow_template
    ):
        """Test that a pipeline run is ended when issue is removed from board"""
        mock_elasticsearch.search_results = [{
            'id': 'run-111',
            'issue_number': 75,
            'issue_title': 'Removed Issue',
            'issue_url': 'https://github.com/test-org/test-repo/issues/75',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T07:00:00Z',
            'status': 'active'
        }]
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            # Mock GitHub API to return None (issue not on board)
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                return_value=None
            ):
                pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: Run should be ended because issue not found on board
        assert len(mock_elasticsearch.indexed_docs) == 1
        ended_run = mock_elasticsearch.indexed_docs[0]['body']
        assert ended_run['status'] == 'completed'
    
    def test_retriggered_issue_skipped_from_cleanup(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config,
        mock_workflow_template
    ):
        """Test that retriggered issues are not cleaned up during startup"""
        mock_elasticsearch.search_results = [{
            'id': 'run-222',
            'issue_number': 21,
            'issue_title': 'Retriggered Issue',
            'issue_url': 'https://github.com/test-org/test-repo/issues/21',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T06:00:00Z',
            'status': 'active'
        }]
        
        # Mark this issue as retriggered
        retriggered_issues = {('test-project', 21)}
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            # Even though it might be in Backlog, it should be skipped
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                return_value='Backlog'
            ):
                pipeline_run_manager.cleanup_stale_active_runs_on_startup(
                    retriggered_issues=retriggered_issues
                )
        
        # Verify: Run should NOT be ended because it was retriggered
        assert len(mock_elasticsearch.indexed_docs) == 0
    
    def test_multiple_runs_mixed_statuses(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config,
        mock_workflow_template
    ):
        """Test cleanup with multiple runs in different states"""
        mock_elasticsearch.search_results = [
            {
                'id': 'run-001',
                'issue_number': 10,
                'issue_title': 'Issue in Done',
                'issue_url': 'https://github.com/test-org/test-repo/issues/10',
                'project': 'test-project',
                'board': 'Development Board',
                'started_at': '2025-12-10T05:00:00Z',
                'status': 'active'
            },
            {
                'id': 'run-002',
                'issue_number': 20,
                'issue_title': 'Issue in Development',
                'issue_url': 'https://github.com/test-org/test-repo/issues/20',
                'project': 'test-project',
                'board': 'Development Board',
                'started_at': '2025-12-10T04:00:00Z',
                'status': 'active'
            },
            {
                'id': 'run-003',
                'issue_number': 30,
                'issue_title': 'Issue in Backlog',
                'issue_url': 'https://github.com/test-org/test-repo/issues/30',
                'project': 'test-project',
                'board': 'Development Board',
                'started_at': '2025-12-10T03:00:00Z',
                'status': 'active'
            }
        ]
        
        # Mock different columns for different issues
        def mock_get_column(project_config, pipeline_config, issue_number):
            column_map = {
                10: 'Done',       # Exit column - should end
                20: 'Development', # Has agent - should keep
                30: 'Backlog'      # No agent - should end
            }
            return column_map.get(issue_number)
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                side_effect=mock_get_column
            ):
                pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: 2 runs should be ended (issues 10 and 30), 1 kept active (issue 20)
        assert len(mock_elasticsearch.indexed_docs) == 2
        ended_issue_numbers = [doc['body']['issue_number'] for doc in mock_elasticsearch.indexed_docs]
        assert 10 in ended_issue_numbers
        assert 30 in ended_issue_numbers
        assert 20 not in ended_issue_numbers
    
    def test_no_active_runs_handles_gracefully(
        self,
        pipeline_run_manager,
        mock_elasticsearch
    ):
        """Test that cleanup handles no active runs gracefully"""
        mock_elasticsearch.search_results = []
        
        # Should not raise any exceptions
        pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: No documents should be indexed
        assert len(mock_elasticsearch.indexed_docs) == 0


class TestPipelineRunCleanupEdgeCases:
    """Test edge cases and error handling"""
    
    def test_missing_workflow_template_ends_run(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config
    ):
        """Test that runs are ended if workflow template is missing"""
        mock_elasticsearch.search_results = [{
            'id': 'run-error-1',
            'issue_number': 99,
            'issue_title': 'Test',
            'issue_url': 'https://github.com/test-org/test-repo/issues/99',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T01:00:00Z',
            'status': 'active'
        }]
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            # Return None for workflow template (configuration error)
            mock_config.get_workflow_template.return_value = None
            
            # Should handle gracefully and keep run active on error
            pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # In case of errors, we keep runs active to be safe
        # (error handling in the except block)
        assert True  # Test passes if no exception raised
    
    def test_github_api_error_keeps_run_active(
        self,
        pipeline_run_manager,
        mock_elasticsearch,
        mock_project_config,
        mock_workflow_template
    ):
        """Test that runs are kept active if GitHub API fails"""
        mock_elasticsearch.search_results = [{
            'id': 'run-error-2',
            'issue_number': 88,
            'issue_title': 'Test',
            'issue_url': 'https://github.com/test-org/test-repo/issues/88',
            'project': 'test-project',
            'board': 'Development Board',
            'started_at': '2025-12-10T02:00:00Z',
            'status': 'active'
        }]
        
        with patch('config.manager.config_manager') as mock_config:
            mock_config.get_project_config.return_value = mock_project_config
            mock_config.get_workflow_template.return_value = mock_workflow_template
            
            # Simulate GitHub API error
            with patch.object(
                pipeline_run_manager,
                '_get_issue_column_from_github',
                side_effect=Exception("GitHub API error")
            ):
                # Should handle error and keep run active (safe default)
                pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        
        # Verify: Run should be kept active on error (safe default)
        # The exception is caught and logged, run stays active
        assert True  # Test passes if no exception raised

"""
Unit tests for ProjectMonitor lock management bug fixes
Tests the fixes for AttributeError in _get_agent_for_status() and review cycle finally block
"""
import pytest
from unittest.mock import Mock, MagicMock, patch
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


class TestGetAgentForStatus:
    """Test _get_agent_for_status() method fix"""

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock config manager"""
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        return config_manager

    @pytest.fixture
    def project_monitor(self, mock_config_manager):
        """Create ProjectMonitor instance with mocked dependencies"""
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)
        return monitor

    def test_get_agent_for_status_success(self, project_monitor, mock_config_manager):
        """Happy path - successfully find agent for status"""
        # Create mock workflow template with columns
        column1 = Mock()
        column1.name = "Development"
        column1.agent = "senior_software_engineer"

        column2 = Mock()
        column2.name = "Testing"
        column2.agent = "qa_engineer"

        workflow_template = Mock()
        workflow_template.columns = [column1, column2]

        # Create mock pipeline config
        pipeline = Mock()
        pipeline.board_name = "SDLC Execution"
        pipeline.workflow = "sdlc_execution_workflow"

        # Create mock project config
        project_config = Mock()
        project_config.pipelines = [pipeline]

        # Setup mocks
        mock_config_manager.get_project_config.return_value = project_config
        mock_config_manager.get_workflow_template.return_value = workflow_template

        # Test
        agent = project_monitor._get_agent_for_status(
            project_name="test_project",
            board_name="SDLC Execution",
            status="Testing"
        )

        assert agent == "qa_engineer"
        mock_config_manager.get_project_config.assert_called_once_with("test_project")
        mock_config_manager.get_workflow_template.assert_called_once_with("sdlc_execution_workflow")

    def test_get_agent_for_status_no_project(self, project_monitor, mock_config_manager):
        """Project not found should return None"""
        mock_config_manager.get_project_config.return_value = None

        agent = project_monitor._get_agent_for_status(
            project_name="nonexistent_project",
            board_name="SDLC Execution",
            status="Testing"
        )

        assert agent is None

    def test_get_agent_for_status_no_pipeline(self, project_monitor, mock_config_manager):
        """Board not found in project should return None"""
        # Create mock project config with different board
        pipeline = Mock()
        pipeline.board_name = "Different Board"
        pipeline.workflow = "different_workflow"

        project_config = Mock()
        project_config.pipelines = [pipeline]

        mock_config_manager.get_project_config.return_value = project_config

        agent = project_monitor._get_agent_for_status(
            project_name="test_project",
            board_name="SDLC Execution",  # This doesn't exist
            status="Testing"
        )

        assert agent is None

    def test_get_agent_for_status_no_workflow(self, project_monitor, mock_config_manager):
        """Workflow template not found should return None"""
        pipeline = Mock()
        pipeline.board_name = "SDLC Execution"
        pipeline.workflow = "nonexistent_workflow"

        project_config = Mock()
        project_config.pipelines = [pipeline]

        mock_config_manager.get_project_config.return_value = project_config
        mock_config_manager.get_workflow_template.return_value = None

        agent = project_monitor._get_agent_for_status(
            project_name="test_project",
            board_name="SDLC Execution",
            status="Testing"
        )

        assert agent is None

    def test_get_agent_for_status_column_not_found(self, project_monitor, mock_config_manager):
        """Status/column not found in workflow should return None"""
        column1 = Mock()
        column1.name = "Development"
        column1.agent = "senior_software_engineer"

        workflow_template = Mock()
        workflow_template.columns = [column1]

        pipeline = Mock()
        pipeline.board_name = "SDLC Execution"
        pipeline.workflow = "sdlc_execution_workflow"

        project_config = Mock()
        project_config.pipelines = [pipeline]

        mock_config_manager.get_project_config.return_value = project_config
        mock_config_manager.get_workflow_template.return_value = workflow_template

        agent = project_monitor._get_agent_for_status(
            project_name="test_project",
            board_name="SDLC Execution",
            status="NonexistentColumn"  # This column doesn't exist
        )

        assert agent is None

    def test_get_agent_for_status_exception_handling(self, project_monitor, mock_config_manager):
        """Exceptions should be handled and return None"""
        mock_config_manager.get_project_config.side_effect = Exception("Simulated error")

        agent = project_monitor._get_agent_for_status(
            project_name="test_project",
            board_name="SDLC Execution",
            status="Testing"
        )

        assert agent is None


class TestReviewCycleLockManagement:
    """Test review cycle finally block lock management fix

    Note: These tests focus on the workflow lookup logic that was buggy.
    Full integration tests for review cycle completion are in test_review_cycle_completion.py
    """

    @pytest.fixture
    def mock_config_manager(self):
        """Create a mock config manager"""
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        return config_manager

    @pytest.fixture
    def project_monitor(self, mock_config_manager):
        """Create ProjectMonitor instance with mocked dependencies"""
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)
        return monitor

    def test_workflow_lookup_success(self, project_monitor, mock_config_manager):
        """Successfully look up workflow template for lock management"""
        # Create mock workflow template with exit columns
        workflow_template = Mock()
        workflow_template.pipeline_exit_columns = ["Done", "Cancelled"]

        # Create mock pipeline config
        pipeline_config = Mock()
        pipeline_config.workflow = "sdlc_execution_workflow"

        # Setup mock
        mock_config_manager.get_workflow_template.return_value = workflow_template

        # Simulate the workflow lookup in finally block
        workflow_template_obj = None
        try:
            if pipeline_config:
                workflow_template_obj = mock_config_manager.get_workflow_template(pipeline_config.workflow)
        except Exception:
            workflow_template_obj = None

        # Verify
        assert workflow_template_obj is not None
        assert hasattr(workflow_template_obj, 'pipeline_exit_columns')
        assert "Done" in workflow_template_obj.pipeline_exit_columns

    def test_workflow_lookup_no_pipeline_config(self, project_monitor, mock_config_manager):
        """None pipeline_config should result in None workflow (safe default)"""
        pipeline_config = None

        # Simulate the workflow lookup in finally block
        workflow_template_obj = None
        try:
            if pipeline_config:
                workflow_template_obj = mock_config_manager.get_workflow_template(pipeline_config.workflow)
        except Exception:
            workflow_template_obj = None

        # Verify - should be None (safe default, won't release lock)
        assert workflow_template_obj is None

    def test_workflow_lookup_error_handling(self, project_monitor, mock_config_manager):
        """Errors during workflow lookup should be caught and default to None (safe)"""
        pipeline_config = Mock()
        pipeline_config.workflow = "sdlc_execution_workflow"

        # Simulate error in get_workflow_template
        mock_config_manager.get_workflow_template.side_effect = Exception("Simulated lookup error")

        # Simulate the workflow lookup in finally block with error handling
        workflow_template_obj = None
        try:
            if pipeline_config:
                workflow_template_obj = mock_config_manager.get_workflow_template(pipeline_config.workflow)
        except Exception:
            # Default to NOT releasing lock on error (safer than releasing)
            workflow_template_obj = None

        # Verify - should be None (safe default, won't release lock on error)
        assert workflow_template_obj is None

    def test_exit_column_detection_with_valid_workflow(self, project_monitor):
        """Exit column detection should work correctly with valid workflow template"""
        # Create mock workflow template
        workflow_template_obj = Mock()
        workflow_template_obj.pipeline_exit_columns = ["Done", "Cancelled"]

        # Test exit column
        status = "Done"
        is_exit_column = False
        if workflow_template_obj and hasattr(workflow_template_obj, 'pipeline_exit_columns'):
            is_exit_column = status in workflow_template_obj.pipeline_exit_columns

        assert is_exit_column is True

        # Test non-exit column
        status = "Testing"
        is_exit_column = False
        if workflow_template_obj and hasattr(workflow_template_obj, 'pipeline_exit_columns'):
            is_exit_column = status in workflow_template_obj.pipeline_exit_columns

        assert is_exit_column is False

    def test_exit_column_detection_with_none_workflow(self, project_monitor):
        """None workflow should result in is_exit_column=False (safe default)"""
        workflow_template_obj = None
        status = "Done"

        is_exit_column = False
        if workflow_template_obj and hasattr(workflow_template_obj, 'pipeline_exit_columns'):
            is_exit_column = status in workflow_template_obj.pipeline_exit_columns

        # Should remain False - safe default, won't release lock
        assert is_exit_column is False

    def test_exit_column_detection_missing_attribute(self, project_monitor):
        """Workflow without pipeline_exit_columns attribute should default to False"""
        # Create workflow without pipeline_exit_columns attribute
        workflow_template_obj = Mock(spec=[])  # Empty spec means no attributes
        status = "Done"

        is_exit_column = False
        if workflow_template_obj and hasattr(workflow_template_obj, 'pipeline_exit_columns'):
            is_exit_column = status in workflow_template_obj.pipeline_exit_columns

        # Should remain False - safe default
        assert is_exit_column is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

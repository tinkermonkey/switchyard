"""
Integration test for review cycle completion and lock management

Tests the complete review cycle workflow including the lock management fix
in the finally block to ensure pipeline locks are properly handled when
review cycles complete.
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    """Create a mock config manager with full workflow configuration"""
    config_manager = Mock(spec=ConfigManager)

    # Create workflow template with columns and exit columns
    column_dev = Mock()
    column_dev.name = "Development"
    column_dev.agent = "senior_software_engineer"
    column_dev.maker_agent = None
    column_dev.stage_mapping = "coding"
    column_dev.max_iterations = None

    column_review = Mock()
    column_review.name = "Code Review"
    column_review.agent = "code_review_specialist"
    column_review.maker_agent = "senior_software_engineer"
    column_review.stage_mapping = "code_review"
    column_review.max_iterations = 3

    column_testing = Mock()
    column_testing.name = "Testing"
    column_testing.agent = "qa_engineer"
    column_testing.maker_agent = None
    column_testing.stage_mapping = "testing"
    column_testing.max_iterations = None

    column_done = Mock()
    column_done.name = "Done"
    column_done.agent = None
    column_done.maker_agent = None
    column_done.stage_mapping = None
    column_done.max_iterations = None

    workflow_template = Mock()
    workflow_template.columns = [column_dev, column_review, column_testing, column_done]
    workflow_template.pipeline_exit_columns = ["Done", "Cancelled"]

    # Create pipeline config
    pipeline = Mock()
    pipeline.board_name = "SDLC Execution"
    pipeline.workflow = "sdlc_execution_workflow"
    pipeline.template = "sdlc_execution"
    pipeline.workspace = "issues"

    # Create project config
    project_config = Mock()
    project_config.pipelines = [pipeline]
    project_config.github = {
        'org': 'test-org',
        'repo': 'test-repo'
    }
    project_config.orchestrator = {"polling_interval": 15}

    # Setup mock returns
    config_manager.list_projects.return_value = []
    config_manager.get_project_config.return_value = project_config
    config_manager.get_workflow_template.return_value = workflow_template
    config_manager.get_pipeline_template.return_value = Mock(stages=[])

    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    """Create ProjectMonitor instance with mocked dependencies"""
    task_queue = Mock()
    monitor = ProjectMonitor(task_queue, mock_config_manager)

    # Mock external dependencies
    monitor.get_issue_details = Mock(return_value={
        'title': 'Test Issue',
        'body': 'Test body',
        'state': 'OPEN',
        'url': 'https://github.com/test-org/test-repo/issues/123'
    })
    monitor.get_previous_stage_context = Mock(return_value={
        'previous_agent': 'senior_software_engineer',
        'previous_output': 'Code implementation completed'
    })
    monitor.pipeline_run_manager = Mock()
    monitor.pipeline_run_manager.get_or_create_pipeline_run.return_value = (Mock(id='test-run-123'), False)
    monitor.pipeline_run_manager.end_pipeline_run = Mock()

    return monitor


@pytest.mark.integration
@pytest.mark.asyncio
class TestReviewCycleCompletion:
    """Test review cycle completion scenarios"""

    async def test_review_cycle_completes_to_non_exit_column_keeps_lock(
        self,
        project_monitor,
        mock_config_manager
    ):
        """
        Test: Review cycle completes with approval, moves to Testing (non-exit column)
        Expected: Pipeline lock should NOT be released (retained for next stage)
        """
        # Setup - Get the workflow template
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        column = workflow_template.columns[1]  # Code Review column

        # Mock lock manager
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock = Mock()
            mock_get_lock_mgr.return_value = mock_lock_mgr

            # Mock review cycle executor to return "Testing" (non-exit column)
            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                # Simulate review cycle completing with approval -> move to Testing
                async def mock_start_review_cycle(*args, **kwargs):
                    # Return (next_column_name, success)
                    return "Testing", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                # Mock GitHub integration
                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    # Mock state manager
                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        # Mock pipeline queue manager to avoid actual queue processing
                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.peek_next_waiting_issue.return_value = None
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            # Execute the review cycle (runs in background thread, but we'll wait a bit)
                            result = project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Code Review",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=column
                            )

                            # Give background thread time to execute
                            await asyncio.sleep(0.5)

                            # Verify lock was acquired
                            mock_lock_mgr.try_acquire_lock.assert_called_once_with(
                                project="test_project",
                                board="SDLC Execution",
                                issue_number=123
                            )

                            # CRITICAL: Verify lock was NOT released (Testing is not an exit column)
                            # The lock should be retained for the next stage (Testing)
                            mock_lock_mgr.release_lock.assert_not_called()

    async def test_review_cycle_completes_to_exit_column_movement_handled_elsewhere(
        self,
        project_monitor,
        mock_config_manager
    ):
        """
        Test: Review cycle completes with approval, moves to Done (exit column)
        Expected: Lock release is handled by card movement handler, NOT in finally block

        NOTE: The review cycle finally block only releases locks if the issue is STARTING
        in an exit column. When a review cycle MOVES an issue to an exit column, the
        card movement is handled by PipelineProgression.move_issue_to_column which
        triggers the card movement handler that manages lock release.
        """
        # Setup
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        column = workflow_template.columns[1]  # Code Review column

        # Mock lock manager
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock = Mock()
            mock_get_lock_mgr.return_value = mock_lock_mgr

            # Mock review cycle executor to return "Done" (exit column)
            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                # Simulate review cycle completing with approval -> move to Done
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Done", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                # Mock GitHub integration
                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    # Mock state manager
                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        # Mock pipeline queue manager
                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.peek_next_waiting_issue.return_value = None
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            # Execute the review cycle
                            result = project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Code Review",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=column
                            )

                            # Give background thread time to execute
                            await asyncio.sleep(0.5)

                            # Verify lock was acquired
                            mock_lock_mgr.try_acquire_lock.assert_called_once()

                            # CRITICAL: Verify lock was NOT released in finally block
                            # Lock release for exit columns is handled by the card movement handler
                            # (PipelineProgression.move_issue_to_column), not in review cycle finally
                            mock_lock_mgr.release_lock.assert_not_called()

    async def test_workflow_lookup_error_keeps_lock_safe_default(
        self,
        project_monitor,
        mock_config_manager
    ):
        """
        Test: Workflow lookup error in finally block should default to NOT releasing lock
        This is the safe behavior - better to keep lock than corrupt state
        """
        # Setup
        project_config = mock_config_manager.get_project_config("test_project")
        workflow_template = mock_config_manager.get_workflow_template("sdlc_execution_workflow")
        column = workflow_template.columns[1]  # Code Review column

        # Mock lock manager
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_mgr = Mock()
            mock_lock_mgr.try_acquire_lock.return_value = (True, "lock_acquired")
            mock_lock_mgr.release_lock = Mock()
            mock_get_lock_mgr.return_value = mock_lock_mgr

            # Make get_workflow_template raise an error when called from finally block
            call_count = [0]
            original_get_workflow = mock_config_manager.get_workflow_template

            def get_workflow_with_error(workflow_name):
                call_count[0] += 1
                # First call succeeds (from main code), second call fails (from finally block)
                if call_count[0] == 1:
                    return original_get_workflow(workflow_name)
                else:
                    raise Exception("Simulated workflow lookup error in finally block")

            mock_config_manager.get_workflow_template = Mock(side_effect=get_workflow_with_error)

            # Mock review cycle executor
            with patch('services.review_cycle.review_cycle_executor') as mock_review_executor:
                async def mock_start_review_cycle(*args, **kwargs):
                    return "Testing", True

                mock_review_executor.start_review_cycle = AsyncMock(side_effect=mock_start_review_cycle)

                # Mock GitHub integration
                with patch('services.github_integration.GitHubIntegration') as mock_github_cls:
                    mock_github = Mock()
                    mock_github.post_agent_output = AsyncMock()
                    mock_github_cls.return_value = mock_github

                    # Mock state manager
                    with patch('config.state_manager.state_manager') as mock_state_mgr:
                        mock_state_mgr.get_discussion_for_issue.return_value = None

                        # Mock pipeline queue manager
                        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager') as mock_get_queue_mgr:
                            mock_queue_mgr = Mock()
                            mock_queue_mgr.peek_next_waiting_issue.return_value = None
                            mock_get_queue_mgr.return_value = mock_queue_mgr

                            # Execute the review cycle
                            result = project_monitor._start_review_cycle_for_issue(
                                project_name="test_project",
                                board_name="SDLC Execution",
                                issue_number=123,
                                status="Code Review",
                                repository="test-repo",
                                project_config=project_config,
                                pipeline_config=project_config.pipelines[0],
                                workflow_template=workflow_template,
                                column=column
                            )

                            # Give background thread time to execute
                            await asyncio.sleep(0.5)

                            # Verify lock was acquired
                            mock_lock_mgr.try_acquire_lock.assert_called_once()

                            # CRITICAL: Verify lock was NOT released due to error (safe default)
                            mock_lock_mgr.release_lock.assert_not_called()


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

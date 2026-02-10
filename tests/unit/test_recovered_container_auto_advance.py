"""
Unit tests for recovered container auto-advance logic.

Tests that _process_recovered_container_completion auto-advances
the issue to the next column when the agent succeeds and the
column has auto_advance_on_approval enabled.
"""

import asyncio
import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Pre-mock modules that fail outside Docker (/app/state doesn't exist)
if 'services.work_execution_state' not in sys.modules:
    mock_wes = MagicMock()
    sys.modules['services.work_execution_state'] = mock_wes
if 'services.dev_container_state' not in sys.modules:
    sys.modules['services.dev_container_state'] = MagicMock()

from claude.docker_runner import DockerAgentRunner


@pytest.fixture
def runner():
    return DockerAgentRunner()


@pytest.fixture
def mock_column_with_auto_advance():
    col = MagicMock()
    col.name = "Documentation"
    col.auto_advance_on_approval = True
    return col


@pytest.fixture
def mock_column_without_auto_advance():
    col = MagicMock()
    col.name = "In Review"
    col.auto_advance_on_approval = False
    return col


@pytest.fixture
def mock_next_column():
    col = MagicMock()
    col.name = "Done"
    return col


def _build_mocks(columns, pipeline_board="Planning & Design"):
    """Helper to build common mock objects for tests."""
    mock_workflow = MagicMock()
    mock_workflow.columns = columns

    mock_pipeline = MagicMock()
    mock_pipeline.board_name = pipeline_board
    mock_pipeline.workflow = "planning_workflow"

    mock_config = MagicMock()
    mock_config.github = {'org': 'test-org', 'repo': 'test-repo'}
    mock_config.pipelines = [mock_pipeline]

    return mock_config, mock_workflow


class TestRecoveredContainerAutoAdvance:
    """Test auto-advance logic in _process_recovered_container_completion."""

    @patch('claude.docker_runner.subprocess')
    def test_auto_advances_on_success(self, mock_subprocess, runner, mock_column_with_auto_advance, mock_next_column):
        """When exit_code=0 and column has auto_advance, should move to next column."""
        mock_config, mock_workflow = _build_mocks(
            [mock_column_with_auto_advance, mock_next_column]
        )

        with patch.object(runner, '_unregister_active_container'), \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            MockGitHub.return_value.post_agent_output = AsyncMock()
            mock_progression = MockProgression.return_value
            mock_progression.move_issue_to_column.return_value = True

            runner._process_recovered_container_completion(
                container_name="claude-agent-test-proj-task_123",
                project="test-proj",
                issue_number=42,
                agent="technical_writer",
                task_id="task_123",
                exit_code=0,
                output="Agent output here",
                column="Documentation"
            )

            mock_progression.move_issue_to_column.assert_called_once_with(
                project_name="test-proj",
                board_name="Planning & Design",
                issue_number=42,
                target_column="Done",
                trigger='recovered_container_auto_advance'
            )

    @patch('claude.docker_runner.subprocess')
    def test_no_auto_advance_on_failure(self, mock_subprocess, runner, mock_column_with_auto_advance, mock_next_column):
        """When exit_code != 0, should NOT auto-advance."""
        mock_config, mock_workflow = _build_mocks(
            [mock_column_with_auto_advance, mock_next_column]
        )

        with patch.object(runner, '_unregister_active_container'), \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            MockGitHub.return_value.post_agent_output = AsyncMock()
            mock_progression = MockProgression.return_value

            runner._process_recovered_container_completion(
                container_name="claude-agent-test-proj-task_123",
                project="test-proj",
                issue_number=42,
                agent="technical_writer",
                task_id="task_123",
                exit_code=1,
                output="Agent output here",
                column="Documentation"
            )

            mock_progression.move_issue_to_column.assert_not_called()

    @patch('claude.docker_runner.subprocess')
    def test_no_auto_advance_when_column_unknown(self, mock_subprocess, runner):
        """When column is 'unknown', should skip auto-advance entirely."""
        mock_config = MagicMock()
        mock_config.github = {'org': 'test-org', 'repo': 'test-repo'}
        mock_config.pipelines = []

        with patch.object(runner, '_unregister_active_container'), \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            MockGitHub.return_value.post_agent_output = AsyncMock()
            mock_progression = MockProgression.return_value

            runner._process_recovered_container_completion(
                container_name="claude-agent-test-proj-task_123",
                project="test-proj",
                issue_number=42,
                agent="technical_writer",
                task_id="task_123",
                exit_code=0,
                output="Agent output here",
                column="unknown"
            )

            mock_progression.move_issue_to_column.assert_not_called()

    @patch('claude.docker_runner.subprocess')
    def test_no_auto_advance_when_column_disabled(self, mock_subprocess, runner, mock_column_without_auto_advance, mock_next_column):
        """When column has auto_advance_on_approval=False, should NOT auto-advance."""
        mock_config, mock_workflow = _build_mocks(
            [mock_column_without_auto_advance, mock_next_column]
        )

        with patch.object(runner, '_unregister_active_container'), \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            MockGitHub.return_value.post_agent_output = AsyncMock()
            mock_progression = MockProgression.return_value

            runner._process_recovered_container_completion(
                container_name="claude-agent-test-proj-task_123",
                project="test-proj",
                issue_number=42,
                agent="technical_writer",
                task_id="task_123",
                exit_code=0,
                output="Agent output here",
                column="In Review"
            )

            mock_progression.move_issue_to_column.assert_not_called()

    @patch('claude.docker_runner.subprocess')
    def test_auto_advance_error_does_not_propagate(self, mock_subprocess, runner, mock_column_with_auto_advance, mock_next_column):
        """Auto-advance errors should be caught and not crash the recovery flow."""
        mock_config, _ = _build_mocks(
            [mock_column_with_auto_advance, mock_next_column]
        )

        with patch.object(runner, '_unregister_active_container'), \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.pipeline_progression.PipelineProgression'), \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.side_effect = RuntimeError("Config error")
            MockGitHub.return_value.post_agent_output = AsyncMock()

            # Should NOT raise — error is caught internally
            runner._process_recovered_container_completion(
                container_name="claude-agent-test-proj-task_123",
                project="test-proj",
                issue_number=42,
                agent="technical_writer",
                task_id="task_123",
                exit_code=0,
                output="Agent output here",
                column="Documentation"
            )

    @patch('claude.docker_runner.subprocess')
    def test_no_auto_advance_when_last_column(self, mock_subprocess, runner, mock_column_with_auto_advance):
        """When current column is the last one, should not attempt to advance."""
        mock_config, mock_workflow = _build_mocks(
            [mock_column_with_auto_advance]  # Only one column — no next column
        )

        with patch.object(runner, '_unregister_active_container'), \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.pipeline_progression.PipelineProgression') as MockProgression, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            MockGitHub.return_value.post_agent_output = AsyncMock()
            mock_progression = MockProgression.return_value

            runner._process_recovered_container_completion(
                container_name="claude-agent-test-proj-task_123",
                project="test-proj",
                issue_number=42,
                agent="technical_writer",
                task_id="task_123",
                exit_code=0,
                output="Agent output here",
                column="Documentation"
            )

            mock_progression.move_issue_to_column.assert_not_called()

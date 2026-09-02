"""
Unit tests for recovering a PR-review-stage phase container.

PRReviewStage.execute() orchestrates several sequential Docker containers from a
single in-process coroutine that does not survive an orchestrator restart. A
recovered phase container's output must be checkpointed and the stage re-triggered
fresh — not posted to GitHub as if it were the whole stage's complete, terminal
result. See claude/docker_runner.py's _process_recovered_pr_review_phase_completion.
"""

import sys
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# Pre-mock modules that fail outside Docker (/app/state doesn't exist)
if 'services.work_execution_state' not in sys.modules:
    sys.modules['services.work_execution_state'] = MagicMock()
if 'services.dev_container_state' not in sys.modules:
    sys.modules['services.dev_container_state'] = MagicMock()

from claude.docker_runner import DockerAgentRunner


@pytest.fixture
def runner():
    return DockerAgentRunner()


def _build_pr_review_stage_config(board_name="Planning & Design"):
    """A project config whose 'In Review' column is owned by the pr_review_stage
    wrapper agent — mirrors the planning_design workflow's real shape."""
    in_review_col = MagicMock()
    in_review_col.name = "In Review"
    in_review_col.agent = "pr_review_stage"

    mock_workflow = MagicMock()
    mock_workflow.columns = [in_review_col]

    mock_pipeline = MagicMock()
    mock_pipeline.board_name = board_name
    mock_pipeline.workflow = "planning_design_workflow"

    mock_config = MagicMock()
    mock_config.github = {'org': 'test-org', 'repo': 'test-repo'}
    mock_config.pipelines = [mock_pipeline]

    return mock_config, mock_workflow


class TestRecoveredPRReviewPhaseCompletion:
    def test_successful_phase_is_checkpointed_not_posted_to_github(self, runner):
        mock_config, mock_workflow = _build_pr_review_stage_config()

        with patch.object(runner, '_unregister_active_container'), \
             patch('claude.docker_runner.subprocess'), \
             patch('pipeline.pr_review_checkpoint.PRReviewCheckpoint') as MockCheckpoint, \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.project_monitor.ProjectMonitor') as MockMonitor, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            mock_checkpoint_instance = MockCheckpoint.return_value

            runner._process_recovered_container_completion(
                container_name='claude-agent-codetoreum-abc123',
                project='codetoreum',
                issue_number=943,
                agent='pr_code_reviewer',
                task_id='abc123',
                exit_code=0,
                output='## PR Review Findings\n...',
                column='In Review',
                pipeline_run_id='old-run-id',
                pr_review_phase='code_review',
                pr_review_cycle='1',
            )

            # The recovered output is checkpointed under the right phase/cycle...
            mock_checkpoint_instance.save_phase_output.assert_called_once_with(
                1, 'code_review', '## PR Review Findings\n...'
            )
            # ...instead of being posted to GitHub as a terminal comment.
            MockGitHub.return_value.post_agent_output.assert_not_called()

    def test_retriggers_pr_review_with_lock_already_acquired(self, runner):
        mock_config, mock_workflow = _build_pr_review_stage_config(board_name="Planning & Design")

        with patch.object(runner, '_unregister_active_container'), \
             patch('claude.docker_runner.subprocess'), \
             patch('pipeline.pr_review_checkpoint.PRReviewCheckpoint'), \
             patch('services.github_integration.GitHubIntegration'), \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.project_monitor.ProjectMonitor') as MockMonitor, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            mock_monitor_instance = MockMonitor.return_value

            runner._process_recovered_container_completion(
                container_name='claude-agent-codetoreum-abc123',
                project='codetoreum',
                issue_number=943,
                agent='pr_code_reviewer',
                task_id='abc123',
                exit_code=0,
                output='some output',
                column='In Review',
                pipeline_run_id='old-run-id',
                pr_review_phase='code_review',
                pr_review_cycle='1',
            )

            mock_monitor_instance.trigger_agent_for_status.assert_called_once_with(
                'codetoreum', 'Planning & Design', 943, 'In Review', 'test-repo',
                lock_already_acquired=True,
            )

    def test_failed_phase_is_not_checkpointed_but_still_retriggers(self, runner):
        mock_config, mock_workflow = _build_pr_review_stage_config()

        with patch.object(runner, '_unregister_active_container'), \
             patch('claude.docker_runner.subprocess'), \
             patch('pipeline.pr_review_checkpoint.PRReviewCheckpoint') as MockCheckpoint, \
             patch('services.github_integration.GitHubIntegration'), \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.project_monitor.ProjectMonitor') as MockMonitor, \
             patch('task_queue.task_manager.TaskQueue'):

            mock_cm.get_project_config.return_value = mock_config
            mock_cm.get_workflow_template.return_value = mock_workflow
            mock_checkpoint_instance = MockCheckpoint.return_value
            mock_monitor_instance = MockMonitor.return_value

            runner._process_recovered_container_completion(
                container_name='claude-agent-codetoreum-abc123',
                project='codetoreum',
                issue_number=943,
                agent='pr_code_reviewer',
                task_id='abc123',
                exit_code=1,
                output='',
                column='In Review',
                pipeline_run_id='old-run-id',
                pr_review_phase='code_review',
                pr_review_cycle='1',
            )

            mock_checkpoint_instance.save_phase_output.assert_not_called()
            mock_monitor_instance.trigger_agent_for_status.assert_called_once()

    def test_non_pr_review_container_uses_the_normal_terminal_path(self, runner):
        """Regression guard: a container with no pr_review_phase label must be
        entirely unaffected by this change and keep posting to GitHub as before."""
        with patch.object(runner, '_unregister_active_container'), \
             patch('claude.docker_runner.subprocess'), \
             patch('pipeline.pr_review_checkpoint.PRReviewCheckpoint') as MockCheckpoint, \
             patch('services.github_integration.GitHubIntegration') as MockGitHub, \
             patch('config.manager.config_manager') as mock_cm, \
             patch('services.work_execution_state.work_execution_tracker'), \
             patch('services.project_monitor.ProjectMonitor') as MockMonitor:

            mock_config = MagicMock()
            mock_config.github = {'org': 'test-org', 'repo': 'test-repo'}
            mock_cm.get_project_config.return_value = mock_config
            MockGitHub.return_value.post_agent_output = AsyncMock(return_value={'success': True})

            runner._process_recovered_container_completion(
                container_name='claude-agent-codetoreum-abc123',
                project='codetoreum',
                issue_number=100,
                agent='senior_software_engineer',
                task_id='abc123',
                exit_code=0,
                output='some output',
                column='unknown',
                pipeline_run_id=None,
                pr_review_phase=None,
                pr_review_cycle=None,
            )

            MockGitHub.return_value.post_agent_output.assert_called_once()
            MockCheckpoint.assert_not_called()
            MockMonitor.return_value.trigger_agent_for_status.assert_not_called()

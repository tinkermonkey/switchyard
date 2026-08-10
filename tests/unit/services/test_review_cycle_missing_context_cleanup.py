"""
Tests for the fix where `_start_review_cycle_for_issue` used to abandon the
pipeline run and lock the caller had already set up, whenever the previous-stage
context lookup came back empty. That left the board locked with zero work in
flight for up to an hour, until the zombie watchdog's scheduled sweep noticed.

See investigation of pipeline run dfd795ad (rounds#159): `trigger_agent_for_status`
creates a pipeline run and (via FAILSAFE) already holds the pipeline lock before
routing into `_start_review_cycle_for_issue`. The old code just `return None`d on
missing context without releasing either — this suite locks in the fix, which
mirrors the cleanup pattern already used by the sibling "skip work" branch in
`trigger_agent_for_status` and by `_start_pr_review_for_issue`'s exception handler.
"""
from unittest.mock import Mock, patch

import pytest

from config.manager import ConfigManager
from services.project_monitor import ProjectMonitor


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    config_manager.get_pipeline_template.return_value = None
    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    task_queue = Mock()
    monitor = ProjectMonitor(task_queue, mock_config_manager)
    monitor.pipeline_run_manager = Mock()
    return monitor


def _review_column():
    column = Mock()
    column.type = 'review'
    column.agent = 'code_reviewer'
    column.maker_agent = 'senior_software_engineer'
    column.max_iterations = 5
    column.stage_mapping = None  # skip current_stage_config resolution
    return column


def _project_config():
    project_config = Mock()
    project_config.github = {'org': 'tinkermonkey', 'repo': 'rounds'}
    project_config.pipelines = []
    return project_config


def _pipeline_config():
    pipeline_config = Mock()
    pipeline_config.workspace = 'issues'
    pipeline_config.template = 'sdlc_execution'
    return pipeline_config


class TestMissingContextCleansUpImmediately:
    def test_ends_pipeline_run_and_releases_lock(self, project_monitor):
        with patch.object(project_monitor, 'get_issue_details', return_value={'title': 'T', 'url': 'u'}), \
             patch.object(project_monitor, 'get_previous_stage_context', return_value=''):
            result = project_monitor._start_review_cycle_for_issue(
                project_name='rounds',
                board_name='SDLC Execution',
                issue_number=159,
                status='Code Review',
                repository='rounds',
                project_config=_project_config(),
                pipeline_config=_pipeline_config(),
                workflow_template=Mock(),
                column=_review_column(),
            )

        assert result is None
        project_monitor.pipeline_run_manager.end_pipeline_run.assert_called_once()
        args, kwargs = project_monitor.pipeline_run_manager.end_pipeline_run.call_args
        assert args[0] == 'rounds'
        assert args[1] == 159
        assert kwargs['retain_lock'] is False
        assert kwargs['outcome'] == 'failed'
        assert 'previous stage output' in kwargs['reason'].lower()

    def test_no_cleanup_when_context_is_present(self, project_monitor):
        """Sanity check: the happy path (context found) must NOT end the run early —
        only the missing-context bail-out should trigger immediate cleanup."""
        with patch.object(project_monitor, 'get_issue_details', return_value={'title': 'T', 'url': 'u'}), \
             patch.object(project_monitor, 'get_previous_stage_context', return_value='## Previous Work\n\nSome real output'), \
             patch.object(project_monitor.pipeline_run_manager, 'get_or_create_pipeline_run',
                           return_value=(Mock(id='run-1'), False)), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager') as mock_get_lock_mgr:
            mock_lock_manager = Mock()
            mock_lock_manager.try_acquire_lock.return_value = (True, 'acquired')
            mock_get_lock_mgr.return_value = mock_lock_manager

            project_monitor._start_review_cycle_for_issue(
                project_name='rounds',
                board_name='SDLC Execution',
                issue_number=159,
                status='Code Review',
                repository='rounds',
                project_config=_project_config(),
                pipeline_config=_pipeline_config(),
                workflow_template=Mock(),
                column=_review_column(),
            )

        project_monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()

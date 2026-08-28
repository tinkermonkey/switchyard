"""
Unit tests for ProjectMonitor.get_issue_details() and its sibling
PipelineProgression._get_issue_details() retrying transient `gh` CLI
failures and raising (instead of returning a placeholder) once retries are
exhausted.

Root cause (pipeline run bc70ac46, issue #941): five projects' boards
syncing in the same second produced an empty-stdout response from
`gh issue view` for a real, non-empty issue — json.loads('') raised
"Expecting value: line 1 column 1 (char 0)", which the old implementation
silently turned into {'title': 'Issue #941', 'body': ''}, indistinguishable
from a genuinely-empty issue. That placeholder then tripped the
empty-description guard in agent_executor.py and halted the pipeline with a
misleading "issue has an empty description" message.
"""
import json
import pytest
from unittest.mock import Mock, patch, MagicMock
from services.project_monitor import ProjectMonitor
from services.pipeline_progression import PipelineProgression
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    return ProjectMonitor(Mock(), mock_config_manager)


class TestProjectMonitorGetIssueDetailsRetry:

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_succeeds_on_first_try_no_retry(self, mock_run, mock_sleep, project_monitor):
        mock_run.return_value = Mock(stdout=json.dumps({'title': 'Real', 'body': 'Real body'}))

        result = project_monitor.get_issue_details('repo', 941, 'org')

        assert result == {'title': 'Real', 'body': 'Real body'}
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_recovers_after_one_transient_empty_stdout(self, mock_run, mock_sleep, project_monitor):
        """Reproduces the exact bc70ac46 failure mode: gh exits 0 but stdout
        is empty, so json.loads('') raises. A retry should succeed."""
        mock_run.side_effect = [
            Mock(stdout=''),  # json.loads('') -> JSONDecodeError, same as production
            Mock(stdout=json.dumps({'title': 'Real', 'body': 'Real body'})),
        ]

        result = project_monitor.get_issue_details('repo', 941, 'org')

        assert result == {'title': 'Real', 'body': 'Real body'}
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_raises_after_exhausting_all_retries(self, mock_run, mock_sleep, project_monitor):
        """After 3 straight failures, must raise rather than return the old
        placeholder — a caller silently getting {'title': ..., 'body': ''}
        back can no longer be distinguished from a genuinely-empty issue."""
        mock_run.return_value = Mock(stdout='')

        with pytest.raises(RuntimeError, match=r"Could not fetch issue #941"):
            project_monitor.get_issue_details('repo', 941, 'org')

        assert mock_run.call_count == 3

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_does_not_retry_forever_on_persistent_failure(self, mock_run, mock_sleep, project_monitor):
        """A real gh failure (e.g. issue truly doesn't exist) shouldn't retry
        indefinitely — bounded at 3 attempts."""
        import subprocess as subprocess_module
        mock_run.side_effect = subprocess_module.CalledProcessError(1, 'gh')

        with pytest.raises(RuntimeError):
            project_monitor.get_issue_details('repo', 999999, 'org')

        assert mock_run.call_count == 3


class TestPipelineProgressionGetIssueDetailsRetry:
    """PipelineProgression._get_issue_details() is a duplicate implementation
    of the same fetch — it must carry the identical fix, since it feeds the
    same class of downstream consumer (task_context['issue'])."""

    @pytest.fixture
    def pipeline_progression(self):
        return PipelineProgression(task_queue=Mock())

    @patch('services.pipeline_progression.time.sleep')
    @patch('services.pipeline_progression.subprocess.run')
    def test_recovers_after_one_transient_empty_stdout(self, mock_run, mock_sleep, pipeline_progression):
        mock_run.side_effect = [
            Mock(stdout=''),
            Mock(stdout=json.dumps({'title': 'Real', 'body': 'Real body'})),
        ]

        result = pipeline_progression._get_issue_details('repo', 941, 'org')

        assert result == {'title': 'Real', 'body': 'Real body'}
        assert mock_run.call_count == 2

    @patch('services.pipeline_progression.time.sleep')
    @patch('services.pipeline_progression.subprocess.run')
    def test_raises_after_exhausting_all_retries(self, mock_run, mock_sleep, pipeline_progression):
        mock_run.return_value = Mock(stdout='')

        with pytest.raises(RuntimeError, match=r"Could not fetch issue #941"):
            pipeline_progression._get_issue_details('repo', 941, 'org')

        assert mock_run.call_count == 3

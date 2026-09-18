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
from unittest.mock import Mock, call, patch, MagicMock
from services.project_monitor import ProjectMonitor
from services.pipeline_progression import PipelineProgression
from services.github_api_client import GitHubBreaker, get_github_client
from config.manager import ConfigManager


@pytest.fixture(autouse=True)
def reset_breaker():
    client = get_github_client()
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None
    yield
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    return ProjectMonitor(Mock(), mock_config_manager)


class TestProjectMonitorGetIssueDetailsRetry:
    """Migrated onto GitHubAPIClient.gh_cli() (GitHub circuit breaker
    consolidation): mocks now set returncode/stderr explicitly since gh_cli()
    inspects both, not just stdout the way the raw subprocess.run(check=True)
    call used to (a bare Mock's auto-generated .returncode is never `in {0}`,
    which would silently misclassify every one of these as a failure).
    `services.project_monitor.subprocess.run` remains a valid patch target
    unlike some sibling files -- project_monitor.py still imports subprocess
    directly for its docker calls, so this still resolves to the same shared
    subprocess module gh_cli() calls internally.
    """

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_succeeds_on_first_try_no_retry(self, mock_run, mock_sleep, project_monitor):
        mock_run.return_value = Mock(returncode=0, stderr='', stdout=json.dumps({'title': 'Real', 'body': 'Real body'}))

        result = project_monitor.get_issue_details('repo', 941, 'org')

        assert result == {'title': 'Real', 'body': 'Real body'}
        assert mock_run.call_count == 1
        # Not asserting sleep was never called: gh_cli()'s own rate-limit
        # throttle (_apply_backoff) may sleep briefly depending on how
        # recently the shared client singleton was used by an earlier test
        # in the suite -- unrelated to whether THIS call retried. The
        # retry-specific 0.5s backoff (this test's actual concern) simply
        # never fires here since call_count == 1 already proves no retry.

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_recovers_after_one_transient_empty_stdout(self, mock_run, mock_sleep, project_monitor):
        """Reproduces the exact bc70ac46 failure mode: gh exits 0 but stdout
        is empty. gh_cli() falls back to raw stdout ('') rather than raising
        the way json.loads('') used to -- the isinstance(dict) guard added
        alongside the migration is what still catches this as a failure. A
        retry should succeed."""
        mock_run.side_effect = [
            Mock(returncode=0, stderr='', stdout=''),
            Mock(returncode=0, stderr='', stdout=json.dumps({'title': 'Real', 'body': 'Real body'})),
        ]

        result = project_monitor.get_issue_details('repo', 941, 'org')

        assert result == {'title': 'Real', 'body': 'Real body'}
        assert mock_run.call_count == 2
        # gh_cli()'s own rate-limit throttle also calls time.sleep(), so this
        # checks the retry's own 0.5s backoff fired rather than the total
        # call count (see test_succeeds_on_first_try_no_retry above).
        assert call(0.5) in mock_sleep.call_args_list

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_raises_after_exhausting_all_retries(self, mock_run, mock_sleep, project_monitor):
        """After 3 straight failures, must raise rather than return the old
        placeholder — a caller silently getting {'title': ..., 'body': ''}
        back can no longer be distinguished from a genuinely-empty issue."""
        mock_run.return_value = Mock(returncode=0, stderr='', stdout='')

        with pytest.raises(RuntimeError, match=r"Could not fetch issue #941"):
            project_monitor.get_issue_details('repo', 941, 'org')

        assert mock_run.call_count == 3

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_does_not_retry_forever_on_persistent_failure(self, mock_run, mock_sleep, project_monitor):
        """A real gh failure (e.g. issue truly doesn't exist) shouldn't retry
        indefinitely — bounded at 3 attempts."""
        mock_run.return_value = Mock(returncode=1, stdout='', stderr='HTTP 404: Not Found')

        with pytest.raises(RuntimeError):
            project_monitor.get_issue_details('repo', 999999, 'org')

        assert mock_run.call_count == 3

    @patch('services.project_monitor.time.sleep')
    @patch('services.project_monitor.subprocess.run')
    def test_open_circuit_breaker_short_circuits_without_calling_subprocess(self, mock_run, mock_sleep, project_monitor):
        """The whole point of the migration: an open breaker now protects
        this call site too, where before it had no protection at all."""
        client = get_github_client()
        client.breaker.state = GitHubBreaker.OPEN
        client.breaker.reset_time = None
        try:
            with pytest.raises(RuntimeError, match=r"Could not fetch issue #941"):
                project_monitor.get_issue_details('repo', 941, 'org')
            mock_run.assert_not_called()
        finally:
            client.breaker.state = GitHubBreaker.CLOSED
            client.breaker._generic_failure_count = 0
            client.breaker.trip_reason = None


class TestPipelineProgressionGetIssueDetailsRetry:
    """PipelineProgression._get_issue_details() is a duplicate implementation
    of the same fetch — it must carry the identical fix, since it feeds the
    same class of downstream consumer (task_context['issue']).

    Migrated onto GitHubAPIClient.gh_cli() (GitHub circuit breaker
    consolidation): mocks patch `subprocess.run` globally now (gh_cli() is
    what actually invokes it — `services.pipeline_progression.subprocess` no
    longer exists), with explicit returncode/stderr since gh_cli() inspects
    both, not just stdout.
    """

    @pytest.fixture
    def pipeline_progression(self):
        return PipelineProgression(task_queue=Mock())

    @patch('services.pipeline_progression.time.sleep')
    @patch('subprocess.run')
    def test_recovers_after_one_transient_empty_stdout(self, mock_run, mock_sleep, pipeline_progression):
        mock_run.side_effect = [
            MagicMock(returncode=0, stdout='', stderr=''),
            MagicMock(returncode=0, stdout=json.dumps({'title': 'Real', 'body': 'Real body'}), stderr=''),
        ]

        result = pipeline_progression._get_issue_details('repo', 941, 'org')

        assert result == {'title': 'Real', 'body': 'Real body'}
        assert mock_run.call_count == 2

    @patch('services.pipeline_progression.time.sleep')
    @patch('subprocess.run')
    def test_raises_after_exhausting_all_retries(self, mock_run, mock_sleep, pipeline_progression):
        mock_run.return_value = MagicMock(returncode=0, stdout='', stderr='')

        with pytest.raises(RuntimeError, match=r"Could not fetch issue #941"):
            pipeline_progression._get_issue_details('repo', 941, 'org')

        assert mock_run.call_count == 3

    @patch('services.pipeline_progression.time.sleep')
    @patch('subprocess.run')
    def test_open_circuit_breaker_short_circuits_without_calling_subprocess(self, mock_run, mock_sleep, pipeline_progression):
        """The whole point of the migration: an open breaker now protects
        this call site too, where before it had no protection at all."""
        from services.github_api_client import get_github_client, GitHubBreaker
        client = get_github_client()
        client.breaker.state = GitHubBreaker.OPEN
        client.breaker.reset_time = None
        try:
            with pytest.raises(RuntimeError, match=r"Could not fetch issue #941"):
                pipeline_progression._get_issue_details('repo', 941, 'org')
            mock_run.assert_not_called()
        finally:
            client.breaker.state = GitHubBreaker.CLOSED
            client.breaker.reset_time = None

"""
Tests for ProjectMonitor's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation, follow-up sweep).

project_monitor.py was missed by the original 13-file audit/plan despite
being the file at the center of the retry-storm investigation that started
this whole effort -- a repo-wide sweep afterward found ~14 more live, raw
`subprocess.run(['gh', ...])` call sites here. This file covers those not
already exercised by tests/unit/services/test_get_issue_details_retry.py
(which covers get_issue_details(), the sibling of
PipelineProgression._get_issue_details()).

One site (_check_for_feedback_OLD_DELETED, a dead function superseded by a
no-op stub -- confirmed zero callers anywhere in the codebase) was
deliberately NOT migrated and is not tested here.
"""
from unittest.mock import MagicMock, Mock, patch

import pytest

from services.project_monitor import ProjectMonitor
from services.github_api_client import GitHubBreaker, get_github_client
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    config_manager.list_visible_projects.return_value = []
    return config_manager


@pytest.fixture
def monitor(mock_config_manager):
    return ProjectMonitor(Mock(), mock_config_manager)


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


def _mock_result(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestGetValidColumnsForBoardFallback:
    """_get_valid_columns_for_board()'s GitHub API fallback (reverse lookup
    via state files finds nothing first, since list_visible_projects()
    returns [] in this fixture).

    `gh project field-list --format json` returns an OBJECT
    ({"fields": [...], "totalCount": N}), not a bare list -- confirmed
    against this same command's other two consumers in this codebase
    (config/state_manager.py's refresh_board_field_ids(),
    github_project_manager.py's _configure_board_columns()). The
    isinstance(result.data, list) guard this function originally shipped
    with could never pass against real `gh` output, making this whole
    fallback path dead code (caught in follow-up code review of PR #270)."""

    def test_fallback_success_parses_status_options(self, monitor):
        result = _mock_result(stdout='{"fields": [{"name": "Status", "options": '
                                      '[{"name": "Backlog"}, {"name": "In Progress"}]}]}')
        with patch('subprocess.run', return_value=result):
            columns = monitor._get_valid_columns_for_board('acme', 7)
        assert columns == {'Backlog', 'In Progress'}

    def test_gh_failure_returns_empty_set(self, monitor):
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', return_value=result):
            columns = monitor._get_valid_columns_for_board('acme', 7)
        assert columns == set()

    def test_malformed_json_on_exit_zero_returns_empty_set_not_crash(self, monitor):
        result = _mock_result(returncode=0, stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            columns = monitor._get_valid_columns_for_board('acme', 7)
        assert columns == set()

    def test_bare_list_shape_is_rejected_not_silently_accepted(self, monitor):
        """A bare list is not what `gh project field-list` actually returns
        -- must be treated as a malformed response (empty set), not parsed
        as if it were the real {"fields": [...]} shape."""
        result = _mock_result(stdout='[{"name": "Status", "options": [{"name": "Backlog"}]}]')
        with patch('subprocess.run', return_value=result):
            columns = monitor._get_valid_columns_for_board('acme', 7)
        assert columns == set()

    def test_open_breaker_returns_empty_set_without_calling_subprocess(self, monitor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            columns = monitor._get_valid_columns_for_board('acme', 7)
        assert columns == set()
        mock_run.assert_not_called()


class TestGetIssueContext:
    def test_success_extracts_agent_comment(self, monitor):
        result = _mock_result(stdout='{"comments": [{"body": "_Processed by the code_reviewer agent_ output text", '
                                      '"createdAt": "2026-01-01T00:00:00Z"}]}')
        workflow_template = MagicMock()
        with patch('subprocess.run', return_value=result):
            context = monitor._get_issue_context('widgets', 1, 'acme', 'Testing', workflow_template)
        assert 'output text' in context

    def test_gh_failure_returns_empty_string(self, monitor):
        result = _mock_result(returncode=1, stderr="boom")
        with patch('subprocess.run', return_value=result):
            context = monitor._get_issue_context('widgets', 1, 'acme', 'Testing', MagicMock())
        assert context == ""

    def test_open_breaker_returns_empty_string_without_calling_subprocess(self, monitor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            context = monitor._get_issue_context('widgets', 1, 'acme', 'Testing', MagicMock())
        assert context == ""
        mock_run.assert_not_called()


class TestGetAgentOutputsFromIssue:
    def test_success_finds_requested_agent_output(self, monitor):
        result = _mock_result(stdout='{"comments": [{"body": "_Processed by the business_analyst agent_ requirements text", '
                                      '"createdAt": "2026-01-01T00:00:00Z"}]}')
        with patch('subprocess.run', return_value=result):
            context = monitor._get_agent_outputs_from_issue('widgets', 1, 'acme', ['business_analyst'])
        assert 'requirements text' in context

    def test_malformed_json_on_exit_zero_returns_empty_string_not_crash(self, monitor):
        result = _mock_result(returncode=0, stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            context = monitor._get_agent_outputs_from_issue('widgets', 1, 'acme', ['business_analyst'])
        assert context == ""

    def test_open_breaker_returns_empty_string_without_calling_subprocess(self, monitor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            context = monitor._get_agent_outputs_from_issue('widgets', 1, 'acme', ['business_analyst'])
        assert context == ""
        mock_run.assert_not_called()


class TestDetermineMissingInputSeverity:
    def test_success_env_issue_is_debug(self, monitor):
        result = _mock_result(stdout='{"body": "", "labels": [{"name": "environment"}]}')
        with patch('subprocess.run', return_value=result):
            severity = monitor._determine_missing_input_severity('widgets', 1, 'acme', 'software_architect')
        assert severity == "DEBUG"

    def test_gh_failure_defaults_to_info(self, monitor):
        result = _mock_result(returncode=1, stderr="boom")
        with patch('subprocess.run', return_value=result):
            severity = monitor._determine_missing_input_severity('widgets', 1, 'acme', 'software_architect')
        assert severity == "INFO"

    def test_open_breaker_defaults_to_info_without_calling_subprocess(self, monitor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            severity = monitor._determine_missing_input_severity('widgets', 1, 'acme', 'software_architect')
        assert severity == "INFO"
        mock_run.assert_not_called()


class TestPostRepairCycleFailureSummary:
    def test_success_posts_comment_and_label(self, monitor):
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            monitor._post_repair_cycle_failure_summary(
                'acme-project', 'SDLC', 1, 'acme/widgets', 'tests failed', 1
            )
        # comment, label create, label add-label -- 3 gh calls
        assert mock_run.call_count == 3
        comment_cmd = mock_run.call_args_list[0].args[0]
        assert comment_cmd[:3] == ['gh', 'issue', 'comment']

    def test_comment_failure_skips_label_calls(self, monitor):
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")) as mock_run:
            monitor._post_repair_cycle_failure_summary(
                'acme-project', 'SDLC', 1, 'acme/widgets', 'tests failed', 1
            )
        # Only the comment attempt -- its failure raises, caught by the
        # outer except, before the label create/add-label calls are reached.
        assert mock_run.call_count == 1

    def test_label_add_failure_does_not_crash(self, monitor):
        comment_ok = _mock_result()
        label_create_ok = _mock_result()
        label_add_fails = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', side_effect=[comment_ok, label_create_ok, label_add_fails]):
            monitor._post_repair_cycle_failure_summary(  # must not raise
                'acme-project', 'SDLC', 1, 'acme/widgets', 'tests failed', 1
            )

    def test_open_breaker_does_not_crash(self, monitor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            monitor._post_repair_cycle_failure_summary(  # must not raise
                'acme-project', 'SDLC', 1, 'acme/widgets', 'tests failed', 1
            )
        mock_run.assert_not_called()


class TestFindOrphanedDiscussion:
    def test_success_finds_matching_discussion(self, monitor):
        result = _mock_result(stdout='{"title": "Fix the thing"}')
        monitor.discussions = MagicMock()
        monitor.discussions.list_discussions.return_value = [
            {'title': 'Requirements: Fix the thing', 'number': 5, 'createdAt': '2026-01-01T00:00:00Z'}
        ]
        with patch('subprocess.run', return_value=result):
            discussion = monitor._find_orphaned_discussion('acme', 'widgets', 1)
        assert discussion is not None
        assert discussion['number'] == 5

    def test_gh_failure_returns_none(self, monitor):
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', return_value=result):
            discussion = monitor._find_orphaned_discussion('acme', 'widgets', 1)
        assert discussion is None

    def test_malformed_json_on_exit_zero_returns_none_not_crash(self, monitor):
        result = _mock_result(returncode=0, stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            discussion = monitor._find_orphaned_discussion('acme', 'widgets', 1)
        assert discussion is None

    def test_open_breaker_returns_none_without_calling_subprocess(self, monitor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            discussion = monitor._find_orphaned_discussion('acme', 'widgets', 1)
        assert discussion is None
        mock_run.assert_not_called()


class TestFinalizeRequirementsToIssue:
    def _setup(self, monitor):
        monitor.config_manager.get_project_config.return_value = MagicMock(
            github={'org': 'acme', 'repo': 'widgets'}
        )
        monitor.discussions = MagicMock()
        monitor.discussions.get_discussion.return_value = {'number': 5, 'url': 'https://x'}
        monitor._extract_requirements_from_discussion = Mock(return_value={
            'executive_summary': 'the requirements'
        })

    def test_success_edits_body_and_label(self, monitor):
        self._setup(monitor)
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            monitor.finalize_requirements_to_issue('acme-project', 'Planning', 1, 'widgets', discussion_id='D_1')
        assert mock_run.call_count == 2
        body_cmd = mock_run.call_args_list[0].args[0]
        assert body_cmd[:3] == ['gh', 'issue', 'edit']
        assert '--body' in body_cmd

    def test_body_update_failure_skips_label_call(self, monitor):
        self._setup(monitor)
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")) as mock_run:
            monitor.finalize_requirements_to_issue('acme-project', 'Planning', 1, 'widgets', discussion_id='D_1')
        assert mock_run.call_count == 1

    def test_open_breaker_does_not_crash(self, monitor):
        self._setup(monitor)
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            monitor.finalize_requirements_to_issue('acme-project', 'Planning', 1, 'widgets', discussion_id='D_1')
        mock_run.assert_not_called()


class TestCreateDiscussionFromIssue:
    """_create_discussion_from_issue()'s link-comment call -- the deepest of
    the migrated sites to reach (state_manager, workspace_router, and
    discussions are all replaced with mocks to isolate just the gh_cli()
    call at the end)."""

    def _setup(self, monitor):
        monitor.workspace_router = MagicMock()
        monitor.workspace_router.determine_workspace.return_value = ('discussions', 'CAT_ID')
        monitor.discussions = MagicMock()
        monitor.discussions.get_repository_id.return_value = 'REPO_ID'
        monitor.discussions.create_discussion.return_value = {
            'id': 'D_NEW', 'number': 9, 'url': 'https://github.com/acme/widgets/discussions/9'
        }
        monitor.get_issue_details = Mock(return_value={'title': 'Fix the thing', 'body': ''})

        project_config = MagicMock(github={'org': 'acme', 'repo': 'widgets'})
        pipeline_config = MagicMock(board_name='Planning', discussion_stages=[])

        state_manager = MagicMock()
        state_manager.get_discussion_for_issue.return_value = None
        return project_config, pipeline_config, state_manager

    def test_success_posts_link_comment(self, monitor):
        project_config, pipeline_config, state_manager = self._setup(monitor)
        with patch('config.state_manager.state_manager', state_manager), \
             patch('subprocess.run', return_value=_mock_result()) as mock_run:
            monitor._create_discussion_from_issue('acme-project', 1, 'widgets', pipeline_config, project_config)
        mock_run.assert_called_once()
        cmd = mock_run.call_args.args[0]
        assert cmd[:3] == ['gh', 'issue', 'comment']

    def test_gh_failure_does_not_crash(self, monitor):
        project_config, pipeline_config, state_manager = self._setup(monitor)
        with patch('config.state_manager.state_manager', state_manager), \
             patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")):
            monitor._create_discussion_from_issue(  # must not raise
                'acme-project', 1, 'widgets', pipeline_config, project_config
            )

    def test_open_breaker_does_not_crash_without_calling_subprocess(self, monitor):
        project_config, pipeline_config, state_manager = self._setup(monitor)
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('config.state_manager.state_manager', state_manager), \
             patch('subprocess.run') as mock_run:
            monitor._create_discussion_from_issue(  # must not raise
                'acme-project', 1, 'widgets', pipeline_config, project_config
            )
        mock_run.assert_not_called()

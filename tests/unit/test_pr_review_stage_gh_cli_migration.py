"""
Tests for PRReviewStage's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation).

Before: ~13 raw `subprocess.run(['gh', ...])` calls, none breaker-protected
except _set_issue_status_on_board (already migrated earlier, per its own
docstring, for the same reason this whole consolidation exists). After: all
routed through gh_cli().

Unlike tests/unit/test_pr_review_stage.py, this file does not module-skip on
missing /app -- confirmed directly that PRReviewStage imports and constructs
fine locally with ConfigManager/GitHubStateManager/pr_review_state_manager
patched, the same pattern that file already uses. These tests need to
actually run to mean anything.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture
def stage():
    with patch('pipeline.pr_review_stage.ConfigManager'), \
         patch('pipeline.pr_review_stage.GitHubStateManager'), \
         patch('pipeline.pr_review_stage.pr_review_state_manager'):
        from pipeline.pr_review_stage import PRReviewStage
        return PRReviewStage()


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


class TestFindPrUrl:
    @pytest.mark.asyncio
    async def test_finds_pr_matching_branch_prefix(self, stage):
        result = _mock_result(stdout='[{"number": 5, "url": "https://github.com/o/r/pull/5", '
                                      '"headRefName": "feature/issue-42-fix"}]')
        with patch('subprocess.run', return_value=result):
            url = await stage._find_pr_url({'org': 'o', 'repo': 'r'}, 42)
        assert url == "https://github.com/o/r/pull/5"

    @pytest.mark.asyncio
    async def test_no_matching_branch_returns_none(self, stage):
        result = _mock_result(stdout='[{"number": 5, "url": "u", "headRefName": "feature/issue-99-other"}]')
        with patch('subprocess.run', return_value=result):
            url = await stage._find_pr_url({'org': 'o', 'repo': 'r'}, 42)
        assert url is None

    @pytest.mark.asyncio
    async def test_open_breaker_short_circuits(self, stage):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            url = await stage._find_pr_url({'org': 'o', 'repo': 'r'}, 42)
        assert url is None
        mock_run.assert_not_called()


class TestGetParentIssueBody:
    def test_success_returns_body(self, stage):
        result = _mock_result(stdout='{"body": "issue body text"}')
        with patch('subprocess.run', return_value=result):
            body = stage._get_parent_issue_body('o/r', 42)
        assert body == "issue body text"

    def test_gh_failure_returns_empty_string(self, stage):
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', return_value=result):
            body = stage._get_parent_issue_body('o/r', 42)
        assert body == ""


class TestCheckCiStatus:
    def test_exit_code_1_pending_is_parsed_not_a_failure(self, stage):
        """gh pr checks: 0=all pass, 1=pending, 8=some failing -- all valid."""
        result = _mock_result(returncode=1, stdout='[{"name": "ci", "bucket": "pending"}]')
        with patch('subprocess.run', return_value=result):
            failures, pending = stage._check_ci_status('https://github.com/o/r/pull/5', 'o/r')
        assert failures == []
        assert len(pending) == 1

    def test_exit_code_8_failing_checks_parsed(self, stage):
        result = _mock_result(returncode=8, stdout='[{"name": "ci", "bucket": "fail"}]')
        with patch('subprocess.run', return_value=result):
            failures, pending = stage._check_ci_status('https://github.com/o/r/pull/5', 'o/r')
        assert len(failures) == 1

    def test_empty_stdout_means_no_checks_configured(self, stage):
        result = _mock_result(returncode=0, stdout='')
        with patch('subprocess.run', return_value=result):
            failures, pending = stage._check_ci_status('https://github.com/o/r/pull/5', 'o/r')
        assert failures == []
        assert pending == []

    def test_unexpected_exit_code_raises(self, stage):
        result = _mock_result(returncode=2, stderr="some other error")
        with patch('subprocess.run', return_value=result):
            with pytest.raises(Exception):
                stage._check_ci_status('https://github.com/o/r/pull/5', 'o/r')

    def test_open_breaker_raises_rather_than_silently_passing(self, stage):
        """_check_ci_status re-raises on failure (callers abort the review
        run on it) -- an open breaker must not look like 'no CI configured'."""
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            with pytest.raises(Exception):
                stage._check_ci_status('https://github.com/o/r/pull/5', 'o/r')
        mock_run.assert_not_called()


class TestLinkSubIssue:
    def test_success_returns_none(self, stage):
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            error = stage._link_sub_issue('PARENT_ID', 'CHILD_ID', '5', 42)
        assert error is None
        cmd = mock_run.call_args.args[0]
        assert '-H' in cmd
        assert 'GraphQL-Features: sub_issues' in cmd

    def test_failure_returns_the_exception(self, stage):
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")):
            error = stage._link_sub_issue('PARENT_ID', 'CHILD_ID', '5', 42)
        assert error is not None

    def test_open_breaker_returns_exception_not_raise(self, stage):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            error = stage._link_sub_issue('PARENT_ID', 'CHILD_ID', '5', 42)
        assert error is not None
        mock_run.assert_not_called()


class TestQueryFeedbackIssueStates:
    def test_returns_lowercased_state(self, stage):
        result = _mock_result(stdout='{"state": "CLOSED"}')
        with patch('subprocess.run', return_value=result):
            states = stage._query_feedback_issue_states('o/r', [1])
        assert states == {1: 'closed'}

    def test_gh_failure_returns_unknown(self, stage):
        result = _mock_result(returncode=1, stderr="boom")
        with patch('subprocess.run', return_value=result):
            states = stage._query_feedback_issue_states('o/r', [1])
        assert states == {1: 'unknown'}

    def test_malformed_json_on_exit_zero_returns_unknown_not_crash(self, stage):
        """gh_cli() falls back to raw stdout on a parse failure instead of
        raising -- must still degrade to 'unknown', not crash the loop."""
        result = _mock_result(returncode=0, stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            states = stage._query_feedback_issue_states('o/r', [1])
        assert states == {1: 'unknown'}


class TestPostCommentOnIssue:
    def test_success(self, stage):
        with patch('subprocess.run', return_value=_mock_result()), \
             patch('monitoring.decision_events.get_decision_event_emitter'):
            assert stage._post_comment_on_issue('o/r', 42, 'a comment') is True

    def test_failure(self, stage):
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")):
            assert stage._post_comment_on_issue('o/r', 42, 'a comment') is False

    def test_open_breaker_returns_false(self, stage):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            assert stage._post_comment_on_issue('o/r', 42, 'a comment') is False
        mock_run.assert_not_called()

"""
Tests for GitHubIntegration's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation).

Before: 8 raw `subprocess.run(['gh', ...])` calls
(has_agent_processed_issue, find_pr_by_branch, add_issue_label,
create_issue_from_agent, create_pr, update_pr_body, mark_pr_ready,
delete_branch), none breaker-protected. After: all routed through gh_cli(),
passing env=self._get_gh_env() explicitly so this class's own GitHub-App-vs-PAT
credential selection is preserved rather than silently switched to gh_cli()'s
own default routing.
"""
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.github_integration import GitHubIntegration
from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture
def integration():
    return GitHubIntegration(repo_owner='acme', repo_name='widgets')


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


class TestHasAgentProcessedIssue:
    @pytest.mark.asyncio
    async def test_finds_signature_in_comments(self, integration):
        result = _mock_result(stdout='{"comments": [{"body": "_Processed by the work_breakdown_agent agent_"}]}')
        with patch('subprocess.run', return_value=result):
            processed = await integration.has_agent_processed_issue(1, 'work_breakdown_agent')
        assert processed is True

    @pytest.mark.asyncio
    async def test_gh_failure_defaults_to_not_processed(self, integration):
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', return_value=result):
            processed = await integration.has_agent_processed_issue(1, 'work_breakdown_agent')
        assert processed is False

    @pytest.mark.asyncio
    async def test_open_breaker_short_circuits(self, integration):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            processed = await integration.has_agent_processed_issue(1, 'work_breakdown_agent')
        assert processed is False
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_malformed_json_on_exit_zero_raises_not_swallowed_as_not_processed(self, integration):
        """gh_cli() falls back to raw stdout (not a raised exception) when gh
        exits 0 with non-JSON output. Must still surface as an error here --
        see tests/unit/services/test_github_owner_resolution.py's
        TestUnresolvedOwnerKeepsEachMethodsContract for the full incident
        this guards (a caller reading a bare `False` can't tell 'confirmed
        not processed' from 'gh's response could not be read')."""
        import json
        result = _mock_result(returncode=0, stdout='not json at all', stderr='')
        with patch('subprocess.run', return_value=result):
            with pytest.raises(json.JSONDecodeError):
                await integration.has_agent_processed_issue(1, 'work_breakdown_agent')


class TestFindPrByBranch:
    @pytest.mark.asyncio
    async def test_finds_existing_pr(self, integration):
        result = _mock_result(stdout='[{"number": 5, "title": "t", "url": "u", "state": "OPEN", "isDraft": false}]')
        with patch('subprocess.run', return_value=result):
            pr = await integration.find_pr_by_branch('feature/x')
        assert pr['pr_number'] == 5

    @pytest.mark.asyncio
    async def test_no_pr_found_returns_none(self, integration):
        result = _mock_result(stdout='[]')
        with patch('subprocess.run', return_value=result):
            pr = await integration.find_pr_by_branch('feature/x')
        assert pr is None


class TestAddIssueLabel:
    @pytest.mark.asyncio
    async def test_success_adds_each_label(self, integration):
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            await integration.add_issue_label(1, ['bug', 'urgent'])
        assert mock_run.call_count == 2

    @pytest.mark.asyncio
    async def test_first_label_failure_stops_remaining_labels(self, integration):
        """Matches the pre-migration check=True behavior: a CalledProcessError
        on the first label used to abort the loop entirely."""
        responses = [_mock_result(returncode=1, stderr="HTTP 422: label not found"), _mock_result()]
        with patch('subprocess.run', side_effect=responses) as mock_run:
            await integration.add_issue_label(1, ['bad-label', 'urgent'])
        assert mock_run.call_count == 1


class TestCreateIssueFromAgent:
    @pytest.mark.asyncio
    async def test_success_parses_issue_number(self, integration):
        result = _mock_result(stdout='https://github.com/acme/widgets/issues/42\n')
        with patch('subprocess.run', return_value=result):
            created = await integration.create_issue_from_agent('title', 'body')
        assert created == {'success': True, 'issue_number': 42, 'url': 'https://github.com/acme/widgets/issues/42'}

    @pytest.mark.asyncio
    async def test_gh_failure_returns_error_dict(self, integration):
        result = _mock_result(returncode=1, stderr="HTTP 403: Forbidden")
        with patch('subprocess.run', return_value=result):
            created = await integration.create_issue_from_agent('title', 'body')
        assert created['success'] is False


class TestCreatePr:
    @pytest.mark.asyncio
    async def test_creates_new_pr_when_none_exists(self, integration):
        find_result = _mock_result(stdout='[]')  # find_pr_by_branch: none found
        create_result = _mock_result(stdout='https://github.com/acme/widgets/pull/9\n')
        with patch('subprocess.run', side_effect=[find_result, create_result]):
            created = await integration.create_pr('feature/x', 'title', 'body')
        assert created == {'success': True, 'pr_number': 9, 'pr_url': 'https://github.com/acme/widgets/pull/9', 'already_existed': False}

    @pytest.mark.asyncio
    async def test_race_condition_already_exists_recovers_via_lookup(self, integration):
        """The 'already exists' stderr text-match race-recovery path needs
        stderr preserved verbatim from gh_cli()."""
        find_result_1 = _mock_result(stdout='[]')  # initial check: none found
        create_result = _mock_result(returncode=1, stderr="a pull request for branch \"feature/x\" already exists")
        find_result_2 = _mock_result(stdout='[{"number": 9, "title": "t", "url": "https://github.com/acme/widgets/pull/9", "state": "OPEN", "isDraft": false}]')
        with patch('subprocess.run', side_effect=[find_result_1, create_result, find_result_2]):
            created = await integration.create_pr('feature/x', 'title', 'body')
        assert created['success'] is True
        assert created['pr_number'] == 9
        assert created['race_condition'] is True


class TestUpdatePrBody:
    @pytest.mark.asyncio
    async def test_success(self, integration):
        with patch('subprocess.run', return_value=_mock_result()):
            assert await integration.update_pr_body(9, 'new body') is True

    @pytest.mark.asyncio
    async def test_failure(self, integration):
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")):
            assert await integration.update_pr_body(9, 'new body') is False


class TestMarkPrReady:
    @pytest.mark.asyncio
    async def test_success_first_attempt(self, integration):
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            assert await integration.mark_pr_ready(9) is True
        assert mock_run.call_count == 1

    @pytest.mark.asyncio
    async def test_is_not_a_draft_treated_as_already_ready(self, integration):
        """stderr text-match idempotency check needs stderr preserved verbatim."""
        result = _mock_result(returncode=1, stderr="pull request #9 is not a draft")
        with patch('subprocess.run', return_value=result):
            assert await integration.mark_pr_ready(9) is True

    @pytest.mark.asyncio
    async def test_timeout_retries_then_succeeds(self, integration):
        with patch('subprocess.run', side_effect=[
                    subprocess.TimeoutExpired(cmd='gh', timeout=30),
                    _mock_result(),
                ]), \
             patch('asyncio.sleep', new_callable=AsyncMock):
            assert await integration.mark_pr_ready(9, max_retries=3) is True

    @pytest.mark.asyncio
    async def test_exhausts_retries_and_returns_false(self, integration):
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="server error")), \
             patch('asyncio.sleep', new_callable=AsyncMock):
            assert await integration.mark_pr_ready(9, max_retries=3) is False

    @pytest.mark.asyncio
    async def test_open_breaker_short_circuits_without_retry_loop_delay(self, integration):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run, \
             patch('asyncio.sleep', new_callable=AsyncMock):
            assert await integration.mark_pr_ready(9, max_retries=3) is False
        mock_run.assert_not_called()


class TestDeleteBranch:
    @pytest.mark.asyncio
    async def test_success(self, integration):
        with patch('subprocess.run', return_value=_mock_result()):
            assert await integration.delete_branch('feature/x') is True

    @pytest.mark.asyncio
    async def test_failure(self, integration):
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="boom")):
            assert await integration.delete_branch('feature/x') is False

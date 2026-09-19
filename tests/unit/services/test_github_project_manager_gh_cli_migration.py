"""
Tests for GitHubProjectManager's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation).

Before: 3 raw `subprocess.run(['gh', ...])` calls
(_reconcile_labels, _link_board_to_repository, _verify_board_exists), none
breaker-protected. After: all routed through gh_cli().

_diagnose_github_board_failure is deliberately NOT migrated (per the
consolidation plan) -- it's a diagnostic tool that intentionally probes raw
GitHub/auth failure modes including when the breaker is open, so migrating
it would defeat its purpose. Not tested here for that reason.
"""
from unittest.mock import MagicMock, patch

import pytest

from services.github_project_manager import GitHubProjectManager
from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture
def manager():
    return GitHubProjectManager(config_manager=MagicMock(), state_manager=MagicMock())


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


class TestReconcileLabels:
    def _project_config(self):
        pipeline = MagicMock(active=True, name='dev', workflow='dev_workflow', description='Dev')
        return MagicMock(pipelines=[pipeline])

    @pytest.mark.asyncio
    async def test_creates_labels_via_gh_cli(self, manager):
        manager.config_manager.get_workflow_templates.return_value = {
            'dev_workflow': MagicMock(columns=[])
        }
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            result = await manager._reconcile_labels('acme-project', self._project_config())
        assert result is True
        assert mock_run.called
        cmd = mock_run.call_args_list[0].args[0]
        assert cmd[:3] == ['gh', 'label', 'create']

    @pytest.mark.asyncio
    async def test_label_already_existing_is_not_fatal(self, manager):
        manager.config_manager.get_workflow_templates.return_value = {
            'dev_workflow': MagicMock(columns=[])
        }
        with patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="already exists")):
            result = await manager._reconcile_labels('acme-project', self._project_config())
        assert result is True

    @pytest.mark.asyncio
    async def test_open_breaker_does_not_crash_reconciliation(self, manager):
        manager.config_manager.get_workflow_templates.return_value = {
            'dev_workflow': MagicMock(columns=[])
        }
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            result = await manager._reconcile_labels('acme-project', self._project_config())
        assert result is True
        mock_run.assert_not_called()


class TestLinkBoardToRepository:
    @pytest.mark.asyncio
    async def test_success(self, manager):
        with patch('subprocess.run', return_value=_mock_result()) as mock_run:
            linked = await manager._link_board_to_repository(7, 'acme', 'widgets', 'SDLC')
        assert linked is True
        cmd = mock_run.call_args.args[0]
        assert cmd == ['gh', 'project', 'link', '7', '--owner', 'acme', '--repo', 'widgets']

    @pytest.mark.asyncio
    async def test_already_linked_treated_as_success(self, manager):
        result = _mock_result(returncode=1, stderr="Project is already linked to this repository")
        with patch('subprocess.run', return_value=result):
            linked = await manager._link_board_to_repository(7, 'acme', 'widgets', 'SDLC')
        assert linked is True

    @pytest.mark.asyncio
    async def test_other_failure_returns_false(self, manager):
        result = _mock_result(returncode=1, stderr="HTTP 403: Forbidden")
        with patch('subprocess.run', return_value=result):
            linked = await manager._link_board_to_repository(7, 'acme', 'widgets', 'SDLC')
        assert linked is False

    @pytest.mark.asyncio
    async def test_open_breaker_returns_false_without_calling_subprocess(self, manager):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            linked = await manager._link_board_to_repository(7, 'acme', 'widgets', 'SDLC')
        assert linked is False
        mock_run.assert_not_called()


class TestVerifyBoardExists:
    @pytest.mark.asyncio
    async def test_board_found_for_organization(self, manager):
        result = _mock_result(stdout='{"data": {"organization": {"projectV2": {"id": "X", "number": 7}}}}')
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('subprocess.run', return_value=result):
            exists = await manager._verify_board_exists(7, 'acme')
        assert exists is True

    @pytest.mark.asyncio
    async def test_board_not_found(self, manager):
        result = _mock_result(stdout='{"data": {"organization": {"projectV2": null}}}')
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('subprocess.run', return_value=result):
            exists = await manager._verify_board_exists(7, 'acme')
        assert exists is False

    @pytest.mark.asyncio
    async def test_gh_failure_assumes_board_still_exists(self, manager):
        """A `gh_cli()`-level failure (breaker-open, timeout, rate limit, an
        unclassified CLI/HTTP error) is never how GitHub reports "this board
        doesn't exist" for this GraphQL query -- that only ever arrives as a
        SUCCESSFUL response with projectV2: null (see test_board_not_found
        above). Any failure here is therefore inconclusive, not a negative
        answer, so it must not trigger the caller's search-by-name ->
        possible-duplicate-creation path. Regression test for a gap found in
        code review of PR #270 (the first fix only special-cased
        circuit_open/timeout, still misreading a plain HTTP 404/generic
        failure as 'board not found')."""
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('subprocess.run', return_value=result):
            exists = await manager._verify_board_exists(7, 'acme')
        assert exists is True

    @pytest.mark.asyncio
    async def test_malformed_json_on_exit_zero_assumes_board_still_exists(self, manager):
        """A successful call with an unparseable body is equally
        inconclusive -- not the structured null response that means the
        board is genuinely gone."""
        result = _mock_result(returncode=0, stdout='not json at all')
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('subprocess.run', return_value=result):
            exists = await manager._verify_board_exists(7, 'acme')
        assert exists is True

    @pytest.mark.asyncio
    async def test_open_breaker_assumes_board_still_exists_without_calling_subprocess(self, manager):
        """A breaker-open failure means we could not ask GitHub at all --
        NOT the same as GitHub answering 'no such board'. The caller treats
        False as license to search-by-name and, on a miss, create a
        duplicate board -- the highest-risk outcome during startup
        reconciliation. Must assume the board still exists rather than risk
        that from an unrelated, transient breaker trip. Regression test for
        a bug found in code review of PR #270 (previously returned False
        here, indistinguishable from a genuinely deleted board)."""
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('services.github_owner_utils.get_owner_type', return_value='organization'), \
             patch('subprocess.run') as mock_run:
            exists = await manager._verify_board_exists(7, 'acme')
        assert exists is True
        mock_run.assert_not_called()

"""
Tests for ReviewCycleExecutor's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation).

Before: 3 raw `subprocess.run(['gh', ...])` calls
(_get_latest_agent_comment, _escalate_blocked, _escalate_max_iterations),
none breaker-protected. After: all routed through gh_cli().
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.review_cycle import ReviewCycleExecutor, ReviewCycleState
from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture
def executor():
    return ReviewCycleExecutor()


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


def _cycle_state(**overrides):
    kwargs = dict(
        issue_number=42,
        repository='widgets',
        maker_agent='senior_software_engineer',
        reviewer_agent='code_reviewer',
        max_iterations=3,
        project_name='acme',
        board_name='SDLC Execution',
        workspace_type='issues',
    )
    kwargs.update(overrides)
    return ReviewCycleState(**kwargs)


class TestGetLatestAgentComment:
    @pytest.mark.asyncio
    async def test_finds_latest_matching_comment(self, executor):
        result = _mock_result(stdout='{"comments": ['
                                      '{"body": "_Processed by the code_reviewer agent_ first"}, '
                                      '{"body": "_Processed by the code_reviewer agent_ latest"}'
                                      ']}')
        with patch('subprocess.run', return_value=result):
            comment = await executor._get_latest_agent_comment(42, 'widgets', 'code_reviewer', org='acme')
        assert comment == "_Processed by the code_reviewer agent_ latest"

    @pytest.mark.asyncio
    async def test_gh_failure_returns_empty_string(self, executor):
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', return_value=result):
            comment = await executor._get_latest_agent_comment(42, 'widgets', 'code_reviewer', org='acme')
        assert comment == ""

    @pytest.mark.asyncio
    async def test_open_breaker_short_circuits(self, executor):
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            comment = await executor._get_latest_agent_comment(42, 'widgets', 'code_reviewer', org='acme')
        assert comment == ""
        mock_run.assert_not_called()


class TestEscalateBlockedLabelling:
    """The label-add is a small fire-and-forget fragment inside a much larger
    method (decision events, comment posting) -- these tests isolate it by
    mocking everything else the method touches."""

    def _make_review_result(self):
        finding = MagicMock(severity='blocking', message='missing null check', category='bug')
        result = MagicMock()
        result.findings = [finding]
        result.blocking_count = 1
        return result

    @pytest.mark.asyncio
    async def test_adds_label_via_gh_cli(self, executor):
        cycle_state = _cycle_state()
        review_result = self._make_review_result()

        with patch.object(executor.decision_events, 'emit_review_cycle_decision'), \
             patch.object(executor, '_get_github_integration', return_value=AsyncMock()), \
             patch('subprocess.run', return_value=_mock_result()) as mock_run:
            await executor._escalate_blocked(cycle_state, review_result)

        cmd = mock_run.call_args.args[0]
        assert cmd == ['gh', 'issue', 'edit', '42', '--repo', 'widgets', '--add-label', 'needs-human-review']

    @pytest.mark.asyncio
    async def test_label_failure_does_not_crash_escalation(self, executor):
        cycle_state = _cycle_state()
        review_result = self._make_review_result()

        with patch.object(executor.decision_events, 'emit_review_cycle_decision'), \
             patch.object(executor, '_get_github_integration', return_value=AsyncMock()), \
             patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="HTTP 403: Forbidden")):
            await executor._escalate_blocked(cycle_state, review_result)  # must not raise

    @pytest.mark.asyncio
    async def test_open_breaker_does_not_crash_escalation(self, executor):
        cycle_state = _cycle_state()
        review_result = self._make_review_result()
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None

        with patch.object(executor.decision_events, 'emit_review_cycle_decision'), \
             patch.object(executor, '_get_github_integration', return_value=AsyncMock()), \
             patch('subprocess.run') as mock_run:
            await executor._escalate_blocked(cycle_state, review_result)

        mock_run.assert_not_called()


class TestEscalateMaxIterationsLabelling:
    def _make_review_result(self):
        result = MagicMock()
        result.findings = []
        result.high_severity_count = 0
        result.score = 0.8
        result.summary = "summary text"
        return result

    @pytest.mark.asyncio
    async def test_adds_label_via_gh_cli(self, executor):
        cycle_state = _cycle_state()
        review_result = self._make_review_result()

        with patch.object(executor.decision_events, 'emit_review_cycle_decision'), \
             patch.object(executor, '_get_github_integration', return_value=AsyncMock()), \
             patch('subprocess.run', return_value=_mock_result()) as mock_run:
            await executor._escalate_max_iterations(cycle_state, review_result)

        cmd = mock_run.call_args.args[0]
        assert cmd == ['gh', 'issue', 'edit', '42', '--repo', 'widgets', '--add-label', 'needs-human-review']

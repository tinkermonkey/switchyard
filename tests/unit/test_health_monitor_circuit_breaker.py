"""
Unit tests for HealthMonitor's GitHub circuit breaker consolidation (#36
follow-up).

HealthMonitor._github_api_call_with_circuit_breaker() used to guard its own
GitHub auth-probe calls (gh api user / gh api repos/...) with a second,
independent CircuitBreaker instance (_github_health_circuit_breaker),
completely disconnected from the real one
(services/github_api_client.py's GitHubAPIClient.breaker) the rest of the
system's GraphQL/REST traffic actually uses.

During a real production incident, the two diverged: the real breaker was
open (GraphQL rate limit exhausted), but the health check's own probe calls
are cheap REST calls hitting a separate rate-limit bucket, so they kept
succeeding - the dashboard reported "no open circuit breakers" and a
healthy rate limit for the entire outage.

Fix: _github_api_call_with_circuit_breaker() now checks the REAL breaker
before attempting a call, instead of maintaining its own independent one.

No live network access - all subprocess/GitHub calls are mocked.
"""
import subprocess

import pytest
from unittest.mock import AsyncMock, Mock, patch

from services.circuit_breaker import CircuitBreakerOpen
from monitoring.health_monitor import HealthMonitor


@pytest.fixture
def monitor():
    return HealthMonitor()


class TestConsolidatedGitHubCircuitBreaker:
    def test_no_longer_has_its_own_independent_breaker(self):
        """The separate _github_health_circuit_breaker must be gone
        entirely - a stray leftover would mean the divergence risk this
        fix addresses is still latent in the class."""
        assert not hasattr(HealthMonitor, '_github_health_circuit_breaker')

    @pytest.mark.asyncio
    async def test_real_breaker_open_blocks_the_call_without_attempting_it(self, monitor):
        """When the real GitHubAPIClient breaker is open, the health
        check's probe call must be rejected immediately (CircuitBreakerOpen)
        without ever invoking the underlying subprocess - matching what the
        rest of the system already does when the real breaker is open."""
        mock_client = Mock()
        mock_client.breaker.is_open.return_value = True

        with patch(
            'monitoring.health_monitor.get_github_client', return_value=mock_client
        ), patch.object(
            monitor, '_run_subprocess_with_retry', new_callable=AsyncMock
        ) as mock_run:
            with pytest.raises(CircuitBreakerOpen):
                await monitor._github_api_call_with_circuit_breaker(
                    ['gh', 'api', 'user'], description="test call"
                )

        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_real_breaker_closed_allows_the_call_through(self, monitor):
        """When the real breaker is closed, the call proceeds normally via
        the existing retry logic - byte-identical to before this fix for
        the common (healthy) case."""
        mock_client = Mock()
        mock_client.breaker.is_open.return_value = False

        expected_result = subprocess.CompletedProcess(['gh', 'api', 'user'], returncode=0, stdout='octocat', stderr='')

        with patch(
            'monitoring.health_monitor.get_github_client', return_value=mock_client
        ), patch.object(
            monitor, '_run_subprocess_with_retry', new_callable=AsyncMock, return_value=expected_result
        ) as mock_run:
            result = await monitor._github_api_call_with_circuit_breaker(
                ['gh', 'api', 'user'], timeout=15, retries=1, description="test call"
            )

        assert result is expected_result
        mock_run.assert_called_once_with(['gh', 'api', 'user'], 15, 1, "test call")

    @pytest.mark.asyncio
    async def test_check_github_reflects_the_real_breaker_being_open(self, monitor):
        """End-to-end: check_github() must surface the real breaker's open
        state (not crash, not silently report healthy) when it's open -
        this is the exact scenario that misled the dashboard in production."""
        mock_client = Mock()
        mock_client.breaker.is_open.return_value = True

        HealthMonitor._github_auth_cache = None
        HealthMonitor._github_auth_cache_time = None

        with patch('monitoring.health_monitor.get_github_client', return_value=mock_client):
            health = await monitor.check_github()

        assert 'healthy' in health
        assert health['healthy'] is False

    @pytest.mark.asyncio
    async def test_calls_check_and_close_before_is_open(self, monitor):
        """Matches every real call site in services/github_api_client.py
        (graphql()/rest()/http_request()/gh_cli() all call check_and_close()
        before is_open()) - without it, this health check could keep
        reporting a stale "circuit open" for up to ~5 minutes after the
        real rate limit resets, since is_open() alone is a pure state read
        that can't itself transition OPEN -> HALF_OPEN once reset_time has
        passed."""
        mock_client = Mock()
        call_order = []
        mock_client.breaker.check_and_close.side_effect = lambda: call_order.append('check_and_close')
        mock_client.breaker.is_open.side_effect = lambda: call_order.append('is_open') or False

        expected_result = subprocess.CompletedProcess(['gh', 'api', 'user'], returncode=0, stdout='octocat', stderr='')

        with patch(
            'monitoring.health_monitor.get_github_client', return_value=mock_client
        ), patch.object(
            monitor, '_run_subprocess_with_retry', new_callable=AsyncMock, return_value=expected_result
        ):
            await monitor._github_api_call_with_circuit_breaker(['gh', 'api', 'user'], description="test call")

        assert call_order == ['check_and_close', 'is_open']

    @pytest.mark.asyncio
    async def test_breaker_open_error_is_flagged_transient_not_a_pat_failure(self, monitor):
        """
        Regression test for the incident this same fix could have caused:
        without an explicit 'transient' flag, main.py's is_transient keyword
        classifier doesn't recognize the circuit breaker's error message
        (it contains none of 'eof'/'timeout'/'connection'/'network'/
        'temporary'), so a normal, self-recovering breaker-open condition
        gets treated as a persistent failure - and after
        max_consecutive_failures cycles, exit(1)s the whole orchestrator
        over exactly the condition the shared breaker exists to tolerate.

        Also verifies the error message no longer misleadingly says "PAT
        authentication failed" for what is actually a shared rate-limit
        condition, not an auth problem.
        """
        mock_client = Mock()
        mock_client.breaker.is_open.return_value = True

        HealthMonitor._github_auth_cache = None
        HealthMonitor._github_auth_cache_time = None

        with patch('monitoring.health_monitor.get_github_client', return_value=mock_client):
            health = await monitor.check_github()

        assert health['transient'] is True
        assert 'PAT authentication failed' not in health['error']
        assert health.get('critical') is None

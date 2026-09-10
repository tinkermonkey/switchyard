"""
Unit tests for HealthMonitor.check_github()'s rate-limit-driven 'degraded'
computation, added/changed alongside the fix to
services/github_api_client.py's get_shared_rate_limit_status(): a Redis
read failure now returns 'unavailable': True, distinct from
'never_observed', and check_github() must fall back to this process's own
local GitHubAPIClient reading rather than silently treating the shared
view's absence as "0% used" (which used to make the degraded check
permanently unable to fire during exactly the outage it matters most for).

No live network access - github_capabilities/github_app/config/the three
gh CLI probes are all mocked so execution reaches the rate-limit section
without exercising real GitHub/subprocess calls.
"""
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from monitoring.health_monitor import HealthMonitor


@pytest.fixture(autouse=True)
def reset_github_auth_cache():
    """check_github() caches its whole result on the HealthMonitor CLASS
    for 30 minutes - without resetting this between tests, only the first
    test in a run would ever actually execute the method body."""
    HealthMonitor._github_auth_cache = None
    HealthMonitor._github_auth_cache_time = None
    yield
    HealthMonitor._github_auth_cache = None
    HealthMonitor._github_auth_cache_time = None


@pytest.fixture
def monitor():
    return HealthMonitor()


def _ok_probe_result(cmd):
    import subprocess
    return subprocess.CompletedProcess(cmd, returncode=0, stdout='{}', stderr='')


def _local_bucket_dict(percentage_used, ever_updated=True):
    return {
        'limit': 5000,
        'remaining': int(5000 * (1 - (percentage_used or 0) / 100)),
        'used': 0,
        'percentage_used': percentage_used,
        'reset_time': None,
        'time_until_reset': None,
        'resource_type': 'graphql',
        'last_updated': '2026-09-01T00:00:00+00:00',
        'ever_updated': ever_updated,
        'stale': False,
    }


def _no_holds():
    """GitHubApp.get_graphql_hold_status()'s shape with neither credential
    held - the normal case."""
    return {
        'app': {'active': False, 'remaining_seconds': None, 'reset_at': None,
                'suppressed_requests': 0},
        'pat': {'active': False, 'remaining_seconds': None, 'reset_at': None,
                'suppressed_requests': 0},
    }


def _hold_on(credential, remaining_seconds=2900.0, suppressed=41):
    holds = _no_holds()
    holds[credential] = {
        'active': True,
        'remaining_seconds': remaining_seconds,
        'reset_at': '2026-09-01T01:00:00+00:00',
        'suppressed_requests': suppressed,
    }
    return holds


async def _run_check_github(monitor, rate_limit_graphql_info, rate_limit_rest_info,
                            local_client_status=None, graphql_hold_status=None):
    """Drives check_github() through to its rate-limit/degraded section by
    mocking every earlier gate to succeed, then mocks
    get_shared_rate_limit_status() and get_github_client() for the section
    under test."""
    mock_capabilities = Mock()
    mock_capabilities.check_capabilities.return_value = {'capabilities': {}, 'warnings': []}
    mock_capabilities.has_capability.return_value = True  # GITHUB_APP_AUTH present -> not degraded from this alone

    mock_github_app = Mock()
    mock_github_app.enabled = False
    # An explicit dict, not a bare Mock attribute: check_github() derives its
    # "a hold is in force" degraded input from this, and a Mock would take the
    # whole rate-limit section into its except-handler.
    if isinstance(graphql_hold_status, Exception):
        mock_github_app.get_graphql_hold_status.side_effect = graphql_hold_status
    else:
        mock_github_app.get_graphql_hold_status.return_value = (
            _no_holds() if graphql_hold_status is None else graphql_hold_status
        )

    mock_config_manager = Mock()
    mock_config_manager.list_projects.return_value = ['test-project']
    mock_project_config = Mock()
    mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
    mock_config_manager.get_project_config.return_value = mock_project_config

    local_status = local_client_status or {
        'rate_limit_graphql': _local_bucket_dict(None, ever_updated=False),
        'rate_limit_rest': _local_bucket_dict(None, ever_updated=False),
        'breaker': {'state': 'closed', 'is_open': False, 'opened_at': None, 'reset_time': None},
        'stats': {'total_requests': 0, 'failed_requests': 0, 'rate_limited_requests': 0, 'backoff_multiplier': 1.0},
    }
    mock_github_client = Mock()
    mock_github_client.get_status.return_value = local_status

    with patch('services.github_capabilities.github_capabilities', mock_capabilities), \
         patch('services.github_app.github_app', mock_github_app), \
         patch.object(
             HealthMonitor, '_github_api_call_with_circuit_breaker',
             new=AsyncMock(side_effect=lambda cmd, **kw: _ok_probe_result(cmd)),
         ), \
         patch('config.manager.ConfigManager', return_value=mock_config_manager), \
         patch('services.github_owner_utils.get_projects_list_for_owner', return_value=['proj1']), \
         patch('monitoring.health_monitor.get_github_client', return_value=mock_github_client), \
         patch(
             'services.github_api_client.get_shared_rate_limit_status',
             # .copy() matches the real function's behavior (a fresh dict
             # per call) - without it, a test passing the SAME dict object
             # for both graphql and rest (as several below deliberately do,
             # for brevity) would have check_github()'s fallback loop
             # mutate one shared object twice, with the second bucket's
             # local values silently clobbering the first's.
             side_effect=lambda resource_type: (
                 rate_limit_graphql_info if resource_type == 'graphql' else rate_limit_rest_info
             ).copy(),
         ):
        return await monitor.check_github()


class TestDegradedFromSharedView:
    @pytest.mark.asyncio
    async def test_never_observed_does_not_degrade(self, monitor):
        """A genuinely quiet bucket (never_observed=True, percentage_used=
        None) must not itself count as degraded."""
        info = {
            'remaining': None, 'limit': None, 'percentage_used': None,
            'reset_time': None, 'last_updated': None,
            'never_observed': True, 'unavailable': False, 'stale': False,
        }
        result = await _run_check_github(monitor, info, info)
        assert result['degraded'] is False

    @pytest.mark.asyncio
    async def test_high_percentage_used_degrades(self, monitor):
        info_high = {
            'remaining': 100, 'limit': 5000, 'percentage_used': 98.0,
            'reset_time': None, 'last_updated': '2026-09-01T00:00:00+00:00',
            'never_observed': False, 'unavailable': False, 'stale': False,
        }
        info_ok = {
            'remaining': 4000, 'limit': 5000, 'percentage_used': 20.0,
            'reset_time': None, 'last_updated': '2026-09-01T00:00:00+00:00',
            'never_observed': False, 'unavailable': False, 'stale': False,
        }
        result = await _run_check_github(monitor, info_high, info_ok)
        assert result['degraded'] is True

    @pytest.mark.asyncio
    async def test_low_percentage_used_does_not_degrade(self, monitor):
        info = {
            'remaining': 4000, 'limit': 5000, 'percentage_used': 20.0,
            'reset_time': None, 'last_updated': '2026-09-01T00:00:00+00:00',
            'never_observed': False, 'unavailable': False, 'stale': False,
        }
        result = await _run_check_github(monitor, info, info)
        assert result['degraded'] is False


class TestUnavailableFallsBackToLocalClient:
    """The fix this class regression-tests: get_shared_rate_limit_status()
    returning 'unavailable': True (a Redis outage, not a quiet bucket)
    must not silently coerce percentage_used to 0 forever - check_github()
    should fall back to this process's own local GitHubAPIClient reading,
    which HealthMonitor can trust since it runs in-process with the
    orchestrator's real traffic."""

    @pytest.mark.asyncio
    async def test_unavailable_with_high_local_percentage_degrades(self, monitor):
        unavailable = {
            'remaining': None, 'limit': None, 'percentage_used': None,
            'reset_time': None, 'last_updated': None,
            'never_observed': False, 'unavailable': True, 'stale': False,
        }
        local_status = {
            'rate_limit_graphql': _local_bucket_dict(97.0, ever_updated=True),
            'rate_limit_rest': _local_bucket_dict(10.0, ever_updated=True),
            'breaker': {'state': 'closed', 'is_open': False, 'opened_at': None, 'reset_time': None},
            'stats': {'total_requests': 10, 'failed_requests': 0, 'rate_limited_requests': 0, 'backoff_multiplier': 1.0},
        }

        result = await _run_check_github(monitor, unavailable, unavailable, local_client_status=local_status)

        assert result['degraded'] is True
        # The reported percentage should reflect the local fallback, not None/0.
        assert result['api_rate_limit_graphql']['percentage_used'] == 97.0

    @pytest.mark.asyncio
    async def test_unavailable_with_low_local_percentage_does_not_degrade(self, monitor):
        unavailable = {
            'remaining': None, 'limit': None, 'percentage_used': None,
            'reset_time': None, 'last_updated': None,
            'never_observed': False, 'unavailable': True, 'stale': False,
        }
        local_status = {
            'rate_limit_graphql': _local_bucket_dict(10.0, ever_updated=True),
            'rate_limit_rest': _local_bucket_dict(5.0, ever_updated=True),
            'breaker': {'state': 'closed', 'is_open': False, 'opened_at': None, 'reset_time': None},
            'stats': {'total_requests': 10, 'failed_requests': 0, 'rate_limited_requests': 0, 'backoff_multiplier': 1.0},
        }

        result = await _run_check_github(monitor, unavailable, unavailable, local_client_status=local_status)

        assert result['degraded'] is False

    @pytest.mark.asyncio
    async def test_unavailable_with_no_local_reading_either_degrades(self, monitor):
        """Total loss of visibility (shared view down AND this process has
        never made a real call of this type either) must itself count as
        degraded - it's a real loss of signal, not silence."""
        unavailable = {
            'remaining': None, 'limit': None, 'percentage_used': None,
            'reset_time': None, 'last_updated': None,
            'never_observed': False, 'unavailable': True, 'stale': False,
        }
        local_status = {
            'rate_limit_graphql': _local_bucket_dict(None, ever_updated=False),
            'rate_limit_rest': _local_bucket_dict(None, ever_updated=False),
            'breaker': {'state': 'closed', 'is_open': False, 'opened_at': None, 'reset_time': None},
            'stats': {'total_requests': 0, 'failed_requests': 0, 'rate_limited_requests': 0, 'backoff_multiplier': 1.0},
        }

        result = await _run_check_github(monitor, unavailable, unavailable, local_client_status=local_status)

        assert result['degraded'] is True


class TestDegradedFromAppGraphQLBudget:
    """#168's other half: the App installation spends its OWN 5000/hr GraphQL
    budget, and exhausting it left /health reporting healthy/not-degraded for
    the entire ~50-minute window - the PAT buckets it did consult were
    untouched, because they are a different quota."""

    @staticmethod
    def _quiet_shared_view():
        return {
            'remaining': 4000, 'limit': 5000, 'percentage_used': 20.0,
            'reset_time': None, 'last_updated': '2026-09-01T00:00:00+00:00',
            'never_observed': False, 'unavailable': False, 'stale': False,
        }

    @staticmethod
    def _local_status(app_bucket):
        return {
            'rate_limit_graphql': _local_bucket_dict(20.0, ever_updated=True),
            'rate_limit_rest': _local_bucket_dict(5.0, ever_updated=True),
            'rate_limit_app_graphql': app_bucket,
            'breaker': {'state': 'closed', 'is_open': False, 'opened_at': None, 'reset_time': None},
            'stats': {'total_requests': 10, 'failed_requests': 0, 'rate_limited_requests': 0, 'backoff_multiplier': 1.0},
        }

    @pytest.mark.asyncio
    async def test_exhausted_app_bucket_degrades(self, monitor):
        """The regression: PAT buckets quiet, App bucket at 100%."""
        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._local_status(
                _local_bucket_dict(100.0, ever_updated=True)
            ),
        )

        assert result['degraded'] is True
        assert result['api_rate_limit_app_graphql']['percentage_used'] == 100.0

    @pytest.mark.asyncio
    async def test_pat_only_deployment_does_not_degrade_by_accident(self, monitor):
        """The App bucket is populated ONLY by real App-credential responses,
        so on a PAT-only deployment it sits at its 5000/5000 constructor
        defaults with ever_updated=False. That placeholder must not be read as
        a measurement in either direction."""
        never_used = _local_bucket_dict(0.0, ever_updated=False)
        never_used['percentage_used'] = 99.9  # would degrade if ever_updated were ignored

        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._local_status(never_used),
        )

        assert result['degraded'] is False

    @pytest.mark.asyncio
    async def test_healthy_app_bucket_does_not_degrade(self, monitor):
        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._local_status(
                _local_bucket_dict(12.0, ever_updated=True)
            ),
        )

        assert result['degraded'] is False

    @pytest.mark.asyncio
    async def test_missing_app_bucket_key_does_not_blank_the_section(self, monitor):
        """An older/stubbed client status without the key must degrade to
        'no App reading', not take the breaker and call stats down with it."""
        local = self._local_status(_local_bucket_dict(100.0, ever_updated=True))
        del local['rate_limit_app_graphql']

        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=local,
        )

        assert result['degraded'] is False
        assert result['api_rate_limit_app_graphql'] is None
        assert result['circuit_breaker'] is not None


class TestDegradedFromGraphQLHold:
    """A hold, not a percentage, is what actually stops GitHubApp.
    graphql_request() from making a call - and a hold on the PAT leg updates
    no bucket at all (only an installation token's response headers are
    attributed to the App bucket), so a percentage-only test cannot see it."""

    @staticmethod
    def _quiet_shared_view():
        return {
            'remaining': 4000, 'limit': 5000, 'percentage_used': 20.0,
            'reset_time': None, 'last_updated': '2026-09-01T00:00:00+00:00',
            'never_observed': False, 'unavailable': False, 'stale': False,
        }

    @staticmethod
    def _quiet_local():
        return {
            'rate_limit_graphql': _local_bucket_dict(20.0, ever_updated=True),
            'rate_limit_rest': _local_bucket_dict(5.0, ever_updated=True),
            'rate_limit_app_graphql': _local_bucket_dict(0.0, ever_updated=False),
            'breaker': {'state': 'closed', 'is_open': False, 'opened_at': None, 'reset_time': None},
            'stats': {'total_requests': 10, 'failed_requests': 0, 'rate_limited_requests': 0, 'backoff_multiplier': 1.0},
        }

    @pytest.mark.asyncio
    async def test_app_hold_degrades_and_is_reported(self, monitor):
        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._quiet_local(),
            graphql_hold_status=_hold_on('app'),
        )

        assert result['degraded'] is True
        assert result['github_app_graphql_holds']['app']['active'] is True
        assert result['github_app_graphql_holds']['app']['suppressed_requests'] == 41

    @pytest.mark.asyncio
    async def test_pat_leg_hold_degrades_even_though_no_bucket_moves(self, monitor):
        """The case a percentage-based check is structurally blind to."""
        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._quiet_local(),
            graphql_hold_status=_hold_on('pat'),
        )

        assert result['degraded'] is True

    @pytest.mark.asyncio
    async def test_no_hold_does_not_degrade(self, monitor):
        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._quiet_local(),
        )

        assert result['degraded'] is False
        assert result['github_app_graphql_holds']['app']['active'] is False

    @pytest.mark.asyncio
    async def test_hold_read_failure_is_survivable(self, monitor):
        """A GitHubApp that cannot report holds must leave the rest of the
        rate-limit section intact rather than blanking it."""
        result = await _run_check_github(
            monitor, self._quiet_shared_view(), self._quiet_shared_view(),
            local_client_status=self._quiet_local(),
            graphql_hold_status=RuntimeError("boom"),
        )

        assert result['degraded'] is False
        assert result['github_app_graphql_holds'] is None
        assert result['api_rate_limit_graphql'] is not None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

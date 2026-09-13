"""
The three guards that keep a restart from exhausting the GraphQL quota.

The failure they prevent is not a crash. The orchestrator idles at 60-75% of a
5,000-point hourly budget because board reconciliation and the queue force-sync
cost the BOARD COUNT, not the work in flight. A restart adds several hundred
calls in a burst on top of that, GitHub starts refusing, and the circuit
breaker opens -- which then refuses issue polling, PR updates, discussions and
agent dispatch for the rest of the reset window. One restart takes the whole
deployment down until the hour turns over.

So each guard is pinned at the point where getting it wrong is silent:

  - the budget reading distinguishes "unknown" from "healthy" (a never-populated
    bucket reads 5000/5000 and would pass any threshold by accident);
  - a deferral is NOT counted as a reconciliation failure (main.py exits(1) when
    every project fails, which a single-project deployment reaches immediately);
  - the cadence and freshness defaults are the cheap ones, since reverting
    either to its old value costs quota with nothing failing.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, patch

import services.github_project_manager as gpm
from services.github_api_client import GitHubAPIClient


class TestTheBudgetReadingIsHonestAboutNotKnowing:
    """None means unknown. Treating it as healthy is the accident this
    prevents: a bucket that has never seen a real GitHub response still holds
    its 5000/5000 constructor defaults."""

    @pytest.fixture
    def client(self):
        with patch('services.github_api_client.GitHubAPIClient._start_call_trace_summarizer'):
            return GitHubAPIClient()

    def test_a_never_populated_bucket_reads_unknown_not_full(self, client):
        bucket = client._bucket(client._resolve_credential(), 'graphql')
        assert bucket.remaining == 5000 and bucket.limit == 5000, (
            "precondition: the constructor defaults are what make this "
            "indistinguishable from a healthy budget"
        )
        bucket.ever_updated = False

        assert client.graphql_budget_fraction_remaining() is None

    def test_a_real_reading_is_reported_as_a_fraction(self, client):
        bucket = client._bucket(client._resolve_credential(), 'graphql')
        bucket.ever_updated = True
        bucket.limit, bucket.remaining = 5000, 1000

        assert client.graphql_budget_fraction_remaining() == pytest.approx(0.2)

    def test_an_exhausted_budget_is_zero_not_negative(self, client):
        """GitHub can report a negative remaining under concurrency."""
        bucket = client._bucket(client._resolve_credential(), 'graphql')
        bucket.ever_updated = True
        bucket.limit, bucket.remaining = 5000, -12

        assert client.graphql_budget_fraction_remaining() == 0.0

    def test_a_zero_limit_does_not_divide_by_zero(self, client):
        bucket = client._bucket(client._resolve_credential(), 'graphql')
        bucket.ever_updated = True
        bucket.limit, bucket.remaining = 0, 0

        assert client.graphql_budget_fraction_remaining() is None


class TestTheBudgetFloorIsReadSafely:
    """A typo in a tuning knob must not stop the orchestrator booting, and
    must not silently disable the guard."""

    @pytest.mark.parametrize("raw", ["not-a-number", "", "0", "1", "-0.5", "1.5"])
    def test_an_unusable_value_falls_back_to_the_default(self, raw):
        with patch.dict(os.environ, {gpm.RECONCILE_MIN_BUDGET_FRACTION_ENV: raw}):
            assert (gpm._reconcile_min_budget_fraction()
                    == gpm.DEFAULT_RECONCILE_MIN_BUDGET_FRACTION)

    def test_a_usable_value_is_honoured(self):
        with patch.dict(os.environ, {gpm.RECONCILE_MIN_BUDGET_FRACTION_ENV: "0.5"}):
            assert gpm._reconcile_min_budget_fraction() == 0.5

    def test_unset_means_the_default(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop(gpm.RECONCILE_MIN_BUDGET_FRACTION_ENV, None)
            assert (gpm._reconcile_min_budget_fraction()
                    == gpm.DEFAULT_RECONCILE_MIN_BUDGET_FRACTION)


class TestTheDefaultsAreTheCheapOnes:
    """Reverting either of these costs quota while nothing fails, so nothing
    else would notice. The numbers, not just the plumbing, are the fix."""

    def test_board_state_is_trusted_for_a_day_not_an_hour(self):
        assert gpm.DEFAULT_RECONCILIATION_FRESHNESS_HOURS == 24

    def test_the_force_sync_cadence_is_at_least_half_an_hour(self):
        from services.scheduled_tasks import DEFAULT_QUEUE_RECONCILIATION_MINUTES
        assert DEFAULT_QUEUE_RECONCILIATION_MINUTES >= 30

    @pytest.mark.parametrize("raw", ["not-a-number", "", "0", "-5"])
    def test_an_unusable_cadence_falls_back_rather_than_hot_looping(self, raw):
        from services.scheduled_tasks import (
            _interval_minutes_env, DEFAULT_QUEUE_RECONCILIATION_MINUTES,
        )
        with patch.dict(os.environ, {'QUEUE_RECONCILIATION_INTERVAL_MINUTES': raw}):
            assert (_interval_minutes_env(
                        'QUEUE_RECONCILIATION_INTERVAL_MINUTES',
                        DEFAULT_QUEUE_RECONCILIATION_MINUTES)
                    == DEFAULT_QUEUE_RECONCILIATION_MINUTES)


class TestADeferralIsNotAFailure:
    """The load-bearing distinction. main.py logs a False return as "GitHub
    project management is not working" and counts it toward an exit(1) that a
    single-project deployment reaches on the first project -- so a low budget
    would boot-crash-loop on a condition that clears by itself in under an
    hour."""

    def test_the_deferral_is_not_an_ordinary_failure(self):
        assert not issubclass(gpm.ProjectsBudgetUnavailable, (ValueError, TypeError))
        assert issubclass(gpm.ProjectsBudgetUnavailable, Exception)

    def test_main_defers_rather_than_counting_a_failure(self):
        """main.py must catch it by name and not reach the failure branch."""
        import inspect
        import main

        source = inspect.getsource(main)
        assert 'except ProjectsBudgetUnavailable' in source, (
            "main.py must catch the deferral explicitly; without the handler it "
            "falls through to the generic path and counts as a failure"
        )
        assert 'projects_budget_skips += 1' in source

        handler = source.split('except ProjectsBudgetUnavailable')[1]
        handler = handler.split('except ')[0].split('\n        if not success')[0]
        assert 'failure_count' not in handler, (
            "a deferral that increments failure_count exits(1) a "
            "single-project deployment"
        )


class TestTheGuardActuallyFires:
    """Drives reconcile_project() itself. Everything above this could pass with
    the pre-flight deleted from the call path -- a constant nothing reads and a
    handler for an exception nothing raises."""

    @pytest.fixture
    def manager(self):
        return gpm.GitHubProjectManager(MagicMock(), MagicMock())

    def _client(self, fraction):
        client = MagicMock()
        client.breaker.is_open.return_value = False
        client.graphql_budget_fraction_remaining.return_value = fraction
        return client

    @pytest.mark.asyncio
    async def test_a_low_budget_defers_before_spending_anything(self, manager):
        with patch.object(gpm, 'get_github_client', return_value=self._client(0.10)), \
             patch.object(manager.state_manager, 'needs_reconciliation') as needs:

            with pytest.raises(gpm.ProjectsBudgetUnavailable) as exc:
                await manager.reconcile_project('any-project')

        assert '10%' in str(exc.value)
        needs.assert_not_called(), (
            "the pre-flight must run before any reconciliation work begins"
        )

    @pytest.mark.asyncio
    async def test_an_ample_budget_does_not_defer(self, manager):
        """The guard must not block the restart it exists to protect."""
        with patch.object(gpm, 'get_github_client', return_value=self._client(0.90)), \
             patch.object(manager.state_manager, 'needs_reconciliation',
                          return_value=False), \
             patch.object(manager.state_manager, 'is_state_fresh',
                          return_value=True):

            assert await manager.reconcile_project('any-project') is True

    @pytest.mark.asyncio
    async def test_an_unknown_budget_does_not_defer(self, manager):
        """A cold start has had no real reading yet. Refusing on a number that
        does not exist would break exactly the restart this protects."""
        with patch.object(gpm, 'get_github_client', return_value=self._client(None)), \
             patch.object(manager.state_manager, 'needs_reconciliation',
                          return_value=False), \
             patch.object(manager.state_manager, 'is_state_fresh',
                          return_value=True):

            assert await manager.reconcile_project('any-project') is True

    @pytest.mark.asyncio
    async def test_the_floor_is_the_configured_one(self, manager):
        """A budget above the default floor but below a raised one still defers,
        so the env var is proven to reach the comparison."""
        with patch.dict(os.environ, {gpm.RECONCILE_MIN_BUDGET_FRACTION_ENV: "0.60"}), \
             patch.object(gpm, 'get_github_client', return_value=self._client(0.40)), \
             patch.object(manager.state_manager, 'needs_reconciliation'):

            with pytest.raises(gpm.ProjectsBudgetUnavailable):
                await manager.reconcile_project('any-project')

    @pytest.mark.asyncio
    async def test_a_nonsense_reading_is_unknown_not_a_failure(self, manager):
        """A guard must not be able to fail the thing it guards.

        The raise sits inside a try whose `except Exception` returns False, and
        main.py reads False as "GitHub project management is not working" and
        counts it toward an exit(1) -- so a comparison against an unexpected
        value would turn a quota safeguard into a boot crash-loop. This is how
        it actually broke three existing permission-guard tests: their client
        mock had no stub for the new method, so the reading was a MagicMock and
        `MagicMock() < 0.25` raised TypeError into that handler.
        """
        client = MagicMock()
        client.breaker.is_open.return_value = False
        # Left as the MagicMock a caller that has not stubbed it would produce.

        with patch.object(gpm, 'get_github_client', return_value=client), \
             patch.object(manager.state_manager, 'needs_reconciliation',
                          return_value=False), \
             patch.object(manager.state_manager, 'is_state_fresh',
                          return_value=True):

            assert await manager.reconcile_project('any-project') is True

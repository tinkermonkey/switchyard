"""
Tests for PipelineWatchdog's awareness of in-flight project resource locks
(#140 item 9).

check_for_zombie_runs() decides a run is dead from "marked active, older than
zombie_threshold_minutes, and no agent container running for the issue". A
dispatch parked in project_checkout_lock / dev_container_build_lock's poll loop
satisfies all three and is still perfectly alive: those waits run up to
DEFAULT_TIMEOUT_SECONDS (~3h with heartbeat refresh), and the operation they
guard (a shared-checkout agent run, an image build) runs no container labelled
for the issue either. Reaping such a run redispatches the same issue while the
original coroutine is still waiting — and that coroutine then acquires the lock
and launches its own container, giving one issue two concurrent executions.

project_checkout_lock publishes every live wait/hold to an in-process registry
(the same shape as review_cycle_executor.active_cycles), and the watchdog now
consults it.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from services import project_checkout_lock
from services.dev_container_build_lock import (
    RESOURCE_NAME as BUILD_RESOURCE_NAME,
    dev_container_build_lock_sync,
)
from services.pipeline_watchdog import PipelineWatchdog
from services.project_checkout_lock import (
    RESOURCE_NAME,
    ProjectCheckoutLockTimeoutError,
    _tracked_resource_activity,
    describe_active_resource_lock_activity,
    project_checkout_lock_sync,
)
from services.project_resource_lock_manager import TouchResult


def old_timestamp():
    """Comfortably past the 30-minute zombie_threshold_minutes default."""
    return (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()


class FakeRedis:
    def __init__(self):
        self.counters = {}

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, ttl):
        pass


@pytest.fixture
def watchdog():
    pipeline_run_manager = Mock()
    pipeline_run_manager.redis = FakeRedis()
    pipeline_run_manager.end_pipeline_run = Mock(return_value=True)
    lock_manager = Mock()
    lock_manager.clear_retained_reason = Mock(return_value=True)
    return PipelineWatchdog(
        es_client=Mock(),
        pipeline_run_manager=pipeline_run_manager,
        lock_manager=lock_manager,
        project_monitor=Mock(),
    )


@pytest.fixture(autouse=True)
def clean_registry():
    """The registry is process-global; make sure no test leaks into another."""
    with project_checkout_lock._resource_activity_guard:
        project_checkout_lock._resource_activity.clear()
    yield
    with project_checkout_lock._resource_activity_guard:
        project_checkout_lock._resource_activity.clear()


def _active_run_hit(pipeline_run_id, project, issue_number, started_at):
    return {
        "_source": {
            "id": pipeline_run_id,
            "project": project,
            "issue_number": issue_number,
            "board": "SDLC Execution",
            "started_at": started_at,
        }
    }


class TestResourceActivityRegistry:
    def test_registers_for_the_lifetime_of_the_context_and_no_longer(self):
        assert describe_active_resource_lock_activity("proj", 42) is None

        with _tracked_resource_activity(RESOURCE_NAME, "proj", 42) as activity:
            described = describe_active_resource_lock_activity("proj", 42)
            assert described is not None
            assert RESOURCE_NAME in described
            assert "waiting" in described

            activity.mark_held()
            assert "held" in describe_active_resource_lock_activity("proj", 42)

        assert describe_active_resource_lock_activity("proj", 42) is None

    def test_deregisters_even_when_the_guarded_body_raises(self):
        with pytest.raises(RuntimeError):
            with _tracked_resource_activity(RESOURCE_NAME, "proj", 42):
                raise RuntimeError("guarded operation blew up")

        assert describe_active_resource_lock_activity("proj", 42) is None

    def test_is_scoped_to_project_and_issue(self):
        with _tracked_resource_activity(RESOURCE_NAME, "proj", 42):
            assert describe_active_resource_lock_activity("proj", 43) is None
            assert describe_active_resource_lock_activity("other-proj", 42) is None

    def test_no_issue_number_registers_nothing(self):
        """project_checkout_lock's issue_number is optional and log-only; an
        activity nothing can key on is an activity nothing can look up."""
        with _tracked_resource_activity(RESOURCE_NAME, "proj", None) as activity:
            assert activity is None
            with project_checkout_lock._resource_activity_guard:
                assert project_checkout_lock._resource_activity == {}

    def test_concurrent_activities_for_one_issue_all_have_to_finish(self):
        """The two locks nest (dev_container_build outer, project_checkout
        inner) — the issue stays covered until BOTH frames unwind."""
        with _tracked_resource_activity(BUILD_RESOURCE_NAME, "proj", 42):
            with _tracked_resource_activity(RESOURCE_NAME, "proj", 42):
                assert describe_active_resource_lock_activity("proj", 42) is not None
            # Inner released, outer still held.
            described = describe_active_resource_lock_activity("proj", 42)
            assert described is not None
            assert BUILD_RESOURCE_NAME in described

        assert describe_active_resource_lock_activity("proj", 42) is None


class TestRealLockContextsPublishActivity:
    """
    The registry is only worth anything if the REAL lock context managers
    actually publish to it -- these go through project_checkout_lock_sync()
    /dev_container_build_lock_sync() themselves rather than the private
    helper, against an injected facade.
    """

    @staticmethod
    def _facade(can_acquire):
        facade = Mock()
        facade.acquire_resource.return_value = (
            (True, "acquired") if can_acquire else (False, "held by another operation")
        )
        facade.touch_resource.return_value = TouchResult.REFRESHED
        facade.release_resource.return_value = True
        return facade

    def test_a_wait_is_published_while_polling_and_cleared_on_timeout(self):
        seen = []
        facade = self._facade(can_acquire=False)

        def _record_while_waiting(*args, **kwargs):
            seen.append(describe_active_resource_lock_activity("proj", 42))
            return (False, "held by another operation")

        facade.acquire_resource.side_effect = _record_while_waiting

        with pytest.raises(ProjectCheckoutLockTimeoutError):
            with project_checkout_lock_sync(
                "proj", 42, timeout_seconds=0.05, poll_interval_seconds=0.01, facade=facade
            ):
                pass

        assert any(d and "waiting" in d for d in seen), \
            "a dispatch blocked in the poll loop must be visible to the watchdog"
        # A timeout unwinds the frame, so nothing is left claiming the issue.
        assert describe_active_resource_lock_activity("proj", 42) is None

    def test_a_hold_is_published_for_the_duration_of_the_guarded_body(self):
        facade = self._facade(can_acquire=True)

        with dev_container_build_lock_sync(
            "proj", 42, timeout_seconds=1, poll_interval_seconds=0.01, facade=facade
        ):
            described = describe_active_resource_lock_activity("proj", 42)
            assert described is not None
            assert "held" in described
            assert BUILD_RESOURCE_NAME in described

        assert describe_active_resource_lock_activity("proj", 42) is None


class TestZombieCleanupSkipsLiveResourceLockWork:
    def _search_returns_one_old_active_run(self, watchdog, issue_number=42):
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [_active_run_hit("run-1", "proj", issue_number, old_timestamp())],
            }
        }

    def test_run_waiting_on_a_resource_lock_is_not_reaped(self, watchdog):
        """REGRESSION (#140 item 9): the dispatch is still in the lock's poll
        loop. Reaping it here redispatches the issue while the original
        coroutine goes on to acquire the lock and launch its own container."""
        self._search_returns_one_old_active_run(watchdog)
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        with _tracked_resource_activity(RESOURCE_NAME, "proj", 42):
            with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
                 patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
                 patch("services.human_feedback_loop.human_feedback_loop_executor") as mock_hfl, \
                 patch("services.review_cycle.review_cycle_executor") as mock_rc, \
                 patch("services.cleanup_guard.try_claim_cleanup", return_value=True), \
                 patch.object(watchdog, "_check_for_agent_container", return_value=False), \
                 patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup, \
                 patch.object(watchdog, "_actively_resume_run") as mock_resume:
                mock_tracker.is_frozen_by_circuit_breaker.return_value = False
                # Explicitly clear the two existing containerless exemptions, so
                # this test proves the RESOURCE LOCK is what spared the run.
                mock_hfl._loop_key.return_value = "proj:42"
                mock_hfl.active_loops = {}
                mock_rc._cycle_key.return_value = "proj:42"
                mock_rc.active_cycles = {}

                result = watchdog.check_for_zombie_runs()

        mock_cleanup.assert_not_called()
        mock_resume.assert_not_called()
        assert result["zombies_found"] == 0
        assert result["checked"] == 1

    def test_run_holding_a_resource_lock_is_not_reaped(self, watchdog):
        """The hold is as invisible to the container probe as the wait: the
        dev_container_build lock's guarded operation is an image build, which
        runs no container labelled for the issue."""
        self._search_returns_one_old_active_run(watchdog)
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        with _tracked_resource_activity(BUILD_RESOURCE_NAME, "proj", 42) as activity:
            activity.mark_held()
            with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
                 patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
                 patch("services.human_feedback_loop.human_feedback_loop_executor") as mock_hfl, \
                 patch("services.review_cycle.review_cycle_executor") as mock_rc, \
                 patch("services.cleanup_guard.try_claim_cleanup", return_value=True), \
                 patch.object(watchdog, "_check_for_agent_container", return_value=False), \
                 patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
                mock_tracker.is_frozen_by_circuit_breaker.return_value = False
                # Explicitly clear the two existing containerless exemptions, so
                # this test proves the RESOURCE LOCK is what spared the run.
                mock_hfl._loop_key.return_value = "proj:42"
                mock_hfl.active_loops = {}
                mock_rc._cycle_key.return_value = "proj:42"
                mock_rc.active_cycles = {}

                result = watchdog.check_for_zombie_runs()

        mock_cleanup.assert_not_called()
        assert result["zombies_found"] == 0

    def test_frozen_run_waiting_on_a_resource_lock_is_not_actively_resumed(self, watchdog):
        """is_frozen_by_circuit_breaker() reads the LAST recorded execution
        outcome, so a fresh dispatch that is right now waiting on a lock still
        looks 'frozen' from a previous attempt. _actively_resume_run() would
        double-dispatch it exactly like _cleanup_zombie_run() would, so the
        lock check has to come first."""
        self._search_returns_one_old_active_run(watchdog)
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        with _tracked_resource_activity(RESOURCE_NAME, "proj", 42):
            with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
                 patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
                 patch.object(watchdog, "_check_for_agent_container", return_value=False), \
                 patch.object(watchdog, "_actively_resume_run") as mock_resume, \
                 patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
                mock_tracker.is_frozen_by_circuit_breaker.return_value = True

                result = watchdog.check_for_zombie_runs()

        mock_resume.assert_not_called()
        mock_cleanup.assert_not_called()
        assert result["zombies_found"] == 0

    def test_run_with_no_resource_lock_activity_is_still_reaped(self, watchdog):
        """Control: the exemption must not blind the watchdog to genuine
        zombies. Nothing registered for this issue -> unchanged behavior."""
        self._search_returns_one_old_active_run(watchdog)
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        # A live activity for a DIFFERENT issue must not shield this one.
        with _tracked_resource_activity(RESOURCE_NAME, "proj", 99):
            with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
                 patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
                 patch("services.human_feedback_loop.human_feedback_loop_executor") as mock_hfl, \
                 patch("services.review_cycle.review_cycle_executor") as mock_rc, \
                 patch("services.cleanup_guard.try_claim_cleanup", return_value=True), \
                 patch.object(watchdog, "_check_for_agent_container", return_value=False), \
                 patch.object(watchdog, "_cleanup_zombie_run", return_value=True) as mock_cleanup:
                mock_tracker.is_frozen_by_circuit_breaker.return_value = False
                mock_hfl._loop_key.return_value = "proj:42"
                mock_hfl.active_loops = {}
                mock_rc._cycle_key.return_value = "proj:42"
                mock_rc.active_cycles = {}

                result = watchdog.check_for_zombie_runs()

        mock_cleanup.assert_called_once()
        assert result["zombies_found"] == 1

    def test_an_unverifiable_registry_fails_safe(self, watchdog):
        """Same fail-safe posture as the review-cycle and feedback-loop checks:
        a run we cannot verify is never killed."""
        self._search_returns_one_old_active_run(watchdog)
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
             patch("services.human_feedback_loop.human_feedback_loop_executor") as mock_hfl, \
             patch("services.review_cycle.review_cycle_executor") as mock_rc, \
             patch("services.cleanup_guard.try_claim_cleanup", return_value=True), \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch(
                 "services.project_checkout_lock.describe_active_resource_lock_activity",
                 side_effect=Exception("registry unavailable"),
             ), \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            mock_tracker.is_frozen_by_circuit_breaker.return_value = False
            mock_hfl._loop_key.return_value = "proj:42"
            mock_hfl.active_loops = {}
            mock_rc._cycle_key.return_value = "proj:42"
            mock_rc.active_cycles = {}

            result = watchdog.check_for_zombie_runs()

        mock_cleanup.assert_not_called()
        assert result["zombies_found"] == 0

    def test_a_run_with_a_container_is_short_circuited_before_the_registry(self, watchdog):
        """Unchanged ordering: a live container is still the first and cheapest
        proof of life, checked before anything else."""
        self._search_returns_one_old_active_run(watchdog)
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
             patch.object(watchdog, "_check_for_agent_container", return_value=True), \
             patch(
                 "services.project_checkout_lock.describe_active_resource_lock_activity"
             ) as mock_describe, \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            mock_tracker.is_frozen_by_circuit_breaker.return_value = False

            result = watchdog.check_for_zombie_runs()

        mock_describe.assert_not_called()
        mock_cleanup.assert_not_called()
        assert result["zombies_found"] == 0

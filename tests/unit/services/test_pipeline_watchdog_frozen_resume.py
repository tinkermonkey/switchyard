"""
Tests for PipelineWatchdog's Claude Code breaker freeze/active-resume behavior.

Covers Austin's "freeze, don't reap" directive: while the global Claude Code
usage-limit breaker is open, check_for_zombie_runs() must skip its entire pass
untouched (no ES query, no reaping) rather than restarting stalled runs from
scratch. Once the breaker closes, a run whose last execution outcome was
specifically 'frozen' must be actively resumed promptly — bypassing the normal
zombie_threshold_minutes age gate — and this must NOT be counted against
ZOMBIE_AUTO_RETRY_LIMIT (a known-reason pause is not evidence of broken work).
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

import pytest

from services.pipeline_watchdog import PipelineWatchdog
from services.work_execution_state import WorkExecutionStateTracker


def _iso(dt):
    return dt.isoformat()


def young_timestamp():
    """Well under the 30-minute zombie_threshold_minutes default."""
    return _iso(datetime.now(timezone.utc) - timedelta(minutes=1))


def old_timestamp():
    """Comfortably past the 30-minute zombie_threshold_minutes default."""
    return _iso(datetime.now(timezone.utc) - timedelta(hours=2))


class FakeRedis:
    def __init__(self):
        self.counters = {}

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, ttl):
        pass

class _IssueUnderTest:
    """A locked_by_issue that matches whichever issue a test passes.

    _cleanup_zombie_run/_actively_resume_run now confirm the issue actually
    holds the board lock before clearing its retained mark and redispatching:
    a non-holder has nothing to self-heal (see _self_heal_still_holds_lock).
    Every test here exercises the holder case and the issue numbers vary per
    test, so report the lock as held by whichever one is asked about rather
    than pinning a number in the fixture -- and report it explicitly, so these
    tests reach the self-heal path deliberately instead of by way of that
    method's unreadable-lock fail-safe.
    """

    def __eq__(self, other):
        return True

    def __hash__(self):
        return 0

    def __repr__(self):
        return "<issue under test>"


def _lock_held_by_issue_under_test():
    lock = Mock()
    lock.locked_by_issue = _IssueUnderTest()
    return lock


@pytest.fixture
def watchdog():
    pipeline_run_manager = Mock()
    pipeline_run_manager.redis = FakeRedis()
    pipeline_run_manager.end_pipeline_run = Mock(return_value=True)
    lock_manager = Mock()
    lock_manager.clear_retained_reason = Mock(return_value=True)
    lock_manager.get_lock_fail_closed = Mock(
        return_value=(_lock_held_by_issue_under_test(), True)
    )
    wd = PipelineWatchdog(
        es_client=Mock(),
        pipeline_run_manager=pipeline_run_manager,
        lock_manager=lock_manager,
        project_monitor=Mock(),
    )
    # Patched, not left to the real thing: services/cleanup_guard.py builds its
    # OWN redis.Redis(host='redis') and check_for_zombie_runs() writes
    # `SET orchestrator:cleanup_guard:proj:<n> zombie_watchdog NX EX 300`
    # through it. In the orchestrator container that Redis is the running
    # deployment's, so a second run of this file within five minutes found its
    # own leftover claim, `continue`d past cleanup, and failed tests that had
    # passed minutes earlier -- the suite's result depending on whether it had
    # been run before, which is the class of defect #174 exists to remove.
    # Nothing these tests assert depends on an external store.
    with patch("services.cleanup_guard.try_claim_cleanup", return_value=True):
        yield wd


class TestActiveResumeNonHolderShortCircuitLeavesTheIssueRePickupable:
    """Twin of TestZombieNonHolderShortCircuitLeavesTheIssueRePickupable in
    test_pipeline_watchdog_retry.py, for the frozen-run resume branch: the
    frozen run is ended and the issue does not hold the lock, so nothing is
    redispatched -- which means end_pipeline_run's cancellation signal and the
    frozen run's 'in_progress' execution record are both still in place, and
    both are consulted by exactly the recovery paths this branch says the
    issue is being left to.
    """

    def _resume_as_non_holder(self, watchdog, signal=None, tracker=None):
        watchdog.lock_manager.get_lock_fail_closed = Mock(
            return_value=(Mock(locked_by_issue=170), True)
        )
        signal = signal or Mock()
        tracker = tracker or Mock()
        with patch("services.cancellation.get_cancellation_signal", return_value=signal), \
             patch("services.work_execution_state.work_execution_tracker", tracker), \
             patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue") as redispatch:
            resumed = watchdog._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at=old_timestamp(),
            )
        return resumed, signal, tracker, notify, redispatch

    def test_clears_the_cancellation_signal_and_abandons_stale_entries(self, watchdog):
        resumed, signal, tracker, notify, redispatch = self._resume_as_non_holder(watchdog)

        assert resumed is True
        redispatch.assert_not_called()
        notify.assert_not_called()
        signal.clear.assert_called_once_with("proj", 42)
        kwargs = tracker.abandon_stale_in_progress_entries.call_args.kwargs
        assert kwargs["project_name"] == "proj"
        assert kwargs["issue_number"] == 42
        assert kwargs["active_task_ids"] == set()
        assert "not an orchestrator restart" in kwargs["reason"]

    def test_stays_a_clean_outcome_when_the_cleanups_themselves_fail(self, watchdog):
        signal = Mock()
        signal.clear.side_effect = RuntimeError("redis down")
        tracker = Mock()
        tracker.abandon_stale_in_progress_entries.side_effect = OSError("state file unreadable")

        resumed, _signal, _tracker, notify, _redispatch = self._resume_as_non_holder(
            watchdog, signal=signal, tracker=tracker
        )

        assert resumed is True
        notify.assert_not_called()


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


class TestGlobalFreezeGate:
    def test_skips_entire_pass_when_breaker_open(self, watchdog):
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = True

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker):
            result = watchdog.check_for_zombie_runs()

        assert result["frozen_skip"] is True
        assert result["checked"] == 0
        watchdog.es.search.assert_not_called()

    def test_proceeds_normally_when_breaker_closed(self, watchdog):
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False
        watchdog.es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker):
            result = watchdog.check_for_zombie_runs()

        assert "frozen_skip" not in result
        watchdog.es.search.assert_called_once()

    def test_breaker_check_failure_fails_open_to_normal_scan(self, watchdog):
        """If checking breaker state itself errors, don't silently skip forever —
        proceed with the normal scan rather than freezing indefinitely."""
        watchdog.es.search.return_value = {"hits": {"total": {"value": 0}, "hits": []}}

        with patch("monitoring.claude_code_breaker.get_breaker", side_effect=Exception("boom")):
            result = watchdog.check_for_zombie_runs()

        assert "frozen_skip" not in result
        watchdog.es.search.assert_called_once()


class TestActivelyResumeRun:
    """
    _redispatch_same_issue is patched to a plain success/failure stub here --
    its own GraphQL/config-lookup internals are covered separately in
    TestRedispatchSameIssue (test_pipeline_watchdog_retry.py). What matters
    here is _actively_resume_run's own decision logic.
    """

    def test_retains_lock_clears_mark_and_redispatches_same_issue(self, watchdog):
        """The lock must never be released outright here (see incident
        e42ca133) — end_pipeline_run always retains it, and the resume then
        clears the durable mark and redispatches the SAME issue directly
        while the lock stays held by that issue throughout."""
        with patch.object(watchdog, "_increment_zombie_retry_count") as incr, \
             patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=True) as redispatch, \
             patch("services.review_cycle.review_cycle_executor") as mock_rc:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.return_value = []

            watchdog._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T15:00:00Z",
            )

        watchdog.pipeline_run_manager.end_pipeline_run.assert_called_once()
        call = watchdog.pipeline_run_manager.end_pipeline_run.call_args
        assert call.kwargs["retain_lock"] is True
        watchdog.lock_manager.clear_retained_reason.assert_called_once_with(
            "proj", "SDLC Execution", 42
        )
        redispatch.assert_called_once_with("proj", "SDLC Execution", 42)
        incr.assert_not_called()
        notify.assert_not_called()

    def test_retains_lock_and_alerts_when_clear_fails(self, watchdog):
        """If clear_retained_reason() itself fails, the lock is already
        durably retained and a human is notified; redispatch must not even
        be attempted."""
        watchdog.lock_manager.clear_retained_reason = Mock(return_value=False)
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue") as redispatch, \
             patch("services.review_cycle.review_cycle_executor") as mock_rc:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.return_value = []

            watchdog._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T15:00:00Z",
            )

        redispatch.assert_not_called()
        notify.assert_called_once()

    def test_restores_durable_retention_when_redispatch_fails_after_clear(self, watchdog):
        """If clear succeeds but the redispatch itself fails, the lock must
        not be left un-retained with nothing dispatched — the durable mark
        must be restored before notifying."""
        with patch.object(watchdog, "_notify_lock_stuck") as notify, \
             patch.object(watchdog, "_redispatch_same_issue", return_value=False), \
             patch("services.review_cycle.review_cycle_executor") as mock_rc:
            mock_rc.active_cycles = {}
            mock_rc._load_active_cycles.return_value = []

            watchdog._actively_resume_run(
                pipeline_run_id="run-1",
                project="proj",
                board="SDLC Execution",
                issue_number=42,
                started_at="2026-08-10T15:00:00Z",
            )

        watchdog.lock_manager.clear_retained_reason.assert_called_once()
        watchdog.lock_manager.mark_lock_failed.assert_called_once()
        assert watchdog.lock_manager.mark_lock_failed.call_args.args[:3] == (
            "proj", "SDLC Execution", 42
        )
        notify.assert_called_once()


class TestFrozenRunBypassesAgeGate:
    def test_frozen_young_run_is_actively_resumed_not_reaped(self, watchdog):
        """A run frozen only seconds ago (well under the 30-minute zombie
        threshold) must still be resumed promptly, not left to wait out the
        age gate."""
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False
        ts = young_timestamp()
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [_active_run_hit("run-1", "proj", 42, ts)],
            }
        }

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch.object(watchdog, "_actively_resume_run") as mock_resume, \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            mock_tracker.is_frozen_by_circuit_breaker.return_value = True

            result = watchdog.check_for_zombie_runs()

        mock_resume.assert_called_once()
        mock_resume.assert_called_with(
            pipeline_run_id="run-1", project="proj", board="SDLC Execution",
            issue_number=42, started_at=ts,
        )
        mock_cleanup.assert_not_called()
        assert result["zombies_cleaned"] == 1

    def test_non_frozen_young_run_still_skipped_by_age_gate(self, watchdog):
        """Unchanged behavior: a run that's NOT marked frozen and is still
        young must be skipped exactly as before — neither resumed nor reaped."""
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [_active_run_hit("run-2", "proj", 43, young_timestamp())],
            }
        }

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch.object(watchdog, "_actively_resume_run") as mock_resume, \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            mock_tracker.is_frozen_by_circuit_breaker.return_value = False

            result = watchdog.check_for_zombie_runs()

        mock_resume.assert_not_called()
        mock_cleanup.assert_not_called()
        assert result["zombies_found"] == 0

    def test_frozen_old_run_with_no_container_takes_resume_path_over_cleanup(self, watchdog):
        """A frozen run that's also old enough to be a normal zombie candidate
        must still go through active-resume, not the ordinary reap path —
        uniform clean-restart takes priority."""
        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [_active_run_hit("run-3", "proj", 44, old_timestamp())],
            }
        }

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker") as mock_tracker, \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch.object(watchdog, "_actively_resume_run") as mock_resume, \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            mock_tracker.is_frozen_by_circuit_breaker.return_value = True

            watchdog.check_for_zombie_runs()

        mock_resume.assert_called_once()
        mock_cleanup.assert_not_called()


class TestFrozenDetectionUsesRealWorkExecutionTracker:
    """
    Regression coverage for the bug where check_for_zombie_runs() relied on
    is_frozen_by_circuit_breaker() mocked directly in other tests above, which
    hid a real bug: the old implementation substring-matched 'circuit breaker'
    in the error text, and the NEW structural detector's actual message format
    never contained that phrase. These tests exercise the REAL
    WorkExecutionStateTracker (not mocked) with the REAL error message shape
    docker_runner.py's ClaudeCodeRateLimitError produces, end to end through
    check_for_zombie_runs()'s routing decision — proving the fix works against
    realistic input rather than an assertion mocked around it.
    """

    def _real_tracker(self, tmp_path):
        return WorkExecutionStateTracker(state_dir=tmp_path)

    def test_run_frozen_via_new_structural_detector_message_is_actively_resumed(self, watchdog, tmp_path):
        """The exact scenario Bug 1 missed: a run frozen by the NEW structural
        detection path (whose error text never says "circuit breaker") must
        still be recognized as frozen and actively resumed, not reaped."""
        tracker = self._real_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=42, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        # Exact message shape from claude/docker_runner.py's ClaudeCodeRateLimitError
        # raise in the structural-detection path — deliberately contains no
        # "circuit breaker" wording.
        tracker.record_execution_outcome(
            issue_number=42, column="Development", agent="senior_software_engineer",
            outcome="frozen", project_name="proj",
            error=(
                "Claude Code rate limit confirmed (source=rate_limit_event, "
                "rate_limit_type=five_hour). Resets at 04:20 PM."
            )
        )

        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [_active_run_hit("run-1", "proj", 42, young_timestamp())],
            }
        }

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker", tracker), \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch.object(watchdog, "_actively_resume_run") as mock_resume, \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            watchdog.check_for_zombie_runs()

        mock_resume.assert_called_once()
        mock_cleanup.assert_not_called()

    def test_run_failed_normally_is_still_reaped_not_resumed(self, watchdog, tmp_path):
        """Control case: a genuinely failed (not frozen) old run must still go
        through the ordinary zombie-reap path, proving the real tracker
        correctly distinguishes the two rather than always returning True."""
        tracker = self._real_tracker(tmp_path)
        tracker.record_execution_start(
            issue_number=43, column="Development", agent="senior_software_engineer",
            trigger_source="manual_move", project_name="proj"
        )
        tracker.record_execution_outcome(
            issue_number=43, column="Development", agent="senior_software_engineer",
            outcome="failure", project_name="proj", error="agent crashed unrelated to rate limits"
        )

        mock_breaker = Mock()
        mock_breaker.is_open.return_value = False
        watchdog.es.search.return_value = {
            "hits": {
                "total": {"value": 1},
                "hits": [_active_run_hit("run-2", "proj", 43, old_timestamp())],
            }
        }

        with patch("monitoring.claude_code_breaker.get_breaker", return_value=mock_breaker), \
             patch("services.work_execution_state.work_execution_tracker", tracker), \
             patch.object(watchdog, "_check_for_agent_container", return_value=False), \
             patch.object(watchdog, "_actively_resume_run") as mock_resume, \
             patch.object(watchdog, "_cleanup_zombie_run") as mock_cleanup:
            watchdog.check_for_zombie_runs()

        mock_resume.assert_not_called()
        mock_cleanup.assert_called_once()

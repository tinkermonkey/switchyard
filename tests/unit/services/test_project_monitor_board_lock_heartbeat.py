"""
Board-lock heartbeat (#153 WI-8, from #140 item 19).

A board lock's Redis TTL was never refreshed while it was held. #140 item 19
describes the mechanism as "the dispatch path re-acquires via
try_acquire_lock() on every poll, and its already_holds_lock branch refreshes
only the TTL, never lock_acquired_at" -- but that re-acquire does not actually
happen: every try_acquire_lock() call site is a dispatch attempt for a
DIFFERENT issue than the current holder, and the two sites an issue can re-enter
through while holding the lock skip the call outright
(trigger_agent_for_status's `already_has_lock` branch, and the PR-review path's
`_lock_held_by_us` check). So nothing refreshed a held board lock at all, and
the real exposure is LOCK_TTL_SECONDS (7200s), not the 4-hour staleness
threshold: once the key lapses, try_acquire_lock()'s Redis transaction reads it
back as an empty dict, which is falsy, and grants the board to the next waiting
issue while the original agent (up to 10800s per config/foundations/agents.yaml)
is still running.

ProjectMonitor._refresh_held_board_locks() closes that, from the monitoring
thread that already visits every board every cycle, with no per-hold heartbeat
thread -- see its docstring for why the project_checkout_lock shape does not
fit the board lock.
"""
import inspect
import sys
import os
import unittest
from datetime import datetime, timezone, timedelta
from unittest.mock import Mock, MagicMock, patch

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.pipeline_lock_manager import PipelineLock, TouchResult
from services.project_checkout_lock import HEARTBEAT_INTERVAL_SECONDS
from services.project_monitor import ProjectMonitor


def _lock(issue_number=123, age_seconds=HEARTBEAT_INTERVAL_SECONDS + 60, retained=None):
    acquired = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    return PipelineLock(
        project="proj",
        board="board",
        locked_by_issue=issue_number,
        lock_acquired_at=acquired.isoformat(),
        lock_status='locked',
        retained_reason=retained,
    )


class BoardLockHeartbeatTestBase(unittest.TestCase):
    def setUp(self):
        pipeline = Mock()
        pipeline.active = True
        pipeline.board_name = "board"
        project_config = Mock()
        project_config.pipelines = [pipeline]

        self.config_manager = Mock()
        self.config_manager.list_projects.return_value = []
        self.config_manager.list_visible_projects.return_value = ["proj"]
        self.config_manager.get_project_config.return_value = project_config

        self.monitor = ProjectMonitor(Mock(), self.config_manager)
        self.monitor.pipeline_run_manager = Mock()
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = Mock(id="run-1")

        self.lock_manager = MagicMock()
        self.lock_manager.touch_lock.return_value = TouchResult.REFRESHED

    def sweep(self):
        with patch(
            'services.pipeline_lock_manager.get_pipeline_lock_manager',
            return_value=self.lock_manager,
        ):
            self.monitor._refresh_held_board_locks()


class TestRefreshesLiveBoardLocks(BoardLockHeartbeatTestBase):

    def test_refreshes_an_aged_board_lock_whose_run_is_still_active(self):
        """The core regression: nothing else in the system resets a held board
        lock's TTL or lock_acquired_at."""
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board", 123)

    def test_does_not_refresh_a_lock_younger_than_the_heartbeat_interval(self):
        """Aged against the lock's OWN lock_acquired_at (which touch_lock
        resets), so no per-board timer has to survive a restart."""
        self.lock_manager.get_lock_fail_closed.return_value = (
            _lock(age_seconds=HEARTBEAT_INTERVAL_SECONDS - 60), True
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_does_not_refresh_when_the_holder_has_no_active_pipeline_run(self):
        """Same predicate _reconcile_active_runs()'s stale-lock watchdog uses to
        decide a lock is abandoned -- refreshing unconditionally would pin an
        abandoned lock forever and disable both recovery paths."""
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)
        self.monitor.pipeline_run_manager.get_active_pipeline_run.return_value = None

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_does_not_refresh_a_retained_lock(self):
        """release_lock()'s "not_found is the normal steady state for a lock
        retained more than two hours" reasoning depends on nothing re-touching
        a retained lock's Redis copy."""
        self.lock_manager.get_lock_fail_closed.return_value = (
            _lock(retained="agent crashed"), True
        )

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_does_not_refresh_an_unlocked_board(self):
        self.lock_manager.get_lock_fail_closed.return_value = (None, True)

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_skips_a_board_whose_lock_state_could_not_be_read(self):
        """Fail closed, matching every other read site in this mechanism: an
        unknown state is not a licence to write."""
        self.lock_manager.get_lock_fail_closed.return_value = (None, False)

        self.sweep()

        self.lock_manager.touch_lock.assert_not_called()

    def test_skips_an_inactive_pipeline(self):
        self.config_manager.get_project_config.return_value.pipelines[0].active = False
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()

        self.lock_manager.get_lock_fail_closed.assert_not_called()


class TestSweepIsResilientAndRateLimited(BoardLockHeartbeatTestBase):

    def test_one_boards_failure_does_not_stop_the_others(self):
        """This runs at the top of the monitoring cycle -- a raise here would
        take the whole cycle into its generic 10s-backoff handler."""
        second = Mock()
        second.active = True
        second.board_name = "board2"
        self.config_manager.get_project_config.return_value.pipelines.append(second)
        self.lock_manager.get_lock_fail_closed.side_effect = [
            Exception("redis exploded"),
            (_lock(), True),
        ]

        self.sweep()

        self.lock_manager.touch_lock.assert_called_once_with("proj", "board2", 123)

    def test_a_project_whose_config_cannot_be_loaded_is_skipped(self):
        self.config_manager.get_project_config.side_effect = Exception("no config")

        self.sweep()

        self.lock_manager.get_lock_fail_closed.assert_not_called()

    def test_the_sweep_is_rate_limited_between_cycles(self):
        """The call site sits above monitor_projects()' circuit-breaker
        `continue`s, where the loop spins on a 5s sleep -- a lock read per board
        every 5s would be pointless I/O."""
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()
        self.sweep()

        self.assertEqual(self.lock_manager.get_lock_fail_closed.call_count, 1)

    def test_the_sweep_runs_again_once_its_interval_has_elapsed(self):
        self.lock_manager.get_lock_fail_closed.return_value = (_lock(), True)

        self.sweep()
        self.monitor._last_board_lock_heartbeat_at -= (
            self.monitor._board_lock_sweep_interval_seconds + 1
        )
        self.sweep()

        self.assertEqual(self.lock_manager.get_lock_fail_closed.call_count, 2)


class TestSweepRunsBeforeTheCircuitBreakerChecks(unittest.TestCase):
    """
    Placement matters and is not observable from _refresh_held_board_locks()
    itself. monitor_projects() short-circuits its whole cycle with `continue`
    while either the GitHub or the Claude Code circuit breaker is open -- but an
    agent that was already running when a breaker opened keeps running and keeps
    holding its board lock, and the release-and-dispatch-next paths in
    pipeline_progression and review_cycle keep running on worker threads
    regardless. The heartbeat touches only local Redis/YAML, so it must sit
    above both.
    """

    def test_the_heartbeat_call_precedes_both_breaker_checks(self):
        source = inspect.getsource(ProjectMonitor.monitor_projects)
        loop_body = source.split("while True:", 1)[1]

        heartbeat_at = loop_body.index("self._refresh_held_board_locks()")
        github_breaker_at = loop_body.index("github_client.breaker.is_open()")
        claude_breaker_at = loop_body.index("breaker and breaker.is_open()")

        self.assertLess(heartbeat_at, github_breaker_at)
        self.assertLess(heartbeat_at, claude_breaker_at)


if __name__ == '__main__':
    unittest.main()

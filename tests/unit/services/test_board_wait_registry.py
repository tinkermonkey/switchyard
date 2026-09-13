"""Coverage for the board-lock wait registry and the release-driven wake (#214).

Two gaps this guards, both left open by #58's wait-and-retry rework:

1. A repair cycle waiting for a busy board had no release-driven wake. Both
   release-time backfill sites ask PipelineQueueManager, which is scoped to the
   pipeline TRIGGER column; a repair cycle waits in "Testing" and is therefore
   invisible to it, so the freed board went straight to the next Development
   issue and the repair cycle found it busy again on its next poll tick.
2. The wait emitted nothing -- no log, no event, no escalation.

What these tests deliberately do NOT assert: anything about wait-vs-steal.
Nothing here evicts a holder; the wake only ever runs on a board that is
already free, and every test that offers the board first checks that.
"""

import os
import tempfile

# services.work_execution_state module-level-constructs its singleton at import
# time and mkdirs ORCHESTRATOR_ROOT (default '/app'). services.project_monitor
# pulls it in transitively, so this must run before that import, at collection
# time. Same guard, same reason, as tests/unit/orchestrator/
# test_repair_cycle_lock_steal.py's.
if 'ORCHESTRATOR_ROOT' not in os.environ:
    os.environ['ORCHESTRATOR_ROOT'] = tempfile.mkdtemp(prefix='switchyard-test-')

import logging
from unittest.mock import Mock, patch

import pytest

from config.manager import ConfigManager
from services.board_wait_registry import (
    WAIT_ENTRY_STALE_SECONDS,
    BoardWaitRegistry,
    get_board_wait_registry,
)


@pytest.fixture(autouse=True)
def clean_global_registry():
    """The registry is a process-global singleton; leaking entries between tests
    would make the "no waiter registered" no-op assertions pass or fail based on
    test order."""
    get_board_wait_registry().clear_all()
    yield
    get_board_wait_registry().clear_all()


class TestBoardWaitRegistry:
    """The registry itself, with the clock injected rather than slept through."""

    def test_first_record_starts_the_wait_and_reports_it_as_new(self):
        reg = BoardWaitRegistry()
        waited, is_new = reg.record_wait('proj', 'dev', 100)
        assert is_new is True
        assert waited == 0.0

    def test_later_records_refresh_without_restarting_the_clock(self):
        """The per-tick refresh must not reset waiting_since. If it did,
        waited_seconds would measure the gap since the last poll tick (15-60s)
        instead of the whole wait, and the escalation below could never fire."""
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        with patch('services.board_wait_registry.time.monotonic', return_value=1042.0):
            waited, is_new = reg.record_wait('proj', 'dev', 100)
        assert is_new is False
        assert waited == pytest.approx(42.0)

    def test_clear_returns_the_total_waited_duration(self):
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        with patch('services.board_wait_registry.time.monotonic', return_value=1007.5):
            assert reg.clear_wait('proj', 'dev', 100) == pytest.approx(7.5)

    def test_clear_with_no_wait_returns_none(self):
        """The production common case: the board was free, the first acquire
        succeeded, and there is no wait to report. None is what keeps the
        acquire path silent instead of logging a wait of zero."""
        reg = BoardWaitRegistry()
        assert reg.clear_wait('proj', 'dev', 100) is None

    def test_clear_is_not_reusable(self):
        reg = BoardWaitRegistry()
        reg.record_wait('proj', 'dev', 100)
        assert reg.clear_wait('proj', 'dev', 100) is not None
        assert reg.clear_wait('proj', 'dev', 100) is None

    def test_waiters_are_returned_longest_waiting_first(self):
        """The anti-starvation ordering: whoever has waited longest is handed
        the next release, so a newer waiter cannot indefinitely overtake it."""
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 200)      # newer
        with patch('services.board_wait_registry.time.monotonic', return_value=900.0):
            reg.record_wait('proj', 'dev', 100)      # older
        with patch('services.board_wait_registry.time.monotonic', return_value=1001.0):
            waiters = reg.get_waiters_for_board('proj', 'dev')
        assert [w.issue_number for w in waiters] == [100, 200]

    def test_waiters_are_scoped_to_one_project_and_board(self):
        reg = BoardWaitRegistry()
        reg.record_wait('proj', 'dev', 100)
        reg.record_wait('proj', 'other-board', 101)
        reg.record_wait('other-proj', 'dev', 102)
        assert [w.issue_number for w in reg.get_waiters_for_board('proj', 'dev')] == [100]

    def test_stale_entries_are_pruned_on_read_and_never_woken(self):
        """A dispatch that stopped waiting for some other reason stops being
        refreshed. Without this, the release path would keep trying to wake it
        forever -- and each attempt costs a real GitHub column lookup."""
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        later = 1000.0 + WAIT_ENTRY_STALE_SECONDS + 1
        with patch('services.board_wait_registry.time.monotonic', return_value=later):
            assert reg.get_waiters_for_board('proj', 'dev') == []
            # Actually removed, not merely filtered out of the response.
            assert reg._entries == {}

    def test_a_record_after_the_stale_window_starts_a_fresh_wait(self):
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        later = 1000.0 + WAIT_ENTRY_STALE_SECONDS + 1
        with patch('services.board_wait_registry.time.monotonic', return_value=later):
            waited, is_new = reg.record_wait('proj', 'dev', 100)
        assert is_new is True
        assert waited == 0.0


class TestRefreshIsWhatKeepsAWaitAlive:
    """The review finding this class exists for: nothing on the dispatch path
    re-records a wait, so an entry is written once and, without an explicit
    refresher, ages out of WAIT_ENTRY_STALE_SECONDS mid-wait -- silently
    disabling the wake, the duration report and the escalation for exactly the
    waits #214 was written for (hours behind senior_software_engineer)."""

    def test_a_wait_longer_than_the_stale_window_survives_if_it_is_refreshed(self):
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)

        t = 1000.0
        # Ten stale windows' worth of wait — far longer than the 300s an
        # unrefreshed entry survives, and the shape of a real wait behind a
        # multi-hour holder.
        while t < 1000.0 + WAIT_ENTRY_STALE_SECONDS * 10:
            t += WAIT_ENTRY_STALE_SECONDS / 2
            with patch('services.board_wait_registry.time.monotonic', return_value=t):
                reg.refresh_waiters('proj', 'dev')

        with patch('services.board_wait_registry.time.monotonic', return_value=t):
            assert [w.issue_number for w in reg.get_waiters_for_board('proj', 'dev')] == [100]
            # And the duration reported is the WHOLE wait, not the gap since
            # the last refresh.
            assert reg.clear_wait('proj', 'dev', 100) == pytest.approx(t - 1000.0)

    def test_an_unrefreshed_wait_is_still_dropped(self):
        """The refresh must not turn staleness off: a dispatch that stopped
        waiting stops being refreshed and must stop being woken."""
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        later = 1000.0 + WAIT_ENTRY_STALE_SECONDS + 1
        with patch('services.board_wait_registry.time.monotonic', return_value=later):
            assert reg.refresh_waiters('proj', 'dev') == []
            assert reg._entries == {}

    def test_refresh_is_scoped_to_one_board(self):
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
            reg.record_wait('proj', 'other', 101)
        mid = 1000.0 + WAIT_ENTRY_STALE_SECONDS * 0.75
        with patch('services.board_wait_registry.time.monotonic', return_value=mid):
            reg.refresh_waiters('proj', 'dev')
        later = mid + WAIT_ENTRY_STALE_SECONDS * 0.75
        with patch('services.board_wait_registry.time.monotonic', return_value=later):
            assert [w.issue_number for w in reg.get_waiters_for_board('proj', 'dev')] == [100]
            assert reg.get_waiters_for_board('proj', 'other') == []

    def test_a_long_wait_is_reported_for_escalation_exactly_once(self):
        """#214: the wait had no escalation of any kind, so a board stalled
        behind a stuck holder was indistinguishable from an idle one. The
        threshold is the caller's, so the registry only reports the crossing."""
        reg = BoardWaitRegistry()
        threshold = 3600.0
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)

        t = 1000.0
        reported = []
        while t < 1000.0 + threshold + 300.0:
            t += WAIT_ENTRY_STALE_SECONDS / 2
            with patch('services.board_wait_registry.time.monotonic', return_value=t):
                reported.extend(
                    reg.refresh_waiters('proj', 'dev', escalate_after_seconds=threshold)
                )

        assert [e.issue_number for e in reported] == [100], \
            "escalation must be reported exactly once, not on every refresh"

    def test_a_short_wait_is_never_reported_for_escalation(self):
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        with patch('services.board_wait_registry.time.monotonic', return_value=1100.0):
            assert reg.refresh_waiters('proj', 'dev', escalate_after_seconds=3600.0) == []

    def test_no_threshold_means_no_escalation_reporting(self):
        reg = BoardWaitRegistry()
        with patch('services.board_wait_registry.time.monotonic', return_value=1000.0):
            reg.record_wait('proj', 'dev', 100)
        with patch('services.board_wait_registry.time.monotonic', return_value=99000.0):
            assert reg.refresh_waiters('proj', 'dev') == []


def _monitor(trigger_return='senior_software_engineer', column='Testing',
             column_reads_ok=True, exit_columns=()):
    """A ProjectMonitor with only what dispatch_waiting_board_lock_waiter touches.

    ConfigManager is patched where project_monitor looks it up, so the wake's
    repo lookup resolves without any real config on disk.

    The wake resolves the column through get_issue_column_sync_CHECKED, which
    returns (column, reads_ok) — a board that could not be read must not be
    reported as a card that is gone.

    `exit_columns` populates the board's workflow template, which is how the
    wake decides a waiter has left the pipeline. The default is empty, i.e. no
    column is an exit column, which is what every test that is not about that
    branch wants.
    """
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    project_config = Mock()
    project_config.github = {'repo': 'test-repo'}
    project_config.pipelines = [Mock(board_name='dev', workflow='dev_workflow')]
    workflow_template = Mock()
    workflow_template.pipeline_exit_columns = list(exit_columns)
    config_manager.get_workflow_template.return_value = workflow_template
    config_manager.get_project_config.return_value = project_config

    from services.project_monitor import ProjectMonitor
    monitor = ProjectMonitor(Mock(), config_manager)
    monitor.trigger_agent_for_status = Mock(return_value=trigger_return)
    monitor.get_issue_column_sync_checked = Mock(
        return_value=(column, column_reads_ok)
    )
    return monitor, config_manager


class TestReleaseDrivenWake:
    """ProjectMonitor.dispatch_waiting_board_lock_waiter -- #214 finding 1."""

    def _wake(self, monitor, config_manager, lock=None):
        lock_manager = Mock()
        lock_manager.get_lock.return_value = lock
        with patch('services.project_monitor.ConfigManager', return_value=config_manager), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.cancellation.get_cancellation_signal') as sig:
            sig.return_value.is_cancelled.return_value = False
            return monitor.dispatch_waiting_board_lock_waiter('proj', 'dev'), lock_manager

    def test_no_waiter_is_a_complete_no_op(self):
        """The production case at capacity 1. Nothing registered means the wake
        must not read the lock, must not touch GitHub and must not dispatch --
        the caller then runs its Development backfill exactly as before."""
        monitor, cm = _monitor()
        result, lock_manager = self._wake(monitor, cm)
        assert result is None
        monitor.trigger_agent_for_status.assert_not_called()
        monitor.get_issue_column_sync_checked.assert_not_called()
        lock_manager.get_lock.assert_not_called()

    def test_a_registered_waiter_is_dispatched_in_its_current_column(self):
        monitor, cm = _monitor()
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result == 100
        monitor.trigger_agent_for_status.assert_called_once_with(
            'proj', 'dev', 100, 'Testing', 'test-repo'
        )

    def test_the_column_comes_from_github_not_from_the_registry(self):
        """The entry is a hint about WHO is waiting, never an authority on board
        state -- a human may have moved the issue since it started waiting."""
        monitor, cm = _monitor(column='Code Review')
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        self._wake(monitor, cm)

        assert monitor.trigger_agent_for_status.call_args[0][3] == 'Code Review'

    def test_a_board_locked_again_is_not_offered(self):
        """"Released" and "free" are not the same thing: a concurrent acquire
        can land in between, and a retained (mark_failed) lock is deliberately
        never released at all."""
        monitor, cm = _monitor()
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(
            monitor, cm, lock=Mock(lock_status='locked', locked_by_issue=777)
        )

        assert result is None
        monitor.trigger_agent_for_status.assert_not_called()

    def test_the_longest_waiter_is_offered_the_board_first(self):
        # Offsets from the REAL monotonic clock, not absolute fake values: the
        # wake itself reads the real clock, and entries backdated past
        # WAIT_ENTRY_STALE_SECONDS would be pruned as stale before the ordering
        # was ever exercised. Both are well inside that window.
        monitor, cm = _monitor()
        reg = get_board_wait_registry()
        import time as _time
        base = _time.monotonic()
        with patch('services.board_wait_registry.time.monotonic', return_value=base - 10):
            reg.record_wait('proj', 'dev', 200)
        with patch('services.board_wait_registry.time.monotonic', return_value=base - 100):
            reg.record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result == 100

    def test_a_waiter_no_longer_on_the_board_is_dropped_not_dispatched(self):
        monitor, cm = _monitor(column=None)
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result is None
        monitor.trigger_agent_for_status.assert_not_called()
        assert get_board_wait_registry().get_waiters_for_board('proj', 'dev') == []

    def test_a_board_that_could_not_be_read_keeps_the_wait(self):
        """get_issue_column_sync() returns None for a removed card AND for a
        GraphQL error, a rate limit or an open circuit breaker. Collapsing the
        two would let one transient board-read failure permanently discard the
        wait — resetting waiting_since, the duration it will report and its
        escalation state — and state it in the log as fact."""
        monitor, cm = _monitor(column=None, column_reads_ok=False)
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result is None
        monitor.trigger_agent_for_status.assert_not_called()
        assert [w.issue_number
                for w in get_board_wait_registry().get_waiters_for_board('proj', 'dev')] == [100]

    def test_a_cancelled_waiter_is_dropped_not_dispatched(self):
        monitor, cm = _monitor()
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        lock_manager = Mock()
        lock_manager.get_lock.return_value = None
        with patch('services.project_monitor.ConfigManager', return_value=cm), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.cancellation.get_cancellation_signal') as sig:
            sig.return_value.is_cancelled.return_value = True
            result = monitor.dispatch_waiting_board_lock_waiter('proj', 'dev')

        assert result is None
        monitor.trigger_agent_for_status.assert_not_called()
        assert get_board_wait_registry().get_waiters_for_board('proj', 'dev') == []

    def test_a_dispatch_that_starts_nothing_keeps_the_wait_and_returns_none(self):
        """A TRANSIENT decline (bare None: a duplicate pending task, a review
        cycle already running, a retained lock). None tells the caller to fall
        through to its Development backfill, so a declined wake never leaves the
        freed board idle. The entry stays: the issue is still waiting and its
        own next refusal will refresh it. Contrast the permanent decline and the
        exit-column cases below, which must drop the entry."""
        monitor, cm = _monitor(trigger_return=None)
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result is None
        monitor.trigger_agent_for_status.assert_called_once()
        assert [w.issue_number
                for w in get_board_wait_registry().get_waiters_for_board('proj', 'dev')] == [100]

    def test_a_permanent_decline_drops_the_wait(self):
        """DispatchDecline is falsy by design, so it arrives at the same branch
        as a transient None while meaning the opposite: no retry can ever change
        the answer. Kept, the entry is re-offered the board on every release
        forever — the per-sweep `gh issue view` loop #165 introduced
        DispatchDecline to end."""
        from services.project_monitor import DispatchDecline

        monitor, cm = _monitor(trigger_return=DispatchDecline.ISSUE_CLOSED)
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result is None
        monitor.trigger_agent_for_status.assert_called_once()
        assert get_board_wait_registry().get_waiters_for_board('proj', 'dev') == []

    def test_a_permanently_declined_waiter_costs_nothing_on_the_next_release(self):
        """The actual cost being removed: with the entry kept, EVERY subsequent
        lock release spends a board read plus a `gh issue view` on an issue that
        is not waiting, and _refresh_board_lock_waits() renews the entry
        indefinitely so it never ages out."""
        from services.project_monitor import DispatchDecline

        monitor, cm = _monitor(trigger_return=DispatchDecline.ISSUE_CLOSED)
        get_board_wait_registry().record_wait('proj', 'dev', 100)
        self._wake(monitor, cm)

        monitor.get_issue_column_sync_checked.reset_mock()
        monitor.trigger_agent_for_status.reset_mock()
        result, lock_manager = self._wake(monitor, cm)

        assert result is None
        lock_manager.get_lock.assert_not_called()
        monitor.get_issue_column_sync_checked.assert_not_called()
        monitor.trigger_agent_for_status.assert_not_called()

    def test_a_permanent_decline_offers_the_board_to_the_next_waiter(self):
        """Dropping the dead waiter must not cost the board: the release is
        still free, so the next-longest waiter gets it in the same pass."""
        from services.project_monitor import DispatchDecline

        monitor, cm = _monitor()
        results = {100: DispatchDecline.ISSUE_CLOSED, 200: 'senior_software_engineer'}
        monitor.trigger_agent_for_status = Mock(
            side_effect=lambda _p, _b, issue, _c, _r: results[issue]
        )
        reg = get_board_wait_registry()
        import time as _time
        base = _time.monotonic()
        with patch('services.board_wait_registry.time.monotonic', return_value=base - 100):
            reg.record_wait('proj', 'dev', 100)
        with patch('services.board_wait_registry.time.monotonic', return_value=base - 10):
            reg.record_wait('proj', 'dev', 200)

        result, _ = self._wake(monitor, cm)

        assert result == 200
        assert [w.issue_number
                for w in reg.get_waiters_for_board('proj', 'dev')] == [200]

    def test_a_waiter_that_reached_an_exit_column_is_dropped_not_dispatched(self):
        """An exit-column card has left the pipeline. Dispatching it starts
        nothing (trigger_agent_for_status's exit-column branch returns a bare
        None) but DOES re-enter _release_pipeline_lock_and_process_next from
        inside a release — and the reentrancy guard blocks only the nested wake,
        not the nested Development backfill, so get_next_n_waiting_issues() runs
        twice. The bare None then reads as transient and the entry survives."""
        monitor, cm = _monitor(column='Staged', exit_columns=('Staged', 'Done'))
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result is None
        monitor.trigger_agent_for_status.assert_not_called()
        assert get_board_wait_registry().get_waiters_for_board('proj', 'dev') == []

    def test_a_mid_pipeline_column_is_never_mistaken_for_an_exit_column(self):
        """The drop is destructive — it discards waiting_since and the
        escalation state — so it must fire only on a configured exit column."""
        monitor, cm = _monitor(column='Testing', exit_columns=('Staged', 'Done'))
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result == 100
        monitor.trigger_agent_for_status.assert_called_once_with(
            'proj', 'dev', 100, 'Testing', 'test-repo'
        )

    def test_unresolvable_exit_columns_keep_the_wait_rather_than_drop_it(self):
        """A config the wake cannot read must fail towards the pre-existing
        behaviour (keep the wait), never towards the destructive one."""
        monitor, cm = _monitor(column='Staged', exit_columns=('Staged',))
        cm.get_workflow_template.side_effect = RuntimeError('config unreadable')
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        # Dispatched (the pre-#219-round-3 behaviour) and, crucially, still
        # registered: the wait was not silently discarded on a config error.
        assert result == 100
        assert [w.issue_number
                for w in get_board_wait_registry().get_waiters_for_board('proj', 'dev')] == [100]

    def test_a_raising_dispatch_never_breaks_the_lock_release(self):
        """The wake sits on the critical path of every stage completion. The
        poll-tick failsafe is still the backstop, so the worst case is the
        pre-#214 behaviour, never a failed release."""
        monitor, cm = _monitor()
        monitor.trigger_agent_for_status = Mock(side_effect=RuntimeError('boom'))
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        result, _ = self._wake(monitor, cm)

        assert result is None


class TestReleasePathPrefersTheWaiterOverTheDevelopmentQueue:
    """#214 finding 1's priority inversion, at the project_monitor release site.

    _release_pipeline_lock_and_process_next() re-locked the freed board for the
    next Development-column issue inline, before the waiting repair cycle's next
    poll tick could run -- and the failsafe's own "mid-pipeline beats
    Development" preference only applies at poll time on a free board.
    """

    def _release(self, monitor, queue):
        workflow_template = Mock()
        workflow_template.columns = []
        lock_manager = Mock()
        lock_manager.get_lock.return_value = None
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('services.review_cycle.review_cycle_executor'), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'):
            monitor._release_pipeline_lock_and_process_next(
                project_name='proj',
                board_name='dev',
                issue_number=50,
                exit_column='Staged',
                repository='test-repo',
                workflow_template=workflow_template,
            )

    def _queue(self):
        queue = Mock()
        queue.is_issue_in_queue.return_value = False
        queue.get_next_n_waiting_issues.return_value = []
        return queue

    def _monitor_for_release(self, wake_result):
        monitor, _cm = _monitor()
        monitor.pipeline_run_manager = Mock()
        monitor.pipeline_run_manager.end_pipeline_run.return_value = True
        monitor.dispatch_waiting_board_lock_waiter = Mock(return_value=wake_result)
        # The CRITICAL tail of the method under test. Counted, not stubbed away:
        # a wake that returns out of the function body skips it.
        pr_checks = []

        async def _record_pr_check(project_name, issue_number, exit_column):
            pr_checks.append((project_name, issue_number, exit_column))

        monitor._check_pr_ready_on_issue_exit = _record_pr_check
        return monitor, pr_checks

    def test_a_waiting_repair_cycle_wins_the_freed_board(self):
        monitor, pr_checks = self._monitor_for_release(wake_result=100)
        queue = self._queue()

        self._release(monitor, queue)

        # The Development backfill must not even be consulted -- consulting it
        # is what re-locked the board out from under the waiter.
        queue.get_next_n_waiting_issues.assert_not_called()
        # ...but waking a waiter must NOT cost the PR-ready check. It is the
        # handler that marks an epic's PR ready once its last sub-issue exits,
        # and a successful wake is exactly the case where an issue just reached
        # Done/Staged — the case that check exists for.
        assert pr_checks == [('proj', 50, 'Staged')]

    def test_with_no_waiter_the_development_backfill_runs_unchanged(self):
        """The no-op proof for production (capacity 1, nothing waiting)."""
        monitor, pr_checks = self._monitor_for_release(wake_result=None)
        queue = self._queue()

        self._release(monitor, queue)

        queue.get_next_n_waiting_issues.assert_called_once_with(1)
        assert pr_checks == [('proj', 50, 'Staged')]


class TestWakeReentrancy:
    """The wake dispatches through trigger_agent_for_status(), and that method
    calls _release_pipeline_lock_and_process_next() itself for an issue sitting
    in a pipeline EXIT column -- which calls the wake again, for the same board,
    with the same waiter still registered (the entry is only cleared on
    acquisition, which the exit-column branch never reaches). Left unguarded
    that recurses until the interpreter's recursion limit fires inside a lock
    release."""

    def test_a_wake_that_re_enters_the_release_path_does_not_recurse(self):
        monitor, cm = _monitor()
        get_board_wait_registry().record_wait('proj', 'dev', 100)

        depth = {'max': 0, 'now': 0}

        def _reentrant_trigger(*_args, **_kwargs):
            # Stands in for trigger_agent_for_status() reaching an exit column
            # and calling the release path, which calls the wake again.
            depth['now'] += 1
            depth['max'] = max(depth['max'], depth['now'])
            try:
                monitor.dispatch_waiting_board_lock_waiter('proj', 'dev')
            finally:
                depth['now'] -= 1
            return 'senior_software_engineer'

        monitor.trigger_agent_for_status = Mock(side_effect=_reentrant_trigger)

        lock_manager = Mock()
        lock_manager.get_lock.return_value = None
        with patch('services.project_monitor.ConfigManager', return_value=cm), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.cancellation.get_cancellation_signal') as sig:
            sig.return_value.is_cancelled.return_value = False
            result = monitor.dispatch_waiting_board_lock_waiter('proj', 'dev')

        assert result == 100
        assert depth['max'] == 1, "the wake must not re-enter itself for the same board"
        assert monitor.trigger_agent_for_status.call_count == 1

    def test_the_guard_is_released_again_after_a_wake_completes(self):
        """A guard that leaked would silently disable the wake for that board
        for the rest of the process's life."""
        monitor, cm = _monitor()
        lock_manager = Mock()
        lock_manager.get_lock.return_value = None
        with patch('services.project_monitor.ConfigManager', return_value=cm), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.cancellation.get_cancellation_signal') as sig:
            sig.return_value.is_cancelled.return_value = False
            for _ in range(3):
                get_board_wait_registry().record_wait('proj', 'dev', 100)
                assert monitor.dispatch_waiting_board_lock_waiter('proj', 'dev') == 100

        assert monitor.trigger_agent_for_status.call_count == 3

    def test_a_raising_dispatch_still_releases_the_guard(self):
        monitor, cm = _monitor()
        monitor.trigger_agent_for_status = Mock(side_effect=RuntimeError('boom'))
        lock_manager = Mock()
        lock_manager.get_lock.return_value = None
        with patch('services.project_monitor.ConfigManager', return_value=cm), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.cancellation.get_cancellation_signal') as sig:
            sig.return_value.is_cancelled.return_value = False
            get_board_wait_registry().record_wait('proj', 'dev', 100)
            assert monitor.dispatch_waiting_board_lock_waiter('proj', 'dev') is None

            # Guard released: a subsequent, working wake must still run.
            monitor.trigger_agent_for_status = Mock(return_value='agent')
            assert monitor.dispatch_waiting_board_lock_waiter('proj', 'dev') == 100

    def test_a_different_board_is_not_blocked_by_a_wake_in_progress(self):
        """The guard is per-board, not global: two boards releasing at once must
        both be able to wake their own waiter."""
        from services.board_wait_registry import wake_reentrancy_guard

        with wake_reentrancy_guard('proj', 'dev') as outer:
            assert outer is True
            with wake_reentrancy_guard('proj', 'other') as other_board:
                assert other_board is True
            with wake_reentrancy_guard('proj', 'dev') as same_board:
                assert same_board is False




class TestEndPipelineRunReleaseSiteYieldsTheBoardToTheWaiter:
    """The SECOND release-time backfill site #214 names:
    PipelineRunManager.end_pipeline_run(). Same trigger-column-only blindness as
    the project_monitor site, reached on a different path (a run ending rather
    than an issue reaching an exit column), so it needs its own wiring and its
    own proof.

    It YIELDS the board rather than dispatching the waiter, and that asymmetry
    is the point (review round): this method is called from the watchdog's
    self-heal sweep, review_cycle and the human feedback loop, and a repair-cycle
    dispatch here would run a GitHub fetch, a worktree resolution under the
    project_checkout lock and a container launch on those callers' threads.
    Before #214 this site only ever enqueued a Task.

    Driven against a REAL PipelineLockManager holding a real lock, so the
    release the yield follows is a real release, not a mocked one.
    """

    def _manager_and_lock(self, tmp_dir):
        from unittest.mock import MagicMock
        from pathlib import Path
        from services.pipeline_lock_manager import PipelineLockManager

        mock_es = MagicMock()
        mock_es.search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}
        mock_redis = MagicMock()
        with patch('services.pipeline_run.Elasticsearch', return_value=mock_es), \
             patch('services.pipeline_run.redis.Redis', return_value=mock_redis):
            from services.pipeline_run import PipelineRunManager
            manager = PipelineRunManager()
        manager.es = mock_es
        manager.redis = mock_redis
        lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), use_redis=False)
        return manager, lock_manager, mock_redis

    def _end_run(self, tmp_path, caplog, register_waiter):
        import json
        manager, lock_manager, mock_redis = self._manager_and_lock(str(tmp_path))
        assert lock_manager.try_acquire_lock('proj', 'board', 159) == (True, 'lock_acquired')

        run = manager.create_pipeline_run(
            issue_number=159, issue_title='t', issue_url='u',
            project='proj', board='board',
        )
        mock_redis.hget.return_value = run.id
        mock_redis.get.side_effect = lambda key: (
            json.dumps(run.to_dict()) if key == manager._get_redis_key(run.id) else None
        )

        if register_waiter:
            get_board_wait_registry().record_wait('proj', 'board', 100)

        queue = Mock()
        queue.get_next_n_waiting_issues.return_value = []
        # A registered global monitor, so "no dispatch happened" is a real
        # assertion rather than an artifact of there being nothing to call.
        monitor = Mock()

        with caplog.at_level(logging.INFO, logger='services.pipeline_run'), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.project_monitor.get_project_monitor', return_value=monitor):
            ended = manager.end_pipeline_run(
                project='proj', issue_number=159, reason='done', retain_lock=False,
            )
        return queue, monitor, ended, caplog.messages

    def test_a_waiting_repair_cycle_keeps_the_freed_board(self, tmp_path, caplog):
        queue, monitor, ended, messages = self._end_run(
            tmp_path, caplog, register_waiter=True
        )

        assert ended is True
        # Consulting the Development queue is what re-locked the board out from
        # under the waiter.
        queue.get_next_n_waiting_issues.assert_not_called()
        # ...and the wait is not dispatched from this thread.
        monitor.dispatch_waiting_board_lock_waiter.assert_not_called()
        monitor.trigger_agent_for_status.assert_not_called()
        # The terminal completion line still runs. An early `return True` at the
        # yield would have skipped it, leaving a run that ended with no "Ended
        # pipeline run" line anywhere in the log.
        assert any(m.startswith('Ended pipeline run') for m in messages), messages

    def test_with_no_waiter_the_development_backfill_runs_unchanged(self, tmp_path, caplog):
        """The no-op proof for this site: production at capacity 1 with nothing
        waiting must behave exactly as it did before #214."""
        queue, monitor, ended, messages = self._end_run(
            tmp_path, caplog, register_waiter=False
        )

        assert ended is True
        queue.get_next_n_waiting_issues.assert_called_once_with(1)
        monitor.dispatch_waiting_board_lock_waiter.assert_not_called()
        assert any(m.startswith('Ended pipeline run') for m in messages), messages

    def test_a_waiter_on_a_different_board_does_not_hold_this_one(self, tmp_path, caplog):
        get_board_wait_registry().record_wait('proj', 'other-board', 100)
        queue, _monitor, ended, _messages = self._end_run(
            tmp_path, caplog, register_waiter=False
        )

        assert ended is True
        queue.get_next_n_waiting_issues.assert_called_once_with(1)


class TestTheFailsafeIsWhatKeepsWaitsAlive:
    """Wiring proof for the review finding: the registry's refresh has exactly
    one production caller, and it is the failsafe's busy-board abort. Without
    that call every wait longer than WAIT_ENTRY_STALE_SECONDS dies mid-wait,
    because nothing else on any path re-records it."""

    def _failsafe(self, monitor, lock):
        lock_manager = Mock()
        lock_manager.get_lock.return_value = lock
        pipeline = Mock()
        pipeline.board_name = 'dev'
        pipeline.active = True
        project_config = Mock()
        project_config.pipelines = [pipeline]
        project_config.github = {'repo': 'test-repo'}
        monitor.config_manager.list_visible_projects.return_value = ['proj']
        monitor.config_manager.get_project_config.return_value = project_config
        monitor._reconcile_stale_state = Mock()
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=Mock()):
            monitor._check_and_process_waiting_issues_failsafe()

    def test_a_busy_board_refreshes_its_registered_waits(self):
        """Backdated to just inside the stale window first: without the refresh
        the entry is one tick from being pruned and the wake it exists for stops
        firing, which is precisely the wait-longer-than-five-minutes case."""
        import time as _time

        monitor, _cm = _monitor()
        monitor._find_stalled_issues_for_pipeline = Mock(return_value=[])
        reg = get_board_wait_registry()
        reg.record_wait('proj', 'dev', 100)
        entry = reg.get_waiters_for_board('proj', 'dev')[0]
        entry.waiting_since = _time.monotonic() - (WAIT_ENTRY_STALE_SECONDS - 1)
        entry.last_seen_at = entry.waiting_since

        self._failsafe(monitor, Mock(lock_status='locked', locked_by_issue=999))

        assert _time.monotonic() - entry.last_seen_at < 1.0, \
            "the failsafe's busy-board abort must refresh the board's waits"
        # And the whole wait is still what gets reported, not the gap since the
        # refresh.
        assert reg.clear_wait('proj', 'dev', 100) == pytest.approx(
            WAIT_ENTRY_STALE_SECONDS - 1, abs=2.0
        )

    def test_a_free_board_does_not_reach_the_refresh(self):
        """The refresh hangs off the busy-board abort specifically. On a free
        board the failsafe proceeds to its stalled scan, which is the path that
        actually dispatches the waiter."""
        import time as _time

        monitor, _cm = _monitor()
        monitor._find_stalled_issues_for_pipeline = Mock(return_value=[])
        reg = get_board_wait_registry()
        reg.record_wait('proj', 'dev', 100)
        entry = reg.get_waiters_for_board('proj', 'dev')[0]
        stamped_at = _time.monotonic() - 120.0
        entry.last_seen_at = stamped_at

        self._failsafe(monitor, Mock(lock_status='unlocked', locked_by_issue=None))

        assert entry.last_seen_at == stamped_at

    def test_a_long_wait_is_escalated_once_with_the_holder_named(self, caplog):
        monitor, _cm = _monitor()
        monitor._find_stalled_issues_for_pipeline = Mock(return_value=[])
        monitor._max_agent_timeout_seconds = Mock(return_value=1.0)
        get_board_wait_registry().record_wait('proj', 'dev', 100)
        import time as _time
        entry = get_board_wait_registry().get_waiters_for_board('proj', 'dev')[0]
        entry.waiting_since = _time.monotonic() - 7200.0

        lock = Mock(lock_status='locked', locked_by_issue=999)
        with caplog.at_level(logging.WARNING, logger='services.project_monitor'):
            self._failsafe(monitor, lock)
            self._failsafe(monitor, lock)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1, "escalation must fire once per wait, not per tick"
        assert '#100' in warnings[0].message
        assert '#999' in warnings[0].message


class TestColumnResolutionSeparatesGoneFromUnreadable:
    """get_issue_column_sync() answers None for a removed card AND for every
    failure — no project state, no board state, a GraphQL error, a rate limit,
    an open circuit breaker. Callers that act destructively on "gone" need the
    two apart, so the wake uses the _checked variant."""

    def _monitor_with_items(self, items, board_state=True, project_state=True):
        monitor, _cm = _monitor()
        # _monitor() stubs the checked resolver for the wake's sake; this class
        # is testing the real one.
        del monitor.get_issue_column_sync_checked
        project_config = Mock()
        project_config.github = {'org': 'test-org'}
        monitor.config_manager.get_project_config.return_value = project_config
        monitor.get_project_items = Mock(return_value=items)
        state = Mock()
        state.boards = {'dev': Mock(project_number=7)} if board_state else {}
        return monitor, (state if project_state else None)

    def _resolve(self, monitor, state):
        with patch('config.state_manager.state_manager') as sm:
            sm.load_project_state.return_value = state
            return monitor.get_issue_column_sync_checked('proj', 'dev', 100)

    def test_a_found_card_reports_its_column_and_a_good_read(self):
        monitor, state = self._monitor_with_items(
            [Mock(issue_number=100, status='Testing')]
        )
        assert self._resolve(monitor, state) == ('Testing', True)

    def test_a_card_absent_from_a_readable_board_is_genuinely_gone(self):
        monitor, state = self._monitor_with_items(
            [Mock(issue_number=101, status='Development')]
        )
        assert self._resolve(monitor, state) == (None, True)

    def test_an_empty_item_list_is_an_unreadable_board_not_an_empty_one(self):
        """get_project_items() returns [] for an open circuit breaker, a failed
        board query and a parse failure alike. Every caller here is asking about
        a board that has at least this issue's card on it."""
        monitor, state = self._monitor_with_items([])
        assert self._resolve(monitor, state) == (None, False)

    def test_missing_project_state_is_an_unreadable_board(self):
        monitor, state = self._monitor_with_items([], project_state=False)
        assert self._resolve(monitor, state) == (None, False)

    def test_missing_board_state_is_an_unreadable_board(self):
        monitor, state = self._monitor_with_items([], board_state=False)
        assert self._resolve(monitor, state) == (None, False)

    def test_a_raising_board_query_is_an_unreadable_board(self):
        monitor, state = self._monitor_with_items([])
        monitor.get_project_items = Mock(side_effect=RuntimeError('rate limited'))
        assert self._resolve(monitor, state) == (None, False)

    def test_the_plain_accessor_still_returns_just_the_column(self):
        """Its several existing callers are unchanged."""
        monitor, state = self._monitor_with_items(
            [Mock(issue_number=100, status='Testing')]
        )
        with patch('config.state_manager.state_manager') as sm:
            sm.load_project_state.return_value = state
            assert monitor.get_issue_column_sync('proj', 'dev', 100) == 'Testing'

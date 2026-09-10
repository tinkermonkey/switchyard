"""
Regression tests for the cleanup side effect #165's closed-issue skip removed.

_find_stalled_issues_for_pipeline() selects an issue when it has an active
pipeline run whose last decision event is STALE, not only when it has no run at
all. Before #165, a closed issue in that state was selected, the failsafe
acquired the board lock for it, trigger_agent_for_status() hit its CLOSED
branch, and _release_pipeline_lock_and_process_next() called
end_pipeline_run() -- so the orphaned run was quietly closed out within one
sweep. Skipping the issue outright removed the dispatch (correct) and that
cleanup (not).

With nothing ending the run it survived to zombie_threshold_minutes, where
PipelineWatchdog._cleanup_zombie_run's self-heal did this:

    cleared = lock_mgr.clear_retained_reason(project, board, issue)   # False
    redispatched = cleared and self._redispatch_same_issue(...)       # skipped

clear_retained_reason() returns False both for a failed durable write and for
a lock the issue does not hold -- and a closed issue that never went through
the failsafe never held it. The second was reported as the first: an ERROR
line, a mark_lock_failed() that correctly refuses for a non-holder, and a "the
pipeline lock is retained, run scripts/release_lock.py" comment posted on a
CLOSED issue naming a lock nobody holds. Precisely the recurring unactionable
error this branch exists to remove, made strictly more reachable than before.

Both halves are pinned here:
  1. the stalled sweep ends a closed issue's orphaned run itself, and
  2. neither watchdog self-heal treats "this issue does not hold the lock" as
     a self-heal failure.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, Mock, patch

from services.pipeline_watchdog import PipelineWatchdog
from services.project_monitor import ProjectItem, ProjectMonitor

PROJECT = 'code-wrapper'
BOARD = 'Planning & Design'
COLUMN = 'In Review'
ISSUE = 61


# ---------------------------------------------------------------------------
# 1. The stalled sweep ends a closed issue's orphaned run
# ---------------------------------------------------------------------------

def _find_stalled(state, active_run=None, end_result=True, run_manager=None):
    """Drive FAILSAFE Scenario 1 for one issue in a mid-pipeline column, with
    whatever active pipeline run the caller wants it to have."""
    monitor = object.__new__(ProjectMonitor)
    monitor.pipeline_run_manager = run_manager or MagicMock()
    if run_manager is None:
        monitor.pipeline_run_manager.get_active_pipeline_run.return_value = active_run
        monitor.pipeline_run_manager.end_pipeline_run.return_value = end_result

    item = ProjectItem(
        item_id='i1', content_id='c1', issue_number=ISSUE, title='t',
        status=COLUMN, repository='code-wrapper',
        last_updated='2026-01-01T00:00:00Z', state=state,
    )

    pipeline = MagicMock()
    pipeline.board_name = BOARD
    pipeline.workflow = 'planning_workflow'
    project_config = MagicMock()
    project_config.pipelines = [pipeline]

    column = MagicMock()
    column.name = COLUMN
    column.agent = 'technical_reviewer'
    workflow_template = MagicMock()
    workflow_template.columns = [column]
    workflow_template.pipeline_exit_columns = ['Done']

    config_manager = MagicMock()
    config_manager.get_project_config.return_value = project_config
    config_manager.get_workflow_template.return_value = workflow_template

    queue = MagicMock()
    queue.load_queue.return_value = []

    tracker = MagicMock()
    tracker.has_active_execution.return_value = False

    signal = MagicMock()
    signal.is_cancelled.return_value = False

    with patch('services.project_monitor.ConfigManager', return_value=config_manager), \
         patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=queue), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.pipeline_run.get_pipeline_run_manager',
               return_value=monitor.pipeline_run_manager), \
         patch('services.cancellation.get_cancellation_signal', return_value=signal):
        stalled = monitor._find_stalled_issues_for_pipeline(
            PROJECT, BOARD, cached_items=[item]
        )
    return stalled, monitor.pipeline_run_manager


def _orphaned_run():
    run = MagicMock()
    run.id = 'ffffffffdeadbeef'
    return run


class TestStalledSweepEndsClosedIssueOrphanedRun:

    def test_closed_issue_with_an_active_run_has_it_ended(self):
        """The regression: nothing else ends this run, so it ages into the
        zombie sweep and the mishandling described in the module docstring."""
        stalled, run_manager = _find_stalled('CLOSED', active_run=_orphaned_run())

        assert stalled == []
        run_manager.end_pipeline_run.assert_called_once()
        kwargs = run_manager.end_pipeline_run.call_args.kwargs
        assert kwargs['project'] == PROJECT
        assert kwargs['issue_number'] == ISSUE
        assert kwargs['board'] == BOARD
        # retain_lock=False mirrors the pre-#165 teardown. end_pipeline_run
        # only releases when this issue actually holds the lock, so a lock
        # held by a different issue is left alone.
        assert kwargs['retain_lock'] is False

    def test_closed_issue_with_no_active_run_writes_nothing(self):
        """Steady state for the four closed cards #165 was filed against: one
        Redis read per sweep, no writes. Moving the work rather than removing
        it would defeat the point of the skip."""
        stalled, run_manager = _find_stalled('CLOSED', active_run=None)

        assert stalled == []
        run_manager.end_pipeline_run.assert_not_called()

    def test_open_issue_is_still_selected_and_its_run_left_alone(self):
        """The cleanup must be about CLOSED, not about mid-pipeline columns --
        resuming genuinely stranded in-flight work is what this sweep is for."""
        stalled, run_manager = _find_stalled('OPEN', active_run=None)

        assert [s['issue_number'] for s in stalled] == [ISSUE]
        run_manager.end_pipeline_run.assert_not_called()

    def test_a_failing_run_manager_does_not_break_the_sweep(self):
        """Best-effort cleanup: it must never be able to take down the scan
        the rest of the board depends on."""
        run_manager = MagicMock()
        run_manager.get_active_pipeline_run.side_effect = RuntimeError("redis down")

        stalled, _ = _find_stalled('CLOSED', run_manager=run_manager)

        assert stalled == []


# ---------------------------------------------------------------------------
# 2. Neither watchdog self-heal pages a human over a lock it never held
# ---------------------------------------------------------------------------

class _FakeRedis:
    def __init__(self):
        self.counters = {}

    def incr(self, key):
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]

    def expire(self, key, ttl):
        pass


def _watchdog(lock_read):
    """A watchdog whose lock manager reports `lock_read` -- the
    (lock_or_None, reads_healthy) tuple get_lock_fail_closed() returns."""
    pipeline_run_manager = Mock()
    pipeline_run_manager.redis = _FakeRedis()
    pipeline_run_manager.end_pipeline_run = Mock(return_value=True)

    lock_manager = Mock()
    lock_manager.clear_retained_reason = Mock(return_value=True)
    lock_manager.get_lock_fail_closed = Mock(return_value=lock_read)

    project_monitor = Mock()
    project_monitor.get_issue_details.return_value = {"state": "CLOSED"}

    return PipelineWatchdog(
        es_client=Mock(),
        pipeline_run_manager=pipeline_run_manager,
        lock_manager=lock_manager,
        project_monitor=project_monitor,
    )


def _lock_held_by(issue_number):
    lock = Mock()
    lock.locked_by_issue = issue_number
    return lock


class TestZombieSelfHealSkipsNonHolders:

    def test_closed_issue_that_never_held_the_lock_is_not_a_failure(self):
        """The exact interleaving from the module docstring: a closed issue's
        orphaned run ages into the zombie sweep while a DIFFERENT issue holds
        the board lock."""
        wd = _watchdog((_lock_held_by(77), True))

        with patch.object(wd, "_notify_lock_stuck") as notify, \
             patch.object(wd, "_redispatch_same_issue") as redispatch:
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project=PROJECT,
                board=BOARD,
                issue_number=ISSUE,
                started_at="2026-08-10T10:07:24Z",
            )

        # The run is still ended -- that half was always right.
        wd.pipeline_run_manager.end_pipeline_run.assert_called_once()
        # ...but nothing is cleared, re-marked, redispatched or escalated.
        wd.lock_manager.clear_retained_reason.assert_not_called()
        wd.lock_manager.mark_lock_failed.assert_not_called()
        redispatch.assert_not_called()
        notify.assert_not_called()

    def test_no_lock_at_all_on_the_board_is_not_a_failure(self):
        wd = _watchdog((None, True))

        with patch.object(wd, "_notify_lock_stuck") as notify, \
             patch.object(wd, "_redispatch_same_issue") as redispatch:
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project=PROJECT,
                board=BOARD,
                issue_number=ISSUE,
                started_at="2026-08-10T10:07:24Z",
            )

        wd.lock_manager.clear_retained_reason.assert_not_called()
        redispatch.assert_not_called()
        notify.assert_not_called()

    def test_the_holder_still_takes_the_normal_self_heal_path(self):
        """The skip must be about non-holders only -- a genuine zombie whose
        issue does hold the lock still gets cleared and redispatched."""
        wd = _watchdog((_lock_held_by(ISSUE), True))

        with patch.object(wd, "_notify_lock_stuck") as notify, \
             patch.object(wd, "_redispatch_same_issue", return_value=True) as redispatch:
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project=PROJECT,
                board=BOARD,
                issue_number=ISSUE,
                started_at="2026-08-10T10:07:24Z",
            )

        wd.lock_manager.clear_retained_reason.assert_called_once_with(
            PROJECT, BOARD, ISSUE
        )
        redispatch.assert_called_once()
        notify.assert_not_called()

    def test_unreadable_lock_state_keeps_the_pre_existing_path(self):
        """Fail CLOSED: when both stores raised, lock state is unknown, and
        this check must not be the thing that quietly decides a self-heal was
        unnecessary."""
        wd = _watchdog((None, False))

        with patch.object(wd, "_notify_lock_stuck"), \
             patch.object(wd, "_redispatch_same_issue", return_value=True) as redispatch:
            wd._cleanup_zombie_run(
                pipeline_run_id="run-1",
                project=PROJECT,
                board=BOARD,
                issue_number=ISSUE,
                started_at="2026-08-10T10:07:24Z",
            )

        wd.lock_manager.clear_retained_reason.assert_called_once()
        redispatch.assert_called_once()


class TestActiveResumeSkipsNonHolders:

    def test_non_holder_resume_is_a_clean_outcome(self):
        wd = _watchdog((_lock_held_by(77), True))

        with patch.object(wd, "_notify_lock_stuck") as notify, \
             patch.object(wd, "_redispatch_same_issue") as redispatch:
            resumed = wd._actively_resume_run(
                pipeline_run_id="run-1",
                project=PROJECT,
                board=BOARD,
                issue_number=ISSUE,
                started_at="2026-08-10T10:07:24Z",
            )

        assert resumed is True
        wd.lock_manager.clear_retained_reason.assert_not_called()
        redispatch.assert_not_called()
        notify.assert_not_called()

    def test_holder_still_takes_the_normal_resume_path(self):
        wd = _watchdog((_lock_held_by(ISSUE), True))

        with patch.object(wd, "_notify_lock_stuck") as notify, \
             patch.object(wd, "_redispatch_same_issue", return_value=True) as redispatch:
            resumed = wd._actively_resume_run(
                pipeline_run_id="run-1",
                project=PROJECT,
                board=BOARD,
                issue_number=ISSUE,
                started_at="2026-08-10T10:07:24Z",
            )

        assert resumed is True
        wd.lock_manager.clear_retained_reason.assert_called_once()
        redispatch.assert_called_once()
        notify.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

"""
Regression tests for #148 C1: a lock-contention teardown must leave the issue
RE-DISPATCHABLE ON THE NEXT POLL, which is what every one of those teardowns
claims in its own log line and comment.

PipelineRunManager.end_pipeline_run() sets a cancellation signal for every
`reason` except the literal "feedback_loop_ended", and that signal has a 1-hour
TTL (services/cancellation.py _TTL_SECONDS). Every automatic recovery path
consults it:

  * _find_stalled_issues_for_pipeline() -- FAILSAFE Scenario 1, the only thing
    that re-dispatches an issue sitting in a mid-pipeline column (Code Review,
    Testing) -- does `if cancellation_signal.is_cancelled(...): continue`;
  * _check_and_process_waiting_issues_failsafe() -- Scenario 2 -- checks it
    after acquiring the lock, releases, and breaks; and for a QUEUE row (not a
    stalled issue) it also calls remove_issue_from_queue(), because
    `is_stalled = 'column' in next_issue` is False for rows that store
    'initial_column'. So the queue entry is purged, not merely skipped;
  * process_board_changes() only dispatches on 'status_changed'/'item_added',
    and a contention teardown moves nothing.

So "released for the next poll" silently meant "invisible for up to an hour",
which with MAX_CONSECUTIVE_LOCK_CONTENTIONS = 3 pushes the escalation this work
made reachable out to ~12h -- the same silent outcome the escalation exists to
prevent. pipeline_watchdog.py already documents this exact hole for its own case.

These tests pin both halves: the suppression itself, and the end-to-end property
that the failsafe can still see the issue on its very next pass.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, patch

from services.pipeline_run import PipelineRunManager
from services.project_monitor import ProjectItem, ProjectMonitor

PROJECT = 'test-project'
BOARD = 'Development'
ISSUE = 8102
COLUMN = 'Testing'


class _FakeSignal:
    """An in-memory stand-in for CancellationSignal. Deliberately NOT the real
    singleton: that one writes to the live Redis this container shares."""

    def __init__(self):
        self.cancelled = set()
        self.cancel_calls = []

    def cancel(self, project, issue_number, reason=""):
        self.cancel_calls.append((project, issue_number, reason))
        self.cancelled.add((project, issue_number))

    def is_cancelled(self, project, issue_number):
        return (project, issue_number) in self.cancelled

    def clear(self, project, issue_number):
        self.cancelled.discard((project, issue_number))


def _run_manager():
    manager = object.__new__(PipelineRunManager)
    manager.redis = MagicMock()
    manager.es = None
    manager.redis_prefix = "orchestrator:pipeline_run"
    manager.redis_issue_mapping = "orchestrator:pipeline_run:issue_mapping"
    return manager


def _end_run(manager, signal, **kwargs):
    active = MagicMock()
    active.id = 'run-1'
    active.board = BOARD
    active.started_at = '2026-01-01T00:00:00Z'
    active.to_dict.return_value = {}

    with patch.object(manager, 'get_active_pipeline_run', return_value=active), \
         patch.object(manager, '_release_or_retain_lock', MagicMock(), create=True), \
         patch('services.cancellation.get_cancellation_signal', return_value=signal), \
         patch('monitoring.observability.get_observability_manager', return_value=MagicMock()):
        return manager.end_pipeline_run(
            project=PROJECT,
            board=BOARD,
            issue_number=ISSUE,
            **kwargs,
        )


class TestSuppressCancellation:
    def test_a_contention_teardown_does_not_cancel_the_issue(self):
        signal = _FakeSignal()
        _end_run(
            _run_manager(), signal,
            reason="Repair cycle blocked by a project resource-lock timeout",
            retain_lock=False,
            suppress_cancellation=True,
        )
        assert signal.cancel_calls == []
        assert not signal.is_cancelled(PROJECT, ISSUE)

    def test_the_default_still_cancels(self):
        """The suppression must be opt-in: every pre-existing caller relies on
        end_pipeline_run() stopping in-flight work for the issue it ends."""
        signal = _FakeSignal()
        _end_run(
            _run_manager(), signal,
            reason="Issue reached exit column 'Done'",
        )
        assert signal.is_cancelled(PROJECT, ISSUE)

    def test_feedback_loop_ended_is_still_exempt_without_the_flag(self):
        signal = _FakeSignal()
        _end_run(_run_manager(), signal, reason="feedback_loop_ended")
        assert not signal.is_cancelled(PROJECT, ISSUE)

    def test_suppression_does_not_clear_a_signal_somebody_else_set(self):
        """An operator kill (services.cancellation.cancel_issue_work) sets the
        same signal and MUST keep the issue stopped. Suppression only declines
        to set a new one; it must never clear an existing one."""
        signal = _FakeSignal()
        signal.cancel(PROJECT, ISSUE, "operator killed the pipeline")

        _end_run(
            _run_manager(), signal,
            reason="Repair cycle blocked by a project resource-lock timeout",
            retain_lock=False,
            suppress_cancellation=True,
        )

        assert signal.is_cancelled(PROJECT, ISSUE)


def _monitor():
    monitor = object.__new__(ProjectMonitor)
    monitor.pipeline_run_manager = MagicMock()
    monitor.pipeline_run_manager.get_active_pipeline_run.return_value = None
    return monitor


def _find_stalled(signal):
    """Drive FAILSAFE Scenario 1 for one issue sitting in a mid-pipeline column
    with no active run and no active execution -- i.e. exactly the state a
    contention teardown leaves behind."""
    monitor = _monitor()

    item = ProjectItem(
        item_id='i1', content_id='c1', issue_number=ISSUE, title='t',
        status=COLUMN, repository='test-repo',
        last_updated='2026-01-01T00:00:00Z',
    )

    pipeline = MagicMock()
    pipeline.board_name = BOARD
    pipeline.workflow = 'dev_workflow'
    project_config = MagicMock()
    project_config.pipelines = [pipeline]

    column = MagicMock()
    column.name = COLUMN
    column.agent = 'senior_software_engineer'
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

    with patch('services.project_monitor.ConfigManager', return_value=config_manager), \
         patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=queue), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.pipeline_run.get_pipeline_run_manager',
               return_value=monitor.pipeline_run_manager), \
         patch('services.cancellation.get_cancellation_signal', return_value=signal):
        return monitor._find_stalled_issues_for_pipeline(
            PROJECT, BOARD, cached_items=[item]
        )


class TestTheFailsafeCanStillSeeTheIssue:
    """The property that actually matters: after a contention teardown, the very
    next failsafe pass must be able to re-dispatch the issue."""

    def test_the_issue_is_visible_after_a_suppressed_teardown(self):
        signal = _FakeSignal()
        _end_run(
            _run_manager(), signal,
            reason="Repair cycle blocked by a project resource-lock timeout",
            retain_lock=False,
            suppress_cancellation=True,
        )

        stalled = _find_stalled(signal)

        assert [s['issue_number'] for s in stalled] == [ISSUE]
        assert stalled[0]['column'] == COLUMN

    def test_without_suppression_the_issue_is_invisible(self):
        """The bug this fixes, pinned so it cannot come back: the same teardown
        without the flag makes the issue unfindable for the signal's whole TTL."""
        signal = _FakeSignal()
        _end_run(
            _run_manager(), signal,
            reason="Repair cycle blocked by a project resource-lock timeout",
            retain_lock=False,
        )

        assert _find_stalled(signal) == []

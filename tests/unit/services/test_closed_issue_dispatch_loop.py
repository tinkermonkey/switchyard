"""
Regression tests for #165: a CLOSED issue must stop being a dispatch candidate.

Observed in production: four closed issues sitting in a mid-pipeline column ran
this cycle on EVERY failsafe sweep, indefinitely --

    FAILSAFE (STALLED): Found in-flight issue #61 in column 'In Review'
    FAILSAFE: Triggering agent for issue #61 in column 'In Review'
    gh issue view #61 ...
    Issue #61 is CLOSED - checking if lock needs release
    ERROR Dispatch of next queued issue #61 ... started nothing and nothing is
          running for it, rolling back lock acquisition and queue status
    Rolled back lock for issue #61

-- one `gh issue view`, one lock acquire/release pair and one ERROR line per
closed issue per sweep, for a condition no retry can resolve.

Three independent halves are pinned here, because the loop had three ways to
keep going:

  1. Stalled detection is BOARD-driven (_find_stalled_issues_for_pipeline reads
     project items, not the queue), so a closed card in 'In Review' was
     re-selected every sweep regardless of queue state. The board query already
     returns the issue's state; it was simply being discarded during parsing.
  2. Nothing removed the closed issue from the pipeline queue, so the
     queue-driven ('waiting') path could re-offer it too.
  3. The WI-2 dispatch rollback (#147) could not tell "declined permanently"
     from "failed transiently" and returned the entry to 'waiting' -- re-arming
     it for the next sweep. That is now a DispatchDecline.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import logging
from unittest.mock import MagicMock, patch

from services.project_monitor import (
    DispatchDecline,
    ProjectItem,
    ProjectMonitor,
    is_permanent_decline,
)

PROJECT = 'code-wrapper'
BOARD = 'Planning & Design'
COLUMN = 'In Review'
ISSUE = 61


# ---------------------------------------------------------------------------
# 1. The board query already knows the issue is closed
# ---------------------------------------------------------------------------

class TestBoardItemsCarryIssueState:
    """_project_v2_fields() has always selected `state` on Issue; parsing threw
    it away, which is what left the stalled sweep unable to tell a closed card
    from live work."""

    @staticmethod
    def _node(number, state):
        return {
            'id': f'item-{number}',
            'content': {
                '__typename': 'Issue',
                'id': f'content-{number}',
                'number': number,
                'title': f'Issue {number}',
                'state': state,
                'repository': {'name': 'code-wrapper'},
                'updatedAt': '2026-01-01T00:00:00Z',
            },
            'fieldValues': {'nodes': [
                {'name': COLUMN, 'field': {'name': 'Status'}},
            ]},
        }

    def test_state_is_parsed_onto_the_item(self):
        monitor = object.__new__(ProjectMonitor)
        items = monitor._parse_board_items({'items': {'nodes': [
            self._node(1, 'OPEN'), self._node(2, 'CLOSED'),
        ]}})

        assert [(i.issue_number, i.state) for i in items] == [(1, 'OPEN'), (2, 'CLOSED')]

    def test_missing_state_defaults_to_open(self):
        """An absent state must NOT silently drop an issue out of the sweep."""
        node = self._node(3, 'OPEN')
        del node['content']['state']

        monitor = object.__new__(ProjectMonitor)
        items = monitor._parse_board_items({'items': {'nodes': [node]}})

        assert items[0].state == 'OPEN'


# ---------------------------------------------------------------------------
# 2. Stalled detection must not select a closed issue
# ---------------------------------------------------------------------------

def _find_stalled(state):
    """Drive FAILSAFE Scenario 1 for one issue in a mid-pipeline column with no
    active run and no active execution -- the exact state the closed issues in
    #165 were in."""
    monitor = object.__new__(ProjectMonitor)
    monitor.pipeline_run_manager = MagicMock()
    monitor.pipeline_run_manager.get_active_pipeline_run.return_value = None

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
        return monitor._find_stalled_issues_for_pipeline(
            PROJECT, BOARD, cached_items=[item]
        )


class TestStalledDetectionSkipsClosedIssues:

    def test_a_closed_issue_is_not_stalled_work(self):
        """The regression itself: without the state check this returns the
        closed issue and the sweep dispatches it again."""
        assert _find_stalled('CLOSED') == []

    def test_an_open_issue_in_the_same_state_is_still_detected(self):
        """The skip must be about CLOSED, not about mid-pipeline columns --
        resuming genuinely stranded in-flight work is what this sweep is for."""
        stalled = _find_stalled('OPEN')

        assert [s['issue_number'] for s in stalled] == [ISSUE]
        assert stalled[0]['column'] == COLUMN


# ---------------------------------------------------------------------------
# 3. The closed-issue branch dequeues, and says so permanently
# ---------------------------------------------------------------------------

def _trigger_for_closed_issue(queue_manager):
    monitor = object.__new__(ProjectMonitor)
    monitor.get_issue_details = lambda repo, num, org: {'state': 'CLOSED'}

    pipeline = MagicMock()
    pipeline.board_name = BOARD
    pipeline.workflow = 'planning_workflow'
    project_config = MagicMock()
    project_config.pipelines = [pipeline]
    project_config.github = {'org': 'tinkermonkey', 'repo': 'code-wrapper'}

    config_manager = MagicMock()
    config_manager.get_project_config.return_value = project_config
    config_manager.get_workflow_template.return_value = MagicMock(columns=[])
    monitor.config_manager = config_manager

    lock_manager = MagicMock()
    lock_manager.get_lock.return_value = None

    with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
               return_value=lock_manager), \
         patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
               return_value=queue_manager):
        return monitor.trigger_agent_for_status(
            PROJECT, BOARD, ISSUE, COLUMN, 'code-wrapper'
        )


class TestClosedIssueDispatchDecline:

    def test_the_queue_entry_is_removed(self):
        """Releasing the lock was never enough: nothing dropped the entry, so
        the issue stayed a dispatch candidate."""
        queue_manager = MagicMock()

        _trigger_for_closed_issue(queue_manager)

        queue_manager.remove_issue_from_queue.assert_called_once_with(ISSUE)

    def test_the_decline_is_permanent_and_still_falsy(self):
        result = _trigger_for_closed_issue(MagicMock())

        assert result is DispatchDecline.ISSUE_CLOSED
        assert is_permanent_decline(result)
        # Every existing caller tests this by truthiness; a decline must stay
        # falsy exactly as the None it replaces was.
        assert not result

    def test_a_failed_dequeue_does_not_break_the_decline(self):
        """The decline is what stops the loop; the dequeue is defence in depth,
        so a queue write failure must not cost us the decline."""
        queue_manager = MagicMock()
        queue_manager.remove_issue_from_queue.side_effect = OSError('queue unwritable')

        assert _trigger_for_closed_issue(queue_manager) is DispatchDecline.ISSUE_CLOSED


# ---------------------------------------------------------------------------
# 4. The rollback must not re-arm a permanently declined entry
# ---------------------------------------------------------------------------

def _run_rollback(dispatch_result, caplog):
    monitor = object.__new__(ProjectMonitor)
    monitor.trigger_agent_for_status = MagicMock(return_value=dispatch_result)
    monitor._get_agent_for_status = MagicMock(return_value='technical_reviewer')
    monitor._has_pending_task_for_issue = MagicMock(return_value=False)

    lock_manager = MagicMock()
    lock_manager.get_lock_fail_closed.return_value = (None, True)
    pipeline_queue = MagicMock()

    tracker = MagicMock()
    tracker.has_active_execution.return_value = False

    with patch('services.work_execution_state.work_execution_tracker', tracker), \
         caplog.at_level(logging.DEBUG, logger='services.project_monitor'):
        dispatched = monitor._trigger_next_issue_with_rollback(
            PROJECT, BOARD, ISSUE, COLUMN, 'code-wrapper',
            pipeline_queue=pipeline_queue,
            activated_at='2026-01-01T00:00:00Z',
            lock_manager=lock_manager,
            lock_already_acquired=True,
        )

    return dispatched, lock_manager, pipeline_queue


class TestRollbackDistinguishesPermanentFromTransient:

    def test_a_permanent_decline_does_not_return_the_entry_to_waiting(self, caplog):
        """The regression: reset_issue_to_waiting() is what makes the closed
        issue a valid candidate again on the very next sweep."""
        dispatched, lock_manager, pipeline_queue = _run_rollback(
            DispatchDecline.ISSUE_CLOSED, caplog
        )

        assert dispatched is False
        pipeline_queue.reset_issue_to_waiting.assert_not_called()
        # The lock still has to come back -- the caller acquired it.
        lock_manager.release_lock.assert_called_once_with(PROJECT, BOARD, ISSUE)

    def test_a_permanent_decline_is_not_logged_as_an_error(self, caplog):
        """A recurring ERROR for a condition no retry can resolve is exactly
        what trains operators to stop reading the log (#177 Phase 1)."""
        _run_rollback(DispatchDecline.ISSUE_CLOSED, caplog)

        errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert errors == [], [r.getMessage() for r in errors]

    def test_a_transient_no_op_dispatch_still_rolls_back_as_before(self, caplog):
        """The other half of the distinction: a plain None still means "try
        again", so the entry must still go back to 'waiting'."""
        dispatched, lock_manager, pipeline_queue = _run_rollback(None, caplog)

        assert dispatched is False
        pipeline_queue.reset_issue_to_waiting.assert_called_once()
        lock_manager.release_lock.assert_called_once_with(PROJECT, BOARD, ISSUE)
        assert any(r.levelno >= logging.ERROR for r in caplog.records)

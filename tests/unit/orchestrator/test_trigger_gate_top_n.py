"""
Queue-priority gate in ProjectMonitor.trigger_agent_for_status (#140 item 21).

This is the 6th dispatch-gating site, and the one #57 never migrated. Unlike the
5 "pick N candidates from the queue" dispatchers, it asks a question about ONE
issue already in hand: "is this exact issue allowed to go now, or must it wait
for higher-priority work?" It used to answer that with equality against the
single top-priority pick:

    next_issue = pipeline_queue.get_next_waiting_issue()
    if next_issue and next_issue['issue_number'] != issue_number:
        return None

Equality is only the right question while exactly one issue may run per board.
The moment Phase 3a raises the board's slot count, an issue sitting 2nd in line
with a free slot for it is still turned away, because the top pick is by
definition somebody else -- the gate silently caps the board back at one.

The fix is top-N MEMBERSHIP: ask the queue for as many candidates as there are
slots, and let the issue through if it is among them. At today's
available_slots=1 the list has at most one element, so membership IS equality
and dispatch behaviour is unchanged -- which the last test here pins.

These tests drive the gate through the queue manager rather than by raising a
slot count: what changed is the SHAPE of the question, and a queue manager
returning a 2-candidate top list is exactly the state Phase 3a produces.
"""

import pytest
from unittest.mock import patch, Mock

from tests.unit.orchestrator.conftest import create_test_issue


def _queue_manager(top_issues):
    """A pipeline queue manager whose top-N pick is `top_issues`."""
    queue_manager = Mock()
    queue_manager.get_next_n_waiting_issues.return_value = list(top_issues)
    # The pre-fix call. Left configured so a regression to the old equality gate
    # would still run (and be caught by the assertions), rather than blowing up
    # on an unconfigured Mock and looking like an unrelated failure.
    queue_manager.get_next_waiting_issue.return_value = (
        list(top_issues)[0] if top_issues else None
    )
    return queue_manager


def _waiting(issue_number, position):
    return {
        'issue_number': issue_number,
        'status': 'waiting',
        'position_in_column': position,
    }


def _dispatch(monitor_kwargs, queue_manager, issue_number):
    """Run trigger_agent_for_status() for `issue_number` against `queue_manager`."""
    (mock_github, mock_config_manager, mock_state_manager,
     mock_task_queue, mock_observability) = monitor_kwargs

    # The gate lives in the trigger-column branch, so the workflow has to name
    # one. mock_config_manager's shared workflow leaves pipeline_trigger_columns
    # at None (no column ever triggers), which would skip the branch entirely.
    mock_config_manager.get_workflow_template.return_value.pipeline_trigger_columns = ['Development']

    create_test_issue(mock_github, issue_number, 'Development')

    with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
         patch('config.state_manager.state_manager', mock_state_manager), \
         patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
         patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=queue_manager), \
         patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:

        mock_run = Mock()
        mock_run.id = 'run-top-n'
        mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)

        from services.project_monitor import ProjectMonitor
        monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
        monitor.decision_events = mock_observability[1]
        monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)

        return monitor.trigger_agent_for_status(
            project_name='test-project',
            board_name='dev',
            issue_number=issue_number,
            status='Development',
            repository='test-repo',
        )


@pytest.fixture
def monitor_kwargs(
    mock_pipeline_lock_manager_auto,
    mock_github,
    mock_config_manager,
    mock_state_manager,
    mock_task_queue,
    mock_observability,
):
    return (mock_github, mock_config_manager, mock_state_manager,
            mock_task_queue, mock_observability)


class TestQueuePriorityGateIsTopNMembership:

    def test_second_in_line_is_let_through_when_it_is_within_the_top_n(
        self, monitor_kwargs, mock_pipeline_lock_manager_auto
    ):
        """
        The regression #140 item 21 names. Issue 202 is 2nd in the board's
        top-2 pick. Under the old equality gate it was turned away because 201
        is the single top pick; under top-N membership it dispatches.
        """
        queue_manager = _queue_manager([_waiting(201, 0), _waiting(202, 1)])

        _dispatch(monitor_kwargs, queue_manager, issue_number=202)

        # Acquisition is the statement immediately after the gate, so it is the
        # signal that the gate let this issue through -- an issue inside the
        # board's top-N pick must not be held behind the single top-priority
        # one. (Dispatch past that point needs a real pipeline template, which
        # these mocks deliberately do not provide: this test is about the gate,
        # not about routing.)
        mock_pipeline_lock_manager_auto.try_acquire_lock.assert_called_once_with(
            project='test-project', board='dev', issue_number=202
        )

    def test_issue_outside_the_top_n_still_waits(
        self, monitor_kwargs, mock_pipeline_lock_manager_auto
    ):
        """Control: membership, not "always let through". 203 is not among the
        board's candidates, so it must still wait its turn."""
        queue_manager = _queue_manager([_waiting(201, 0), _waiting(202, 1)])

        result = _dispatch(monitor_kwargs, queue_manager, issue_number=203)

        assert result is None
        mock_pipeline_lock_manager_auto.try_acquire_lock.assert_not_called()

    def test_empty_queue_pick_does_not_gate(
        self, monitor_kwargs, mock_pipeline_lock_manager_auto
    ):
        """An empty candidate list means the queue has nothing to say about
        priority -- unchanged from the old `if next_issue and ...` shape, which
        also fell through on None."""
        queue_manager = _queue_manager([])

        _dispatch(monitor_kwargs, queue_manager, issue_number=204)

        mock_pipeline_lock_manager_auto.try_acquire_lock.assert_called_once_with(
            project='test-project', board='dev', issue_number=204
        )


class TestSingleSlotBehaviourIsUnchanged:

    def test_gate_asks_for_exactly_one_candidate_today(self, monitor_kwargs):
        """available_slots is still 1 at this site: PipelineLockManager remains a
        single-holder lock, so asking for more would only manufacture candidates
        that could never acquire. Raising it is Phase 3a's job, together with the
        acquire primitive."""
        queue_manager = _queue_manager([_waiting(205, 0)])

        _dispatch(monitor_kwargs, queue_manager, issue_number=205)

        queue_manager.get_next_n_waiting_issues.assert_called_once_with(1)

    def test_top_pick_still_blocks_everyone_else_at_one_slot(
        self, monitor_kwargs, mock_pipeline_lock_manager_auto
    ):
        """With one slot the candidate list is a single element, so membership
        collapses back to the pre-fix equality check."""
        queue_manager = _queue_manager([_waiting(206, 0)])

        result = _dispatch(monitor_kwargs, queue_manager, issue_number=207)

        assert result is None
        mock_pipeline_lock_manager_auto.try_acquire_lock.assert_not_called()

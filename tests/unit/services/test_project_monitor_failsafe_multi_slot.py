"""
Multi-slot behaviour of the FAILSAFE dispatch site (#140 item 27).

Phase 2 (#57) wrapped _check_and_process_waiting_issues_failsafe()'s per-board
body in `for _dispatch_slot in range(available_slots):`. That mechanical rewrite
turned every `continue` -- which had meant "skip to the next PIPELINE in the
outer loop" -- into a `break`, which now only exits the inner per-slot loop.
Invisible at available_slots=1 (the body runs exactly once either way), but the
moment Phase 3a raises the count, the first slot hitting ANY of those conditions
aborted every remaining slot attempt for that board, silently giving back the
concurrency that was just enabled. No test exercised available_slots > 1 at this
site at all.

These tests drive the loop at 2-3 slots via services.project_monitor's
FAILSAFE_DISPATCH_SLOTS (still 1 in production -- nothing here raises a real
concurrency limit) and pin which conditions are per-CANDIDATE skips (`continue`
-- try the next candidate, the board's slot is still open) and which are
per-BOARD aborts (`break` -- no candidate can get around this).

#154/WI-9 review: turning those `break`s into `continue`s was not by itself
enough. The site fetched its candidate with the n=1 queue wrapper ONCE PER SLOT,
and a candidate that fails try_acquire_lock() or mark_issue_active() is neither
dequeued nor marked active -- it stays 'waiting' at the same position, so the
next fetch returned the same issue and the loop terminated on it. Net behaviour
at >1 slot was identical to the old `break`. The site now takes ONE
get_next_n_waiting_issues(available_slots) snapshot per board and walks it, the
same shape as the five sites #57 generalized. These tests therefore mock a
STABLE ordered candidate list rather than a side_effect sequence of successive
fetches -- a queue that hands back a different head after a failed attempt is
not a state real PipelineQueueManager can be in, and a test built on one passes
whether or not a second candidate is genuinely reachable.
"""
import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, patch

import services.project_monitor as project_monitor_module
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    """One project with one active pipeline board, mirroring
    test_project_monitor_failsafe.py's fixture."""
    config_manager = Mock(spec=ConfigManager)

    active_pipeline = Mock()
    active_pipeline.active = True
    active_pipeline.board_name = "SDLC Execution"
    active_pipeline.workflow = "sdlc_execution_workflow"

    project_config = Mock()
    project_config.pipelines = [active_pipeline]
    project_config.github = {'repo': 'test-org/test-repo'}
    project_config.orchestrator = {"polling_interval": 30}

    mock_column = Mock()
    mock_column.name = 'Development'
    mock_column.type = 'standard'
    workflow_template = Mock()
    workflow_template.columns = [mock_column]
    workflow_template.pipeline_trigger_columns = None
    workflow_template.pipeline_exit_columns = None

    config_manager.list_projects.return_value = []
    config_manager.list_visible_projects.return_value = ['test_project']
    config_manager.get_project_config.return_value = project_config
    config_manager.get_workflow_template.return_value = workflow_template

    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    task_queue = Mock()
    task_queue.get_pending_tasks.return_value = []
    monitor = ProjectMonitor(task_queue, mock_config_manager)
    monitor.trigger_agent_for_status = Mock()
    monitor.get_issue_column_sync = Mock(return_value='Development')
    # No stalled issues unless a test says otherwise -- these tests are about
    # the Development-queue path.
    monitor._find_stalled_issues_for_pipeline = Mock(return_value=[])
    # No live pipeline run unless a test says otherwise -- a bare Mock's
    # .status is truthy and would send every candidate down the
    # feedback_listening branch.
    monitor.pipeline_run_manager = Mock()
    monitor.pipeline_run_manager.get_active_pipeline_run.return_value = None
    return monitor


@pytest.fixture
def unlocked_lock_manager():
    lock_manager = Mock()
    unlocked = Mock()
    unlocked.lock_status = 'unlocked'
    lock_manager.get_lock.return_value = unlocked
    return lock_manager


def _waiting(issue_number):
    return {'issue_number': issue_number, 'status': 'waiting', 'title': f'Issue {issue_number}'}


def _queue(*issue_numbers):
    """A queue manager whose board-order snapshot is STABLE across fetches --
    what a real PipelineQueueManager gives back while nothing has dequeued or
    activated any of its entries."""
    queue_manager = Mock()
    queue_manager.get_next_n_waiting_issues.return_value = [
        _waiting(n) for n in issue_numbers
    ]
    return queue_manager


def _run(monitor, lock_manager, queue_manager, slots, **failsafe_kwargs):
    """Run the failsafe with FAILSAFE_DISPATCH_SLOTS temporarily raised."""
    with patch.object(project_monitor_module, 'FAILSAFE_DISPATCH_SLOTS', slots), \
         patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=lock_manager), \
         patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=queue_manager):
        monitor._check_and_process_waiting_issues_failsafe(**failsafe_kwargs)


def _triggered_issue_numbers(monitor):
    """Issue numbers passed to trigger_agent_for_status(), in call order.

    Signature is trigger_agent_for_status(project, board, issue_number, column,
    repo, ...) -- positional at this call site.
    """
    return [call.args[2] for call in monitor.trigger_agent_for_status.call_args_list]


def _attempted_issue_numbers(lock_manager):
    return [
        call.kwargs['issue_number']
        for call in lock_manager.try_acquire_lock.call_args_list
    ]


class TestCandidateLevelSkipsDoNotAbortRemainingSlots:
    """The conditions that are about ONE candidate must `continue`."""

    def test_acquire_failure_does_not_abort_the_next_slot(
        self, project_monitor, unlocked_lock_manager
    ):
        """
        The regression #140 item 27 names. Issue 201 is spoken for by another
        dispatch path, so its try_acquire_lock() fails -- but the BOARD's slot is
        still open, and 202 is next in line. Before the fix the `break` here
        ended the whole board's slot loop and 202 was never attempted.

        The queue is deliberately a stable two-entry snapshot: 201 failing to
        acquire leaves it 'waiting' at the head, so reaching 202 has to come
        from walking the snapshot past an already-attempted candidate, not from
        the queue handing back something new.
        """
        queue_manager = _queue(201, 202)
        unlocked_lock_manager.try_acquire_lock.side_effect = [
            (False, 'locked_by_issue_999'),
            (True, 'lock_acquired'),
        ]

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=2)

        attempted = _attempted_issue_numbers(unlocked_lock_manager)
        assert attempted == [201, 202], (
            "the second slot must still try the next candidate after the first "
            f"candidate's acquire failed, got {attempted}"
        )
        assert _triggered_issue_numbers(project_monitor) == [202]
        assert queue_manager.get_next_n_waiting_issues.call_count == 1, (
            "one top-N snapshot per board per cycle, not one fetch per slot"
        )

    def test_cancelled_issue_does_not_abort_the_next_slot(
        self, project_monitor, unlocked_lock_manager
    ):
        """A cancelled issue says nothing about the rest of the queue, and the
        lock it briefly held is released again before the skip."""
        queue_manager = _queue(301, 302)
        unlocked_lock_manager.try_acquire_lock.return_value = (True, 'lock_acquired')

        signal = Mock()
        signal.is_cancelled.side_effect = lambda project, issue: issue == 301

        with patch('services.cancellation.get_cancellation_signal', return_value=signal):
            _run(project_monitor, unlocked_lock_manager, queue_manager, slots=2)

        assert _triggered_issue_numbers(project_monitor) == [302]
        queue_manager.remove_issue_from_queue.assert_called_once_with(301)
        unlocked_lock_manager.release_lock.assert_any_call(
            'test_project', 'SDLC Execution', 301
        )

    def test_mark_issue_active_failure_does_not_abort_the_next_slot(
        self, project_monitor, unlocked_lock_manager
    ):
        """
        mark_issue_active() raising releases the just-acquired lock, so the
        board's slot is genuinely still free for the next candidate.

        This is the skip the per-slot n=1 fetch broke most completely: the queue
        write did not happen, so 401 is still 'waiting' at the head of the SAME
        snapshot. Only attempted_issues moves the loop on to 402.
        """
        queue_manager = _queue(401, 402)
        queue_manager.mark_issue_active.side_effect = [
            OSError("read-only state volume"),
            '2026-01-01T00:00:00+00:00',
        ]
        unlocked_lock_manager.try_acquire_lock.return_value = (True, 'lock_acquired')

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=2)

        assert _triggered_issue_numbers(project_monitor) == [402]
        unlocked_lock_manager.release_lock.assert_any_call(
            'test_project', 'SDLC Execution', 401
        )

    def test_a_live_feedback_loop_does_not_abort_the_next_slot(
        self, project_monitor, unlocked_lock_manager
    ):
        """
        The fourth `continue` this diff introduced, and the least mechanical of
        them: the other three release a lock they just took (or never took one)
        in the same code block, while this one rests on the claim that a live
        conversational loop holds no pipeline lock of its own. Pinned here, both
        halves: 501's just-acquired lock IS released before the skip, and the
        next slot is free to dispatch 502 onto the same board.
        """
        queue_manager = _queue(501, 502)
        unlocked_lock_manager.try_acquire_lock.return_value = (True, 'lock_acquired')

        listening = Mock()
        listening.status = "feedback_listening"
        project_monitor.pipeline_run_manager.get_active_pipeline_run.side_effect = (
            lambda project, issue: listening if issue == 501 else None
        )

        # The loop's own liveness marker is a plain Redis key existence check.
        live_redis = Mock()
        live_redis.exists.return_value = 1

        with patch('redis.Redis', return_value=live_redis):
            _run(project_monitor, unlocked_lock_manager, queue_manager, slots=2)

        assert _triggered_issue_numbers(project_monitor) == [502], (
            "an issue with a live feedback loop must not be re-triggered, and "
            "must not cost the board its remaining slots"
        )
        unlocked_lock_manager.release_lock.assert_any_call(
            'test_project', 'SDLC Execution', 501
        )

    def test_stalled_candidates_are_not_re_picked_by_later_slots(
        self, project_monitor, unlocked_lock_manager
    ):
        """
        The stalled-issue scan is a pure read of cached board items, so without
        a per-board memory of what this pass already decided on, every slot
        would re-pick stalled_issues[0] and dispatch the same issue N times.

        Pins a LOOP property under a mocked return the real method cannot
        currently produce: _find_stalled_issues_for_pipeline() caps its result
        at FAILSAFE_DISPATCH_SLOTS (== 1 in production), and the real
        PipelineLockManager grants one lock per board, so neither the two-entry
        scan result nor the second acquire below happens today. This is not
        evidence that the stalled path dispatches N per cycle -- it is the
        assertion that attempted_issues, not the scan, is what stops the re-pick
        once Phase 3a makes both possible.
        """
        project_monitor._find_stalled_issues_for_pipeline = Mock(return_value=[
            {'issue_number': 601, 'column': 'Code Review'},
            {'issue_number': 602, 'column': 'Testing'},
        ])
        project_monitor.get_issue_column_sync = Mock(side_effect=lambda p, b, n: (
            'Code Review' if n == 601 else 'Testing'
        ))
        queue_manager = _queue()
        unlocked_lock_manager.try_acquire_lock.return_value = (True, 'lock_acquired')

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=2)

        assert _triggered_issue_numbers(project_monitor) == [601, 602]


class TestBoardLevelAbortsStillStopEveryRemainingSlot:
    """The conditions that are about the BOARD must stay `break`."""

    def test_board_lock_held_stops_all_slots(self, project_monitor):
        lock_manager = Mock()
        held = Mock()
        held.lock_status = 'locked'
        lock_manager.get_lock.return_value = held
        queue_manager = _queue(101)

        _run(project_monitor, lock_manager, queue_manager, slots=3)

        # One get_lock() read, then out -- no candidate work at all.
        assert lock_manager.get_lock.call_count == 1
        queue_manager.get_next_n_waiting_issues.assert_not_called()
        lock_manager.try_acquire_lock.assert_not_called()

    def test_empty_queue_stops_all_slots(self, project_monitor, unlocked_lock_manager):
        queue_manager = _queue()

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=3)

        assert queue_manager.get_next_n_waiting_issues.call_count == 1
        unlocked_lock_manager.try_acquire_lock.assert_not_called()

    def test_a_board_not_due_this_cycle_stops_all_slots(
        self, project_monitor, unlocked_lock_manager
    ):
        """Due-ness is a property of the board (per-board adaptive backoff), so
        it is identical for every slot -- and the whole point of the gate is
        that a not-due board must not touch the network at all."""
        queue_manager = _queue(801, 802)
        unlocked_lock_manager.try_acquire_lock.return_value = (True, 'lock_acquired')

        with patch('services.github_owner_utils.execute_batched_board_queries',
                   return_value=({}, {})):
            # An empty due list means "no board is due this cycle" -- distinct
            # from None, which means the caller isn't gating on due-ness at all.
            _run(project_monitor, unlocked_lock_manager, queue_manager, slots=3,
                 due_boards_this_cycle=[])

        assert project_monitor._find_stalled_issues_for_pipeline.call_count == 1, (
            "the not-due break must stop the slot loop, not just this slot"
        )
        queue_manager.get_next_n_waiting_issues.assert_not_called()
        unlocked_lock_manager.try_acquire_lock.assert_not_called()

    def test_an_unexpected_exception_stops_all_slots(
        self, project_monitor, unlocked_lock_manager
    ):
        """The slot body's own except handler `break`s: an unexpected exception
        says nothing about which candidate is at fault, and this matches the
        pre-#57 shape where the same handler skipped to the next pipeline."""
        unlocked_lock_manager.get_lock.side_effect = RuntimeError("redis is gone")
        queue_manager = _queue(901, 902)

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=3)

        assert unlocked_lock_manager.get_lock.call_count == 1
        queue_manager.get_next_n_waiting_issues.assert_not_called()
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_an_exhausted_candidate_list_stops_all_slots_rather_than_spinning(
        self, project_monitor, unlocked_lock_manager
    ):
        """
        Termination guard for the `continue`s above. A candidate that failed to
        acquire is left 'waiting', so it is still in the snapshot. Without the
        already-attempted filter every remaining slot would re-attempt the same
        issue; with it, the loop recognises there is no progress to be made and
        gives the board up for this cycle.
        """
        queue_manager = _queue(1001)
        unlocked_lock_manager.try_acquire_lock.return_value = (False, 'locked_by_issue_999')

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=5)

        assert unlocked_lock_manager.try_acquire_lock.call_count == 1, (
            "the same candidate must not be re-attempted once per slot"
        )
        assert queue_manager.get_next_n_waiting_issues.call_count == 1, (
            "the snapshot is taken once per board, not re-fetched per slot"
        )
        project_monitor.trigger_agent_for_status.assert_not_called()


class TestSingleSlotBehaviourIsUnchanged:
    """Production still runs at FAILSAFE_DISPATCH_SLOTS == 1."""

    def test_production_slot_count_is_still_one(self):
        assert project_monitor_module.FAILSAFE_DISPATCH_SLOTS == 1

    def test_one_slot_makes_exactly_one_attempt(
        self, project_monitor, unlocked_lock_manager
    ):
        queue_manager = _queue(701, 702)
        unlocked_lock_manager.try_acquire_lock.return_value = (False, 'locked_by_issue_999')

        _run(project_monitor, unlocked_lock_manager, queue_manager, slots=1)

        assert queue_manager.get_next_n_waiting_issues.call_count == 1
        assert queue_manager.get_next_n_waiting_issues.call_args.args[0] == 1, (
            "at one slot the site must ask the queue for exactly one candidate, "
            "the same single top pick the pre-#57 n=1 wrapper returned"
        )
        assert unlocked_lock_manager.try_acquire_lock.call_count == 1
        project_monitor.trigger_agent_for_status.assert_not_called()

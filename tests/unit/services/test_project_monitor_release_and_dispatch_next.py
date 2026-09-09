"""
Unit tests for ProjectMonitor._release_pipeline_lock_and_process_next().

Phase 2 (issue #57, parent #88, umbrella #34): this is one of the "get next
-> try_acquire_lock -> mark_issue_active" dispatch call sites generalized to
loop over available slots (get_next_waiting_issue() -> get_next_n_waiting_
issues(n)). "available_slots" is hardcoded to 1 today -- PipelineLockManager
still enforces exactly one concurrent issue per (project, board) -- so this
must drive the exact same single-dispatch behavior as the pre-#57
get_next_waiting_issue()-based implementation.

No live network/Docker access - all dependencies are mocked.
"""
import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, AsyncMock, patch, call

from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


@pytest.fixture
def mock_config_manager():
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    return config_manager


@pytest.fixture
def project_monitor(mock_config_manager):
    task_queue = Mock()
    monitor = ProjectMonitor(task_queue, mock_config_manager)

    monitor.get_issue_column_sync = Mock(return_value='Development')
    # A truthy return means "an agent was actually dispatched" - the signal
    # _trigger_next_issue_with_rollback() keys its rollback decision off.
    monitor.trigger_agent_for_status = Mock(return_value='senior_software_engineer')
    monitor.pipeline_run_manager = Mock()
    monitor.pipeline_run_manager.end_pipeline_run.return_value = True
    # _check_pr_ready_on_issue_exit is awaited via asyncio.run() inside the
    # method under test - must be a real coroutine function, not a bare Mock.
    monitor._check_pr_ready_on_issue_exit = AsyncMock(return_value=None)

    return monitor


def _workflow_template():
    dev_column = Mock()
    dev_column.name = 'Development'
    # Deliberately no 'type' set to 'conversational' - Mock() auto-creates
    # `.type` as a fresh Mock, so `column.type == 'conversational'` is False,
    # taking the non-conversational (exclusive lock) branch, same as today.
    workflow_template = Mock()
    workflow_template.columns = [dev_column]
    return workflow_template


class TestReleasePipelineLockAndProcessNext:
    """_release_pipeline_lock_and_process_next()'s next-queued-issue dispatch."""

    def test_dispatches_single_next_queued_issue_at_capacity_one(self, project_monitor):
        """Byte-identical-at-capacity-1 check: exactly one waiting issue ->
        lock released for the exiting issue, get_next_n_waiting_issues(1)
        queried, lock acquired for the next issue, marked active, and the
        agent triggered for it - the same sequence get_next_waiting_issue()
        drove before #57."""
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0}
        ]

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('services.review_cycle.review_cycle_executor'), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'):

            project_monitor._release_pipeline_lock_and_process_next(
                'test-project', 'SDLC Execution', 100, 'Done', 'test-repo',
                _workflow_template()
            )

        # Lock released for the completed issue
        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'SDLC Execution', 100)

        # Queried for exactly 1 slot (today's hardcoded available_slots)
        mock_queue.get_next_n_waiting_issues.assert_called_once_with(1)

        # Lock acquired for the next queued issue
        mock_lock_manager.try_acquire_lock.assert_called_once_with(
            project='test-project', board='SDLC Execution', issue_number=200
        )

        # Next issue marked active and agent triggered for it
        mock_queue.mark_issue_active.assert_called_once_with(200)
        project_monitor.trigger_agent_for_status.assert_called_once_with(
            'test-project', 'SDLC Execution', 200, 'Development', 'test-repo',
            lock_already_acquired=False, raise_on_error=True
        )

        # Nothing failed, so nothing is rolled back.
        mock_queue.reset_issue_to_waiting.assert_not_called()
        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'SDLC Execution', 100)

    def test_no_dispatch_when_queue_empty(self, project_monitor):
        """Control case: empty queue (get_next_n_waiting_issues(1) -> [])
        must still release the lock/clean up but dispatch nothing - matching
        the pre-#57 get_next_waiting_issue() -> None behavior."""
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.get_next_n_waiting_issues.return_value = []

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('services.review_cycle.review_cycle_executor'), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'):

            project_monitor._release_pipeline_lock_and_process_next(
                'test-project', 'SDLC Execution', 100, 'Done', 'test-repo',
                _workflow_template()
            )

        mock_queue.get_next_n_waiting_issues.assert_called_once_with(1)
        mock_queue.mark_issue_active.assert_not_called()
        mock_lock_manager.try_acquire_lock.assert_not_called()
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_next_issue_not_dispatched_when_lock_acquisition_fails(self, project_monitor):
        """If try_acquire_lock fails for the next issue (raced by another
        process), no dispatch happens - same as before #57."""
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (False, "locked_by_issue_999")

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0}
        ]

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('services.review_cycle.review_cycle_executor'), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'):

            project_monitor._release_pipeline_lock_and_process_next(
                'test-project', 'SDLC Execution', 100, 'Done', 'test-repo',
                _workflow_template()
            )

        mock_queue.mark_issue_active.assert_not_called()
        project_monitor.trigger_agent_for_status.assert_not_called()


def _conversational_workflow_template():
    """A workflow whose next column IS conversational — the Planning & Design
    shape, where the queue's own trigger column ("Research") is conversational
    and dispatch therefore happens WITHOUT the pipeline lock."""
    column = Mock()
    column.name = 'Development'
    column.type = 'conversational'
    workflow_template = Mock()
    workflow_template.columns = [column]
    return workflow_template


class TestReleasePipelineLockAndProcessNextRollback:
    """Issue #147: these two dispatch branches had NO rollback at all. They
    acquire the lock (non-conversational branch), mark the queue entry 'active',
    then call trigger_agent_for_status() — which ends in a bare
    `except Exception: logger.error(...); return None` — and ignore its return
    value. A Redis blip on the enqueue, or a GitHub 5xx inside
    ensure_pipeline_run_for_task(), therefore left the lock held and the entry
    stuck at 'active' with no exception raised anywhere.

    Neither automated recovery covers that state: the stale-lock watchdog only
    reaps when get_active_pipeline_run() is None (the run has usually already
    been created by then), and _reset_stranded_active_issues() skips the lock
    holder by design."""

    ACTIVATED_AT = '2026-01-01T00:00:00+00:00'

    def _mocks(self):
        our_lock = Mock()
        our_lock.locked_by_issue = 100

        mock_lock_manager = Mock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = Mock()
        mock_queue.is_issue_in_queue.return_value = True
        mock_queue.mark_issue_active.return_value = self.ACTIVATED_AT
        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0}
        ]
        return mock_lock_manager, mock_queue

    def _run(self, project_monitor, mock_lock_manager, mock_queue,
             workflow_template=None, has_active_execution=False):
        mock_tracker = Mock()
        mock_tracker.has_active_execution.return_value = has_active_execution

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.work_execution_state.work_execution_tracker', mock_tracker), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('services.review_cycle.review_cycle_executor'), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'):

            project_monitor._release_pipeline_lock_and_process_next(
                'test-project', 'SDLC Execution', 100, 'Done', 'test-repo',
                workflow_template if workflow_template is not None else _workflow_template()
            )

    def test_rolls_back_when_dispatch_raises(self, project_monitor):
        """REGRESSION (#147): with raise_on_error=True the enqueue failure now
        propagates, so the caller can tell "it blew up" from "it declined" and
        undo both halves of the acquisition."""
        mock_lock_manager, mock_queue = self._mocks()
        project_monitor.trigger_agent_for_status = Mock(
            side_effect=RuntimeError("redis down")
        )

        self._run(project_monitor, mock_lock_manager, mock_queue)

        mock_queue.mark_issue_active.assert_called_once_with(200)
        mock_lock_manager.release_lock.assert_any_call('test-project', 'SDLC Execution', 200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=self.ACTIVATED_AT
        )

    def test_rolls_back_when_dispatch_silently_starts_nothing(self, project_monitor):
        """The most silent shape of all: trigger_agent_for_status() swallowed an
        internal failure and returned None, and the caller ignored it. Nothing is
        running for the issue, so both halves are undone."""
        mock_lock_manager, mock_queue = self._mocks()
        project_monitor.trigger_agent_for_status = Mock(return_value=None)

        self._run(project_monitor, mock_lock_manager, mock_queue)

        mock_lock_manager.release_lock.assert_any_call('test-project', 'SDLC Execution', 200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=self.ACTIVATED_AT
        )

    def test_does_not_roll_back_a_no_op_dispatch_with_work_already_running(self, project_monitor):
        """trigger_agent_for_status() also returns None for legitimate reasons —
        a duplicate pending task, a review or repair cycle already in flight. A
        positive liveness check gates the rollback so those are not torn down."""
        mock_lock_manager, mock_queue = self._mocks()
        project_monitor.trigger_agent_for_status = Mock(return_value=None)

        self._run(project_monitor, mock_lock_manager, mock_queue, has_active_execution=True)

        mock_queue.reset_issue_to_waiting.assert_not_called()
        # Only the exiting issue's own release.
        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'SDLC Execution', 100)

    def test_releases_lock_before_the_compare_and_swap_reset(self, project_monitor):
        """ORDER: the reset must not run while the lock is still held.
        try_acquire_lock() returns True/"already_holds_lock" for the current
        holder and trigger_agent_for_status() dispatches on that branch, so a
        competing poll could genuinely start #200 in that window — and the
        release would then free the lock out from under it."""
        mock_lock_manager, mock_queue = self._mocks()
        project_monitor.trigger_agent_for_status = Mock(side_effect=RuntimeError("boom"))

        call_order = []
        mock_lock_manager.release_lock.side_effect = (
            lambda *a, **kw: call_order.append(f'release-{a[2]}') or True
        )
        mock_queue.reset_issue_to_waiting.side_effect = (
            lambda *a, **kw: call_order.append('reset') or True
        )

        self._run(project_monitor, mock_lock_manager, mock_queue)

        assert call_order == ['release-100', 'release-200', 'reset']

    def test_releases_lock_when_mark_issue_active_raises(self, project_monitor):
        """A queue YAML write failure (ENOSPC, read-only state/ bind mount)
        between acquiring the lock and dispatching used to leave the lock held
        with nothing running — a board deadlock until the 4h staleness
        heuristic. Nothing was stamped, so there is no reset to make."""
        mock_lock_manager, mock_queue = self._mocks()
        mock_queue.mark_issue_active.side_effect = OSError(
            "[Errno 28] No space left on device"
        )

        self._run(project_monitor, mock_lock_manager, mock_queue)

        mock_lock_manager.release_lock.assert_any_call('test-project', 'SDLC Execution', 200)
        mock_queue.reset_issue_to_waiting.assert_not_called()
        project_monitor.trigger_agent_for_status.assert_not_called()

    def test_rolls_back_conversational_entry_with_no_lock_to_release(self, project_monitor):
        """The conversational branch takes no lock, but a stuck 'active' entry
        excludes the issue from every future dispatch just the same — and for the
        Planning & Design workflow the queue's trigger column IS conversational,
        so this is the ordinary path there, not an edge case."""
        mock_lock_manager, mock_queue = self._mocks()
        project_monitor.trigger_agent_for_status = Mock(side_effect=RuntimeError("boom"))

        self._run(project_monitor, mock_lock_manager, mock_queue,
                  workflow_template=_conversational_workflow_template())

        mock_queue.mark_issue_active.assert_called_once_with(200)
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=self.ACTIVATED_AT
        )
        # No lock was ever taken for the conversational issue, so the only
        # release is the exiting issue's own.
        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'SDLC Execution', 100)

    def test_no_rollback_when_liveness_check_itself_errors(self, project_monitor):
        """Fails closed: if it can't be established that nothing is running,
        leave the lock and entry alone rather than risk tearing down a live
        agent."""
        mock_lock_manager, mock_queue = self._mocks()
        project_monitor.trigger_agent_for_status = Mock(return_value=None)

        mock_tracker = Mock()
        mock_tracker.has_active_execution.side_effect = RuntimeError("state file unreadable")

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.work_execution_state.work_execution_tracker', mock_tracker), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('services.review_cycle.review_cycle_executor'), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'):

            project_monitor._release_pipeline_lock_and_process_next(
                'test-project', 'SDLC Execution', 100, 'Done', 'test-repo',
                _workflow_template()
            )

        mock_queue.reset_issue_to_waiting.assert_not_called()
        mock_lock_manager.release_lock.assert_called_once_with('test-project', 'SDLC Execution', 100)

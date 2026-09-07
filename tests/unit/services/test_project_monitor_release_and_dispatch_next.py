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
    monitor.trigger_agent_for_status = Mock()
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
            'test-project', 'SDLC Execution', 200, 'Development', 'test-repo'
        )

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

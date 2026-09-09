"""
End-state tests for ProjectMonitor._trigger_next_issue_with_rollback().

The existing rollback tests mock trigger_agent_for_status() wholesale and assert
that reset_issue_to_waiting() was CALLED. That shape of assertion cannot see the
#147 defect it was written for: trigger_agent_for_status() marks the queue entry
active AGAIN on every branch that reaches dispatch, which used to stamp a fresh
`activated_at` and make the caller's rollback token stale before it could be
used. The compare-and-swap then refused, the entry stayed 'active' forever, and
get_next_n_waiting_issues() (which filters strictly on 'waiting') silently
dropped the issue from every future dispatch.

So these tests drive the REAL trigger_agent_for_status() against a REAL
PipelineQueueManager and a REAL WorkExecutionStateTracker on tmp state dirs, and
assert the terminal state - what the queue entry and the execution record
actually ARE when the dust settles - not which methods were called.

No live network/Docker access - GitHub reads and the task queue are mocked.
"""
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from config.manager import ConfigManager
from services.pipeline_queue_manager import PipelineQueueManager
from services.project_monitor import ProjectMonitor
from services.work_execution_state import WorkExecutionStateTracker

PROJECT = 'test_project'
BOARD = 'SDLC Execution'
COLUMN = 'Development'
AGENT = 'senior_software_engineer'
ISSUE = 200
REPO = 'test-org/test-repo'


@pytest.fixture
def temp_state_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


def _column(name=COLUMN, col_type='work', agent=AGENT):
    column = Mock()
    column.name = name
    column.type = col_type
    column.agent = agent
    column.stage_mapping = None
    return column


def _workflow_template(columns=None):
    template = Mock()
    template.columns = columns if columns is not None else [_column()]
    template.pipeline_trigger_columns = [COLUMN]
    template.pipeline_exit_columns = ['Done']
    return template


def _project_config(workspace='issues'):
    pipeline_config = Mock()
    pipeline_config.board_name = BOARD
    pipeline_config.workflow = 'dev_workflow'
    pipeline_config.workspace = workspace
    pipeline_config.template = 'sdlc_execution'
    pipeline_config.name = 'sdlc'

    project_config = Mock()
    project_config.name = PROJECT
    project_config.pipelines = [pipeline_config]
    project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
    return project_config


def _queue_manager(state_dir, entries):
    queue = PipelineQueueManager(
        project_name=PROJECT, board_name=BOARD, state_dir=state_dir
    )
    # No GitHub reads: enqueue_issue()/sync_queue_with_github() both start by
    # asking for the column ordering.
    queue.get_issues_in_column_order = Mock(return_value=[])
    queue._get_pipeline_trigger_column = Mock(return_value=COLUMN)
    queue.save_queue(entries)
    return queue


def _waiting(issue_number=ISSUE):
    now = datetime.now(timezone.utc).isoformat()
    return {
        'issue_number': issue_number,
        'status': 'waiting',
        'position_in_column': 0,
        'queued_at': now,
        'last_position_check': now,
    }


def _monitor(tracker, workspace='issues', columns=None):
    """A ProjectMonitor wired far enough to run trigger_agent_for_status()."""
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    config_manager.get_project_config.return_value = _project_config(workspace)
    config_manager.get_workflow_template.return_value = _workflow_template(columns)
    config_manager.get_pipeline_template.return_value = None

    task_queue = Mock()
    task_queue.get_pending_tasks.return_value = []

    monitor = ProjectMonitor(task_queue, config_manager)
    monitor.get_issue_details = Mock(return_value={'state': 'OPEN', 'number': ISSUE})
    monitor.get_previous_stage_context = Mock(return_value=None)
    monitor.decision_events = Mock()
    monitor.observability = Mock()

    pipeline_run = Mock()
    pipeline_run.id = 'run-1'
    pipeline_run.context_dir = None
    monitor.pipeline_run_manager = Mock()
    monitor.pipeline_run_manager.get_or_create_pipeline_run.return_value = (
        pipeline_run, True
    )
    return monitor


def _lock_manager():
    lock_manager = Mock()
    lock_manager.get_retained_reason.return_value = None
    lock_manager.get_lock.return_value = None
    lock_manager.release_lock.return_value = True
    return lock_manager


class TestDispatchRollbackEndState:
    """The queue entry and execution record after a failed dispatch."""

    def _dispatch(self, monitor, queue, lock_manager, tracker, activated_at):
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.cancellation.get_cancellation_signal'):

            return monitor._trigger_next_issue_with_rollback(
                PROJECT, BOARD, ISSUE, COLUMN, REPO,
                pipeline_queue=queue,
                activated_at=activated_at,
                lock_manager=lock_manager,
                lock_already_acquired=True,
            )

    def test_entry_ends_up_waiting_despite_the_internal_re_mark(
        self, temp_state_dir
    ):
        """REGRESSION (#147): trigger_agent_for_status() marks the entry active a
        second time on the way to the enqueue. That stamp used to invalidate the
        caller's compare-and-swap token, so the rollback was refused on EVERY
        dispatch and the entry stayed 'active' - excluded from all future
        dispatch, the exact silent loss the rollback exists to prevent."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)
        monitor.task_queue.enqueue.side_effect = RuntimeError("redis down")

        activated_at = queue.mark_issue_active(ISSUE)
        dispatched = self._dispatch(
            monitor, queue, lock_manager, tracker, activated_at
        )

        assert dispatched is False
        # The end state, not the call: the issue is selectable again.
        assert queue.get_issue_status(ISSUE) == 'waiting'
        assert queue.get_next_n_waiting_issues(1)[0]['issue_number'] == ISSUE
        lock_manager.release_lock.assert_any_call(PROJECT, BOARD, ISSUE)

    def test_in_progress_execution_record_is_cleared(self, temp_state_dir):
        """REGRESSION (#147): record_execution_start() runs immediately before the
        enqueue. When the enqueue blows up, an orphan 'manual' in_progress probe
        used to survive for the life of the process (the stale-probe self-heal is
        scoped to pipeline_progression probes), making has_active_execution()
        permanently True - which disarms BOTH this rollback's own liveness guard
        and the stranded-'active' sweep for that issue."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)
        monitor.task_queue.enqueue.side_effect = RuntimeError("redis down")

        activated_at = queue.mark_issue_active(ISSUE)
        self._dispatch(monitor, queue, lock_manager, tracker, activated_at)

        assert tracker.has_active_execution(PROJECT, ISSUE) is False

    def test_successful_dispatch_leaves_the_entry_active(self, temp_state_dir):
        """Control: nothing failed, so nothing is undone."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)

        activated_at = queue.mark_issue_active(ISSUE)
        dispatched = self._dispatch(
            monitor, queue, lock_manager, tracker, activated_at
        )

        assert dispatched is True
        assert queue.get_issue_status(ISSUE) == 'active'
        lock_manager.release_lock.assert_not_called()

    def test_pending_task_for_the_issue_blocks_the_rollback(self, temp_state_dir):
        """trigger_agent_for_status() returns None when a task for this issue is
        already queued ("skipping duplicate"). A queued-but-unstarted task is
        invisible to has_active_execution(), so without a pending-queue check the
        rollback released the board lock and handed the entry back to the queue
        while that task was still waiting to run."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        existing_task = Mock()
        existing_task.agent = AGENT
        existing_task.context = {
            'issue_number': ISSUE, 'project': PROJECT, 'board': BOARD
        }

        monitor = _monitor(tracker)
        monitor.task_queue.get_pending_tasks.return_value = [existing_task]

        activated_at = queue.mark_issue_active(ISSUE)
        dispatched = self._dispatch(
            monitor, queue, lock_manager, tracker, activated_at
        )

        assert dispatched is False
        # Nothing undone: the queued task still needs the lock and the entry.
        assert queue.get_issue_status(ISSUE) == 'active'
        lock_manager.release_lock.assert_not_called()

    def test_refused_compare_and_swap_is_reported_at_critical(
        self, temp_state_dir, caplog
    ):
        """A genuinely concurrent re-activation makes the compare-and-swap refuse
        - correctly - but the entry is then left 'active' by a rollback that
        believed it had cleaned up. reset_issue_to_waiting() only logs INFO on its
        way out, so the site has to report the refusal itself or the stranded
        entry is invisible to an operator."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)
        monitor.trigger_agent_for_status = Mock(side_effect=RuntimeError("boom"))

        queue.mark_issue_active(ISSUE)
        stale_token = 'not-the-current-activation'

        with caplog.at_level('CRITICAL'):
            self._dispatch(monitor, queue, lock_manager, tracker, stale_token)

        assert queue.get_issue_status(ISSUE) == 'active'
        assert any(
            record.levelname == 'CRITICAL' and str(ISSUE) in record.getMessage()
            for record in caplog.records
        )


class TestResumedLoopIsNotRolledBack:
    """trigger_agent_for_status()'s discussions-workspace resume paths."""

    def test_resumed_feedback_loop_reports_dispatched_work(self, temp_state_dir):
        """REGRESSION (#147): the resume branch starts a background loop and then
        returned None, which told the rollback nothing had started. The
        has_active_execution() probe cannot correct that - the thread doesn't
        register itself until several GitHub round-trips later - so the rollback
        released the board lock and reset the queue entry out from under a live
        conversational loop. The Planning & Design board's trigger column IS
        conversational, so this was the ordinary path there."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        conversational = _column(col_type='conversational')
        monitor = _monitor(tracker, workspace='discussions',
                           columns=[conversational])

        github = Mock()
        state_manager = Mock()
        state_manager.get_discussion_for_issue.return_value = 'D_kwDO'
        monitor.discussions = Mock()
        monitor.discussions.get_discussion.return_value = {'id': 'D_kwDO'}

        activated_at = queue.mark_issue_active(ISSUE)

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('config.state_manager.state_manager', state_manager), \
             patch('services.github_integration.GitHubIntegration',
                   return_value=github), \
             patch('services.human_feedback_loop.human_feedback_loop_executor'), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch.object(ProjectMonitor, '_check_agent_processed_issue_sync',
                          return_value=True):

            # The discussions-workspace resume is gated on the comment-signature
            # check, which is awaited in a worker thread.
            async def _processed(discussion_id, agent):
                return True
            github.has_agent_processed_discussion = _processed

            dispatched = monitor._trigger_next_issue_with_rollback(
                PROJECT, BOARD, ISSUE, COLUMN, REPO,
                pipeline_queue=queue,
                activated_at=activated_at,
                lock_manager=lock_manager,
                lock_already_acquired=True,
            )

        assert dispatched is True
        # The loop owns the board and its queue slot until it finishes.
        assert queue.get_issue_status(ISSUE) == 'active'
        lock_manager.release_lock.assert_not_called()

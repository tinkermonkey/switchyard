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


def _lock_manager(retained_reason=None, retained_by=ISSUE):
    lock_manager = Mock()
    lock_manager.get_retained_reason.return_value = None
    lock_manager.get_lock.return_value = None
    lock_manager.release_lock.return_value = True

    lock = None
    if retained_reason is not None:
        lock = Mock()
        lock.locked_by_issue = retained_by
        lock.retained_reason = retained_reason
    # (lock_or_None, reads_healthy) - the rollback checks this before releasing,
    # because trigger_agent_for_status() can deliberately RETAIN the lock on its
    # way to returning None (mark_failed).
    lock_manager.get_lock_fail_closed.return_value = (lock, True)
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

    def test_refused_compare_and_swap_is_not_reported_at_critical(
        self, temp_state_dir, caplog
    ):
        """REGRESSION (#147 review): a refused compare-and-swap used to be paged
        as CRITICAL ("excluded from all future dispatch until a human
        intervenes"). Per reset_issue_to_waiting()'s own contract a refusal means
        another dispatcher legitimately re-activated the issue, so leaving the
        entry 'active' is the CORRECT outcome -- not something a human must fix.

        This matters in production, not just in theory: it is reachable through
        several routine interleavings. trigger_agent_for_status()'s
        MAX_CONSECUTIVE_DISPATCH_FAILURES branch resets the entry itself and
        returns None, and so does its no-agent-column branch -- both land here
        with an entry that is no longer 'active'."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)
        monitor.trigger_agent_for_status = Mock(side_effect=RuntimeError("boom"))

        queue.mark_issue_active(ISSUE)
        stale_token = 'not-the-current-activation'

        with caplog.at_level('DEBUG'):
            self._dispatch(monitor, queue, lock_manager, tracker, stale_token)

        # The entry is correctly left alone for whoever owns that activation...
        assert queue.get_issue_status(ISSUE) == 'active'
        # ...and no human is paged for it.
        assert not [r for r in caplog.records if r.levelname == 'CRITICAL']
        assert any('correct outcome' in r.getMessage() for r in caplog.records)

    def test_already_waiting_entry_is_not_reported_at_critical(
        self, temp_state_dir, caplog
    ):
        """Interleaving A/B from the #147 review, end to end: the entry was
        already returned to 'waiting' by the same call that declined the dispatch
        (mark_failed's reset, or the no-agent-column branch's), so the rollback's
        own CAS reset finds nothing active. That is a healthy state."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)
        token = queue.mark_issue_active(ISSUE)

        # trigger_agent_for_status() resets the entry itself, then declines.
        def _reset_then_decline(*args, **kwargs):
            queue.reset_issue_to_waiting(ISSUE)
            return None

        monitor.trigger_agent_for_status = Mock(side_effect=_reset_then_decline)

        with caplog.at_level('DEBUG'):
            self._dispatch(monitor, queue, lock_manager, tracker, token)

        assert queue.get_issue_status(ISSUE) == 'waiting'
        assert not [r for r in caplog.records if r.levelname == 'CRITICAL']


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


class TestRollbackRespectsARetainedLock:
    """REGRESSION (#147 review): the rollback called release_lock()
    unconditionally, on a lock trigger_agent_for_status() may have DELIBERATELY
    retained on its way to returning None.

    That path is real and routine: the MAX_CONSECUTIVE_DISPATCH_FAILURES branch
    calls PipelineRunManager.mark_failed(), which durably marks the lock
    retained-due-to-failure precisely so siblings on this board stay blocked
    until a human resolves it, and then returns None -- landing straight in this
    rollback. release_lock() happens to refuse a retained lock without
    force=True, but the rollback's correctness must not rest on a guard it never
    mentions, and the log gave an operator no way to tell a deliberate retention
    from a failure."""

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

    def test_does_not_attempt_to_release_a_retained_lock(self, temp_state_dir, caplog):
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager(
            retained_reason='3 consecutive dispatch failures for X in Development'
        )

        monitor = _monitor(tracker)
        monitor.trigger_agent_for_status = Mock(return_value=None)

        activated_at = queue.mark_issue_active(ISSUE)
        with caplog.at_level('DEBUG'):
            dispatched = self._dispatch(
                monitor, queue, lock_manager, tracker, activated_at
            )

        assert dispatched is False
        lock_manager.release_lock.assert_not_called()
        assert any(
            'retained due to a failed run' in r.getMessage()
            for r in caplog.records
        )

    def test_still_resets_the_queue_entry_when_the_lock_is_retained(
        self, temp_state_dir
    ):
        """The retention can belong to a DIFFERENT issue -- release_lock()'s
        retained guard is deliberately not holder-scoped -- so the entry for THIS
        issue must still go back to 'waiting'."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager(
            retained_reason='failed run on a sibling', retained_by=999
        )

        monitor = _monitor(tracker)
        monitor.trigger_agent_for_status = Mock(return_value=None)

        activated_at = queue.mark_issue_active(ISSUE)
        self._dispatch(monitor, queue, lock_manager, tracker, activated_at)

        assert queue.get_issue_status(ISSUE) == 'waiting'

    def test_releases_normally_when_the_lock_is_not_retained(self, temp_state_dir):
        """Control: the guard must not simply stop the release from ever firing."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        monitor = _monitor(tracker)
        monitor.trigger_agent_for_status = Mock(return_value=None)

        activated_at = queue.mark_issue_active(ISSUE)
        self._dispatch(monitor, queue, lock_manager, tracker, activated_at)

        lock_manager.release_lock.assert_any_call(PROJECT, BOARD, ISSUE)

    def test_unreadable_lock_state_defers_to_release_locks_own_guard(
        self, temp_state_dir
    ):
        """A read failure must not skip the release outright -- release_lock()
        repeats this check itself and fails closed on an unreadable lock."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()
        lock_manager.get_lock_fail_closed.side_effect = RuntimeError("redis down")

        monitor = _monitor(tracker)
        monitor.trigger_agent_for_status = Mock(return_value=None)

        activated_at = queue.mark_issue_active(ISSUE)
        self._dispatch(monitor, queue, lock_manager, tracker, activated_at)

        lock_manager.release_lock.assert_any_call(PROJECT, BOARD, ISSUE)


class TestPendingTaskSuppressionIsBounded:
    """REGRESSION (#147 review): _has_pending_task_for_issue() suppressed the
    rollback for ANY pending task matching the issue, with no agent match and no
    age bound.

    A single ORPHANED pending task for #N -- one no worker will ever run --
    therefore suppressed the rollback for #N forever. The lock is then never
    released, and _reset_stranded_active_issues() skips the lock holder by
    design, so nothing recovers it: bug #147's exact end state, reached through
    the guard added to fix it."""

    def _pending(self, agent=AGENT, issue_number=ISSUE, age_seconds=0):
        from datetime import timedelta
        task = Mock()
        task.id = f'task-{issue_number}'
        task.agent = agent
        task.created_at = (
            datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
        ).isoformat()
        task.context = {
            'issue_number': issue_number,
            'project': PROJECT,
            'board': BOARD,
        }
        return task

    def _monitor_with(self, pending):
        tracker = Mock()
        monitor = _monitor(tracker)
        monitor.task_queue.get_pending_tasks.side_effect = (
            lambda agent=None: [
                t for t in pending if agent is None or t.agent == agent
            ]
        )
        return monitor

    def test_recent_matching_task_still_suppresses(self):
        monitor = self._monitor_with([self._pending(age_seconds=5)])
        assert monitor._has_pending_task_for_issue(
            PROJECT, BOARD, ISSUE, agent=AGENT
        ) is True

    def test_orphaned_task_no_longer_suppresses_forever(self):
        """The whole point: past the suppression window the task is treated as
        orphaned, so the rollback can finally run and free the board."""
        from services.project_monitor import PENDING_TASK_SUPPRESSION_SECS

        monitor = self._monitor_with([
            self._pending(age_seconds=PENDING_TASK_SUPPRESSION_SECS + 60)
        ])
        assert monitor._has_pending_task_for_issue(
            PROJECT, BOARD, ISSUE, agent=AGENT
        ) is False

    def test_task_for_a_different_agent_does_not_suppress(self):
        """trigger_agent_for_status()'s duplicate-task branch compares
        `existing_task.agent == agent`. A pending task for some OTHER agent is
        not why this dispatch declined, so it must not veto the rollback."""
        monitor = self._monitor_with([self._pending(agent='code_reviewer')])
        assert monitor._has_pending_task_for_issue(
            PROJECT, BOARD, ISSUE, agent=AGENT
        ) is False

    def test_task_for_a_different_issue_does_not_suppress(self):
        monitor = self._monitor_with([self._pending(issue_number=999)])
        assert monitor._has_pending_task_for_issue(
            PROJECT, BOARD, ISSUE, agent=AGENT
        ) is False

    def test_unparseable_timestamp_is_treated_as_recent(self):
        """Fail safe on the age check itself: a format change must not silently
        disable the guard."""
        task = self._pending()
        task.created_at = 'not-a-timestamp'
        monitor = self._monitor_with([task])
        assert monitor._has_pending_task_for_issue(
            PROJECT, BOARD, ISSUE, agent=AGENT
        ) is True

    def test_unreadable_queue_still_fails_closed(self):
        monitor = _monitor(Mock())
        monitor.task_queue.get_pending_tasks.side_effect = RuntimeError("redis down")
        assert monitor._has_pending_task_for_issue(
            PROJECT, BOARD, ISSUE, agent=AGENT
        ) is True

    def test_orphaned_task_does_not_block_the_end_to_end_rollback(
        self, temp_state_dir
    ):
        """End state, not just the predicate: with only an orphaned pending task
        for #N, a no-op dispatch must still release the lock and return the entry
        to 'waiting'."""
        from datetime import timedelta
        from services.project_monitor import PENDING_TASK_SUPPRESSION_SECS

        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])
        lock_manager = _lock_manager()

        orphan = Mock()
        orphan.id = 'orphan'
        orphan.agent = AGENT
        orphan.created_at = (
            datetime.now(timezone.utc)
            - timedelta(seconds=PENDING_TASK_SUPPRESSION_SECS + 60)
        ).isoformat()
        orphan.context = {
            'issue_number': ISSUE, 'project': PROJECT, 'board': BOARD
        }

        monitor = _monitor(tracker)
        monitor.task_queue.get_pending_tasks.side_effect = (
            lambda agent=None: [orphan]
        )
        monitor.trigger_agent_for_status = Mock(return_value=None)

        activated_at = queue.mark_issue_active(ISSUE)
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
                   return_value=lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.cancellation.get_cancellation_signal'):
            dispatched = monitor._trigger_next_issue_with_rollback(
                PROJECT, BOARD, ISSUE, COLUMN, REPO,
                pipeline_queue=queue,
                activated_at=activated_at,
                lock_manager=lock_manager,
                lock_already_acquired=True,
            )

        assert dispatched is False
        assert queue.get_issue_status(ISSUE) == 'waiting'
        lock_manager.release_lock.assert_any_call(PROJECT, BOARD, ISSUE)


class TestObservabilityFailureDoesNotCountAsDispatchFailure:
    """REGRESSION (#147 review): emit_task_queued() sat inside the try whose
    handler records outcome='failure' for this issue/column/agent.

    That record feeds count_consecutive_failures(); three such polls trip
    MAX_CONSECUTIVE_DISPATCH_FAILURES, which calls mark_failed() -- retaining the
    board lock and requiring human recovery. An Elasticsearch/observability
    outage could therefore halt a perfectly healthy board."""

    def test_emit_failure_does_not_fail_the_dispatch(self, temp_state_dir):
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])

        monitor = _monitor(tracker)
        monitor.decision_events.emit_task_queued.side_effect = RuntimeError(
            "elasticsearch unreachable"
        )

        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.cancellation.get_cancellation_signal'):
            agent = monitor.trigger_agent_for_status(
                PROJECT, BOARD, ISSUE, COLUMN, REPO,
                lock_already_acquired=True,
                raise_on_error=True,
            )

        # The dispatch completed: the task was enqueued and the agent returned.
        assert agent == AGENT
        monitor.task_queue.enqueue.assert_called_once()

        # And crucially, NO failure outcome was recorded -- nothing feeds
        # count_consecutive_failures() from an observability outage.
        history = tracker.load_state(PROJECT, ISSUE)['execution_history']
        assert not [e for e in history if e.get('outcome') == 'failure']

    def test_enqueue_failure_is_still_recorded_as_a_failure(self, temp_state_dir):
        """Control: the enqueue itself blowing up must still be undone and
        recorded -- that is the case the try was written for."""
        tracker = WorkExecutionStateTracker(state_dir=temp_state_dir / 'exec')
        queue = _queue_manager(temp_state_dir / 'queue', [_waiting()])

        monitor = _monitor(tracker)
        monitor.task_queue.enqueue.side_effect = RuntimeError("redis down")

        with patch('services.pipeline_queue_manager.get_pipeline_queue_manager',
                   return_value=queue), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.cancellation.get_cancellation_signal'), \
             pytest.raises(RuntimeError):
            monitor.trigger_agent_for_status(
                PROJECT, BOARD, ISSUE, COLUMN, REPO,
                lock_already_acquired=True,
                raise_on_error=True,
            )

        history = tracker.load_state(PROJECT, ISSUE)['execution_history']
        assert [e for e in history if e.get('outcome') == 'failure']

"""
Unit tests for PipelineRunManager.end_pipeline_run()'s post-release
"dispatch next queued issue" block.

Phase 2 (issue #57, parent #88, umbrella #34): this is one of the "get next
-> try_acquire_lock -> mark_issue_active" dispatch call sites generalized to
loop over available slots (get_next_waiting_issue() -> get_next_n_waiting_
issues(n)). "available_slots" is hardcoded to 1 today -- PipelineLockManager
still enforces exactly one concurrent issue per (project, board) -- so this
must drive the exact same single-dispatch behavior as the pre-#57
get_next_waiting_issue()-based implementation.

No live Redis/Elasticsearch/Docker/network access - everything is mocked,
following the pattern in tests/unit/services/test_pipeline_failure_durability.py.
"""
import json
import unittest
from unittest.mock import MagicMock, Mock, patch

from services.pipeline_run import PipelineRunManager

# The activated_at stamp mark_issue_active() returns and the rollback hands
# back to reset_issue_to_waiting() as its compare-and-swap token.
ACTIVATED_AT = '2026-01-01T00:00:00+00:00'


class TestEndPipelineRunDispatchNextSlots(unittest.TestCase):
    def setUp(self):
        self.mock_es = MagicMock()
        self.mock_es.search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}
        self.mock_redis = MagicMock()
        with patch('services.pipeline_run.Elasticsearch', return_value=self.mock_es), \
             patch('services.pipeline_run.redis.Redis', return_value=self.mock_redis):
            self.manager = PipelineRunManager()
        self.manager.es = self.mock_es
        self.manager.redis = self.mock_redis

    def _make_active_run(self, project, board, issue_number):
        run = self.manager.create_pipeline_run(
            issue_number=issue_number, issue_title="t", issue_url="u",
            project=project, board=board,
        )
        self.mock_redis.hget.return_value = run.id
        self.mock_redis.get.side_effect = lambda key: (
            json.dumps(run.to_dict()) if key == self.manager._get_redis_key(run.id) else None
        )
        return run

    def _project_config(self, board):
        pipeline_config = Mock()
        pipeline_config.board_name = board
        pipeline_config.name = "sdlc"
        pipeline_config.workflow = "sdlc_execution_workflow"

        project_config = Mock()
        project_config.pipelines = [pipeline_config]
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
        return project_config

    def _workflow_template(self, agent='senior_software_engineer'):
        column = Mock()
        column.name = 'Development'
        column.agent = agent
        template = Mock()
        template.columns = [column]
        return template

    def test_dispatches_single_next_queued_issue_at_capacity_one(self):
        """Byte-identical-at-capacity-1 check: with exactly one waiting
        issue queued, ending the run must release the lock, query
        get_next_n_waiting_issues(1), acquire the lock for the returned
        issue, mark it active, and enqueue a task for it."""
        self._make_active_run("proj", "board", 100)

        our_lock = Mock()
        our_lock.lock_status = 'locked'
        our_lock.locked_by_issue = 100

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = MagicMock()
        mock_queue.mark_issue_active.return_value = ACTIVATED_AT
        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0}
        ]

        project_config = self._project_config("board")
        workflow_template = self._workflow_template()

        mock_config_manager_instance = Mock()
        mock_config_manager_instance.get_project_config.return_value = project_config
        mock_config_manager_instance.get_workflow_template.return_value = workflow_template

        self.manager._resolve_issue_column_from_github = Mock(return_value=('Development', True))
        self.manager.ensure_pipeline_run_for_task = Mock(return_value='run-200')

        mock_gh_result = Mock()
        mock_gh_result.stdout = json.dumps({'title': 'Next issue', 'body': '', 'url': 'https://x'})

        mock_task_queue_instance = MagicMock()

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('config.manager.ConfigManager', return_value=mock_config_manager_instance), \
             patch('subprocess.run', return_value=mock_gh_result), \
             patch('task_queue.task_manager.TaskQueue', return_value=mock_task_queue_instance):

            ended = self.manager.end_pipeline_run(
                project="proj", issue_number=100,
                reason="done", retain_lock=False, outcome="success",
            )

        self.assertTrue(ended)

        # Lock released for the completed issue
        mock_lock_manager.release_lock.assert_called_once_with("proj", "board", 100)

        # Queried for exactly 1 slot (today's hardcoded available_slots)
        mock_queue.get_next_n_waiting_issues.assert_called_once_with(1)

        # Lock acquired for the next queued issue
        mock_lock_manager.try_acquire_lock.assert_called_once_with(
            project="proj", board="board", issue_number=200
        )

        # Next issue marked active and a task dispatched for it
        mock_queue.mark_issue_active.assert_called_once_with(200)
        mock_task_queue_instance.enqueue.assert_called_once()
        dispatched_task = mock_task_queue_instance.enqueue.call_args[0][0]
        self.assertEqual(dispatched_task.context['issue_number'], 200)
        self.assertEqual(dispatched_task.context['trigger'], 'lock_release_queue_processing')

    def test_no_dispatch_when_queue_empty(self):
        """Control case: empty queue (get_next_n_waiting_issues(1) -> [])
        must still release the lock but dispatch nothing - matching the
        pre-#57 get_next_waiting_issue() -> None behavior."""
        self._make_active_run("proj", "board", 100)

        our_lock = Mock()
        our_lock.lock_status = 'locked'
        our_lock.locked_by_issue = 100

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True

        mock_queue = MagicMock()
        mock_queue.get_next_n_waiting_issues.return_value = []

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('monitoring.observability.get_observability_manager'):

            ended = self.manager.end_pipeline_run(
                project="proj", issue_number=100,
                reason="done", retain_lock=False, outcome="success",
            )

        self.assertTrue(ended)
        mock_lock_manager.release_lock.assert_called_once_with("proj", "board", 100)
        mock_queue.get_next_n_waiting_issues.assert_called_once_with(1)
        mock_queue.mark_issue_active.assert_not_called()
        mock_lock_manager.try_acquire_lock.assert_not_called()

    # --- Issue #142: dispatch-failure rollback -----------------------------
    # When dispatch fails after mark_issue_active(), BOTH halves of the
    # acquisition must be rolled back. Rolling back only the lock (the pre-fix
    # behavior) leaves the queue entry at status='active', and
    # get_next_n_waiting_issues() selects strictly on status=='waiting' -- so
    # the issue is silently excluded from every future dispatch, forever, with
    # no automated recovery.

    def _dispatch_rollback_mocks(self):
        our_lock = Mock()
        our_lock.lock_status = 'locked'
        our_lock.locked_by_issue = 100

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock.return_value = our_lock
        mock_lock_manager.release_lock.return_value = True
        mock_lock_manager.try_acquire_lock.return_value = (True, "lock_acquired")

        mock_queue = MagicMock()
        mock_queue.mark_issue_active.return_value = ACTIVATED_AT
        mock_queue.get_next_n_waiting_issues.return_value = [
            {'issue_number': 200, 'position_in_column': 0}
        ]
        return mock_lock_manager, mock_queue

    def _end_run_with_failing_dispatch(self, mock_lock_manager, mock_queue):
        """Drive end_pipeline_run() through a dispatch that fails partway.

        ensure_pipeline_run_for_task() returning None is one of the real
        failure modes the dispatch block raises on.
        """
        self._make_active_run("proj", "board", 100)

        mock_config_manager_instance = Mock()
        mock_config_manager_instance.get_project_config.return_value = self._project_config("board")
        mock_config_manager_instance.get_workflow_template.return_value = self._workflow_template()

        self.manager._resolve_issue_column_from_github = Mock(return_value=('Development', True))
        self.manager.ensure_pipeline_run_for_task = Mock(return_value=None)

        mock_gh_result = Mock()
        mock_gh_result.stdout = json.dumps({'title': 'Next issue', 'body': '', 'url': 'https://x'})

        mock_task_queue_instance = MagicMock()

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('config.manager.ConfigManager', return_value=mock_config_manager_instance), \
             patch('subprocess.run', return_value=mock_gh_result), \
             patch('task_queue.task_manager.TaskQueue', return_value=mock_task_queue_instance):

            ended = self.manager.end_pipeline_run(
                project="proj", issue_number=100,
                reason="done", retain_lock=False, outcome="success",
            )

        return ended, mock_task_queue_instance

    def test_resets_queue_entry_to_waiting_when_dispatch_fails(self):
        """REGRESSION (#142): a failed dispatch must reset the queue entry back
        to 'waiting', not just release the lock."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()

        ended, mock_task_queue_instance = self._end_run_with_failing_dispatch(
            mock_lock_manager, mock_queue
        )

        self.assertTrue(ended)
        # The issue was marked active, then dispatch failed before any enqueue.
        mock_queue.mark_issue_active.assert_called_once_with(200)
        mock_task_queue_instance.enqueue.assert_not_called()

        # Both halves rolled back: the lock AND the queue entry.
        self.assertIn(
            unittest.mock.call("proj", "board", 200),
            mock_lock_manager.release_lock.call_args_list,
        )
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

    def test_resets_queue_entry_even_when_lock_rollback_fails(self):
        """The queue reset is not gated on the lock release succeeding: leaving
        the entry 'active' guarantees permanent loss of the issue whether or not
        the lock came free."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()
        # First call releases the completed issue's lock; the second is the
        # rollback attempt for the next issue.
        mock_lock_manager.release_lock.side_effect = [True, RuntimeError("redis down")]

        self._end_run_with_failing_dispatch(mock_lock_manager, mock_queue)

        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

    def test_rollback_releases_the_lock_before_the_compare_and_swap_reset(self):
        """ORDER REGRESSION (#147): the reset must NOT run while the lock is
        still held. try_acquire_lock() returns True/"already_holds_lock" for the
        current holder and ProjectMonitor.trigger_agent_for_status() dispatches
        on that branch, so a competing poll can genuinely start #200 in that
        window -- and the unconditional release that follows would then free the
        lock out from under a running agent. Releasing first opens the symmetric
        window ("lock free, entry still 'active'"), which the activated_at
        compare-and-swap closes instead."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()

        call_order = []
        mock_queue.reset_issue_to_waiting.side_effect = (
            lambda *a, **kw: call_order.append('reset')
        )
        mock_lock_manager.release_lock.side_effect = (
            lambda *a, **kw: call_order.append(f'release-{a[2]}') or True
        )

        self._end_run_with_failing_dispatch(mock_lock_manager, mock_queue)

        # release-100 is the completed issue's own release, before dispatch.
        self.assertEqual(call_order, ['release-100', 'release-200', 'reset'])

    def test_rollback_still_releases_lock_when_queue_reset_raises(self):
        """A failing reset loses one issue; a retained lock deadlocks the whole
        board. The release runs first and unconditionally so the second can never
        happen because of the first."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()
        mock_queue.reset_issue_to_waiting.side_effect = RuntimeError("queue file unwritable")

        self._end_run_with_failing_dispatch(mock_lock_manager, mock_queue)

        self.assertIn(
            unittest.mock.call("proj", "board", 200),
            mock_lock_manager.release_lock.call_args_list,
        )

    def test_rolls_back_lock_when_mark_issue_active_raises(self):
        """REGRESSION (#147): mark_issue_active() sat ABOVE the try/except the
        rollback hangs off, so an fcntl/YAML write failure (ENOSPC, a read-only
        or full state/ bind mount, a permissions change) jumped straight past the
        rollback to the outer handler, which only logs -- leaving the lock held
        by #200 with nothing dispatched. That is the very deadlock this block
        exists to prevent, one statement above the guard.

        Nothing was stamped, so there is no CAS token and no reset to make; the
        lock release is what must still happen."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()
        mock_queue.mark_issue_active.side_effect = OSError("[Errno 28] No space left on device")

        self._end_run_with_failing_dispatch(mock_lock_manager, mock_queue)

        self.assertIn(
            unittest.mock.call("proj", "board", 200),
            mock_lock_manager.release_lock.call_args_list,
        )
        # No token was ever stamped, so resetting could only clobber someone
        # else's activation.
        mock_queue.reset_issue_to_waiting.assert_not_called()

    def test_dispatch_deferred_message_distinguishes_a_failed_board_read(self):
        """REGRESSION (#147): the column resolver returned a bare None for BOTH
        "the issue isn't on this board" and "the board query blew up", so a rate
        limit or a network blip was reported to the operator as a missing card.
        The (column, reads_healthy) pair keeps them apart."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()
        self._make_active_run("proj", "board", 100)

        mock_config_manager_instance = Mock()
        mock_config_manager_instance.get_project_config.return_value = self._project_config("board")
        mock_config_manager_instance.get_workflow_template.return_value = self._workflow_template()

        # (None, False) == "couldn't read the board", not "not on the board".
        self.manager._resolve_issue_column_from_github = Mock(return_value=(None, False))

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('config.manager.ConfigManager', return_value=mock_config_manager_instance), \
             self.assertLogs('services.pipeline_run', level='ERROR') as logs:

            self.manager.end_pipeline_run(
                project="proj", issue_number=100,
                reason="done", retain_lock=False, outcome="success",
            )

        joined = "\n".join(logs.output)
        self.assertIn("Could not read board", joined)
        self.assertNotIn("not found on board", joined)

        # Still fully rolled back so the next exit retries.
        self.assertIn(
            unittest.mock.call("proj", "board", 200),
            mock_lock_manager.release_lock.call_args_list,
        )
        mock_queue.reset_issue_to_waiting.assert_called_once_with(
            200, expected_activated_at=ACTIVATED_AT
        )

    def test_no_queue_reset_on_successful_dispatch(self):
        """Control case: a dispatch that succeeds must NOT reset the entry it
        just marked active."""
        mock_lock_manager, mock_queue = self._dispatch_rollback_mocks()
        self._make_active_run("proj", "board", 100)

        mock_config_manager_instance = Mock()
        mock_config_manager_instance.get_project_config.return_value = self._project_config("board")
        mock_config_manager_instance.get_workflow_template.return_value = self._workflow_template()

        self.manager._resolve_issue_column_from_github = Mock(return_value=('Development', True))
        self.manager.ensure_pipeline_run_for_task = Mock(return_value='run-200')

        mock_gh_result = Mock()
        mock_gh_result.stdout = json.dumps({'title': 'Next issue', 'body': '', 'url': 'https://x'})

        mock_task_queue_instance = MagicMock()

        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal'), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('config.manager.ConfigManager', return_value=mock_config_manager_instance), \
             patch('subprocess.run', return_value=mock_gh_result), \
             patch('task_queue.task_manager.TaskQueue', return_value=mock_task_queue_instance):

            self.manager.end_pipeline_run(
                project="proj", issue_number=100,
                reason="done", retain_lock=False, outcome="success",
            )

        mock_task_queue_instance.enqueue.assert_called_once()
        mock_queue.reset_issue_to_waiting.assert_not_called()


if __name__ == '__main__':
    unittest.main()

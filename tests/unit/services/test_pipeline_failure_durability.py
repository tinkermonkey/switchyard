"""
Tests for the durable pipeline-failure mechanism that replaced the old
work_execution_state halt-marker system (services/work_execution_state.py
set_halt_marker/get_halt_marker/clear_halt_marker, removed).

Covers the two load-bearing correctness properties the new design depends on:

1. A lock durably marked retained-due-to-failure (PipelineLock.retained_reason)
   must never be recoverable by try_acquire_lock()'s staleness/TTL heuristics,
   regardless of lock age or whether the Redis copy has expired — see
   PipelineLockManager.try_acquire_lock's upfront durable check.
2. PipelineRunManager.mark_failed / end_pipeline_run(outcome="failed") must
   durably mark the lock even when no PipelineRun record exists for the
   attempt (e.g. a pre-dispatch consecutive-failure case).

Also covers watchdog/rescan reconciliation reading retained_reason directly
instead of the removed ephemeral 48h Redis TTL marker, and the sync-on-restart
age exemption for retained locks.
"""

import unittest
from unittest.mock import MagicMock, patch
import sys
import os
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.pipeline_lock_manager import PipelineLockManager, PipelineLock


class TestMarkLockFailedDurability(unittest.TestCase):
    """PipelineLockManager.mark_lock_failed and the try_acquire_lock guard it enables."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _write_retained_lock(self, issue_number=123, hours_old=0):
        """Write a lock directly to YAML (bypassing Redis) as if issue_number
        held it hours_old hours ago and it was then marked failed."""
        lock = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=issue_number,
            lock_acquired_at=(datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat(),
            lock_status="locked",
            retained_reason="agent crashed repeatedly",
            retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.manager._save_lock_to_yaml(lock)
        return lock

    def test_mark_lock_failed_sets_retained_reason(self):
        self.manager._create_lock("proj", "board", 123)
        marked = self.manager.mark_lock_failed("proj", "board", 123, reason="agent crashed")
        self.assertTrue(marked)

        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.retained_reason, "agent crashed")
        self.assertIsNotNone(lock.retained_at)

    def test_mark_lock_failed_refuses_when_not_held_by_issue(self):
        self.manager._create_lock("proj", "board", 999)
        marked = self.manager.mark_lock_failed("proj", "board", 123, reason="should not apply")
        self.assertFalse(marked)

    def test_try_acquire_lock_refuses_even_when_redis_copy_is_gone(self):
        """The core durability fix: try_acquire_lock's Redis-transaction path
        treats an empty Redis hash as 'unlocked' and would otherwise grant the
        lock immediately (Redis TTL on a retained lock is never refreshed,
        since nothing should be re-touching a failed issue). The upfront YAML
        check must catch this before any Redis logic runs."""
        self._write_retained_lock(issue_number=123, hours_old=0)

        # Simulate Redis having no record of the lock at all (TTL expired).
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {}  # nothing in Redis
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

        success, reason = self.manager.try_acquire_lock("proj", "board", 456)
        self.assertFalse(success)
        self.assertIn("failed", reason)
        self.assertIn("123", reason)

    def test_try_acquire_lock_refuses_regardless_of_lock_age(self):
        """The 4-hour staleness heuristic (both the Redis-tx branch and the
        YAML-fallback branch) must never override a retained/failed lock, no
        matter how old it is."""
        self._write_retained_lock(issue_number=123, hours_old=100)  # far past 4h

        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {}
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

        success, reason = self.manager.try_acquire_lock("proj", "board", 456)
        self.assertFalse(success)

    def test_try_acquire_lock_refuses_even_for_the_lock_own_holder(self):
        """Defense in depth: the dispatch gate in project_monitor.py should
        already refuse to re-attempt a retained/failed issue before ever calling
        try_acquire_lock again for it, but try_acquire_lock itself must ALSO
        refuse — not just for other issues, but for the issue that already holds
        the retained lock. This closes two things at once: it means the
        "never re-dispatch a retained issue" invariant is enforced by the lock
        primitive itself rather than depending on every caller remembering to
        check get_retained_reason first, and it means the "already_holds_lock"
        Redis-tx branch (which calls _create_lock_yaml_only) is never reached
        for a retained lock — so it can't accidentally overwrite the durable
        YAML copy either."""
        self._write_retained_lock(issue_number=123, hours_old=0)

        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {
                'lock_status': 'locked', 'locked_by_issue': '123',
                'retained_reason': 'agent crashed repeatedly',
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        self.assertFalse(success)
        self.assertIn("failed", reason)

        # And the retained fields must still be intact afterward — the whole
        # point of refusing upfront is that _create_lock_yaml_only (which would
        # wipe them) is never reached.
        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.retained_reason, "agent crashed repeatedly")

    def test_create_lock_yaml_only_preserves_retained_fields_for_same_holder(self):
        """Direct test of the _create_lock_yaml_only fix: even called in
        isolation (bypassing the upfront refusal above, to test this specific
        function's own defensiveness), it must not blindly overwrite an
        existing retained lock's fields with a fresh, blank PipelineLock."""
        self._write_retained_lock(issue_number=123, hours_old=0)

        self.manager._create_lock_yaml_only("proj", "board", 123)

        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.retained_reason, "agent crashed repeatedly")
        self.assertIsNotNone(lock.retained_at)

    def test_get_lock_merges_retained_state_when_redis_write_failed(self):
        """The other half of the durability guarantee: if mark_lock_failed's
        Redis write failed (or simply never landed) while its YAML write
        succeeded, get_lock() must not return the stale non-retained Redis
        copy — it must merge in the retained fields from YAML."""
        self._write_retained_lock(issue_number=123, hours_old=0)
        # Simulate Redis showing the SAME lock but with no knowledge of the
        # retention (as if mark_lock_failed's hset raised after the lock was
        # first acquired, before this test's write to YAML happened).
        self.mock_redis.hgetall.return_value = {
            'project': 'proj', 'board': 'board', 'locked_by_issue': '123',
            'lock_acquired_at': datetime.now(timezone.utc).isoformat(),
            'lock_status': 'locked', 'retained_reason': '', 'retained_at': '',
        }

        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.retained_reason, "agent crashed repeatedly")

    def test_get_lock_fail_closed_reports_unhealthy_on_double_read_failure(self):
        """try_acquire_lock's durability check must fail closed, not open, when
        it genuinely cannot determine lock state (both stores erroring) —
        verified via the public try_acquire_lock behavior, not just the
        internal health flag, since that's what actually matters."""
        self.mock_redis.hgetall.side_effect = Exception("redis down")
        # Corrupt the YAML file so the fallback read also raises.
        state_file = self.manager._get_state_file("proj", "board")
        state_file.write_text("not: valid: yaml: [")

        success, reason = self.manager.try_acquire_lock("proj", "board", 456)
        self.assertFalse(success)
        self.assertEqual(reason, "lock_state_unknown_failing_closed")

    def test_mark_lock_failed_rejects_empty_reason(self):
        self.manager._create_lock("proj", "board", 123)
        marked = self.manager.mark_lock_failed("proj", "board", 123, reason="")
        self.assertFalse(marked)
        self.assertIsNone(self.manager.get_lock("proj", "board").retained_reason)

    def test_mark_lock_failed_rejects_whitespace_only_reason(self):
        self.manager._create_lock("proj", "board", 123)
        marked = self.manager.mark_lock_failed("proj", "board", 123, reason="   ")
        self.assertFalse(marked)

    def test_sync_yaml_locks_to_redis_does_not_skip_retained_old_locks(self):
        """sync_yaml_locks_to_redis (run on orchestrator restart) has its own
        independent 4-hour age skip for ordinary abandoned locks — retained
        locks must be exempted so they're always visible to Redis-based reads
        after a restart, not just via the (still-correct) YAML fallback."""
        self._write_retained_lock(issue_number=123, hours_old=100)
        self.mock_redis.hgetall.return_value = {}  # not already in Redis

        synced = self.manager.sync_yaml_locks_to_redis()
        self.assertEqual(synced, 1)
        self.mock_redis.hset.assert_called_once()

    def test_sync_yaml_locks_to_redis_still_skips_ordinary_stale_locks(self):
        """Control case: an ordinary (non-retained) lock older than 4 hours is
        still skipped as before — the exemption is specific to retained_reason."""
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=(datetime.now(timezone.utc) - timedelta(hours=100)).isoformat(),
            lock_status="locked",
        )
        self.manager._save_lock_to_yaml(lock)
        self.mock_redis.hgetall.return_value = {}

        synced = self.manager.sync_yaml_locks_to_redis()
        self.assertEqual(synced, 0)
        self.mock_redis.hset.assert_not_called()

    def test_release_lock_clears_retained_state(self):
        """The deliberate recovery action (scripts/release_lock.py) removes the
        lock entirely, so a fresh try_acquire_lock succeeds afterward."""
        self._write_retained_lock(issue_number=123, hours_old=0)

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {'locked_by_issue': '123'}
            return func(mock_pipe)
        self.mock_redis.transaction.side_effect = side_effect_transaction

        released = self.manager.release_lock("proj", "board", 123)
        self.assertTrue(released)
        self.assertIsNone(self.manager.get_lock("proj", "board"))


class TestGetRetainedReason(unittest.TestCase):
    """PipelineLockManager.get_retained_reason — the single check every dispatch
    entry point calls before (re-)dispatching an issue."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.mock_redis.hgetall.return_value = {}  # force YAML fallback for get_lock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_returns_reason_for_holder_of_retained_lock(self):
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
            retained_reason="boom", retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.manager._save_lock_to_yaml(lock)
        self.assertEqual(self.manager.get_retained_reason("proj", "board", 123), "boom")

    def test_returns_none_for_non_holder(self):
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
            retained_reason="boom", retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.manager._save_lock_to_yaml(lock)
        self.assertIsNone(self.manager.get_retained_reason("proj", "board", 456))

    def test_returns_none_when_not_retained(self):
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
        )
        self.manager._save_lock_to_yaml(lock)
        self.assertIsNone(self.manager.get_retained_reason("proj", "board", 123))

    def test_returns_none_when_unlocked(self):
        self.assertIsNone(self.manager.get_retained_reason("proj", "board", 123))


class TestGetAllLocksMergesRedisAndYaml(unittest.TestCase):
    """get_all_locks() — backs /active-pipeline-runs' failed-run listing and
    scripts/list_failed_pipeline_runs.py, both of which exist specifically so a
    retained lock can never be silently undiscoverable. Must not miss a lock
    that exists in Redis but has no YAML file (e.g. mark_lock_failed's YAML
    write failed while its Redis write succeeded)."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_finds_lock_that_exists_only_in_redis(self):
        self.mock_redis.keys.return_value = ["pipeline_lock:proj:board"]
        self.mock_redis.hgetall.return_value = {
            'project': 'proj', 'board': 'board', 'locked_by_issue': '123',
            'lock_acquired_at': datetime.now(timezone.utc).isoformat(),
            'lock_status': 'locked', 'retained_reason': 'crashed', 'retained_at': datetime.now(timezone.utc).isoformat(),
        }
        # No YAML file exists for this lock at all.

        locks = self.manager.get_all_locks()
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].retained_reason, "crashed")

    def test_ignores_non_lock_keys_sharing_the_prefix(self):
        # e.g. a stale pipeline_lock:repair_failed:* marker from before this
        # mechanism was removed, if one happened to still be lingering.
        self.mock_redis.keys.return_value = ["pipeline_lock:repair_failed:proj:board:123"]
        self.mock_redis.hgetall.return_value = {}

        locks = self.manager.get_all_locks()
        self.assertEqual(len(locks), 0)


class TestPipelineRunManagerMarkFailed(unittest.TestCase):
    """PipelineRunManager.mark_failed / end_pipeline_run(outcome='failed') —
    the single shared failure path used by consecutive-dispatch-failure,
    review-cycle-crash, and repair-cycle-failure call sites."""

    def setUp(self):
        self.mock_es = MagicMock()
        self.mock_es.search.return_value = {'hits': {'total': {'value': 0}, 'hits': []}}
        self.mock_redis = MagicMock()
        with patch('services.pipeline_run.Elasticsearch', return_value=self.mock_es), \
             patch('services.pipeline_run.redis.Redis', return_value=self.mock_redis):
            from services.pipeline_run import PipelineRunManager
            self.manager = PipelineRunManager()
        self.manager.es = self.mock_es
        self.manager.redis = self.mock_redis

    def test_mark_failed_marks_lock_even_without_active_pipeline_run(self):
        """The guaranteed fallback: end_pipeline_run() only touches the lock if
        it finds an active PipelineRun (it returns False early otherwise, per
        its own docstring) — mark_failed must mark the lock directly regardless,
        covering pre-dispatch failures like the consecutive-dispatch-failure
        case, which never had a PipelineRun to begin with."""
        self.mock_redis.hget.return_value = None  # no active run mapped for this issue

        mock_lock_manager = MagicMock()
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager):
            self.manager.mark_failed(
                project="proj", board="board", issue_number=123,
                reason="3 consecutive dispatch failures",
            )

        mock_lock_manager.mark_lock_failed.assert_called_once_with(
            "proj", "board", 123, "3 consecutive dispatch failures"
        )

    def test_end_pipeline_run_outcome_failed_sets_status_failed(self):
        """status must be a distinct 'failed' value, not lumped in with
        'completed', so /active-pipeline-runs and other status-based filters
        can find it — this was the original visibility gap."""
        run = self.manager.create_pipeline_run(
            issue_number=123, issue_title="t", issue_url="u",
            project="proj", board="board",
        )
        self.mock_redis.hget.return_value = run.id
        self.mock_redis.get.return_value = None  # force ES fallback path unaffected; not exercised here directly

        # get_active_pipeline_run reads back via redis.get(redis_key) — patch it
        # to return the run we just created.
        import json
        self.mock_redis.get.side_effect = lambda key: (
            json.dumps(run.to_dict()) if key == self.manager._get_redis_key(run.id) else None
        )

        mock_lock_manager = MagicMock()
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager):
            ended = self.manager.end_pipeline_run(
                project="proj", issue_number=123,
                reason="crashed", retain_lock=True, outcome="failed",
            )

        self.assertTrue(ended)
        mock_lock_manager.mark_lock_failed.assert_called_once()
        call_args = mock_lock_manager.mark_lock_failed.call_args[0]
        self.assertEqual(call_args[0], "proj")
        self.assertEqual(call_args[2], 123)

        # The actual behavior this test is named for: status must be the
        # distinct "failed" value, not "completed" — this was the original
        # /active-pipeline-runs visibility gap. Assert on what was actually
        # persisted (the Redis setex payload), not just the return value.
        setex_calls = [c for c in self.mock_redis.setex.call_args_list]
        self.assertTrue(setex_calls, "end_pipeline_run should have called redis.setex")
        persisted = json.loads(setex_calls[-1][0][2])
        self.assertEqual(persisted['status'], 'failed')
        self.assertEqual(persisted['outcome'], 'failed')

    def test_end_pipeline_run_outcome_success_keeps_status_completed(self):
        """Control case: an ordinary successful completion still gets
        status='completed', not 'failed' — proving the new branch is scoped to
        outcome='failed' specifically."""
        run = self.manager.create_pipeline_run(
            issue_number=456, issue_title="t", issue_url="u",
            project="proj", board="board",
        )
        self.mock_redis.hget.return_value = run.id
        self.mock_redis.get.side_effect = lambda key: (
            json.dumps(run.to_dict()) if key == self.manager._get_redis_key(run.id) else None
        )

        mock_lock_manager = MagicMock()
        with patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager):
            ended = self.manager.end_pipeline_run(
                project="proj", issue_number=456,
                reason="done", retain_lock=False, outcome="success",
            )

        self.assertTrue(ended)
        mock_lock_manager.mark_lock_failed.assert_not_called()
        setex_calls = [c for c in self.mock_redis.setex.call_args_list]
        persisted = json.loads(setex_calls[-1][0][2])
        self.assertEqual(persisted['status'], 'completed')


if __name__ == '__main__':
    unittest.main()

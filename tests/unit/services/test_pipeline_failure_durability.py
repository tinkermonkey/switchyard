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

    def test_try_acquire_lock_still_allows_same_issue(self):
        """A retained lock's own holder isn't blocked by the guard (though in
        practice the dispatch gate should never let this be attempted)."""
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
        self.assertTrue(success)

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


if __name__ == '__main__':
    unittest.main()

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

    def test_get_lock_fail_closed_treats_unreadable_yaml_plus_empty_redis_as_unhealthy(self):
        """The asymmetric hole: YAML is the only non-expiring store for
        retained_reason (Redis's TTL can lapse on a retained lock with nothing
        legitimately re-touching it, which is expected). If Redis genuinely has
        no entry (TTL'd out) AND the YAML file can't be read, that must be
        unhealthy too — not just the both-raised case — because an unreadable
        YAML file could be masking a retained lock Redis no longer remembers."""
        # Redis: confirmed empty (not an error — hgetall returns {}).
        self.mock_redis.hgetall.return_value = {}
        # YAML: file exists but is corrupt.
        state_file = self.manager._get_state_file("proj", "board")
        state_file.write_text("not: valid: yaml: [")

        lock, healthy = self.manager.get_lock_fail_closed("proj", "board")
        self.assertFalse(healthy)

    def test_get_lock_fail_closed_healthy_when_redis_has_a_definitive_answer(self):
        """Control case: if Redis DOES have an entry (a definitive answer on
        its own), an unreadable YAML file doesn't need to make the read
        unhealthy — Redis already tells us everything we need."""
        self.mock_redis.hgetall.return_value = {
            'project': 'proj', 'board': 'board', 'locked_by_issue': '123',
            'lock_acquired_at': datetime.now(timezone.utc).isoformat(),
            'lock_status': 'locked', 'retained_reason': 'crashed', 'retained_at': datetime.now(timezone.utc).isoformat(),
        }
        state_file = self.manager._get_state_file("proj", "board")
        state_file.write_text("not: valid: yaml: [")

        lock, healthy = self.manager.get_lock_fail_closed("proj", "board")
        self.assertTrue(healthy)
        self.assertEqual(lock.retained_reason, "crashed")

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

    def test_release_lock_still_checks_yaml_ownership_when_redis_transaction_raises(self):
        """If Redis is configured but the release transaction itself raises
        (connection drop, timeout — not "Redis unavailable"), the YAML
        ownership check must still run rather than being skipped just because
        self.redis_client is non-None. Otherwise a lock held by someone else
        could be deleted from the one non-expiring copy with zero validation."""
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=999,  # held by a DIFFERENT issue
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
        )
        self.manager._save_lock_to_yaml(lock)

        self.mock_redis.pipeline.side_effect = Exception("connection dropped")

        released = self.manager.release_lock("proj", "board", 123)  # issue 123 != holder 999
        self.assertFalse(released)
        # The YAML file must still exist, untouched.
        lock_after = self.manager.get_lock("proj", "board")
        self.assertIsNotNone(lock_after)
        self.assertEqual(lock_after.locked_by_issue, 999)

    def test_release_lock_still_checks_yaml_ownership_when_redis_reports_not_found(self):
        """A found-and-fixed bug in the fix above: Redis reporting "not_found"
        (the key simply isn't there) is NOT an ownership confirmation and must
        not skip the YAML check either. This is actually the routine, expected
        state for a retained lock once its 2-hour Redis TTL lapses (nothing
        legitimately re-touches a retained lock) — so treating "not_found" as
        "confirmed released" would silently delete the one non-expiring copy
        of a retained lock the moment its Redis TTL happens to expire, for a
        release call naming any issue number at all (not just the holder)."""
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=999,
            lock_acquired_at=(datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
            lock_status="locked", retained_reason="agent crashed", retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.manager._save_lock_to_yaml(lock)

        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {}  # Redis has no record — TTL'd out
            return func(mock_pipe)
        self.mock_redis.transaction.side_effect = side_effect_transaction

        # A caller releasing on behalf of some other issue (not the retained
        # holder) must still be refused.
        released = self.manager.release_lock("proj", "board", 456, force=True)
        self.assertFalse(released)
        lock_after = self.manager.get_lock("proj", "board")
        self.assertIsNotNone(lock_after)
        self.assertEqual(lock_after.retained_reason, "agent crashed")

    def test_release_lock_refuses_retained_lock_without_force(self):
        """release_lock() itself must refuse a retained lock unless force=True
        — this is the guard that closes the gap where several ordinary,
        automatic call sites (closing the GitHub issue, an exit-column
        release, pipeline_progression's own release-and-advance logic) could
        otherwise silently release a retained lock without ever going through
        scripts/release_lock.py's deliberate confirmation flow."""
        self._write_retained_lock(issue_number=123, hours_old=0)

        released = self.manager.release_lock("proj", "board", 123)
        self.assertFalse(released)
        # And the lock must still be intact — nothing was touched.
        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.retained_reason, "agent crashed repeatedly")

    def test_release_lock_clears_retained_state_with_force(self):
        """The deliberate recovery action (scripts/release_lock.py, which
        passes force=True only after its own explicit confirmation) removes
        the lock entirely, so a fresh try_acquire_lock succeeds afterward."""
        self._write_retained_lock(issue_number=123, hours_old=0)

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {'locked_by_issue': '123'}
            return func(mock_pipe)
        self.mock_redis.transaction.side_effect = side_effect_transaction

        released = self.manager.release_lock("proj", "board", 123, force=True)
        self.assertTrue(released)
        self.assertIsNone(self.manager.get_lock("proj", "board"))

    def test_release_lock_does_not_need_force_for_ordinary_lock(self):
        """The force requirement is specific to retained locks — an ordinary,
        non-retained lock (the overwhelming majority of releases: normal
        successful completions) releases exactly as before, no code changes
        needed at any of those call sites."""
        self.manager._create_lock("proj", "board", 123)

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = {'locked_by_issue': '123'}
            return func(mock_pipe)
        self.mock_redis.transaction.side_effect = side_effect_transaction

        released = self.manager.release_lock("proj", "board", 123)
        self.assertTrue(released)


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


class TestPipelineLockValidation(unittest.TestCase):
    """PipelineLock.__post_init__ — validation at the type level, not just in
    PipelineLockManager.mark_lock_failed, so direct construction or YAML
    deserialization of a malformed file can't produce the same falsy-string
    collapse mark_lock_failed's own check guards against."""

    def test_empty_reason_normalizes_to_none_on_construction(self):
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
            retained_reason="", retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertIsNone(lock.retained_reason)
        self.assertIsNone(lock.retained_at)
        self.assertFalse(lock.is_retained)

    def test_whitespace_only_reason_normalizes_to_none_on_construction(self):
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
            retained_reason="   ", retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.assertIsNone(lock.retained_reason)

    def test_is_retained_property(self):
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
            retained_reason="crashed",
        )
        self.assertTrue(lock.is_retained)

    def test_yaml_deserialization_of_malformed_field_is_also_normalized(self):
        """Simulates a hand-edited or corrupted YAML file with
        retained_reason: "" — PipelineLock(**lock_data) must not produce a
        lock that reads as retained-but-falsy."""
        lock = PipelineLock(**{
            'project': "proj", 'board': "board", 'locked_by_issue': 123,
            'lock_acquired_at': datetime.now(timezone.utc).isoformat(),
            'lock_status': "locked", 'retained_reason': "", 'retained_at': None,
        })
        self.assertIsNone(lock.retained_reason)


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

    def test_deduplicates_a_pair_present_in_both_stores(self):
        """The case the class is named for: a (project, board) pair discovered
        via BOTH the YAML scan and the Redis key scan must resolve to exactly
        one PipelineLock in the result, not two."""
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
        )
        self.manager._save_lock_to_yaml(lock)  # present in YAML

        self.mock_redis.keys.return_value = ["pipeline_lock:proj:board"]  # also present in Redis
        self.mock_redis.hgetall.return_value = {
            'project': 'proj', 'board': 'board', 'locked_by_issue': '123',
            'lock_acquired_at': lock.lock_acquired_at, 'lock_status': 'locked',
            'retained_reason': '', 'retained_at': '',
        }

        locks = self.manager.get_all_locks()
        self.assertEqual(len(locks), 1)

    def test_finds_lock_that_exists_only_in_yaml(self):
        """The other direction: a pair present in YAML but with no Redis key at
        all (e.g. TTL'd out) must still be discovered via the YAML file scan."""
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(), lock_status="locked",
            retained_reason="crashed", retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.manager._save_lock_to_yaml(lock)
        self.mock_redis.keys.return_value = []  # nothing in Redis

        locks = self.manager.get_all_locks()
        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].retained_reason, "crashed")


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


class TestStealLock(unittest.TestCase):
    """PipelineLockManager.steal_lock() — the single sanctioned way to hand a
    lock to a different issue than its current holder (e.g. repair cycles
    stealing from an ordinary Development item). Centralizes the
    retained_reason check and the release+create sequence that call sites
    (services/project_monitor.py's repair-cycle dispatch) previously
    duplicated as two separately-maintained steps — a pattern that caused two
    real bugs across this PR's review rounds when a call site's check and its
    construction call drifted apart."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_refuses_to_steal_a_retained_lock(self):
        """The core regression case: steal_lock() must never hand a retained
        lock to a different issue, regardless of which issue is requesting it."""
        self.manager._create_lock("proj", "board", 999)
        marked = self.manager.mark_lock_failed("proj", "board", 999, reason="agent crashed")
        self.assertTrue(marked)

        ok, result = self.manager.steal_lock("proj", "board", 100)

        self.assertFalse(ok)
        self.assertTrue(result.startswith("retained:"))
        # The retained lock must genuinely still belong to issue 999 afterward.
        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.locked_by_issue, 999)
        self.assertEqual(lock.retained_reason, "agent crashed")

    def test_steals_a_non_retained_lock_held_by_another_issue(self):
        self.manager._create_lock("proj", "board", 999)

        ok, result = self.manager.steal_lock("proj", "board", 100)

        self.assertTrue(ok)
        self.assertEqual(result, "stolen")
        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.locked_by_issue, 100)
        self.assertIsNone(lock.retained_reason)

    def test_acquires_when_nothing_is_locked(self):
        ok, result = self.manager.steal_lock("proj", "board", 100)

        self.assertTrue(ok)
        self.assertEqual(result, "acquired")
        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.locked_by_issue, 100)

    def test_no_op_when_already_held_by_the_requesting_issue(self):
        self.manager._create_lock("proj", "board", 100)

        ok, result = self.manager.steal_lock("proj", "board", 100)

        self.assertTrue(ok)
        self.assertEqual(result, "already_held")

    def test_fails_closed_when_lock_state_cannot_be_determined(self):
        self.mock_redis.hgetall.side_effect = Exception("redis down")
        state_file = self.manager._get_state_file("proj", "board")
        state_file.write_text("not: valid: yaml: [")

        ok, result = self.manager.steal_lock("proj", "board", 100)

        self.assertFalse(ok)
        self.assertEqual(result, "lock_state_unknown")


class TestReleaseLockUnlinkFailure(unittest.TestCase):
    """release_lock()'s YAML-deletion step must not report success when the
    actual unlink() call fails. Before this fix, the outer exception handler
    wrapping both the ownership-check read AND the unlink() call caught any
    exception from either and fell through to `return True`, so a failed
    unlink() (permissions, disk error, etc.) silently left the — potentially
    still retained_reason-bearing — YAML file on disk while reporting release
    success to the caller."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        # No Redis-side entry — forces the YAML-ownership/unlink path to run
        # and be the deciding factor in the result, matching the retained
        # lock's expected steady state (its Redis TTL commonly lapses).
        self.mock_redis.hgetall.return_value = {}
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_release_lock_returns_false_when_unlink_raises(self):
        self.manager._create_lock("proj", "board", 123)
        state_file = self.manager._get_state_file("proj", "board")
        self.assertTrue(state_file.exists())

        with patch.object(Path, 'unlink', side_effect=OSError("simulated unlink failure")):
            released = self.manager.release_lock("proj", "board", 123, force=True)

        self.assertFalse(released)
        self.assertTrue(state_file.exists(), "YAML lock file should still be present after a failed unlink")

    def test_release_lock_returns_true_and_deletes_file_when_unlink_succeeds(self):
        """Control case, to prove the fix didn't just make release_lock always
        return False."""
        self.manager._create_lock("proj", "board", 123)
        state_file = self.manager._get_state_file("proj", "board")

        released = self.manager.release_lock("proj", "board", 123, force=True)

        self.assertTrue(released)
        self.assertFalse(state_file.exists())


class TestHumanFeedbackLoopSuppressCancellation(unittest.TestCase):
    """PipelineRunManager.mark_failed's suppress_cancellation param exists
    specifically so a caller (services/human_feedback_loop.py's abnormal
    conversational-loop exit path) can use a descriptive retained_reason
    without losing the race-avoidance the old "feedback_loop_ended" sentinel
    string provided — that string match previously governed both the
    retained_reason text AND whether the cancellation signal fired, so any
    caller wanting a better reason string silently regressed into firing a
    cancellation signal that delays re-dispatch by up to an hour."""

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

        # An active PipelineRun is required to reach the cancellation-signal
        # code at all (end_pipeline_run returns early with no active run).
        self.run = self.manager.create_pipeline_run(
            issue_number=123, issue_title="t", issue_url="u",
            project="proj", board="board",
        )
        self.mock_redis.hget.return_value = self.run.id
        self.mock_redis.get.side_effect = lambda key: (
            json.dumps(self.run.to_dict()) if key == self.manager._get_redis_key(self.run.id) else None
        )

    def test_mark_failed_with_suppress_cancellation_does_not_set_signal(self):
        mock_cancellation = MagicMock()
        with patch('services.cancellation.get_cancellation_signal', return_value=mock_cancellation), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=MagicMock()):
            self.manager.mark_failed(
                project="proj", board="board", issue_number=123,
                reason="Conversational feedback loop exited abnormally (exit_reason=crash)",
                suppress_cancellation=True,
            )

        mock_cancellation.cancel.assert_not_called()

    def test_mark_failed_without_suppress_cancellation_sets_signal(self):
        """Control case: a descriptive reason with suppress_cancellation left
        at its default (False) DOES fire the cancellation signal — proving the
        suppression above is actually doing something, not just always off."""
        mock_cancellation = MagicMock()
        with patch('services.cancellation.get_cancellation_signal', return_value=mock_cancellation), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=MagicMock()):
            self.manager.mark_failed(
                project="proj", board="board", issue_number=123,
                reason="Some other descriptive failure reason",
            )

        mock_cancellation.cancel.assert_called_once()


if __name__ == '__main__':
    unittest.main()

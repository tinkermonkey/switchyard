"""
Tests for ProjectResourceLockManager (services/project_resource_lock_manager.py),
the thin project-scoped facade over PipelineLockManager (issue #53).

Mirrors PipelineLockManager's own existing test coverage
(tests/unit/services/test_pipeline_lock_manager.py and
tests/unit/services/test_pipeline_failure_durability.py) for
acquire/release/fail-closed/retained-lock/staleness behavior, applied
through the facade -- these are pass-through behaviors from
PipelineLockManager, not new logic, so these tests exist to prove the facade
doesn't lose or alter any of them, not to re-derive them from scratch.

Also verifies the get_all_locks() key-format compatibility claim: that the
colon-free "__resource__{resource_name}" board-naming convention keeps a
resource lock's Redis key at exactly 2 colons (matching an ordinary
pipeline_lock:{project}:{board} key), so it is not silently dropped by
get_all_locks()'s `key.count(':') != 2` filter.
"""

import unittest
from unittest.mock import MagicMock
import sys
import os
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.pipeline_lock_manager import PipelineLockManager, PipelineLock
from services.project_resource_lock_manager import (
    ProjectResourceLockManager,
    RESOURCE_BOARD_PREFIX,
    InvalidResourceNameError,
)


class TestProjectResourceLockManagerAcquireRelease(unittest.TestCase):
    """Mirrors test_pipeline_lock_manager.py's acquire/release coverage, through the facade."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _mock_transaction(self, hgetall_return):
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = hgetall_return
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

    def test_acquire_resource_success(self):
        self._mock_transaction({})

        success, reason = self.facade.acquire_resource("proj", "db_migration", 123)

        self.assertTrue(success)
        self.assertEqual(reason, "lock_acquired")

    def test_acquire_resource_already_held(self):
        self._mock_transaction({'lock_status': 'locked', 'locked_by_issue': '123'})

        success, reason = self.facade.acquire_resource("proj", "db_migration", 123)

        self.assertTrue(success)
        self.assertEqual(reason, "already_holds_lock")

    def test_acquire_resource_held_by_other(self):
        self._mock_transaction({
            'lock_status': 'locked',
            'locked_by_issue': '456',
            'lock_acquired_at': datetime.now(timezone.utc).isoformat(),
        })

        success, reason = self.facade.acquire_resource("proj", "db_migration", 123)

        self.assertFalse(success)
        self.assertEqual(reason, "locked_by_issue_456")

    def test_release_resource_success(self):
        self._mock_transaction({'locked_by_issue': '123'})

        result = self.facade.release_resource("proj", "db_migration", 123)

        self.assertTrue(result)

    def test_release_resource_held_by_other(self):
        self._mock_transaction({'locked_by_issue': '456'})

        result = self.facade.release_resource("proj", "db_migration", 123)

        self.assertFalse(result)

    def test_get_resource_lock_reflects_acquired_state(self):
        self._mock_transaction({})
        self.facade.acquire_resource("proj", "db_migration", 123)

        lock = self.facade.get_resource_lock("proj", "db_migration")

        self.assertIsNotNone(lock)
        self.assertEqual(lock.locked_by_issue, 123)
        self.assertEqual(lock.project, "proj")
        self.assertEqual(lock.board, f"{RESOURCE_BOARD_PREFIX}db_migration")

    def test_get_resource_lock_none_when_unlocked(self):
        lock = self.facade.get_resource_lock("proj", "db_migration")
        self.assertIsNone(lock)

    def test_two_different_resources_do_not_collide(self):
        """Two distinct resource_names for the same project must be
        independently lockable -- the facade's whole raison d'etre."""
        self._mock_transaction({})

        success_a, _ = self.facade.acquire_resource("proj", "db_migration", 111)
        success_b, _ = self.facade.acquire_resource("proj", "docker_build", 222)

        self.assertTrue(success_a)
        self.assertTrue(success_b)
        self.assertEqual(self.facade.get_resource_lock("proj", "db_migration").locked_by_issue, 111)
        self.assertEqual(self.facade.get_resource_lock("proj", "docker_build").locked_by_issue, 222)


class TestProjectResourceLockManagerRetainedAndStaleness(unittest.TestCase):
    """Mirrors test_pipeline_failure_durability.py's retained/staleness coverage, through the facade."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _write_retained_lock(self, resource_name="db_migration", issue_number=123, hours_old=0):
        """Write a retained resource lock directly to YAML (bypassing Redis),
        as if issue_number held it hours_old hours ago and it was then marked
        failed -- mirrors test_pipeline_failure_durability's
        _write_retained_lock helper, but through the facade's board naming."""
        lock = PipelineLock(
            project="proj",
            board=ProjectResourceLockManager._resource_board(resource_name),
            locked_by_issue=issue_number,
            lock_acquired_at=(datetime.now(timezone.utc) - timedelta(hours=hours_old)).isoformat(),
            lock_status="locked",
            retained_reason="agent crashed repeatedly",
            retained_at=datetime.now(timezone.utc).isoformat(),
        )
        self.lock_manager._save_lock_to_yaml(lock)
        return lock

    def _mock_transaction(self, hgetall_return):
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = hgetall_return
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

    def test_acquire_resource_refuses_when_retained_even_if_redis_copy_is_gone(self):
        self._write_retained_lock(issue_number=123, hours_old=0)
        self._mock_transaction({})  # Redis has no record (TTL expired)

        success, reason = self.facade.acquire_resource("proj", "db_migration", 456)

        self.assertFalse(success)
        self.assertIn("failed", reason)
        self.assertIn("123", reason)

    def test_acquire_resource_refuses_regardless_of_lock_age(self):
        self._write_retained_lock(issue_number=123, hours_old=100)  # far past the 4h staleness window
        self._mock_transaction({})

        success, reason = self.facade.acquire_resource("proj", "db_migration", 456)

        self.assertFalse(success)

    def test_acquire_resource_refuses_even_for_the_lock_own_holder(self):
        self._write_retained_lock(issue_number=123, hours_old=0)
        self._mock_transaction({
            'lock_status': 'locked', 'locked_by_issue': '123',
            'retained_reason': 'agent crashed repeatedly',
        })

        success, reason = self.facade.acquire_resource("proj", "db_migration", 123)

        self.assertFalse(success)
        self.assertIn("failed", reason)

    def test_release_resource_refuses_when_retained_without_force(self):
        self._write_retained_lock(issue_number=123, hours_old=0)

        result = self.facade.release_resource("proj", "db_migration", 123)

        self.assertFalse(result)
        lock = self.facade.get_resource_lock("proj", "db_migration")
        self.assertEqual(lock.retained_reason, "agent crashed repeatedly")

    def test_release_resource_succeeds_when_retained_with_force(self):
        self._write_retained_lock(issue_number=123, hours_old=0)
        self._mock_transaction({'locked_by_issue': '123'})

        result = self.facade.release_resource("proj", "db_migration", 123, force=True)

        self.assertTrue(result)

    def test_acquire_resource_fails_closed_on_double_read_failure(self):
        """Both Redis and YAML reads failing must refuse, not silently grant."""
        self.mock_redis.hgetall.side_effect = Exception("redis down")
        state_file = self.lock_manager._get_state_file(
            "proj", ProjectResourceLockManager._resource_board("db_migration")
        )
        state_file.write_text("not: valid: yaml: [")

        success, reason = self.facade.acquire_resource("proj", "db_migration", 456)

        self.assertFalse(success)
        self.assertEqual(reason, "lock_state_unknown_failing_closed")

    def test_release_resource_fails_closed_on_double_read_failure(self):
        self.mock_redis.hgetall.side_effect = Exception("redis down")
        state_file = self.lock_manager._get_state_file(
            "proj", ProjectResourceLockManager._resource_board("db_migration")
        )
        state_file.write_text("not: valid: yaml: [")

        result = self.facade.release_resource("proj", "db_migration", 456)

        self.assertFalse(result)


class TestGetAllLocksCompatibility(unittest.TestCase):
    """
    Verifies the compatibility risk flagged in issue #53: PipelineLockManager.
    get_all_locks() scans Redis keys and skips any key where
    `key.count(':') != 2` (its guard against unrelated keys sharing the
    "pipeline_lock:" prefix). A resource lock's Redis key is
    "pipeline_lock:{project}:{board}" where board is the namespaced resource
    board name -- if that board value itself contained a colon, the full key
    would have 3 colons and be silently skipped by that filter. This confirms
    the colon-free "__resource__{resource_name}" convention keeps the key at
    exactly 2 colons and stays discoverable, for both the Redis-key scan and
    end-to-end get_all_locks() with a real board lock present alongside it.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # redis_client=None exercises the YAML-only path directly and avoids
        # having to hand-mock a redis `keys()`/`hgetall()` scan for this test.
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_resource_lock_key_has_exactly_two_colons(self):
        resource_board = ProjectResourceLockManager._resource_board("db_migration")
        key = self.lock_manager._get_lock_key("proj", resource_board)

        self.assertEqual(key.count(':'), 2, f"resource lock key {key!r} would be skipped by get_all_locks()'s filter")
        self.assertEqual(key, "pipeline_lock:proj:__resource__db_migration")

    def test_get_all_locks_discovers_both_real_board_and_resource_locks(self):
        self.lock_manager._create_lock("proj", "dev_workflow", 111)
        self.facade.acquire_resource("proj", "db_migration", 222)

        locks = self.lock_manager.get_all_locks()
        pairs = {(lock.project, lock.board, lock.locked_by_issue) for lock in locks}

        self.assertIn(("proj", "dev_workflow", 111), pairs)
        self.assertIn(("proj", "__resource__db_migration", 222), pairs)
        self.assertEqual(len(locks), 2)

    def test_resource_board_name_does_not_collide_with_a_real_board_name(self):
        """A resource lock and a same-named-ish real board lock for the same
        project must be tracked as fully independent locks."""
        self.lock_manager._create_lock("proj", "db_migration", 111)  # a real board literally named "db_migration"
        self.facade.acquire_resource("proj", "db_migration", 222)  # the resource lock

        real_lock = self.lock_manager.get_lock("proj", "db_migration")
        resource_lock = self.facade.get_resource_lock("proj", "db_migration")

        self.assertEqual(real_lock.locked_by_issue, 111)
        self.assertEqual(resource_lock.locked_by_issue, 222)


class TestResourceNameValidation(unittest.TestCase):
    """
    resource_name flows straight into a Redis key and an on-disk YAML lock
    file path -- validated to prevent path traversal and to preserve the
    2-colon Redis key format get_all_locks() depends on for discovery.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_rejects_empty_resource_name(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "", 123)

    def test_rejects_colon_in_resource_name(self):
        """A colon would push the Redis key to 3 colons, defeating get_all_locks()."""
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "docker:build", 123)

    def test_rejects_forward_slash_path_traversal(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "../../../../tmp/evil", 123)

    def test_rejects_backslash(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "sub\\path", 123)

    def test_rejects_dotdot_segment_without_slash(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "..", 123)

    def test_release_and_get_lock_also_validate(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.release_resource("proj", "a:b", 123)
        with self.assertRaises(InvalidResourceNameError):
            self.facade.get_resource_lock("proj", "a:b")

    def test_valid_resource_name_with_hyphens_and_underscores_is_accepted(self):
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline
        self.mock_redis.transaction.side_effect = (
            lambda func, *keys, **kwargs: func(MagicMock(hgetall=MagicMock(return_value={})))
        )

        success, _ = self.facade.acquire_resource("proj", "dev-container_build.v2", 123)

        self.assertTrue(success)


class TestProjectResourceLockManagerRetainedReasonPassthrough(unittest.TestCase):
    """
    mark_resource_failed/clear_resource_retained_reason/get_resource_retained_reason
    -- without these, a crashed resource-lock holder could only be recovered
    by the ordinary staleness heuristic rather than durably retained for
    human recovery, contradicting this facade's claim that every retained-
    lock semantic applies unchanged.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _mock_transaction(self, hgetall_return):
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline

        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            mock_pipe.hgetall.return_value = hgetall_return
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

    def test_mark_resource_failed_then_get_retained_reason_round_trips(self):
        self._mock_transaction({})
        self.facade.acquire_resource("proj", "db_migration", 123)

        marked = self.facade.mark_resource_failed("proj", "db_migration", 123, "agent crashed")

        self.assertTrue(marked)
        self.assertEqual(
            self.facade.get_resource_retained_reason("proj", "db_migration", 123),
            "agent crashed",
        )

    def test_clear_resource_retained_reason_leaves_lock_held(self):
        self._mock_transaction({})
        self.facade.acquire_resource("proj", "db_migration", 123)
        self.facade.mark_resource_failed("proj", "db_migration", 123, "agent crashed")

        cleared = self.facade.clear_resource_retained_reason("proj", "db_migration", 123)

        self.assertTrue(cleared)
        self.assertIsNone(self.facade.get_resource_retained_reason("proj", "db_migration", 123))
        lock = self.facade.get_resource_lock("proj", "db_migration")
        self.assertEqual(lock.locked_by_issue, 123)

    def test_mark_resource_failed_refuses_empty_reason(self):
        self._mock_transaction({})
        self.facade.acquire_resource("proj", "db_migration", 123)

        marked = self.facade.mark_resource_failed("proj", "db_migration", 123, "")

        self.assertFalse(marked)


if __name__ == '__main__':
    unittest.main()

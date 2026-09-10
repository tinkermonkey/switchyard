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
from unittest.mock import MagicMock, patch
import sys
import os
import tempfile
import shutil
import time
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


class TestGetAllLocksRedisKeyScan(unittest.TestCase):
    """
    TestGetAllLocksCompatibility below exercises get_all_locks() with
    redis_client=None, which only proves the YAML-glob discovery path --
    the Redis-key `key.count(':') != 2` filter this module's docstring and
    issue #53's own acceptance criteria call out is never actually executed
    there. This class uses a real (mocked) redis_client so that filter is
    genuinely exercised, with a resource lock that exists ONLY in Redis (no
    YAML file at all) so a broken filter or a broken Redis-scan path would
    be caught rather than masked by the YAML fallback.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_malformed_three_colon_key_is_skipped_while_resource_lock_key_is_discovered(self):
        resource_board = ProjectResourceLockManager._resource_board("db_migration")
        real_key = self.lock_manager._get_lock_key("proj", resource_board)
        self.assertEqual(real_key.count(':'), 2)

        # A hypothetical corrupted/pre-validation key with an embedded colon --
        # exactly the shape get_all_locks()'s "!= 2" filter exists to drop.
        malformed_key = "pipeline_lock:proj:__resource__nightly:backup"
        self.assertEqual(malformed_key.count(':'), 3)

        self.mock_redis.keys.return_value = [malformed_key, real_key]

        def hgetall_side_effect(key):
            if key == real_key:
                return {
                    'project': 'proj',
                    'board': resource_board,
                    'locked_by_issue': '222',
                    'lock_acquired_at': '2026-09-06T00:00:00+00:00',
                    'lock_status': 'locked',
                    'retained_reason': '',
                    'retained_at': '',
                }
            # The malformed key must never reach here -- it should be dropped
            # by the colon-count filter before any per-key read is attempted.
            raise AssertionError(f"hgetall() called for a key that should have been filtered: {key!r}")

        self.mock_redis.hgetall.side_effect = hgetall_side_effect

        locks = self.lock_manager.get_all_locks()

        self.assertEqual(len(locks), 1)
        self.assertEqual(locks[0].board, resource_board)
        self.assertEqual(locks[0].locked_by_issue, 222)


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

    def test_dots_alone_are_not_rejected(self):
        """
        '..' is only dangerous as a path segment; since '/' and '\\' are
        already rejected, board can never become more than a single filename
        component (state_dir / f"{project}_{board}.yaml"), so a resource_name
        that merely contains consecutive dots (e.g. a version-range-style
        name) can never actually traverse a directory and must be accepted.
        """
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline
        self.mock_redis.transaction.side_effect = (
            lambda func, *keys, **kwargs: func(MagicMock(hgetall=MagicMock(return_value={})))
        )

        success, _ = self.facade.acquire_resource("proj", "v1..2-migration", 123)

        self.assertTrue(success)

    def test_bare_dotdot_is_also_not_rejected(self):
        """
        Same reasoning as test_dots_alone_are_not_rejected, exercised for the
        exact bare ".." input specifically -- pinned as its own test so a
        future refactor of _resource_board()/_get_state_file() that changes
        how project/board are joined into a path can't silently reintroduce a
        real traversal hole for this exact input without a test noticing.
        """
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline
        self.mock_redis.transaction.side_effect = (
            lambda func, *keys, **kwargs: func(MagicMock(hgetall=MagicMock(return_value={})))
        )

        success, _ = self.facade.acquire_resource("proj", "..", 123)

        self.assertTrue(success)

    def test_release_and_get_lock_also_validate(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.release_resource("proj", "a:b", 123)
        with self.assertRaises(InvalidResourceNameError):
            self.facade.get_resource_lock("proj", "a:b")

    def test_rejects_leading_or_trailing_whitespace(self):
        """
        Without this, ' db_migration' and 'db_migration' would map to
        different board strings for what every caller intends as the same
        resource, silently defeating mutual exclusion between them.
        """
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", " db_migration", 123)
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "db_migration ", 123)

    def test_rejects_null_byte(self):
        """A null byte survives the other character checks but makes the
        on-disk YAML write fail while Redis succeeds, silently degrading the
        lock's durability -- reject it outright instead."""
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "cache\x00flush", 123)

    def test_rejects_other_control_characters(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "cache\nflush", 123)

    def test_rejects_non_string_resource_name(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", 12345, 123)

    def test_rejects_overlong_resource_name(self):
        """An unbounded resource_name can push the YAML lock file's path past
        the filesystem's filename length limit, raising an uncaught OSError
        deeper in PipelineLockManager instead of failing cleanly here."""
        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", "x" * 300, 123)

    def test_rejects_overlong_multibyte_resource_name_under_the_char_count_but_not_the_byte_count(self):
        """
        The limit is enforced in UTF-8 BYTES, not code points, since ext4's
        NAME_MAX is byte-based. A multi-byte string can stay under a
        char-count limit while still exceeding the byte limit -- this pins
        that the check is actually byte-based, not just character-based.
        """
        multibyte_name = "漢" * 140  # 140 chars, but 3 bytes each in UTF-8 = 420 bytes
        self.assertLess(len(multibyte_name), 150)
        self.assertGreater(len(multibyte_name.encode("utf-8")), 150)

        with self.assertRaises(InvalidResourceNameError):
            self.facade.acquire_resource("proj", multibyte_name, 123)

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


class TestProjectResourceLockManagerDefaultConstruction(unittest.TestCase):
    """
    Omitting `lock_manager` must default to the process-wide
    get_pipeline_lock_manager() singleton (a shared, already-warmed Redis
    connection), not a fresh PipelineLockManager() -- constructing a fresh
    one opens its own Redis connection (including its connect timeout) on
    every call, an easy footgun for a future caller that does
    ProjectResourceLockManager() per-request instead of threading a shared
    instance through explicitly.
    """

    def test_default_construction_uses_the_shared_singleton(self):
        sentinel = MagicMock(name="shared_pipeline_lock_manager")
        with patch(
            "services.project_resource_lock_manager.get_pipeline_lock_manager",
            return_value=sentinel,
        ) as mock_getter:
            facade = ProjectResourceLockManager()

        mock_getter.assert_called_once()
        self.assertIs(facade._lock_manager, sentinel)

    def test_explicit_lock_manager_is_not_overridden_by_the_singleton(self):
        explicit = MagicMock(name="explicit_lock_manager")
        with patch("services.project_resource_lock_manager.get_pipeline_lock_manager") as mock_getter:
            facade = ProjectResourceLockManager(lock_manager=explicit)

        mock_getter.assert_not_called()
        self.assertIs(facade._lock_manager, explicit)


class TestTouchResource(unittest.TestCase):
    """touch_resource() delegates to PipelineLockManager.touch_lock() -- see
    its own tests (test_pipeline_lock_manager.py::TestTouchLock) for the full
    liveness-refresh contract this passes through unchanged."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_refreshes_lock_acquired_at_for_the_current_holder(self):
        self.facade.acquire_resource("proj", "db_migration", 123)
        original = self.facade.get_resource_lock("proj", "db_migration")

        time.sleep(0.01)
        result = self.facade.touch_resource("proj", "db_migration", 123)

        self.assertTrue(result)
        refreshed = self.facade.get_resource_lock("proj", "db_migration")
        self.assertGreater(refreshed.lock_acquired_at, original.lock_acquired_at)

    def test_returns_false_for_a_different_holder(self):
        self.facade.acquire_resource("proj", "db_migration", 123)

        result = self.facade.touch_resource("proj", "db_migration", 456)

        self.assertFalse(result)

    def test_validates_resource_name(self):
        with self.assertRaises(InvalidResourceNameError):
            self.facade.touch_resource("proj", "a:b", 123)


if __name__ == '__main__':
    unittest.main()


class TestRecoverOrphanedResourceLocks(unittest.TestCase):
    """
    #152 review: a resource lock held by the process that died survives the
    restart (PipelineLockManager reclaims it only after its 4-hour staleness
    heuristic or the 7200s Redis TTL), and main.py's startup lock recovery
    iterates configured pipeline BOARDS, so it never sees a lock parked under
    the reserved `__resource__*` board. That made
    services/work_execution_state.py's post-restart dev-container
    reconciliation a guaranteed no-op in exactly the case it exists for: the
    orphaned lock exists if and only if a setup/verifier session was in flight,
    which is the same condition that produces the stuck record.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # YAML-only path -- the recovery is a get_all_locks() scan plus releases,
        # neither of which needs a hand-mocked Redis transaction.
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_releases_the_dead_holders_lock_so_the_next_acquire_succeeds(self):
        self.facade.acquire_resource("proj", "dev_container_build", -12345)
        self.assertIsNotNone(self.facade.get_resource_lock("proj", "dev_container_build"))

        released = self.facade.recover_orphaned_resource_locks("dev_container_build")

        self.assertEqual(released, 1)
        self.assertIsNone(self.facade.get_resource_lock("proj", "dev_container_build"))
        can_execute, _ = self.facade.acquire_resource("proj", "dev_container_build", -999)
        self.assertTrue(can_execute)

    def test_recovers_every_project_not_just_one(self):
        for project in ("alpha", "beta", "gamma"):
            self.facade.acquire_resource(project, "dev_container_build", -1)

        self.assertEqual(self.facade.recover_orphaned_resource_locks("dev_container_build"), 3)
        for project in ("alpha", "beta", "gamma"):
            self.assertIsNone(self.facade.get_resource_lock(project, "dev_container_build"))

    def test_leaves_other_resources_and_real_board_locks_alone(self):
        """Scoped by resource name: recovering one resource must not free a
        different resource's lock, nor any pipeline board lock."""
        self.facade.acquire_resource("proj", "dev_container_build", -1)
        self.facade.acquire_resource("proj", "project_checkout", -2)
        self.lock_manager._create_lock("proj", "dev_workflow", 111)

        self.facade.recover_orphaned_resource_locks("dev_container_build")

        self.assertIsNone(self.facade.get_resource_lock("proj", "dev_container_build"))
        self.assertIsNotNone(self.facade.get_resource_lock("proj", "project_checkout"))
        self.assertEqual(self.lock_manager.get_lock("proj", "dev_workflow").locked_by_issue, 111)

    def test_leaves_a_retained_lock_in_place(self):
        """A lock retained after a failed run is a durable marker for deliberate
        human recovery -- only scripts/release_lock.py may clear it."""
        self.facade.acquire_resource("proj", "dev_container_build", -7)
        self.facade.mark_resource_failed("proj", "dev_container_build", -7, "build blew up")

        released = self.facade.recover_orphaned_resource_locks("dev_container_build")

        self.assertEqual(released, 0)
        self.assertIsNotNone(self.facade.get_resource_lock("proj", "dev_container_build"))

    def test_is_a_no_op_when_nothing_is_held(self):
        self.assertEqual(self.facade.recover_orphaned_resource_locks("dev_container_build"), 0)

    def test_an_unreadable_lock_store_does_not_break_startup(self):
        with patch.object(self.lock_manager, 'get_all_locks', side_effect=RuntimeError("redis down")):
            self.assertEqual(self.facade.recover_orphaned_resource_locks("dev_container_build"), 0)

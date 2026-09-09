import unittest
from unittest.mock import MagicMock, patch, call
import sys
import os
import tempfile
import shutil
from pathlib import Path
from datetime import datetime, timezone, timedelta

# Add project root to path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

from services.pipeline_lock_manager import PipelineLockManager, PipelineLock, TouchResult

class TestPipelineLockManager(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_try_acquire_lock_success(self):
        # Setup Redis pipeline mock
        pipeline = self.mock_redis.pipeline.return_value
        pipeline.__enter__.return_value = pipeline
        
        # First pass: exists(key) -> False (lock doesn't exist)
        # But we changed logic to use transaction callback.
        # The transaction method calls the callback.
        
        # We need to mock the transaction behavior.
        # Since transaction executes a callable, we can just invoke it manually or trust the logic?
        # Mocking redis transaction is hard.
        # Let's mock the transaction method to call our callback.
        
        def side_effect_transaction(func, *keys, **kwargs):
            # Create a mock pipe that simulates the state we want
            mock_pipe = MagicMock()
            
            # Scenario: Lock does not exist
            mock_pipe.hgetall.return_value = {} 
            
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        
        self.assertTrue(success)
        self.assertEqual(reason, "lock_acquired")

    def test_try_acquire_lock_already_held(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock exists and held by us
            mock_pipe.hgetall.return_value = {
                'lock_status': 'locked',
                'locked_by_issue': '123'
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        
        self.assertTrue(success)
        self.assertEqual(reason, "already_holds_lock")

    def test_try_acquire_lock_held_by_other(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock exists and held by OTHER
            mock_pipe.hgetall.return_value = {
                'lock_status': 'locked',
                'locked_by_issue': '456',
                'lock_acquired_at': datetime.now(timezone.utc).isoformat()
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        success, reason = self.manager.try_acquire_lock("proj", "board", 123)
        
        self.assertFalse(success)
        self.assertEqual(reason, "locked_by_issue_456")

    def test_release_lock_success(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock held by us
            mock_pipe.hgetall.return_value = {
                'locked_by_issue': '123'
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction
        
        result = self.manager.release_lock("proj", "board", 123)
        self.assertTrue(result)

    def test_release_lock_held_by_other(self):
        def side_effect_transaction(func, *keys, **kwargs):
            mock_pipe = MagicMock()
            # Scenario: Lock held by OTHER
            mock_pipe.hgetall.return_value = {
                'locked_by_issue': '456'
            }
            return func(mock_pipe)

        self.mock_redis.transaction.side_effect = side_effect_transaction

        result = self.manager.release_lock("proj", "board", 123)
        self.assertFalse(result)


class TestTouchLock(unittest.TestCase):
    """
    touch_lock() (added for services/project_checkout_lock.py's heartbeat
    mechanism, #56 review) must refresh BOTH the Redis TTL AND
    lock_acquired_at -- unlike try_acquire_lock()'s "already_holds_lock"
    reentry branch, which only refreshes the TTL. Uses redis_client=None
    (YAML-only) so lock_acquired_at is observable directly from the on-disk
    state without fighting a stateless Redis mock.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_refreshes_lock_acquired_at_for_the_current_holder(self):
        self.manager._create_lock("proj", "board", 123)
        original = self.manager.get_lock("proj", "board")

        # Force a real, observable time difference regardless of clock
        # resolution/timing flakiness.
        import time as _time
        _time.sleep(0.01)

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertTrue(result)
        refreshed = self.manager.get_lock("proj", "board")
        self.assertGreater(refreshed.lock_acquired_at, original.lock_acquired_at)
        self.assertEqual(refreshed.locked_by_issue, 123)

    def test_returns_not_held_and_does_not_touch_a_lock_held_by_a_different_issue(self):
        self.manager._create_lock("proj", "board", 123)
        original = self.manager.get_lock("proj", "board")

        result = self.manager.touch_lock("proj", "board", 456)

        # NOT_HELD, not REFRESH_FAILED: this is a CONFIRMED loss, which
        # project_checkout_lock.py's heartbeat escalates very differently from
        # "the stores are down" -- see TouchResult.
        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(result)
        unchanged = self.manager.get_lock("proj", "board")
        self.assertEqual(unchanged.lock_acquired_at, original.lock_acquired_at)
        self.assertEqual(unchanged.locked_by_issue, 123)

    def test_returns_not_held_when_no_lock_exists_at_all(self):
        result = self.manager.touch_lock("proj", "board", 123)
        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(result)

    def test_preserves_retained_reason_if_somehow_set(self):
        """Defensive: touch_lock() should never called on a retained lock in
        practice (nothing that calls it also calls mark_lock_failed), but
        must not silently clear retained_reason if it ever is."""
        self.manager._create_lock("proj", "board", 123)
        self.manager.mark_lock_failed("proj", "board", 123, "agent crashed")

        self.manager.touch_lock("proj", "board", 123)

        lock = self.manager.get_lock("proj", "board")
        self.assertEqual(lock.retained_reason, "agent crashed")


class TestTouchLockFailsClosedOnUnhealthyReads(unittest.TestCase):
    """
    CRITICAL regression (found in PR #138 review, /pr-review-toolkit:review-pr):
    touch_lock() used to read via plain get_lock(), which collapses
    "confirmed unlocked" and "both Redis and YAML reads raised" into the
    same None -- so a transient dual-store outage was indistinguishable
    from "lock genuinely lost to another holder" to callers, and
    project_checkout_lock.py's heartbeat logs the latter as a specific,
    alarming ERROR. Must use get_lock_fail_closed() instead so a read
    failure returns False WITHOUT being conflated with confirmed loss.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_returns_refresh_failed_when_both_reads_fail_without_a_healthy_read_ever_happening(self):
        # Redis read raises.
        self.mock_redis.hgetall.side_effect = Exception("redis down")
        # YAML read also fails: corrupt the state file directly.
        state_file = self.manager._get_state_file("proj", "board")
        state_file.write_text("not: valid: yaml: [")

        result = self.manager.touch_lock("proj", "board", 123)

        # REFRESH_FAILED, distinct from NOT_HELD: unknown state is not proof
        # the lock was lost. Found in WI-1 (#146) review -- collapsing the two
        # into one False made project_checkout_lock.py's heartbeat log an
        # alarming "the lock was LOST, you may be racing another holder" ERROR
        # on every tick of a storage outage, and left its sustained-failure
        # escalation unreachable.
        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertFalse(result)

    def test_returns_refresh_failed_when_both_refresh_writes_fail(self):
        self.mock_redis.hgetall.return_value = {}  # healthy read, "not locked in Redis"
        self.manager._create_lock("proj", "board", 123)
        self.mock_redis.hset.side_effect = Exception("redis down")

        with patch.object(self.manager, '_save_lock_to_yaml', return_value=False):
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertFalse(result)

    def test_returns_refresh_failed_when_only_the_redis_refresh_write_fails(self):
        """
        CRITICAL regression (found in a later #146 WI-1 review round): a
        Redis-writes-fail/reads-succeed outage (OOM under noeviction, MISCONF
        after a failed BGSAVE, READONLY after a failover) used to return
        REFRESHED because the YAML leg succeeded. Only the Redis key has a
        TTL, so nothing was actually extended -- and the heartbeat's success
        branch then reset its failure run every tick, making the
        sustained-failure escalation unreachable for the one outage it was
        written for.
        """
        self.mock_redis.hgetall.return_value = {}  # healthy read, "not locked in Redis"
        self.manager._create_lock("proj", "board", 123)
        self.mock_redis.hset.side_effect = Exception("OOM command not allowed")

        # YAML write deliberately left working -- that's the whole point.
        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertFalse(result)

    def test_returns_refresh_failed_when_only_the_redis_expire_fails(self):
        """Same hazard through the other Redis call: hset can land while
        EXPIRE fails, which leaves the key's original TTL still running
        down."""
        self.mock_redis.hgetall.return_value = {}
        self.manager._create_lock("proj", "board", 123)
        self.mock_redis.expire.side_effect = Exception("READONLY You can't write against a read only replica")

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertFalse(result)

    def test_returns_refreshed_with_no_redis_client_when_the_yaml_write_succeeds(self):
        """The Redis-leg requirement is conditional on a client being
        configured -- a YAML-only manager has no expiring copy to extend, so
        a successful YAML write is a genuine full refresh there."""
        # Nulled after construction rather than passed as None: the
        # constructor builds a real client when none is supplied.
        self.manager.redis_client = None
        self.manager._create_lock("proj", "board", 123)

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertTrue(result)

    def test_still_works_normally_once_reads_are_healthy_again(self):
        """Not a general regression test of the happy path (see TestTouchLock
        above) -- specifically confirms the fail-closed branch doesn't
        permanently wedge the method once reads recover."""
        self.mock_redis.hgetall.return_value = {}  # empty dict: healthy, "not locked in Redis"
        self.manager._create_lock("proj", "board", 123)

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertTrue(result)


class TestYamlFallbackAcquisitionIsSerialized(unittest.TestCase):
    """
    try_acquire_lock()'s YAML fallback (the branch taken whenever Redis is
    unavailable) is a plain read-modify-write: read the current lock, decide,
    then create one. Found in review of #146 WI-1 that nothing made it atomic
    -- the single-threaded event loop had been accidentally supplying that
    atomicity for every async caller, and moving the acquire attempt into a
    worker thread let genuinely concurrent callers all read "no lock" before
    any of them wrote one, granting every one of them the same lock. It is now
    serialized by a dedicated advisory file lock (across processes too, so
    scripts/rebuild_project_images.py and scripts/release_lock.py are covered
    alongside the orchestrator).
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
        # Explicit, not just redis_client=None: None makes the constructor
        # build a real client from REDIS_HOST, which succeeds in the
        # orchestrator container.
        self.manager.redis_client = None

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_only_one_of_many_concurrent_threads_is_granted_the_lock(self):
        import threading

        granted = []
        granted_lock = threading.Lock()
        start = threading.Barrier(8)

        def worker(issue_number):
            start.wait(timeout=10)
            success, _reason = self.manager.try_acquire_lock("proj", "board", issue_number)
            if success:
                with granted_lock:
                    granted.append(issue_number)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(len(granted), 1, f"the same lock was granted to {granted}")
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, granted[0])

    def test_a_guard_that_cannot_be_taken_refuses_rather_than_granting_unguarded(self):
        """Fail closed, matching this method's unhealthy-reads check: an
        unguarded read-modify-write is exactly the double-grant the guard
        exists to prevent, and every caller polls, so a refusal is retried."""
        with patch('utils.file_lock.file_lock', side_effect=TimeoutError("guard busy")):
            success, reason = self.manager.try_acquire_lock("proj", "board", 1)

        self.assertFalse(success)
        self.assertEqual(reason, "lock_acquire_serialization_timeout")
        self.assertIsNone(self.manager.get_lock("proj", "board"))


if __name__ == '__main__':
    unittest.main()

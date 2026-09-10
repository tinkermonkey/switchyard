import contextlib
import threading
import time
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

import redis

from services.pipeline_lock_manager import (
    LOCK_REDIS_RETRY,
    LOCK_REDIS_SOCKET_TIMEOUT_SECONDS,
    LockStateSerializationError,
    PipelineLockManager,
    PipelineLock,
    RELEASE_GUARD_RETRY_TIMEOUT_SECONDS,
    ReleaseResult,
    STATE_LOCK_TIMEOUT_SECONDS,
    TouchResult,
    refusal_leaves_caller_holding_lock,
    refusal_must_not_end_caller_run,
)
from tests.utils.fake_redis import ThreadSafeFakeRedis


def _touch_transaction_side_effect(hgetall_result, hset_exc=None, expire_exc=None, before=None):
    """
    Stand in for redis-py's Redis.transaction(func, *watches,
    value_from_callable=True), which touch_lock()'s Redis leg now goes through
    (#153 WI-8 made that leg a WATCH/MULTI compare-and-set instead of a blind
    hset on the client).

    `hgetall_result` is what the transaction's own re-read sees INSIDE the
    watch -- which is what makes the "a second caller won the lock between the
    upfront read and the write" race expressible in a test at all. hset/expire
    failures are raised at call time rather than at execute(); either way the
    exception leaves transaction(), which is the boundary touch_lock() catches.
    `before` runs just before the callable does, so a test can land a
    concurrent release in the middle of a touch.

    The returned function records the ORDER of the pipeline calls the callable
    makes on `.calls`. That ordering is load-bearing and otherwise invisible:
    MagicMock makes pipe.multi() a no-op, but in real redis-py a watching
    pipeline executes commands immediately until multi() is called, so
    hset/expire issued before it would fire outside the transaction and the
    WATCH would gate nothing.
    """
    calls = []

    def _record(name, exc):
        def _call(*args, **kwargs):
            calls.append(name)
            if exc is not None:
                raise exc
        return _call

    def _side_effect(func, *keys, **kwargs):
        if before is not None:
            before()
        mock_pipe = MagicMock()
        mock_pipe.hgetall.return_value = (
            hgetall_result() if callable(hgetall_result) else hgetall_result
        )
        mock_pipe.multi.side_effect = _record('multi', None)
        mock_pipe.hset.side_effect = _record('hset', hset_exc)
        mock_pipe.expire.side_effect = _record('expire', expire_exc)
        return func(mock_pipe)

    _side_effect.calls = calls
    return _side_effect


class _FileLockSpy:
    """
    Records the ORDER in which utils.file_lock.file_lock() contexts are entered
    and exited, while still taking the real locks.

    Both facts these tests need are otherwise invisible: which of the two lock
    files is OUTER (an ordering cycle between them would deadlock), and whether
    a read and the write that depends on it happen inside ONE held
    '<state>.yaml.lock' (a release_lock() landing in a gap between two separate
    acquisitions is exactly what re-created a lock that had just been released).
    """

    def __init__(self):
        self.events = []

    @property
    def paths(self):
        return [path for kind, path in self.events if kind == 'enter']

    def note(self, label):
        self.events.append(('note', label))

    def patch(self):
        import utils.file_lock as file_lock_module
        real_file_lock = file_lock_module.file_lock

        @contextlib.contextmanager
        def spy(path, *args, **kwargs):
            self.events.append(('enter', str(path)))
            try:
                with real_file_lock(path, *args, **kwargs):
                    yield
            finally:
                self.events.append(('exit', str(path)))

        return patch('utils.file_lock.file_lock', side_effect=spy)


def _redis_lock_hash(issue_number, acquired_at=None):
    """A redis hgetall() result for a lock held by issue_number."""
    return {
        'project': 'proj',
        'board': 'board',
        'locked_by_issue': str(issue_number),
        'lock_acquired_at': acquired_at or datetime.now(timezone.utc).isoformat(),
        'lock_status': 'locked',
        'retained_reason': '',
        'retained_at': '',
        'owner_process': 'main.py#deadbeef',
    }


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
    reentry branch, which only refreshes the TTL.

    #139 audit: YAML-only, deliberately. lock_acquired_at is what these assert
    on and it is observable directly from the on-disk state; the Redis leg's
    own compare-and-set behaviour is covered separately by
    TestTouchLockIsACompareAndSet and TestTouchLockGuardsBothLegs, both of
    which inject a Redis. Said with use_redis=False rather than
    redis_client=None, which means "connect one yourself".
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)

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
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect(
            {}, hset_exc=Exception("redis down")
        )

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
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect(
            {}, hset_exc=Exception("OOM command not allowed")
        )

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
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect(
            {}, expire_exc=Exception("READONLY You can't write against a read only replica")
        )

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
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect({})

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertTrue(result)


class TestTouchLockIsACompareAndSet(unittest.TestCase):
    """
    #153 WI-8 (from #140 item 15): touch_lock() used to read the lock through
    get_lock_fail_closed() and then BLINDLY overwrite both stores. That read
    does not authorize the write -- a heartbeat late enough for its own holder
    to have been judged stale (7200s Redis TTL, then the 4-hour age heuristic)
    and the lock handed to a second caller in the meantime could still land its
    refresh on top of that second caller's record, silently taking the lock
    back from a live holder. Each store's write is now a compare-and-set
    against that store's own current record: Redis inside a WATCH/MULTI
    transaction (the shape try_acquire_lock() already uses), YAML under
    try_acquire_lock()'s own '<state>.yaml.acquire.lock' guard.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_redis_leg_refuses_a_holder_that_changed_between_the_read_and_the_write(self):
        """The exact item-15 race: the upfront read still sees this holder,
        but by the time the write happens the lock is somebody else's."""
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect(
            _redis_lock_hash(456)
        )

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(result)
        # Nothing was written anywhere -- in particular the YAML leg is not
        # reached once Redis has given a definitive "somebody else's now".
        self.mock_redis.hset.assert_not_called()
        self.assertFalse(self.manager._get_state_file("proj", "board").exists())

    def test_redis_leg_still_re_establishes_a_key_whose_ttl_lapsed(self):
        """An ABSENT key is the TTL having lapsed under a hold the
        non-expiring YAML copy still records as ours -- re-establishing it is
        the self-heal the heartbeat exists for. What authorizes it is the
        GUARDED YAML re-read confirming the holder, not this call's opening
        snapshot, so the write only happens on a second transaction after that
        confirmation."""
        self.mock_redis.hgetall.return_value = {}
        self.manager._create_lock("proj", "board", 123)
        side_effect = _touch_transaction_side_effect({})
        self.mock_redis.transaction.side_effect = side_effect

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertEqual(self.mock_redis.transaction.call_count, 2)
        self.assertEqual(side_effect.calls, ['multi', 'hset', 'expire'])

    def test_the_redis_leg_watches_the_lock_key_and_takes_the_callables_value(self):
        """
        The two arguments that MAKE this a compare-and-set, and which a
        stateless MagicMock cannot fail on: the watched key (without it nothing
        is WATCHed and a concurrent acquire between the read and the execute is
        never detected) and value_from_callable (without it redis-py returns
        execute()'s list, the "not_held" comparison is never true, and the leg
        reports REFRESHED for every input -- a blind overwrite again).
        """
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)
        self.manager._create_lock("proj", "board", 123)
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect(
            _redis_lock_hash(123)
        )

        self.manager.touch_lock("proj", "board", 123)

        self.assertEqual(
            self.mock_redis.transaction.call_args.args[1:],
            (self.manager._get_lock_key("proj", "board"),),
        )
        self.assertIs(self.mock_redis.transaction.call_args.kwargs["value_from_callable"], True)

    def test_the_redis_leg_buffers_its_writes_behind_multi(self):
        """A watching redis-py pipeline runs commands immediately until
        multi() is called, so an hset issued before it would land outside the
        transaction and the WATCH would gate nothing."""
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)
        self.manager._create_lock("proj", "board", 123)
        side_effect = _touch_transaction_side_effect(_redis_lock_hash(123))
        self.mock_redis.transaction.side_effect = side_effect

        self.manager.touch_lock("proj", "board", 123)

        self.assertEqual(side_effect.calls, ['multi', 'hset', 'expire'])

    def test_yaml_leg_refuses_a_holder_that_changed_between_the_read_and_the_write(self):
        """Same race on the YAML side, which try_acquire_lock()'s YAML fallback
        can grant to a different issue whenever Redis is down."""
        self.manager.redis_client = None
        self.manager._create_lock("proj", "board", 456)  # what is actually on disk
        stale_view = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=123,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked',
        )

        with patch.object(self.manager, 'get_lock_fail_closed', return_value=(stale_view, True)):
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(result)
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, 456)

    def test_reports_failure_rather_than_refreshing_unguarded(self):
        """Fail closed exactly like try_acquire_lock()'s own guarded path: a
        guard that cannot be taken means the read-modify-write below it is not
        atomic, so it must not happen at all."""
        self.manager.redis_client = None
        self.manager._create_lock("proj", "board", 123)
        original = self.manager.get_lock("proj", "board")

        with patch('utils.file_lock.file_lock', side_effect=TimeoutError("guard busy")):
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertEqual(
            self.manager.get_lock("proj", "board").lock_acquired_at,
            original.lock_acquired_at,
        )

    def test_the_yaml_leg_reports_failure_when_the_state_lock_cannot_be_taken(self):
        """Its read and its write must share ONE held '<state>.yaml.lock' (a
        release landing in the gap is what re-created a released lock), so
        failing to take that lock means the leg cannot run at all."""
        self.manager.redis_client = None
        self.manager._create_lock("proj", "board", 123)
        original = self.manager.get_lock("proj", "board")

        with patch('utils.file_lock.file_lock', side_effect=TimeoutError("state lock busy")):
            result, _ = self.manager._touch_lock_yaml_unguarded(
                "proj", "board", 123, original, allow_reestablish=False
            )

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertEqual(
            self.manager.get_lock("proj", "board").lock_acquired_at,
            original.lock_acquired_at,
        )

    def test_the_acquire_guard_is_taken_outside_the_state_file_lock(self):
        """
        The two lock files must stay distinct (fcntl.flock() conflicts between
        two descriptors of the same file even in one process) and must always
        be taken in this order -- '.acquire.lock' outer, '<state>.yaml.lock'
        inner -- which is the order try_acquire_lock()'s guarded path and
        release_lock() also establish. Taking them the other way round
        anywhere would be an ordering cycle.
        """
        self.manager.redis_client = None
        self.manager._create_lock("proj", "board", 123)
        taken = _FileLockSpy()

        with taken.patch():
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        state_file = self.manager._get_state_file("proj", "board")
        self.assertEqual(taken.paths[0], str(state_file) + '.acquire.lock')
        self.assertIn(str(state_file) + '.lock', taken.paths[1:])


class TestTouchLockDoesNotResurrectAReleasedLock(unittest.TestCase):
    """
    Found in the #153 WI-8 review round. Neither store can tell "the TTL
    lapsed under a live hold" from "release_lock() just deleted this": an
    absent Redis key and a missing state file are exactly what a completed
    release leaves behind. Both legs used to re-create their record from
    `fallback_lock` -- the snapshot touch_lock() read BEFORE the race -- so a
    touch overlapping a release silently put the lock back, with a fresh
    lock_acquired_at and a fresh 7200s TTL, held by an issue whose run had
    already ended. WATCH does not help: it aborts on a concurrent CREATE, and
    the absent branch is reached by a concurrent DELETE (redis-py then re-runs
    the callable, whose re-read sees the same absent key).

    Nothing reclaims such a lock at runtime -- _reconcile_active_runs()'s
    stale-lock watchdog runs only at orchestrator startup and
    pipeline_watchdog reaps runs, not locks -- so the board stops dispatching
    until the TTL lapses again or scripts/release_lock.py is run.

    The rule now is cross-store: a record missing from one store is only
    re-created while the OTHER store still positively names this holder.
    Both stores empty is a release, and stays released.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_a_release_landing_between_the_read_and_the_write_is_not_put_back(self):
        """release_lock() deletes the Redis key and then unlinks the state
        file, taking neither guard this method could serialize against."""
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)  # opening read: still ours
        self.manager._create_lock("proj", "board", 123)
        state_file = self.manager._get_state_file("proj", "board")
        side_effect = _touch_transaction_side_effect({}, before=state_file.unlink)
        self.mock_redis.transaction.side_effect = side_effect

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(result)
        self.assertEqual(side_effect.calls, [])  # nothing written to Redis
        self.assertFalse(state_file.exists())

    def test_a_yaml_only_manager_does_not_re_create_a_record_released_under_it(self):
        """Same race with no Redis configured, where the state file is the
        only copy there is -- and there is no authoritative Redis record to
        overwrite it on the next acquire."""
        self.manager.redis_client = None
        self.manager._create_lock("proj", "board", 123)
        state_file = self.manager._get_state_file("proj", "board")
        stale_view = self.manager.get_lock("proj", "board")
        state_file.unlink()

        with patch.object(self.manager, 'get_lock_fail_closed', return_value=(stale_view, True)):
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(state_file.exists())

    def test_a_missing_yaml_record_is_still_healed_while_redis_names_this_holder(self):
        """The other direction is NOT a release signature: release_lock()
        deletes the Redis key BEFORE unlinking the state file, so "Redis has
        it, YAML doesn't" can only be a YAML write that failed at acquisition
        time -- which is exactly what this leg should heal."""
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)
        self.mock_redis.transaction.side_effect = _touch_transaction_side_effect(
            _redis_lock_hash(123)
        )
        state_file = self.manager._get_state_file("proj", "board")
        self.assertFalse(state_file.exists())

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertTrue(state_file.exists())
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, 123)

    def test_a_lock_gone_from_both_stores_stays_gone(self):
        """
        What a completed release_lock() leaves behind, seen by a touch that
        started after it. Neither leg may re-create anything from
        touch_lock()'s opening snapshot.
        """
        self.manager._create_lock("proj", "board", 123)
        stale_view = self.manager.get_lock("proj", "board")
        self.manager._get_state_file("proj", "board").unlink()
        side_effect = _touch_transaction_side_effect({})
        self.mock_redis.transaction.side_effect = side_effect

        with patch.object(self.manager, 'get_lock_fail_closed', return_value=(stale_view, True)):
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertEqual(side_effect.calls, [])
        self.assertFalse(self.manager._get_state_file("proj", "board").exists())


class TestTouchAndReleaseCannotInterleave(unittest.TestCase):
    """
    Found in the WI-8 review round. touch_lock() and release_lock() are both
    two-store read-modify-writes over the same lock, and nothing serialized
    them: the '<state>.yaml.acquire.lock' guard was taken only by
    try_acquire_lock()'s YAML fallback and by touch_lock()'s YAML leg, while
    release_lock() took only the inner '<state>.yaml.lock'. Two interleavings
    put a released lock back, both ending in a durable 'locked' record naming
    an issue whose run had ended, with the 4h staleness clock reset:

      - inside the YAML leg: its read and its write each took and released the
        inner lock separately, so a release could unlink the state file in the
        gap and the write re-created it -- `allow_reestablish` did not gate
        this at all, because `existing` was present at read time; and
      - across the two legs: the Redis leg refreshed the key, the release then
        deleted the key AND unlinked the state file, and the YAML leg re-created
        the durable record because redis_ok said Redis still named this holder.

    Nothing reclaims such a lock at runtime (_reconcile_active_runs() is
    startup-only, pipeline_watchdog reaps runs rather than locks), so the board
    stops dispatching until an operator intervenes -- and every get_lock()-based
    probe reports a phantom holder in the meantime.

    Both are closed the same way: touch_lock() and release_lock() take the
    acquire guard for the WHOLE of their two-store work, and the YAML leg's
    read and write share one held inner lock.

    #139 audit: YAML-only, and only the FIRST of the two interleavings above is
    what this class proves. Its last test turns on try_acquire_lock()'s YAML
    fallback specifically, and the file-lock ordering the other three assert on
    is the YAML leg's. The second, cross-leg interleaving needs a
    Redis to exist at all and is covered by
    TestTouchAndReleaseCannotInterleaveAcrossTheTwoStores below -- until #139
    it was covered by nothing, because redis_client=None here silently meant
    "no Redis" rather than "connect one yourself".
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        self.state_file = self.manager._get_state_file("proj", "board")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_a_release_landing_mid_refresh_wins_and_the_lock_stays_released(self):
        """
        The reviewer's repro, as the concurrency it actually models: a real
        release_lock() on another thread, landing between the YAML leg's read
        and its write. It must block on the guard rather than interleave, and
        the lock must be gone once both have finished -- a release that
        completes cannot leave a 'locked' record behind it.
        """
        self.manager._create_lock("proj", "board", 123)
        released = []
        workers = []
        real_refreshed_from = self.manager._refreshed_from

        def release_from_another_thread(*args, **kwargs):
            worker = threading.Thread(
                target=lambda: released.append(
                    self.manager.release_lock("proj", "board", 123)
                )
            )
            workers.append(worker)
            worker.start()
            # Long enough for the release to reach (and block on) the guard.
            time.sleep(0.3)
            return real_refreshed_from(*args, **kwargs)

        with patch.object(self.manager, '_refreshed_from', side_effect=release_from_another_thread):
            self.manager.touch_lock("proj", "board", 123)

        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(released, [ReleaseResult.RELEASED])
        self.assertFalse(self.state_file.exists())
        self.assertIsNone(self.manager.get_lock("proj", "board"))

    def test_the_yaml_leg_holds_the_state_lock_across_its_read_and_its_write(self):
        """
        The structural half of the same fix. release_lock() takes
        '<state>.yaml.lock' to verify ownership and unlink, so the leg's read
        and its write must happen inside ONE held instance of that lock -- two
        separate acquisitions leave a gap a release fits through, and the write
        that follows re-creates what it deleted.
        """
        self.manager._create_lock("proj", "board", 123)
        spy = _FileLockSpy()
        real_read = self.manager._read_yaml_lock_only_unlocked
        real_write = self.manager._save_lock_to_yaml_unlocked

        def noted_read(*args, **kwargs):
            spy.note('read')
            return real_read(*args, **kwargs)

        def noted_write(*args, **kwargs):
            spy.note('write')
            return real_write(*args, **kwargs)

        with spy.patch(), \
                patch.object(self.manager, '_read_yaml_lock_only_unlocked', side_effect=noted_read), \
                patch.object(self.manager, '_save_lock_to_yaml_unlocked', side_effect=noted_write):
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        state_lock = str(self.state_file) + '.lock'
        self.assertIn(
            [('enter', state_lock), ('note', 'read'), ('note', 'write'), ('exit', state_lock)],
            [spy.events[i:i + 4] for i in range(len(spy.events) - 3)],
        )

    def test_release_lock_takes_the_acquire_guard_around_its_whole_delete(self):
        """Deleting the Redis key and unlinking the state file are two writes;
        without the guard, touch_lock() could read one store before and the
        other after."""
        self.manager._create_lock("proj", "board", 123)
        spy = _FileLockSpy()

        with spy.patch():
            self.assertTrue(self.manager.release_lock("proj", "board", 123))

        self.assertEqual(spy.paths[0], str(self.state_file) + '.acquire.lock')
        self.assertEqual(spy.events[-1], ('exit', str(self.state_file) + '.acquire.lock'))
        self.assertIn(str(self.state_file) + '.lock', spy.paths[1:])

    def test_the_stale_lock_recovery_path_does_not_deadlock_on_its_own_guard(self):
        """
        try_acquire_lock()'s YAML fallback auto-releases a >4h lock from INSIDE
        the acquire guard, so it must go through _release_lock_unguarded():
        utils.file_lock refuses a re-entrant acquire (ReentrantFileLockError)
        rather than hanging on it, which would have turned every stale-lock
        recovery into a hard failure.
        """
        stale = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=123,
            lock_acquired_at=(datetime.now(timezone.utc) - timedelta(hours=5)).isoformat(),
            lock_status='locked',
        )
        self.manager._save_lock_to_yaml(stale)

        acquired, reason = self.manager.try_acquire_lock("proj", "board", 456)

        self.assertTrue(acquired)
        self.assertEqual(reason, "stale_lock_recovered")
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, 456)


class TestTouchLockGuardsBothLegs(unittest.TestCase):
    """
    The cross-leg half of the interleaving above: the guard has to be held
    across the REDIS leg too, not just the YAML one. Held only around the YAML
    leg, a release could complete in full between the two -- Redis refreshed,
    then key deleted and state file unlinked -- and the YAML leg would re-create
    the durable record because the Redis leg had already reported REFRESHED.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_the_redis_leg_runs_inside_the_acquire_guard(self):
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)
        self.manager._create_lock("proj", "board", 123)
        spy = _FileLockSpy()

        def noted_transaction(*args, **kwargs):
            spy.note('redis_transaction')
            return _touch_transaction_side_effect(_redis_lock_hash(123))(*args, **kwargs)

        self.mock_redis.transaction.side_effect = noted_transaction

        with spy.patch():
            result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.REFRESHED)
        guard = str(self.manager._get_acquire_guard_file("proj", "board"))
        opened = spy.events.index(('enter', guard))
        closed = spy.events.index(('exit', guard))
        transacted = spy.events.index(('note', 'redis_transaction'))
        self.assertLess(opened, transacted)
        self.assertLess(transacted, closed)


class TestTouchLockSurfacesAYamlConfirmedLoss(unittest.TestCase):
    """
    Found in the #153 WI-8 review round: a YAML-confirmed loss was downgraded
    to REFRESH_FAILED whenever a Redis client was merely CONFIGURED, on the
    reasoning that "Redis already answered above". It has not answered when its
    own leg raised -- and a Redis outage is precisely when try_acquire_lock()
    falls to its YAML path and hands the lock to a second caller, so the YAML
    record is the only store that knows. Reporting REFRESH_FAILED there left
    project_checkout_lock's "may now be racing a different holder" ERROR
    unreachable for that outage, which is the one thing TouchResult exists to
    make loud.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.mock_redis = MagicMock()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.mock_redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_not_held_when_the_redis_leg_errored_and_yaml_names_another_holder(self):
        self.mock_redis.hgetall.return_value = _redis_lock_hash(123)  # opening read: ours
        self.manager._create_lock("proj", "board", 456)  # what is actually on disk
        self.mock_redis.transaction.side_effect = Exception("connection reset by peer")

        result = self.manager.touch_lock("proj", "board", 123)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(result)
        # Read the YAML copy directly: get_lock() prefers the (mocked) Redis
        # record, which is the stale view this test is about.
        yaml_lock, _ = self.manager._read_yaml_lock_only("proj", "board")
        self.assertEqual(yaml_lock.locked_by_issue, 456)


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
        # use_redis=False, not redis_client=None: None means "connect one
        # yourself", and the YAML fallback this class is named for is only
        # reached when there is genuinely no Redis (#139).
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)

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

    def test_a_guard_refusal_leaves_the_existing_holder_exactly_as_it_was(self):
        """#174 review round: the guard is taken BEFORE either store is read or
        written, so this refusal decides nothing about who holds the lock -- the
        previous holder is still the recorded one, and at project_monitor's two
        dispatch gates (both reached by an issue that may have carried the lock
        in from an earlier stage) that is often the refused caller itself.

        The asymmetry makes it the likely outcome rather than a race: the
        acquire gives the guard utils.file_lock's 10s default while
        release_lock() waits RELEASE_GUARD_TIMEOUT_SECONDS then
        RELEASE_GUARD_RETRY_TIMEOUT_SECONDS for the same file, so a guard
        contended past 10s refuses the acquire and then grants the release that
        end_pipeline_run() issues on the back of it.

        refusal_must_not_end_caller_run() is what a teardown call site has to
        ask, because the reason string cannot tell holder from contender --
        refusal_leaves_caller_holding_lock() deliberately still says False here.
        """
        from utils.file_lock import file_lock as _real_file_lock

        def _guard_is_busy(path, *args, **kwargs):
            if str(path).endswith('.acquire.lock'):
                raise TimeoutError("guard busy")
            return _real_file_lock(path, *args, **kwargs)

        self.assertEqual(
            self.manager.try_acquire_lock("proj", "board", 159), (True, "lock_acquired")
        )

        with patch('utils.file_lock.file_lock', side_effect=_guard_is_busy):
            success, reason = self.manager.try_acquire_lock("proj", "board", 159)

        self.assertEqual((success, reason), (False, "lock_acquire_serialization_timeout"))
        self.assertEqual(self.manager.get_lock_holder("proj", "board"), 159)
        self.assertFalse(refusal_leaves_caller_holding_lock(reason))
        self.assertTrue(
            refusal_must_not_end_caller_run(reason, self.manager, "proj", "board", 159)
        )
        self.assertIsNone(
            refusal_must_not_end_caller_run(reason, self.manager, "proj", "board", 999),
            "a genuine contender must still be free to end its own run",
        )


class TestReleaseIsNotAbandonedByGuardContention(unittest.TestCase):
    """
    Found in the WI-8 review round. release_lock() gained the acquire guard in
    this work item, with utils.file_lock's incidental 10s default as its only
    budget and a bare False on timeout -- and both halves of that were wrong:

      - The contention is asymmetric against the release. try_acquire_lock()'s
        YAML-fallback path takes the SAME guard and is reached exactly when
        Redis is unavailable, so every attempt holds it for several seconds of
        Redis socket timeouts while its waiters re-poll every
        DEFAULT_POLL_INTERVAL_SECONDS. An acquire that loses is retried
        seconds later; a release that loses is simply dropped, and the comment
        at project_checkout_lock's release site spells out the cost -- nothing
        else in the process knows that holder_id, so the lock leaks until the
        7200s TTL or the 4-hour staleness heuristic, blocking every
        acquisition for that project meanwhile.
      - Every caller read that False as "retained", the same value the method
        returns for a considered-and-refused release. The three that log about
        it told operators to go looking for a durable failure record that does
        not exist.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # YAML-only (#139 audit): this whole class models Redis being
        # unavailable -- that is the exact condition under which
        # try_acquire_lock() takes the guard release_lock() then has to
        # contend with. use_redis=False, not redis_client=None, which
        # means "connect one yourself".
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        self.state_file = self.manager._get_state_file("proj", "board")
        self.manager._create_lock("proj", "board", 123)
        self.guard_file = self.manager._get_acquire_guard_file("proj", "board")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @contextlib.contextmanager
    def _guard_held_elsewhere(self, hold_seconds):
        """
        Hold the acquire guard from ANOTHER thread for `hold_seconds`, the way
        a concurrent try_acquire_lock() burning Redis socket timeouts does.
        Another thread rather than this one because utils.file_lock refuses a
        re-entrant acquire outright instead of contending.
        """
        from utils.file_lock import file_lock

        taken = threading.Event()

        def hold():
            with file_lock(self.guard_file):
                taken.set()
                time.sleep(hold_seconds)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(taken.wait(timeout=10))
        try:
            yield
        finally:
            holder.join(timeout=30)
            self.assertFalse(holder.is_alive())

    def test_a_contended_guard_is_retried_on_a_longer_budget_and_the_release_happens(self):
        """
        The release must not be abandoned just because the first, short
        attempt lost the guard -- it has already been authorized, and its
        caller does not re-reach this site (pipeline_progression's release
        fires only on the move INTO an exit column, so it never re-fires).
        """
        with patch('services.pipeline_lock_manager.RELEASE_GUARD_TIMEOUT_SECONDS', 0), \
                patch('services.pipeline_lock_manager.RELEASE_GUARD_RETRY_TIMEOUT_SECONDS', 10), \
                self.assertLogs('services.pipeline_lock_manager', level='WARNING') as logs, \
                self._guard_held_elsewhere(0.4):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.RELEASED)
        self.assertFalse(self.state_file.exists())
        # The first attempt really did lose the guard -- otherwise this test
        # would pass without the retry existing at all.
        self.assertTrue(
            any("retrying for up to" in line for line in logs.output),
            logs.output,
        )

    def test_a_guard_that_never_frees_reports_serialization_failed_not_a_refusal(self):
        """
        The distinction the three logging callers depend on. NOT_RELEASED means
        the release was considered and correctly declined, so the lock's state
        is exactly what the caller was told; SERIALIZATION_FAILED means nothing
        was attempted at all and the release is still outstanding. Both are
        falsy, so callers written against the old bool keep their meaning.
        """
        with patch('services.pipeline_lock_manager.RELEASE_GUARD_TIMEOUT_SECONDS', 0), \
                patch('services.pipeline_lock_manager.RELEASE_GUARD_RETRY_TIMEOUT_SECONDS', 0), \
                self._guard_held_elsewhere(0.6):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.SERIALIZATION_FAILED)
        self.assertFalse(result)
        # Nothing was attempted, so the lock is exactly as it was.
        self.assertTrue(self.state_file.exists())
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, 123)

    def test_a_guard_file_that_cannot_be_opened_is_also_serialization_failed(self):
        """An OSError on the guard file is the same fact as a timeout: the
        release was never considered, so it must not be reported as one that
        was."""
        with patch('utils.file_lock.file_lock', side_effect=OSError("no fds")):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.SERIALIZATION_FAILED)
        self.assertTrue(self.state_file.exists())

    def test_a_considered_refusal_is_still_not_released(self):
        """The other side of the split: a release that IS considered and
        declined (here, by an issue that does not hold the lock) must stay
        NOT_RELEASED, or the new sentinel would swallow the retained-lock
        diagnosis it was added to protect."""
        result = self.manager.release_lock("proj", "board", 456)

        self.assertIs(result, ReleaseResult.NOT_RELEASED)
        self.assertTrue(self.state_file.exists())

    def test_an_unserializable_yaml_read_is_reported_as_a_read_failure(self):
        """The generic contract of _read_yaml_lock_only() is unchanged: a read
        that cannot be serialized collapses into read_ok=False, which every
        caller of it treats fail-closed. Only the release path (below) needs
        the finer answer."""
        with patch('utils.file_lock.file_lock', side_effect=TimeoutError("inner lock busy")):
            lock, read_ok = self.manager._read_yaml_lock_only("proj", "board")

        self.assertIsNone(lock)
        self.assertFalse(read_ok)


class TestEveryInnerStateLockAcquisitionInsideTheGuardIsBounded(unittest.TestCase):
    """
    REGRESSION (WI-8 review round). release_lock()'s bounded wait for the
    '<state>.yaml.acquire.lock' guard is only derivable if a guard HOLDER is
    itself bounded, and the first pass at that bounded only ONE of the three
    acquisitions of the inner '<state>.yaml.lock' that run inside the guard:

      - _read_yaml_lock_only() -- was fixed,
      - _release_lock_unguarded()'s YAML delete -- was left `with file_lock(p)`,
        a blocking flock reached from release_lock() and from
        try_acquire_lock()'s stale-lock recovery, both inside the guard,
      - _save_lock_to_yaml() -> safe_yaml_write() -- likewise, reached inside
        the guard via _create_lock().

    So the guard hold still had no bound, and the retry budget sized against
    "roughly 35s worst case" was not derivable from anything. The old test for
    this asserted only on _read_yaml_lock_only() and would have passed
    unchanged with both remaining sites blocking, so it is replaced here by one
    that walks the whole guarded section.

    "The whole guarded section" includes the REDIS branch (#139 review round):
    it takes the same guard, and the _create_lock_yaml_only() that mirrors its
    grant to disk makes two more inner-lock takes inside that hold. The
    manager here is YAML-only, so the last test below builds its own with a
    ThreadSafeFakeRedis and walks that branch too -- without it, a
    _save_lock_to_yaml() regressed back to an unbounded safe_yaml_write()
    would leave every test in this class passing while the hold on the path
    production takes whenever Redis is up became unbounded again.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # YAML-only for the first four tests: they walk the YAML leg's inner
        # '<state>.yaml.lock' takes inside the acquire guard. use_redis=False,
        # not redis_client=None -- see TestConstructionSaysWhichStoresItHas.
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        self.state_file = self.manager._get_state_file("proj", "board")
        self.state_lock = str(self.state_file) + '.lock'

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @contextlib.contextmanager
    def _recording_file_lock(self, seen):
        """Record every file_lock() acquisition, including the ones
        safe_yaml_write() makes on its callers' behalf (it takes the module
        global, so patching it here covers that path too)."""
        import utils.file_lock as file_lock_module
        real_file_lock = file_lock_module.file_lock

        @contextlib.contextmanager
        def noting(path, *args, **kwargs):
            seen.append((str(path), kwargs.get('enforce_timeout', False)))
            with real_file_lock(path, *args, **kwargs):
                yield

        with patch('utils.file_lock.file_lock', side_effect=noting):
            yield

    def _assert_state_lock_always_bounded(self, seen):
        state_lock_takes = [enforce for path, enforce in seen if path == self.state_lock]
        self.assertTrue(state_lock_takes, f"the inner state lock was never taken: {seen}")
        self.assertTrue(
            all(state_lock_takes),
            f"an unbounded acquisition of {self.state_lock}: {seen}",
        )

    def test_a_release_never_takes_the_inner_state_lock_unbounded(self):
        """Two acquisitions: the fail-closed state read, and the delete."""
        self.manager._create_lock("proj", "board", 123)
        seen = []

        with self._recording_file_lock(seen):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.RELEASED)
        self.assertGreaterEqual(
            len([p for p, _ in seen if p == self.state_lock]), 2, seen
        )
        self._assert_state_lock_always_bounded(seen)

    def test_a_yaml_fallback_acquire_never_takes_the_inner_state_lock_unbounded(self):
        """_create_lock() -> _save_lock_to_yaml() -> safe_yaml_write(), inside
        try_acquire_lock()'s guard."""
        seen = []

        with self._recording_file_lock(seen):
            success, _reason = self.manager.try_acquire_lock("proj", "board", 123)

        self.assertTrue(success)
        self._assert_state_lock_always_bounded(seen)

    def test_the_stale_lock_recovery_never_takes_the_inner_state_lock_unbounded(self):
        """The longest guarded section there is, and the one
        RELEASE_GUARD_RETRY_TIMEOUT_SECONDS is sized against: a read, a
        fail-closed read, a delete and a write, all inside one guard hold."""
        stale = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=111,
            lock_acquired_at=(
                datetime.now(timezone.utc) - timedelta(hours=5)
            ).isoformat(),
            lock_status='locked',
        )
        self.assertTrue(self.manager._save_lock_to_yaml(stale))
        seen = []

        with self._recording_file_lock(seen):
            success, reason = self.manager.try_acquire_lock("proj", "board", 222)

        self.assertTrue(success)
        self.assertEqual(reason, "stale_lock_recovered")
        self.assertGreaterEqual(
            len([p for p, _ in seen if p == self.state_lock]), 4, seen
        )
        self._assert_state_lock_always_bounded(seen)

    def test_a_redis_acquire_never_takes_the_inner_state_lock_unbounded(self):
        """
        The branch production actually takes whenever Redis is up, and the one
        the rest of this class cannot see: try_acquire_lock()'s Redis grant
        mirrors itself to disk with _create_lock_yaml_only(), whose
        _read_yaml_lock_only() and _save_lock_to_yaml() are two more inner-lock
        takes inside the same guard hold.
        """
        fake_redis = ThreadSafeFakeRedis()
        manager = PipelineLockManager(
            state_dir=Path(self.test_dir), redis_client=fake_redis
        )
        # A stale record in BOTH stores, so the mirror has an existing lock to
        # read and a different holder to write over -- an acquire on an empty
        # board short-circuits both of _read_yaml_lock_only()'s takes (no file
        # to lock) and would walk only one of the three.
        stale = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=111,
            lock_acquired_at=(
                datetime.now(timezone.utc) - timedelta(hours=5)
            ).isoformat(),
            lock_status='locked',
        )
        self.assertTrue(manager._save_lock_to_yaml(stale))
        fake_redis.hset(
            manager._get_lock_key("proj", "board"),
            mapping=manager._lock_to_redis_mapping(stale),
        )
        seen = []

        with self._recording_file_lock(seen):
            success, reason = manager.try_acquire_lock("proj", "board", 222)

        self.assertEqual((success, reason), (True, "lock_acquired"))
        # The upfront fail-closed read, plus the mirror's read and its write.
        self.assertGreaterEqual(
            len([p for p, _ in seen if p == self.state_lock]), 3, seen
        )
        self._assert_state_lock_always_bounded(seen)

    def test_a_write_that_cannot_be_serialized_is_reported_as_a_write_failure(self):
        """safe_yaml_write() had no way to be bounded at all before this, so
        the pass-through is half the fix; the other half is that a timeout
        arrives as the write failure _save_lock_to_yaml()'s callers already
        model, rather than as an unbounded park inside the guard."""
        seen = []
        lock = PipelineLock(
            project="proj", board="board", locked_by_issue=1,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked',
        )

        # The holder is started BEFORE the recorder so its own (deliberately
        # blocking) acquire is not one of the acquisitions under test.
        with patch('services.pipeline_lock_manager.STATE_LOCK_TIMEOUT_SECONDS', 0), \
                self._state_lock_held_elsewhere(0.4), \
                self._recording_file_lock(seen):
            written = self.manager._save_lock_to_yaml(lock)

        self.assertFalse(written)
        self._assert_state_lock_always_bounded(seen)

    @contextlib.contextmanager
    def _state_lock_held_elsewhere(self, hold_seconds):
        """Hold '<state>.yaml.lock' from ANOTHER thread -- utils.file_lock
        refuses a re-entrant acquire outright instead of contending."""
        from utils.file_lock import file_lock

        taken = threading.Event()

        def hold():
            with file_lock(Path(self.state_lock)):
                taken.set()
                time.sleep(hold_seconds)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(taken.wait(timeout=10))
        try:
            yield
        finally:
            holder.join(timeout=30)
            self.assertFalse(holder.is_alive())


class TestAnUnserializableReleaseIsNotMisreportedAsARefusal(unittest.TestCase):
    """
    REGRESSION (WI-8 review round). Bounding the inner '<state>.yaml.lock'
    recreated, one level down, exactly the misattribution the guard's
    ReleaseResult split had just removed: a 20s timeout on that inner lock came
    back from _read_yaml_lock_only() as reads_healthy=False, which
    _release_lock_unguarded() fails closed on ("could not determine lock state
    ... refusing to release without force=True") and _release_lock_to_result()
    then mapped to NOT_RELEASED.

    NOT_RELEASED means the release was CONSIDERED and correctly declined -- and
    pipeline_progression._release_lock_and_process_next() acts on it by logging
    "it is held by this issue but likely retained due to a failed run ... Use
    scripts/release_lock.py to investigate" and returning without ending the
    run or dispatching the next queued issue. That site fires only on the move
    INTO an exit column, so it never re-fires: the board wedges until a human
    chases a durable failure record that does not exist.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # YAML-only (#139 audit): the release this refuses is refused
        # because the YAML leg's inner state lock cannot be taken, so
        # the on-disk copy has to be the store that decides.
        # use_redis=False, not redis_client=None.
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        self.state_file = self.manager._get_state_file("proj", "board")
        self.manager._create_lock("proj", "board", 123)
        self.state_lock = self.state_file.with_suffix(self.state_file.suffix + '.lock')

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    @contextlib.contextmanager
    def _state_lock_held_elsewhere(self, hold_seconds):
        """Hold '<state>.yaml.lock' from ANOTHER thread, the way a concurrent
        touch_lock() or _save_lock_to_yaml() does. Another thread rather than
        this one because utils.file_lock refuses a re-entrant acquire outright
        instead of contending."""
        from utils.file_lock import file_lock

        taken = threading.Event()

        def hold():
            with file_lock(self.state_lock):
                taken.set()
                time.sleep(hold_seconds)

        holder = threading.Thread(target=hold)
        holder.start()
        self.assertTrue(taken.wait(timeout=10))
        try:
            yield
        finally:
            holder.join(timeout=30)
            self.assertFalse(holder.is_alive())

    def test_an_inner_lock_timeout_is_serialization_failed_not_not_released(self):
        with patch('services.pipeline_lock_manager.STATE_LOCK_TIMEOUT_SECONDS', 0), \
                self._state_lock_held_elsewhere(0.5):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.SERIALIZATION_FAILED)
        self.assertFalse(result)
        # The lock is exactly as it was -- which is what makes NOT_RELEASED's
        # "likely retained due to a failed run" the wrong thing to tell an
        # operator about it.
        self.assertTrue(self.state_file.exists())
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, 123)

    def test_a_delete_that_cannot_be_serialized_is_also_serialization_failed(self):
        """The second of the release's two inner-lock acquisitions. The state
        read succeeds here; only the delete loses the lock."""
        real_detail = self.manager._get_lock_fail_closed_detail

        with patch('services.pipeline_lock_manager.STATE_LOCK_TIMEOUT_SECONDS', 0), \
                patch.object(
                    self.manager, '_get_lock_fail_closed_detail',
                    side_effect=lambda p, b: (real_detail(p, b)[0], True, False),
                ), \
                self._state_lock_held_elsewhere(0.5):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.SERIALIZATION_FAILED)
        self.assertTrue(self.state_file.exists())

    def test_a_genuinely_unreadable_state_is_still_a_refusal(self):
        """The other side of the split, and the reason it cannot simply be
        collapsed: an unreadable lock state IS a considered outcome -- the
        release fails closed on it deliberately -- so it must stay
        NOT_RELEASED, or the fail-closed refusal becomes indistinguishable
        from contention."""
        with patch.object(
            self.manager, '_read_yaml_lock_only_detail', return_value=(None, False, False)
        ):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertIs(result, ReleaseResult.NOT_RELEASED)
        self.assertTrue(self.state_file.exists())

    def test_the_stale_lock_recovery_refuses_rather_than_stealing_the_lock(self):
        """_release_lock_unguarded()'s other caller. An acquire is the easy
        side of this: refusing costs one poll cycle and its own poll loop
        retries. What it must NOT do is let the exception fall into the generic
        "Failed to check lock age" handler and then go on to _create_lock(),
        stealing a lock it could not verify it had released."""
        stale = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=111,
            lock_acquired_at=(
                datetime.now(timezone.utc) - timedelta(hours=5)
            ).isoformat(),
            lock_status='locked',
        )
        self.assertTrue(self.manager._save_lock_to_yaml(stale))

        with patch.object(
            self.manager, '_read_yaml_lock_only_detail', return_value=(stale, True, True)
        ):
            with self.assertLogs('services.pipeline_lock_manager', level='WARNING') as logs:
                success, reason = self.manager.try_acquire_lock("proj", "board", 222)

        self.assertFalse(success)
        self.assertEqual(reason, "locked_by_issue_111")
        self.assertNotIn(
            "Failed to check lock age", "\n".join(logs.output), logs.output
        )
        # The stale holder's record survives -- the recovery refused rather
        # than half-clearing it.
        self.assertEqual(self.manager.get_lock("proj", "board").locked_by_issue, 111)

    def test_an_unserializable_release_never_reaches_the_retained_lock_diagnosis(self):
        """The end-to-end shape of the misattribution, at the boundary the
        three logging callers read: SERIALIZATION_FAILED is falsy like
        NOT_RELEASED (so callers written against the old bool are unchanged)
        but is a different member, which is what lets
        pipeline_progression/end_pipeline_run/_release_and_warn stop reporting
        contention as a retained failed run."""
        with patch('services.pipeline_lock_manager.STATE_LOCK_TIMEOUT_SECONDS', 0), \
                self._state_lock_held_elsewhere(0.5):
            result = self.manager.release_lock("proj", "board", 123)

        self.assertFalse(result)
        self.assertIsNot(result, ReleaseResult.NOT_RELEASED)


class TestConstructionSaysWhichStoresItHas(unittest.TestCase):
    """
    #139. A function-local `import os` in the `state_dir is None` branch made
    `os` local to the whole of __init__ (any name assigned anywhere in a
    function is local to all of it), so a caller who supplied state_dir and
    omitted redis_client -- the documented "connect one yourself" default --
    hit an UnboundLocalError on the os.environ.get() in the connect block. The
    surrounding except swallowed it, logged "Redis connection failed for locks"
    for a connection that had never been attempted, and latched that instance
    into YAML-only mode.

    Dormant in production (no call site passes state_dir without a client), but
    load-bearing for this suite: every lock test that passed a state_dir was
    exercising the YAML fallback while reading as though it covered the Redis
    WATCH/MULTI path. PR #155 found a real double-grant in that fallback under
    coverage that had never once run against Redis.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_a_state_dir_with_no_client_still_connects(self):
        """The regression itself: this raised UnboundLocalError inside the
        except, so redis.Redis was never even called."""
        fake = ThreadSafeFakeRedis()
        with patch('services.pipeline_lock_manager.redis.Redis', return_value=fake) as mock_redis:
            manager = PipelineLockManager(state_dir=Path(self.test_dir))

        mock_redis.assert_called_once()
        self.assertIs(manager.redis_client, fake)

    def test_a_genuine_connect_failure_still_falls_back_to_yaml(self):
        with patch('services.pipeline_lock_manager.redis.Redis',
                   side_effect=OSError("no route to host")):
            manager = PipelineLockManager(state_dir=Path(self.test_dir))

        self.assertIsNone(manager.redis_client)

    def test_use_redis_false_attempts_no_connection_at_all(self):
        """The intent that had no way to be expressed before #139, and the
        reason so many tests were accidentally YAML-only: redis_client=None
        means "connect one yourself", so there was nothing to pass."""
        with patch('services.pipeline_lock_manager.redis.Redis') as mock_redis:
            manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)

        mock_redis.assert_not_called()
        self.assertIsNone(manager.redis_client)

    def test_use_redis_false_does_not_report_a_failed_connection(self):
        """The misleading half of the old behaviour: an operator reading
        "Redis connection failed for locks" went looking for an outage that
        was not happening."""
        with self.assertLogs('services.pipeline_lock_manager', level='INFO') as logs:
            PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)

        self.assertNotIn("Redis connection failed", "\n".join(logs.output))

    def test_a_client_with_use_redis_false_is_refused_rather_than_half_honoured(self):
        with self.assertRaises(ValueError):
            PipelineLockManager(
                state_dir=Path(self.test_dir),
                redis_client=ThreadSafeFakeRedis(),
                use_redis=False,
            )

    def test_a_misconfigured_port_is_not_laundered_as_an_outage(self):
        """Found in the #139 review round: fixing the UnboundLocalError left
        the mechanism that made it silent in place. `int(REDIS_PORT)` inside
        the try meant a typo in .env was reported as "Redis connection failed
        for locks", sending an operator after an outage that was not happening
        while every lock in the process ran through the YAML fallback PR #155
        found a double-grant in."""
        with patch.dict(os.environ, {'REDIS_PORT': 'redis'}):
            with self.assertRaises(ValueError):
                PipelineLockManager(state_dir=Path(self.test_dir))

    def test_a_programming_error_in_the_connect_block_is_not_reported_as_an_outage(self):
        """Same mechanism, the next error to land in it: only what genuinely
        means "the service is not reachable" may be absorbed into YAML-only."""
        with patch('services.pipeline_lock_manager.redis.Redis',
                   side_effect=TypeError("unexpected keyword argument")):
            with self.assertRaises(TypeError):
                PipelineLockManager(state_dir=Path(self.test_dir))

    def test_a_redis_error_is_still_absorbed(self):
        """The narrowing must not stop a genuine outage from degrading."""
        with patch('services.pipeline_lock_manager.redis.Redis',
                   side_effect=redis.exceptions.ConnectionError("connection refused")):
            manager = PipelineLockManager(state_dir=Path(self.test_dir))

        self.assertIsNone(manager.redis_client)

    def test_a_degraded_instance_is_distinguishable_from_a_deliberate_one(self):
        """`redis_client is None` is checked in a dozen places and means both
        "deliberately YAML-only" and "Redis fell over at boot and we silently
        degraded", which health reporting has no other way to tell apart."""
        deliberate = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        with patch('services.pipeline_lock_manager.redis.Redis',
                   side_effect=OSError("no route to host")):
            degraded = PipelineLockManager(state_dir=Path(self.test_dir))

        self.assertFalse(deliberate.use_redis)
        self.assertTrue(degraded.use_redis)
        self.assertIsNone(deliberate.redis_client)
        self.assertIsNone(degraded.redis_client)


class TestOneRedisRoundTripIsBoundedByItsSocketTimeout(unittest.TestCase):
    """
    REGRESSION (#139 review round). Every budget in this module that mentions
    Redis is derived from "a call against a Redis that is not answering costs
    socket_connect_timeout" -- and that was not true of the client this class
    builds. redis-py has defaulted every client to
    Retry(ExponentialWithJitterBackoff(), retries=10) since 6.0, so the
    5s socket timeouts bought eleven attempts plus backoff: ~59s per call
    against a host that drops SYNs, measured against this deployment's 8.1.0.

    That multiplier lands inside the '<state>.yaml.acquire.lock' guard --
    try_acquire_lock() takes it around BOTH branches now, and the YAML fallback
    makes four more Redis calls after the Redis branch has already failed -- so
    a partitioned (not refused) Redis stretched one acquire's guard hold to
    minutes, past the RELEASE_GUARD_RETRY_TIMEOUT_SECONDS a concurrent
    release_lock() is willing to wait. The release then returns
    SERIALIZATION_FAILED, nothing reclaims the lock, and the board stops
    dispatching until the 4-hour staleness heuristic.

    Nothing about that is visible at a call site, and the comment sizing the
    budget said "5s socket timeout each", so this pins the multiplier itself
    rather than the prose.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_the_client_is_built_with_an_explicit_retry_policy(self):
        """A client built without `retry=` silently inherits redis-py's
        default, which is the whole defect."""
        with patch('services.pipeline_lock_manager.redis.Redis',
                   return_value=ThreadSafeFakeRedis()) as mock_redis:
            PipelineLockManager(state_dir=Path(self.test_dir))

        kwargs = mock_redis.call_args.kwargs
        self.assertIs(kwargs.get('retry'), LOCK_REDIS_RETRY)
        self.assertEqual(
            kwargs.get('socket_connect_timeout'), LOCK_REDIS_SOCKET_TIMEOUT_SECONDS
        )
        self.assertEqual(
            kwargs.get('socket_timeout'), LOCK_REDIS_SOCKET_TIMEOUT_SECONDS
        )

    def test_the_policy_makes_exactly_one_attempt(self):
        """Asserted through redis-py's own retry machinery rather than by
        reading LOCK_REDIS_RETRY's constructor arguments back: what matters is
        how many times the socket timeout is actually paid."""
        attempts = []

        def do():
            attempts.append(1)
            raise redis.exceptions.ConnectionError("no route to host")

        with self.assertRaises(redis.exceptions.ConnectionError):
            LOCK_REDIS_RETRY.call_with_retry(do, lambda _e: None)

        self.assertEqual(len(attempts), 1, attempts)

    def test_the_release_budget_still_covers_the_worst_case_guard_hold(self):
        """The derivation spelled out at RELEASE_GUARD_RETRY_TIMEOUT_SECONDS,
        as arithmetic: the longest guarded section is a failed Redis branch
        followed by the YAML fallback's stale-lock recovery -- four bounded
        inner-lock takes and five Redis calls that each have to fail first.
        Changing any of the three constants without re-deriving this is what
        made the figure it used to cite unrecoverable."""
        worst_case_guard_hold = (
            4 * STATE_LOCK_TIMEOUT_SECONDS + 5 * LOCK_REDIS_SOCKET_TIMEOUT_SECONDS
        )

        self.assertGreaterEqual(
            RELEASE_GUARD_RETRY_TIMEOUT_SECONDS,
            worst_case_guard_hold,
            "a release now gives up before the longest legitimate guard holder "
            "can let go, which leaks the lock until the 4-hour staleness heuristic",
        )


class TestAGrantIsOnlyReportedWhenItsDurableCopyLanded(unittest.TestCase):
    """
    The remaining hole in the both-stores-agree invariant
    TestAcquireAndReleaseCannotInterleaveAcrossTheTwoStores establishes (#139
    review round). The guard closed the path where a CONCURRENT RELEASE
    unlinked the incoming holder's state file; it does nothing about the path
    where the file was never written.

    Both grant paths discarded the bool that says so:

      - the Redis branch called _create_lock_yaml_only() and returned
        (True, result) unconditionally, and that helper swallowed
        _save_lock_to_yaml()'s False; and
      - the YAML fallback called _create_lock() and returned
        (True, 'lock_acquired')/(True, 'stale_lock_recovered') without looking,
        even though _create_lock()'s own docstring names that caller as the one
        that must not treat a both-failed write as an acquisition.

    A full or read-only state dir, or another process (scripts/release_lock.py,
    the observability server's release endpoint) holding '<state>.yaml.lock'
    past STATE_LOCK_TIMEOUT_SECONDS, was therefore enough to dispatch a run on
    a lock whose only copy was the TTL'd Redis key -- invisible to
    get_all_locks(), a HEALTHY "no lock" to _read_yaml_lock_only(), and gone at
    LOCK_TTL_SECONDS, at which point the same board is granted to a second
    issue while the first run is still live.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.redis = ThreadSafeFakeRedis()
        self.manager = PipelineLockManager(
            state_dir=Path(self.test_dir), redis_client=self.redis
        )
        self.lock_key = self.manager._get_lock_key("proj", "board")
        self.state_file = self.manager._get_state_file("proj", "board")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_a_redis_grant_with_no_durable_copy_is_refused_and_rolled_back(self):
        with patch.object(self.manager, '_save_lock_to_yaml', return_value=False):
            success, reason = self.manager.try_acquire_lock("proj", "board", 456)

        self.assertEqual((success, reason), (False, "lock_mirror_write_failed"))
        self.assertEqual(
            self.redis.hgetall(self.lock_key), {},
            "the Redis-only grant survived, so it still vanishes with its TTL "
            "and the board is granted twice",
        )
        self.assertFalse(self.state_file.exists())

    def test_a_live_holders_key_is_not_deleted_by_the_refusal(self):
        """The repeat call an issue makes on every poll while it holds the
        lock. Its mirror can fail too -- but rolling THAT back would release a
        lock out from under a running pipeline, so it is refused in place.

        The reason is DISTINCT from the new-grant refusal (#139 review round):
        this is the one refusal that PROVES the caller is still the recorded
        holder on the reason string alone, and its teardown call sites branch on
        exactly that -- see refusal_leaves_caller_holding_lock(), and
        refusal_must_not_end_caller_run() for the acquire-guard refusals, which
        leave the holder in place too but have to be read to find out.
        """
        self.assertEqual(
            self.manager.try_acquire_lock("proj", "board", 123), (True, "lock_acquired")
        )
        self.state_file.unlink()

        with patch.object(self.manager, '_save_lock_to_yaml', return_value=False):
            success, reason = self.manager.try_acquire_lock("proj", "board", 123)

        self.assertEqual((success, reason), (False, "lock_mirror_write_failed_while_held"))
        self.assertTrue(refusal_leaves_caller_holding_lock(reason))
        self.assertEqual(str(self.redis.hgetall(self.lock_key)['locked_by_issue']), '123')

    def test_a_new_grants_refusal_does_not_claim_the_caller_still_holds_it(self):
        """The other half of the same distinction: a rolled-back NEW grant
        leaves the caller holding nothing, so a teardown call site must be
        free to end the run and release exactly as it always did."""
        with patch.object(self.manager, '_save_lock_to_yaml', return_value=False):
            _success, reason = self.manager.try_acquire_lock("proj", "board", 456)

        self.assertEqual(reason, "lock_mirror_write_failed")
        self.assertFalse(refusal_leaves_caller_holding_lock(reason))
        self.assertFalse(refusal_leaves_caller_holding_lock("locked_by_issue_999"))
        self.assertFalse(refusal_leaves_caller_holding_lock("lock_write_failed"))
        self.assertFalse(refusal_leaves_caller_holding_lock(None))

    def test_the_yaml_fallback_refuses_a_grant_recorded_only_in_redis(self):
        """The sibling path, WITH Redis configured -- the case the use_redis=False
        cases below structurally cannot reach.

        try_acquire_lock() falls into its YAML fallback whenever the Redis
        transaction raises (a stale connection, and LOCK_REDIS_RETRY leaves
        redis-py one attempt to notice). redis-py reconnects for the next
        command, so _create_lock()'s own hset/expire then succeed -- and
        _create_lock's default "recorded in at least one store" bar reported
        that as a grant even with the non-expiring YAML copy missing. The only
        record of it is then a key that vanishes at LOCK_TTL_SECONDS, after
        which the same board is granted to a second issue while the first run
        is live.
        """
        with patch.object(self.redis, 'transaction',
                          side_effect=redis.ConnectionError("connection reset")), \
             patch.object(self.manager, '_save_lock_to_yaml', return_value=False):
            success, reason = self.manager.try_acquire_lock("proj", "board", 456)

        self.assertEqual((success, reason), (False, "lock_write_failed"))
        self.assertEqual(
            self.redis.hgetall(self.lock_key), {},
            "the fallback's Redis-only grant survived, so it still vanishes "
            "with its TTL and the board is granted twice",
        )
        self.assertFalse(self.state_file.exists())

    def test_the_yaml_fallbacks_stale_recovery_refuses_a_redis_only_grant(self):
        """Same path, its worse half: the stale holder's YAML record has
        already been deleted by the time _create_lock() runs, so a Redis-only
        'grant' leaves the board with no durable lock record at all."""
        stale = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=111,
            lock_acquired_at=(
                datetime.now(timezone.utc) - timedelta(hours=5)
            ).isoformat(),
            lock_status='locked',
        )
        self.assertTrue(self.manager._save_lock_to_yaml(stale))

        with patch.object(self.redis, 'transaction',
                          side_effect=redis.ConnectionError("connection reset")), \
             patch.object(self.manager, '_save_lock_to_yaml', return_value=False):
            success, reason = self.manager.try_acquire_lock("proj", "board", 222)

        self.assertEqual((success, reason), (False, "lock_write_failed"))
        self.assertEqual(self.redis.hgetall(self.lock_key), {})

    def test_the_yaml_fallback_still_grants_when_the_durable_copy_lands(self):
        """The check must not have inverted the fallback's ordinary path."""
        with patch.object(self.redis, 'transaction',
                          side_effect=redis.ConnectionError("connection reset")):
            success, reason = self.manager.try_acquire_lock("proj", "board", 456)

        self.assertEqual((success, reason), (True, "lock_acquired"))
        yaml_lock, healthy = self.manager._read_yaml_lock_only("proj", "board")
        self.assertTrue(healthy)
        self.assertEqual(yaml_lock.locked_by_issue, 456)

    def test_a_yaml_fallback_grant_that_was_recorded_nowhere_is_refused(self):
        manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)

        with patch.object(manager, '_save_lock_to_yaml', return_value=False):
            success, reason = manager.try_acquire_lock("proj", "board", 456)

        self.assertEqual((success, reason), (False, "lock_write_failed"))
        self.assertIsNone(manager.get_lock("proj", "board"))

    def test_a_stale_lock_recovery_that_was_recorded_nowhere_is_refused(self):
        """The worse half: the stale holder's record has already been deleted
        by this point, so reporting success leaves a dispatched run with no
        lock record in either store."""
        manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        stale = PipelineLock(
            project="proj",
            board="board",
            locked_by_issue=111,
            lock_acquired_at=(
                datetime.now(timezone.utc) - timedelta(hours=5)
            ).isoformat(),
            lock_status='locked',
        )
        self.assertTrue(manager._save_lock_to_yaml(stale))

        with patch.object(manager, '_save_lock_to_yaml', return_value=False):
            success, reason = manager.try_acquire_lock("proj", "board", 222)

        self.assertEqual((success, reason), (False, "lock_write_failed"))

    def test_a_healthy_grant_is_still_reported_as_one(self):
        """The check must not have inverted the ordinary path: both stores
        agree, and the acquisition succeeds."""
        success, reason = self.manager.try_acquire_lock("proj", "board", 456)

        self.assertEqual((success, reason), (True, "lock_acquired"))
        self.assertEqual(str(self.redis.hgetall(self.lock_key)['locked_by_issue']), '456')
        yaml_lock, healthy = self.manager._read_yaml_lock_only("proj", "board")
        self.assertTrue(healthy)
        self.assertEqual(yaml_lock.locked_by_issue, 456)


class TestRedisAcquisitionIsSerialized(unittest.TestCase):
    """
    The Redis-path counterpart to TestYamlFallbackAcquisitionIsSerialized, and
    the coverage gap #139 was really about: until the constructor bug was
    fixed, every state_dir-passing test in this file ran the YAML fallback, so
    try_acquire_lock()'s WATCH/MULTI branch -- the one production takes
    whenever Redis is up, i.e. almost always -- had no concurrency test at all
    against a store that remembers what was written to it.

    ThreadSafeFakeRedis.transaction() serializes the whole read-decide-write
    the way a single Redis instance does, so a genuine multi-thread race
    through it is a faithful test of the branch.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.redis = ThreadSafeFakeRedis()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_only_one_of_many_concurrent_threads_is_granted_the_lock(self):
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

    def test_the_durable_copy_names_the_same_holder_redis_does(self):
        """Both stores are written on the Redis path too -- a lock that exists
        only in Redis does not survive the 7200s TTL, and get_all_locks()'
        YAML glob would not see it."""
        self.assertTrue(self.manager.try_acquire_lock("proj", "board", 123)[0])

        yaml_lock, healthy = self.manager._read_yaml_lock_only("proj", "board")

        self.assertTrue(healthy)
        self.assertIsNotNone(yaml_lock)
        self.assertEqual(yaml_lock.locked_by_issue, 123)
        redis_copy = self.redis.hgetall(self.manager._get_lock_key("proj", "board"))
        # str(): the fake stores whatever _lock_to_redis_mapping() produced,
        # where real redis-py with decode_responses=True would hand back the
        # stringified form. The holder is the assertion, not its type.
        self.assertEqual(str(redis_copy['locked_by_issue']), '123')


@contextlib.contextmanager
def _guard_contention_signal():
    """Yield an Event set the moment a non-main thread starts polling for a
    file lock somebody else holds.

    utils.file_lock only sleeps inside its enforce_timeout poll loop, i.e.
    after a non-blocking flock has actually been REFUSED -- so this is positive
    evidence that the other thread really is blocked on the guard, not an
    assumption that a sleep was long enough. Found in review: the first version
    of the test below drove its interleaving with a bare time.sleep(0.3)
    commented "long enough for the release to reach (and block on) the guard".
    Nothing verified that, and every assertion still held in the ordering where
    it did not -- so on a loaded runner the test went green while exercising no
    interleaving at all, which is the exact class of untrustworthiness this
    branch exists to remove.

    Patches the module's own `time` reference rather than time.sleep globally,
    so only utils.file_lock's polling is observed.
    """
    import utils.file_lock as file_lock_module

    blocked = threading.Event()
    real_time = file_lock_module.time

    class _NotingTime:
        monotonic = staticmethod(real_time.monotonic)

        @staticmethod
        def sleep(seconds):
            if threading.current_thread() is not threading.main_thread():
                blocked.set()
            return real_time.sleep(seconds)

    with patch.object(file_lock_module, 'time', _NotingTime):
        yield blocked


class TestTouchAndReleaseCannotInterleaveAcrossTheTwoStores(unittest.TestCase):
    """
    The second of the two interleavings TestTouchAndReleaseCannotInterleave's
    docstring names, which needs a Redis to exist at all and so had never run
    (#139 audit): the Redis leg refreshes the key, a release then deletes the
    key AND unlinks the state file, and the YAML leg re-creates the durable
    record because redis_ok said Redis still named this holder.

    Nothing reclaims such a lock at runtime, so the board stops dispatching
    until an operator intervenes.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.redis = ThreadSafeFakeRedis()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.redis)
        self.state_file = self.manager._get_state_file("proj", "board")

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_a_release_landing_between_the_two_legs_wins_in_both_stores(self):
        self.manager._create_lock("proj", "board", 123)
        released = []
        workers = []
        interleaved = []
        real_touch_redis = self.manager._touch_lock_redis

        with _guard_contention_signal() as blocked_on_the_guard:
            def release_from_another_thread(*args, **kwargs):
                # Run the Redis leg first, THEN let a release land before the
                # YAML leg gets its turn -- the exact ordering that used to
                # resurrect the durable record.
                result = real_touch_redis(*args, **kwargs)
                worker = threading.Thread(
                    target=lambda: released.append(
                        self.manager.release_lock("proj", "board", 123)
                    )
                )
                workers.append(worker)
                worker.start()
                # Wait for the release to be demonstrably blocked on the guard
                # rather than guessing at how long that takes.
                interleaved.append(blocked_on_the_guard.wait(timeout=30))
                return result

            with patch.object(self.manager, '_touch_lock_redis',
                              side_effect=release_from_another_thread):
                self.manager.touch_lock("proj", "board", 123)

            for worker in workers:
                worker.join(timeout=30)
                self.assertFalse(worker.is_alive())

        self.assertEqual(
            interleaved, [True],
            "the release never blocked on the acquire guard, so this run "
            "exercised no interleaving at all and asserts nothing"
        )
        self.assertEqual(released, [ReleaseResult.RELEASED])
        self.assertFalse(self.state_file.exists())
        self.assertEqual(self.redis.hgetall(self.manager._get_lock_key("proj", "board")), {})
        self.assertIsNone(self.manager.get_lock("proj", "board"))


class TestAcquireAndReleaseCannotInterleaveAcrossTheTwoStores(unittest.TestCase):
    """
    The third pairing, and the one the guard did NOT close until the #139
    review round: release_lock() holds the acquire guard across its whole
    two-store delete and touch_lock() takes it too, but try_acquire_lock()'s
    REDIS branch walked straight past it -- its WATCH/MULTI transaction is
    atomic in Redis, and the _create_lock_yaml_only() that mirrors the grant to
    disk is a second, unguarded write.

    Reproduced directly against ThreadSafeFakeRedis by letting an acquire for
    456 run at the point release_lock_tx returns 'released':

        release_lock("proj", "board", 123) -> ReleaseResult.RELEASED
        try_acquire_lock("proj", "board", 456) -> (True, 'lock_acquired')
        redis holder now: 456
        yaml state file exists: False
        _read_yaml_lock_only -> (None, True)      # "healthy read, no lock"
        get_all_locks() -> []

    123's release sets redis_confirmed_ownership=True after its transaction and
    then deliberately SKIPS the YAML ownership re-check, so the file it unlinks
    is the one 456 had just written. What survived was a live lock whose only
    copy was the Redis key: invisible to get_all_locks() (which is what
    recover_orphaned_resource_locks() and the operator tooling scan), reported
    by _read_yaml_lock_only() as a HEALTHY "no lock" so nothing fails closed,
    and gone entirely once the 7200s TTL lapsed -- at which point
    try_acquire_lock()'s transaction reads the absent key back as an empty dict
    and grants the same board to a second issue while 456's run is still live.
    That is the double-grant #155 is about, reached from the Redis path.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.redis = ThreadSafeFakeRedis()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.redis)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_an_acquire_landing_between_the_two_legs_is_not_unlinked_by_the_departing_holder(self):
        self.manager._create_lock("proj", "board", 123)
        acquired = []
        workers = []
        progressed = []
        real_transaction = self.redis.transaction

        with _guard_contention_signal() as blocked_on_the_guard:
            entered = threading.Event()
            finished = threading.Event()

            def acquire_in_the_gap():
                entered.set()
                acquired.append(self.manager.try_acquire_lock("proj", "board", 456))
                finished.set()

            def acquire_from_another_thread(func, *keys, **kwargs):
                result = real_transaction(func, *keys, **kwargs)
                if result != "released":
                    return result
                # The Redis leg of 123's release is done and the YAML leg has
                # not run yet -- the exact gap the acquire used to slip into.
                worker = threading.Thread(target=acquire_in_the_gap)
                workers.append(worker)
                worker.start()
                entered.wait(timeout=30)
                # Either outcome resolves in milliseconds: guarded, the acquire
                # blocks on the guard this release holds; unguarded, it runs to
                # completion right here. Waiting for one of them rather than
                # sleeping is what keeps the test from passing vacuously.
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if blocked_on_the_guard.is_set() or finished.is_set():
                        break
                    time.sleep(0.01)
                progressed.append(blocked_on_the_guard.is_set() or finished.is_set())
                return result

            with patch.object(self.redis, 'transaction',
                              side_effect=acquire_from_another_thread):
                released = self.manager.release_lock("proj", "board", 123)

            for worker in workers:
                worker.join(timeout=30)
                self.assertFalse(worker.is_alive())

        self.assertEqual(
            progressed, [True],
            "the acquire never reached the release's gap, so this run asserts nothing"
        )
        self.assertIs(released, ReleaseResult.RELEASED)
        self.assertEqual(acquired, [(True, "lock_acquired")])

        # The invariant: whoever Redis names as the holder is also the holder
        # named on disk. Neither store may be left describing a lock the other
        # one does not have.
        redis_copy = self.redis.hgetall(self.manager._get_lock_key("proj", "board"))
        self.assertEqual(str(redis_copy['locked_by_issue']), '456')
        yaml_lock, healthy = self.manager._read_yaml_lock_only("proj", "board")
        self.assertTrue(healthy)
        self.assertIsNotNone(
            yaml_lock,
            "the departing holder unlinked the incoming holder's state file: the "
            "surviving lock exists only in Redis, and vanishes with its TTL"
        )
        self.assertEqual(yaml_lock.locked_by_issue, 456)
        self.assertEqual([lock.locked_by_issue for lock in self.manager.get_all_locks()], [456])


if __name__ == '__main__':
    unittest.main()

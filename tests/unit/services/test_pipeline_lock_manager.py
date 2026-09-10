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

from services.pipeline_lock_manager import (
    PipelineLockManager,
    PipelineLock,
    ReleaseResult,
    TouchResult,
)


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
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
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
        self.manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
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

    def test_the_yaml_read_inside_the_guard_cannot_block_indefinitely(self):
        """
        The other half of the same failure: release_lock()'s wait for the guard
        is only meaningful if a guard HOLDER is itself bounded.
        _read_yaml_lock_only() runs inside the guard on all three of the paths
        that decide who holds the lock, and it took '<state>.yaml.lock' with a
        blocking, unbounded file_lock -- so a holder parked on the inner lock
        made the outer guard's hold unbounded too, and no release budget could
        be honoured. A read that cannot be serialized is a read FAILURE, which
        every caller already treats fail-closed.
        """
        seen = []
        import utils.file_lock as file_lock_module
        real_file_lock = file_lock_module.file_lock

        @contextlib.contextmanager
        def noting(path, *args, **kwargs):
            seen.append((str(path), kwargs.get('enforce_timeout', False)))
            with real_file_lock(path, *args, **kwargs):
                yield

        with patch('utils.file_lock.file_lock', side_effect=noting):
            self.manager._read_yaml_lock_only("proj", "board")

        state_lock = str(self.state_file) + '.lock'
        self.assertEqual(seen, [(state_lock, True)])

    def test_an_unserializable_yaml_read_is_reported_as_a_read_failure(self):
        with patch('utils.file_lock.file_lock', side_effect=TimeoutError("inner lock busy")):
            lock, read_ok = self.manager._read_yaml_lock_only("proj", "board")

        self.assertIsNone(lock)
        self.assertFalse(read_ok)


if __name__ == '__main__':
    unittest.main()

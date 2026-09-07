"""
Tests for services/project_checkout_lock.py (issue #54, Phase 2 of the
concurrency redesign, #88/#34).

Two things are covered:

1. Sequential unit coverage of project_checkout_lock_sync()/_async()'s own
   poll/retry/timeout mechanics, through the REAL ProjectResourceLockManager
   facade (#53) backed by a real temp-dir PipelineLockManager + a small
   thread-safe fake Redis client (see ThreadSafeFakeRedis below) -- not the
   unittest.mock.MagicMock-per-call pattern test_project_resource_lock_manager.py
   uses elsewhere, because THIS module's whole job is retrying across
   multiple acquire_resource() calls whose answers change over time (busy,
   then free), which a single fixed mock return value can't model.

2. The acceptance-criteria regression test itself
   (TestConcurrentCollisionSerializes): two genuinely concurrent OS threads
   (project_checkout_lock_sync) and two genuinely concurrent asyncio tasks
   (project_checkout_lock_async) both acquiring the SAME project's
   project_checkout lock, proving they serialize (never run their guarded
   section at the same time) rather than racing -- through the same real
   facade + fake-Redis backend as (1), so this exercises the actual
   production acquire/retry/release code path, not a hand-rolled stand-in
   lock.

ThreadSafeFakeRedis deliberately does NOT use unittest.mock: PipelineLockManager's
Redis-backed try_acquire_lock()/release_lock() call self.redis_client.transaction(
func, key, value_from_callable=True), which must genuinely serialize concurrent
callers (exactly like real Redis's WATCH/MULTI/EXEC) for a threaded test of
actual mutual exclusion to mean anything -- a bare MagicMock has no such
behavior, and PipelineLockManager's own YAML-only fallback path (used when no
Redis is configured) is plain check-then-write with no atomicity guard, which
would make a real-thread test of THAT path flaky for reasons unrelated to this
issue. Guarding the whole read-modify-write with one process-wide lock is a
faithful enough model of single-Redis-instance atomicity for this purpose.
"""

import asyncio
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock
import tempfile
import shutil

import pytest

from services.pipeline_lock_manager import PipelineLockManager
from services.project_resource_lock_manager import ProjectResourceLockManager
from services.project_checkout_lock import (
    project_checkout_lock_async,
    project_checkout_lock_sync,
    _held_with_heartbeat_async,
    _held_with_heartbeat_sync,
    _mint_unique_holder_id,
    ProjectCheckoutLockTimeoutError,
    RESOURCE_NAME,
)


class ThreadSafeFakeRedis:
    """Minimal in-memory stand-in for a single Redis instance's hash + atomic
    transaction API, sufficient for PipelineLockManager's try_acquire_lock()/
    release_lock()/get_lock() -- see module docstring for why this exists
    instead of a MagicMock or the real redis client."""

    def __init__(self):
        self._store = {}
        # RLock, not Lock: transaction() holds this for the whole func(pipe)
        # call, and func (acquire_lock_tx/release_lock_tx in
        # pipeline_lock_manager.py) calls pipe.hgetall()/.hset()/.delete(),
        # which re-enter this same lock from the same thread -- a plain
        # non-reentrant Lock would self-deadlock there.
        self._global_lock = threading.RLock()

    def ping(self):
        return True

    def hgetall(self, key):
        with self._global_lock:
            return dict(self._store.get(key, {}))

    def hset(self, key, mapping):
        with self._global_lock:
            self._store.setdefault(key, {}).update(mapping)

    def delete(self, key):
        with self._global_lock:
            self._store.pop(key, None)

    def expire(self, key, seconds):
        pass  # TTL not needed for these tests

    class _Pipe:
        """Stands in for both Redis.pipeline()'s context-managed object
        (.watch()/.exists(), whose results this codebase's acquire_lock
        currently discards -- see pipeline_lock_manager.py's own comments)
        and the callable-transaction pipe passed to acquire_lock_tx/
        release_lock_tx (.hgetall()/.multi()/.hset()/.expire()/.delete())."""

        def __init__(self, redis):
            self._redis = redis

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def watch(self, key):
            return None

        def exists(self, key):
            with self._redis._global_lock:
                return key in self._redis._store

        def multi(self):
            return None

        def hgetall(self, key):
            return self._redis.hgetall(key)

        def hset(self, key, mapping):
            return self._redis.hset(key, mapping)

        def expire(self, key, seconds):
            return None

        def delete(self, key):
            return self._redis.delete(key)

    def pipeline(self):
        return ThreadSafeFakeRedis._Pipe(self)

    def transaction(self, func, *keys, value_from_callable=False):
        # The whole read-decide-write sequence runs under one process-wide
        # lock -- see class docstring for why this is a faithful enough model
        # of real single-Redis-instance atomicity for these tests.
        with self._global_lock:
            return func(ThreadSafeFakeRedis._Pipe(self))


def _make_facade(tmp_dir: str) -> ProjectResourceLockManager:
    lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), redis_client=ThreadSafeFakeRedis())
    return ProjectResourceLockManager(lock_manager=lock_manager)


class TestProjectCheckoutLockSyncMechanics(unittest.TestCase):
    """Sequential (single-thread) coverage of the sync context manager's own
    poll/retry/timeout/release mechanics."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_acquires_and_releases_when_uncontended(self):
        # issue_number=111 is log attribution only -- the lock's actual
        # holder identity is an internally-minted unique id (always
        # negative), never the caller's real issue_number. See this module's
        # "Why every acquisition gets its own unique holder id" docstring.
        entered = []
        with project_checkout_lock_sync("proj", 111, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
            entered.append(True)
            lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
            self.assertIsNotNone(lock)
            self.assertLess(lock.locked_by_issue, 0)

        self.assertEqual(entered, [True])
        # Released on exit
        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_releases_on_exception_inside_with_block(self):
        with self.assertRaises(ValueError):
            with project_checkout_lock_sync("proj", 111, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
                raise ValueError("boom")

        # finally: still released despite the exception
        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_waits_then_acquires_once_the_other_holder_releases(self):
        """Simulates the collision window without real threads: issue 1 holds
        the lock; issue 2's acquire attempt must poll (not fail immediately)
        until issue 1 releases, then succeed."""
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)

        release_at = time.monotonic() + 0.2

        def release_after_delay():
            while time.monotonic() < release_at:
                time.sleep(0.01)
            self.facade.release_resource("proj", RESOURCE_NAME, 1)

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()
        try:
            start = time.monotonic()
            with project_checkout_lock_sync("proj", 2, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.02):
                elapsed = time.monotonic() - start
                lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
                # Not issue 1 (the released prior holder) and not literally
                # "2" either -- an internally-minted unique id (see above).
                self.assertLess(lock.locked_by_issue, 0)
            # Genuinely waited for the release, not an instant no-op success
            self.assertGreaterEqual(elapsed, 0.15)
        finally:
            releaser.join()

    def test_raises_timeout_error_when_never_freed(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)  # never released

        with self.assertRaises(ProjectCheckoutLockTimeoutError):
            with project_checkout_lock_sync("proj", 2, facade=self.facade, timeout_seconds=0.1, poll_interval_seconds=0.02):
                pass  # pragma: no cover -- must never be entered

        # The failed attempt must not have left issue 2 holding anything
        lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
        self.assertEqual(lock.locked_by_issue, 1)


@pytest.mark.asyncio
class TestProjectCheckoutLockAsyncMechanics:
    """Async counterpart of the sequential mechanics tests above."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    async def test_acquires_and_releases_when_uncontended(self):
        # issue_number=111 is log attribution only -- see the sync test's
        # comment above for why the lock's real holder id is never this.
        async with project_checkout_lock_async("proj", 111, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
            lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
            assert lock.locked_by_issue < 0

        assert self.facade.get_resource_lock("proj", RESOURCE_NAME) is None

    async def test_raises_timeout_error_when_never_freed(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)

        with pytest.raises(ProjectCheckoutLockTimeoutError):
            async with project_checkout_lock_async("proj", 2, facade=self.facade, timeout_seconds=0.1, poll_interval_seconds=0.02):
                pass  # pragma: no cover


class TestConcurrentCollisionSerializes(unittest.TestCase):
    """
    THE regression test called for by issue #54's acceptance criteria: two
    genuinely concurrent operations against the same project's
    project_checkout resource lock must serialize (never overlap) instead of
    racing, and both must eventually complete (this is a mutex, not a
    one-wins-one-fails gate).
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_two_threads_racing_the_same_project_serialize(self):
        """Two real OS threads simulate 'a board's git checkout racing
        another board's docker build' against the same project's shared
        directory (issue #54's own example) -- both must be able to run
        their guarded section, but never at the same time."""
        concurrent_count = {"value": 0}
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = []
        errors = []

        def worker(issue_number):
            try:
                with project_checkout_lock_sync(
                    "shared-project", issue_number, facade=self.facade,
                    timeout_seconds=5, poll_interval_seconds=0.01,
                ):
                    with count_lock:
                        concurrent_count["value"] += 1
                        max_concurrent["value"] = max(max_concurrent["value"], concurrent_count["value"])
                    # Hold the lock long enough that, if serialization were
                    # broken, the other thread's acquire would overlap this
                    # window almost certainly, not just by chance timing.
                    time.sleep(0.1)
                    with count_lock:
                        concurrent_count["value"] -= 1
                    completed.append(issue_number)
            except Exception as e:  # pragma: no cover -- surfaced via errors list
                errors.append(e)

        t1 = threading.Thread(target=worker, args=(101,))
        t2 = threading.Thread(target=worker, args=(102,))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(completed), [101, 102])  # both got to run
        self.assertEqual(max_concurrent["value"], 1)  # ...but never at the same time
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))

    def test_two_asyncio_tasks_racing_the_same_project_serialize(self):
        """Same collision, modeled as two concurrent asyncio tasks (the shape
        run_claude_code()'s Docker/local branches and
        AutoCommitService.commit_agent_changes() actually run in) instead of
        OS threads."""

        async def run():
            concurrent_count = {"value": 0}
            max_concurrent = {"value": 0}
            completed = []

            async def worker(issue_number):
                async with project_checkout_lock_async(
                    "shared-project", issue_number, facade=self.facade,
                    timeout_seconds=5, poll_interval_seconds=0.01,
                ):
                    concurrent_count["value"] += 1
                    max_concurrent["value"] = max(max_concurrent["value"], concurrent_count["value"])
                    await asyncio.sleep(0.1)
                    concurrent_count["value"] -= 1
                    completed.append(issue_number)

            await asyncio.gather(worker(201), worker(202))
            return max_concurrent["value"], completed

        max_concurrent, completed = asyncio.run(run())

        self.assertEqual(sorted(completed), [201, 202])
        self.assertEqual(max_concurrent, 1)
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))

    def test_two_callers_with_no_issue_number_still_serialize(self):
        """
        issue_number=None (no real GitHub issue in scope, e.g.
        project_workspace.initialize_project() at startup) must not prevent
        two genuinely different concurrent callers from serializing --
        every acquisition mints its own internal holder id regardless of
        what issue_number was passed for logging (see
        test_two_callers_with_the_SAME_real_issue_number_still_serialize for
        why this matters even more when issue_number IS given).
        """
        concurrent_count = {"value": 0}
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = {"count": 0}

        def worker():
            with project_checkout_lock_sync(
                "shared-project", None, facade=self.facade,
                timeout_seconds=5, poll_interval_seconds=0.01,
            ):
                with count_lock:
                    concurrent_count["value"] += 1
                    max_concurrent["value"] = max(max_concurrent["value"], concurrent_count["value"])
                time.sleep(0.1)
                with count_lock:
                    concurrent_count["value"] -= 1
                    completed["count"] += 1

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(completed["count"], 2)  # both got to run
        self.assertEqual(max_concurrent["value"], 1)  # ...but never at the same time
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))

    def test_two_callers_with_the_SAME_real_issue_number_still_serialize(self):
        """
        THE critical regression this round of review found:
        PipelineLockManager.try_acquire_lock() treats a MATCHING issue_number
        as reentrant ("already_holds_lock") with no other identity check. If
        this lock passed the caller's real issue_number straight through as
        the holder identity, two genuinely different concurrent operations
        that happen to share a real issue number (e.g. a Docker agent run
        and an unrelated auto-commit/watchdog redispatch both tagged the same
        issue) would each be told they already hold the lock and both run
        concurrently -- and whichever finished first would release the lock
        out from under the other still-running one. Passing the SAME
        issue_number for both racing callers here (instead of two different
        ones, as the other tests in this class use) is the whole point: it
        proves the fix holds even in the worst case the old design got wrong.
        """
        concurrent_count = {"value": 0}
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = {"count": 0}
        SAME_ISSUE_NUMBER = 42

        def worker():
            with project_checkout_lock_sync(
                "shared-project", SAME_ISSUE_NUMBER, facade=self.facade,
                timeout_seconds=5, poll_interval_seconds=0.01,
            ):
                with count_lock:
                    concurrent_count["value"] += 1
                    max_concurrent["value"] = max(max_concurrent["value"], concurrent_count["value"])
                time.sleep(0.1)
                with count_lock:
                    concurrent_count["value"] -= 1
                    completed["count"] += 1

        t1 = threading.Thread(target=worker)
        t2 = threading.Thread(target=worker)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        self.assertEqual(completed["count"], 2)  # both got to run
        self.assertEqual(max_concurrent["value"], 1)  # ...but never at the same time (the old bug: this would be 2)
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))


class TestMintUniqueHolderId(unittest.TestCase):
    """_mint_unique_holder_id() must be unique per call (never a shared
    constant, and never the caller's real issue_number -- see this module's
    "Why every acquisition gets its own unique holder id" docstring) and
    never collide with a real, always-positive GitHub issue number."""

    def test_is_negative(self):
        self.assertLess(_mint_unique_holder_id(), 0)

    def test_successive_calls_are_unique(self):
        ids = [_mint_unique_holder_id() for _ in range(50)]
        self.assertEqual(len(ids), len(set(ids)))

    def test_concurrent_calls_across_threads_are_unique(self):
        ids = []
        ids_lock = threading.Lock()

        def worker():
            holder_id = _mint_unique_holder_id()
            with ids_lock:
                ids.append(holder_id)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)

        self.assertEqual(len(ids), 20)
        self.assertEqual(len(ids), len(set(ids)))


class TestHeldWithHeartbeatSync(unittest.TestCase):
    """
    CRITICAL regression (found in code review): PipelineLockManager's Redis
    lock-key TTL is fixed at 7200s and refreshed ONLY as a side effect of a
    repeat acquire_resource() call for the SAME holder_id -- never
    proactively. A hold that outlives 7200s with no heartbeat would have its
    Redis copy silently expire while still legitimately held, and (confirmed
    by direct source reading) a second caller's acquire attempt at that
    point would succeed immediately -- a real double-acquisition of a
    mutual-exclusion lock. _held_with_heartbeat_sync()/_async() close this by
    periodically re-calling acquire_resource() with the same holder_id
    (safe: PipelineLockManager treats this as a TTL-refreshing no-op, not a
    new acquisition) for as long as the `with` body is still running.
    """

    def test_heartbeat_calls_acquire_resource_again_with_the_same_holder_id_while_held(self):
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "already_holds_lock")

        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            time.sleep(0.09)  # long enough for several heartbeat ticks

        # At least one heartbeat refresh call, always with the SAME holder_id
        # (never a fresh id -- that would be a new acquisition attempt, not
        # a refresh of this hold).
        self.assertGreaterEqual(facade.acquire_resource.call_count, 2)
        for call in facade.acquire_resource.call_args_list:
            self.assertEqual(call.args, ("proj", "proj_checkout", -42))

    def test_heartbeat_stops_once_the_with_block_exits(self):
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "already_holds_lock")

        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            pass

        count_at_exit = facade.acquire_resource.call_count
        time.sleep(0.08)  # give a stray heartbeat thread every chance to fire again
        self.assertEqual(facade.acquire_resource.call_count, count_at_exit)

    def test_heartbeat_refresh_failure_does_not_propagate_or_stop_the_with_block(self):
        facade = MagicMock()
        facade.acquire_resource.side_effect = Exception("redis blip")

        # Must not raise, and the body must still run to completion.
        ran = []
        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            time.sleep(0.05)
            ran.append(True)

        self.assertEqual(ran, [True])


@pytest.mark.asyncio
class TestHeldWithHeartbeatAsync:
    """Async counterpart of TestHeldWithHeartbeatSync -- see its class
    docstring for the full rationale."""

    async def test_heartbeat_calls_acquire_resource_again_with_the_same_holder_id_while_held(self):
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "already_holds_lock")

        async with _held_with_heartbeat_async(facade, "dev_container_build", "proj", holder_id=-7, heartbeat_interval_seconds=0.02):
            await asyncio.sleep(0.09)

        assert facade.acquire_resource.call_count >= 2
        for call in facade.acquire_resource.call_args_list:
            assert call.args == ("proj", "dev_container_build", -7)

    async def test_heartbeat_stops_once_the_with_block_exits(self):
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "already_holds_lock")

        async with _held_with_heartbeat_async(facade, "dev_container_build", "proj", holder_id=-7, heartbeat_interval_seconds=0.02):
            pass

        count_at_exit = facade.acquire_resource.call_count
        await asyncio.sleep(0.08)
        assert facade.acquire_resource.call_count == count_at_exit


if __name__ == '__main__':
    unittest.main()

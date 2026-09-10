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
import logging
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import shutil

import pytest

import services.project_checkout_lock as project_checkout_lock
from services.pipeline_lock_manager import LOCK_TTL_SECONDS, PipelineLockManager, TouchResult
from services.project_resource_lock_manager import ProjectResourceLockManager
from services.project_checkout_lock import (
    project_checkout_lock_async,
    project_checkout_lock_sync,
    _acquire_and_start_heartbeat_off_loop,
    _default_facade_off_loop,
    _held_with_heartbeat_async,
    _held_with_heartbeat_sync,
    _join_heartbeat_thread_async,
    _log_heartbeat_failure,
    _mint_unique_holder_id,
    _release_and_warn,
    _release_and_warn_async,
    HEARTBEAT_FAILURE_ESCALATION_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    ProjectCheckoutLockTimeoutError,
    REDIS_LOCK_TTL_SECONDS,
    RESOURCE_NAME,
)
from tests.utils.fake_redis import ThreadSafeFakeRedis, TtlFakeRedis


# Compressed stand-in for REDIS_LOCK_TTL_SECONDS in the TTL tests below.
_COMPRESSED_TTL_SECONDS = 0.3


def _make_facade(tmp_dir: str) -> ProjectResourceLockManager:
    lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), redis_client=ThreadSafeFakeRedis())
    return ProjectResourceLockManager(lock_manager=lock_manager)


def _make_yaml_only_facade(tmp_dir: str) -> ProjectResourceLockManager:
    """
    Facade over PipelineLockManager's DOCUMENTED YAML-only fallback -- the
    branch it takes whenever Redis is unavailable ("Redis connection failed
    for locks, using YAML only"), and the one that latches on for the whole
    process lifetime if Redis is merely slow to come up at boot, since the
    singleton only attempts the connection once.

    Every other concurrency fixture in this file injects ThreadSafeFakeRedis,
    whose transaction() serializes the whole read-modify-write -- so they only
    ever exercise try_acquire_lock()'s ATOMIC Redis branch. use_redis=False
    rather than redis_client=None (#139): None means "connect one yourself",
    and the post-construction clear this used to do was a workaround for a
    constructor bug that made it look otherwise.
    """
    lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), use_redis=False)
    return ProjectResourceLockManager(lock_manager=lock_manager)


def _heartbeat_interval_override(seconds: float):
    """
    Force every heartbeat started while this patch is active onto `seconds`.

    HEARTBEAT_INTERVAL_SECONDS (1800s in production) is not a parameter of the
    composed context managers, so _start_heartbeat_thread()'s interval
    argument is the only seam -- the same one
    TestReleaseNeverOvertakesAnInFlightHeartbeat already uses. Patching the
    module constant would not work: it is bound as a default argument value at
    def time.
    """
    real_start = project_checkout_lock._start_heartbeat_thread

    def _start(facade, resource_name, project, holder_id, _interval):
        return real_start(facade, resource_name, project, holder_id, seconds)

    return patch('services.project_checkout_lock._start_heartbeat_thread', _start)


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


class TestYamlOnlyFallbackConcurrency(unittest.TestCase):
    """
    The same mutual-exclusion acceptance criterion as
    TestConcurrentCollisionSerializes, but over PipelineLockManager's YAML-only
    fallback instead of its atomic Redis transaction (see
    _make_yaml_only_facade).

    Found in review of #146 WI-1: every other concurrency fixture in this file
    injects ThreadSafeFakeRedis, whose transaction() holds one process-wide
    RLock across the entire read-decide-write -- so `max_concurrent == 1` was
    only ever asserted for the branch that was already atomic. The YAML
    fallback's own read-then-write had no concurrency coverage at all, and it
    is precisely the branch that broke when the acquire attempt moved off the
    event loop: the loop's single thread had been silently supplying the
    atomicity that branch lacks, and the poll loop's shared
    asyncio.sleep(poll_interval_seconds) deliberately releases several waiters
    on the same tick, so they all read "no lock" before any of them writes one.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_yaml_only_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_asyncio_tasks_racing_the_same_project_serialize_without_redis(self):
        """Six tasks rather than two: the poll loop releases every waiter on
        the same tick, so a broken guard grants them all at once -- two tasks
        can still collide by luck of scheduling, six make it unmissable."""

        async def run():
            inside = []
            max_concurrent = {"value": 0}
            completed = []

            async def worker(n):
                async with project_checkout_lock_async(
                    "shared-project", facade=self.facade,
                    timeout_seconds=20, poll_interval_seconds=0.01,
                ):
                    inside.append(n)
                    max_concurrent["value"] = max(max_concurrent["value"], len(inside))
                    await asyncio.sleep(0.05)
                    inside.remove(n)
                    completed.append(n)

            await asyncio.gather(*(worker(n) for n in range(6)))
            return max_concurrent["value"], completed

        max_concurrent, completed = asyncio.run(run())

        self.assertEqual(sorted(completed), list(range(6)))  # all got to run
        self.assertEqual(max_concurrent, 1)  # ...but never at the same time
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))

    def test_threads_racing_the_same_project_serialize_without_redis(self):
        """project_checkout_lock_sync() runs on the
        asyncio.to_thread(initialize_all_projects) thread at startup and races
        the same unguarded window from real OS threads."""
        inside = []
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = []
        errors = []

        def worker(n):
            try:
                with project_checkout_lock_sync(
                    "shared-project", facade=self.facade,
                    timeout_seconds=20, poll_interval_seconds=0.01,
                ):
                    with count_lock:
                        inside.append(n)
                        max_concurrent["value"] = max(max_concurrent["value"], len(inside))
                    time.sleep(0.05)
                    with count_lock:
                        inside.remove(n)
                        completed.append(n)
            except Exception as e:  # pragma: no cover -- surfaced via errors list
                errors.append(e)

        threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        self.assertEqual(errors, [])
        self.assertEqual(sorted(completed), list(range(6)))
        self.assertEqual(max_concurrent["value"], 1)
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))


class TestHeartbeatConstantsTrackTheRealRedisTtl(unittest.TestCase):
    """
    Found in review of #146 WI-1: project_checkout_lock's TTL-derived
    constants used to restate a literal 7200.0 that pipeline_lock_manager.py
    spelled out separately at seven expire() call sites, with nothing tying
    the two together. Shortening the real TTL there would have left
    HEARTBEAT_INTERVAL_SECONDS racing its own key expiry with zero margin and
    HEARTBEAT_FAILURE_ESCALATION_SECONDS unable to fire before the TTL lapsed
    -- the one operator signal for that outage -- with the whole suite green.
    """

    def test_the_heartbeat_interval_leaves_margin_under_the_real_ttl(self):
        # "Comfortably under half" -- at least one full heartbeat must land
        # before expiry even under scheduling jitter.
        self.assertLess(HEARTBEAT_INTERVAL_SECONDS, REDIS_LOCK_TTL_SECONDS / 2)

    def test_the_failure_escalation_fires_before_the_real_ttl_lapses(self):
        self.assertLess(HEARTBEAT_FAILURE_ESCALATION_SECONDS, REDIS_LOCK_TTL_SECONDS)
        # ...with at least one more heartbeat interval of margin after it does.
        self.assertLessEqual(
            HEARTBEAT_FAILURE_ESCALATION_SECONDS + HEARTBEAT_INTERVAL_SECONDS,
            REDIS_LOCK_TTL_SECONDS,
        )

    def test_the_constant_matches_the_ttl_pipeline_lock_manager_actually_writes(self):
        """Not just the two module constants agreeing with each other: the TTL
        actually handed to Redis by a real try_acquire_lock() call."""
        self.assertEqual(REDIS_LOCK_TTL_SECONDS, float(LOCK_TTL_SECONDS))

        test_dir = tempfile.mkdtemp()
        try:
            fake_redis = TtlFakeRedis(ttl_seconds=_COMPRESSED_TTL_SECONDS)
            lock_manager = PipelineLockManager(state_dir=Path(test_dir), redis_client=fake_redis)
            facade = ProjectResourceLockManager(lock_manager=lock_manager)

            can_execute, _ = facade.acquire_resource("proj", RESOURCE_NAME, -1)

            self.assertTrue(can_execute)
            self.assertTrue(fake_redis.expire_calls, "the lock key was written with no TTL at all")
            for _key, seconds in fake_redis.expire_calls:
                self.assertEqual(float(seconds), REDIS_LOCK_TTL_SECONDS)
        finally:
            shutil.rmtree(test_dir)


class TestHeartbeatPreventsDoubleAcquisitionAcrossTheRedisTtl(unittest.TestCase):
    """
    The guarantee itself, not the mechanism: a hold that outlives the Redis
    lock-key TTL must not be acquirable by a second caller.

    Found in review of #146 WI-1: every other heartbeat test here asserts that
    touch_resource() was called (with the right holder_id, from an OS thread,
    returning the right TouchResult) -- never that the refresh actually
    prevents anything, because ThreadSafeFakeRedis.expire() is a no-op. So
    touch_lock() dropping its expire() call, _get_lock_key()/
    _lock_to_redis_mapping() drifting so the refresh writes a key
    try_acquire_lock() does not read, or HEARTBEAT_INTERVAL_SECONDS being
    raised above the TTL would all ship green while restoring exactly the
    double-acquisition the module docstring is about.

    The pair below pins both halves: with the heartbeat effectively off the
    hazard is real (the second acquire SUCCEEDS -- so these tests are not
    passing vacuously), and with it running at a sane interval the same
    acquire is refused. See TtlFakeRedis for the compressed clock.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.fake_redis = TtlFakeRedis(ttl_seconds=_COMPRESSED_TTL_SECONDS)
        lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.fake_redis)
        self.facade = ProjectResourceLockManager(lock_manager=lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    # Long enough that a hold spans several compressed TTLs.
    _HOLD_SECONDS = _COMPRESSED_TTL_SECONDS * 3
    _PROBE_INTERVAL_SECONDS = 0.02

    def _poll_second_caller(self, results):
        """
        A completely unrelated caller's acquire attempt, repeated for the whole
        hold rather than sampled once at the end.

        Polling matters: some ways of breaking the refresh leave only a WINDOW
        of vulnerability rather than a permanent one (a heartbeat that hset's
        the key but stops extending its TTL lets it lapse, then re-creates it
        on the next tick), and a single sample almost always lands outside it.
        Stops at the first grant -- that IS the failure, and continuing would
        only mutate the lock further.
        """
        deadline = time.monotonic() + self._HOLD_SECONDS
        while time.monotonic() < deadline:
            results.append(self.facade.acquire_resource("proj", RESOURCE_NAME, -999))
            if results[-1][0]:
                return
            time.sleep(self._PROBE_INTERVAL_SECONDS)

    async def _poll_second_caller_async(self, results):
        """Async counterpart of _poll_second_caller() -- same probe, driven
        from inside the async context manager's guarded body."""
        deadline = time.monotonic() + self._HOLD_SECONDS
        while time.monotonic() < deadline:
            results.append(self.facade.acquire_resource("proj", RESOURCE_NAME, -999))
            if results[-1][0]:
                return
            await asyncio.sleep(self._PROBE_INTERVAL_SECONDS)

    def _assert_hazard_reproduced(self, results):
        self.assertTrue(
            [r for r in results if r[0]],
            "the hazard this heartbeat exists to close is not reproducible, so its "
            "paired 'refused' test proves nothing"
        )

    def _assert_never_granted(self, results):
        self.assertTrue(results, "the second caller never actually tried to acquire")
        granted = [r for r in results if r[0]]
        self.assertEqual(granted, [], "the lock was double-acquired despite a live heartbeat")
        for _can_execute, reason in results:
            self.assertIn("locked_by_issue_", reason)
        # ...and the real holder still owned it throughout, so its own release worked.
        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_sync_without_a_heartbeat_the_lapsed_ttl_lets_a_second_caller_in(self):
        results = []
        with _heartbeat_interval_override(_COMPRESSED_TTL_SECONDS * 100):
            with project_checkout_lock_sync(
                "proj", facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01
            ):
                self._poll_second_caller(results)

        self._assert_hazard_reproduced(results)

    def test_sync_with_the_heartbeat_running_the_second_caller_is_refused(self):
        results = []
        with _heartbeat_interval_override(_COMPRESSED_TTL_SECONDS / 3):
            with project_checkout_lock_sync(
                "proj", facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01
            ):
                self._poll_second_caller(results)

        self._assert_never_granted(results)

    def test_async_without_a_heartbeat_the_lapsed_ttl_lets_a_second_caller_in(self):
        """Same pair through the composed async wiring -- off-loop acquire plus
        the heartbeat thread it adopts -- not just the sync path."""

        async def run():
            results = []
            with _heartbeat_interval_override(_COMPRESSED_TTL_SECONDS * 100):
                async with project_checkout_lock_async(
                    "proj", facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01
                ):
                    await self._poll_second_caller_async(results)
            return results

        self._assert_hazard_reproduced(asyncio.run(run()))

    def test_async_with_the_heartbeat_running_the_second_caller_is_refused(self):
        async def run():
            results = []
            with _heartbeat_interval_override(_COMPRESSED_TTL_SECONDS / 3):
                async with project_checkout_lock_async(
                    "proj", facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01
                ):
                    await self._poll_second_caller_async(results)
            return results

        self._assert_never_granted(asyncio.run(run()))


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
    periodically calling touch_resource() with the same holder_id (safe:
    PipelineLockManager treats this as a liveness refresh of an already-held
    lock, never a new acquisition -- and unlike re-calling acquire_resource(),
    it also resets lock_acquired_at, so the separate 4-hour staleness
    heuristic can't steal an actively-heartbeating lock either) for as long
    as the `with` body is still running.
    """

    def test_heartbeat_calls_touch_resource_again_with_the_same_holder_id_while_held(self):
        facade = MagicMock()
        facade.touch_resource.return_value = True

        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            time.sleep(0.09)  # long enough for several heartbeat ticks

        # At least one heartbeat refresh call, always with the SAME holder_id
        # (never a fresh id -- that would be a new acquisition attempt, not
        # a refresh of this hold), and never acquire_resource() (which would
        # only refresh the TTL, not lock_acquired_at).
        self.assertGreaterEqual(facade.touch_resource.call_count, 2)
        for call in facade.touch_resource.call_args_list:
            self.assertEqual(call.args, ("proj", "proj_checkout", -42))
        facade.acquire_resource.assert_not_called()

    def test_heartbeat_stops_once_the_with_block_exits(self):
        facade = MagicMock()
        facade.touch_resource.return_value = True

        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            pass

        count_at_exit = facade.touch_resource.call_count
        time.sleep(0.08)  # give a stray heartbeat thread every chance to fire again
        self.assertEqual(facade.touch_resource.call_count, count_at_exit)

    def test_heartbeat_refresh_failure_does_not_propagate_or_stop_the_with_block(self):
        facade = MagicMock()
        facade.touch_resource.side_effect = Exception("redis blip")

        # Must not raise, and the body must still run to completion.
        ran = []
        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            time.sleep(0.05)
            ran.append(True)

        self.assertEqual(ran, [True])

    def test_heartbeat_finding_lock_lost_does_not_raise_or_stop_the_with_block(self):
        """touch_resource() returning False (lock lost to a competing holder,
        e.g. staleness recovery winning a race) is logged, not raised --
        there is no safe way to interrupt the `with` body from a background
        heartbeat thread."""
        facade = MagicMock()
        facade.touch_resource.return_value = False

        ran = []
        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            time.sleep(0.05)
            ran.append(True)

        self.assertEqual(ran, [True])

    def test_release_waits_for_an_in_flight_heartbeat_call_to_finish(self):
        """
        Regression for a real race found in review: a bounded join (the
        original implementation used timeout=5) could return while a
        heartbeat's in-flight touch_resource() call was still running; the
        outer code would then release the lock, and the orphaned call could
        complete AFTER that release and silently re-establish it under the
        now-abandoned holder_id. The join must wait for real, however long
        the in-flight call takes.
        """
        call_finished = threading.Event()

        def slow_touch(*args):
            time.sleep(0.15)  # longer than the old hardcoded 5s join would
            call_finished.set()  # ...well, longer than this test's patience;
            return True         # the assertion below is what actually proves it

        facade = MagicMock()
        facade.touch_resource.side_effect = slow_touch

        with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
            time.sleep(0.03)  # let exactly one heartbeat tick start its (slow) call

        # By the time the `with` block has exited, the slow in-flight call
        # must have already completed -- proving the join genuinely waited
        # for it rather than returning early.
        self.assertTrue(call_finished.is_set())


@pytest.mark.asyncio
class TestHeldWithHeartbeatAsync:
    """Async counterpart of TestHeldWithHeartbeatSync -- see its class
    docstring for the full rationale."""

    async def test_heartbeat_calls_touch_resource_again_with_the_same_holder_id_while_held(self):
        facade = MagicMock()
        facade.touch_resource.return_value = True

        async with _held_with_heartbeat_async(facade, "dev_container_build", "proj", holder_id=-7, heartbeat_interval_seconds=0.02):
            await asyncio.sleep(0.09)

        assert facade.touch_resource.call_count >= 2
        for call in facade.touch_resource.call_args_list:
            assert call.args == ("proj", "dev_container_build", -7)
        facade.acquire_resource.assert_not_called()

    async def test_heartbeat_stops_once_the_with_block_exits(self):
        facade = MagicMock()
        facade.touch_resource.return_value = True

        async with _held_with_heartbeat_async(facade, "dev_container_build", "proj", holder_id=-7, heartbeat_interval_seconds=0.02):
            pass

        count_at_exit = facade.touch_resource.call_count
        await asyncio.sleep(0.08)
        assert facade.touch_resource.call_count == count_at_exit

    async def test_heartbeat_finding_lock_lost_does_not_raise_or_stop_the_with_block(self):
        facade = MagicMock()
        facade.touch_resource.return_value = False

        ran = []
        async with _held_with_heartbeat_async(facade, "dev_container_build", "proj", holder_id=-7, heartbeat_interval_seconds=0.02):
            await asyncio.sleep(0.05)
            ran.append(True)

        assert ran == [True]


@pytest.mark.asyncio
class TestHeartbeatSurvivesABlockingGuardedOperation:
    """
    THE regression test called for by #141 (WI-1): the async heartbeat must
    still fire while the guarded body blocks the event loop synchronously for
    its whole duration.

    Both real callers do exactly that -- claude/docker_runner.py's
    _execute_in_container() monitors the container with a synchronous
    `claude_done_event.wait(timeout=...)` loop, and
    claude/claude_integration.py's _run_claude_code_locally() reads the
    subprocess with a synchronous `for line in iter(process.stdout.readline,
    '')` loop -- so with the original sibling-asyncio.Task heartbeat, nothing
    ever scheduled the heartbeat until the multi-hour operation it was
    protecting had already finished. The pre-existing TestHeldWithHeartbeatAsync
    cases all `await asyncio.sleep(...)` in their bodies, which yields to the
    loop and therefore never reproduces this.
    """

    async def test_heartbeat_fires_while_the_guarded_body_blocks_the_event_loop(self):
        touched = threading.Event()
        facade = MagicMock()
        facade.touch_resource.side_effect = lambda *args: (touched.set(), True)[1]

        async with _held_with_heartbeat_async(facade, "proj_checkout", "proj", holder_id=-11, heartbeat_interval_seconds=0.02):
            # No `await` anywhere in here, exactly like the two real callers:
            # the event loop is blocked for this whole wait, so a heartbeat
            # that depends on the loop to schedule it can never run.
            fired_during_the_block = touched.wait(timeout=5.0)

        assert fired_during_the_block, (
            "heartbeat never fired while the guarded body blocked the event loop"
        )
        assert facade.touch_resource.call_count >= 1
        for call in facade.touch_resource.call_args_list:
            assert call.args == ("proj", "proj_checkout", -11)

    async def test_heartbeat_runs_on_an_os_thread_not_an_asyncio_task(self):
        """The design decision behind the fix above, asserted directly so a
        future refactor back onto an asyncio.Task fails here as well as in the
        behavioral test."""
        facade = MagicMock()
        facade.touch_resource.return_value = True

        # A heartbeat interval far longer than the body, so this observes the
        # thread's existence rather than any refresh it happens to make.
        async with _held_with_heartbeat_async(facade, "proj_checkout", "proj", holder_id=-12, heartbeat_interval_seconds=60.0):
            thread_names = [t.name for t in threading.enumerate()]

        assert "lock-heartbeat-proj_checkout-proj" in thread_names


@pytest.mark.asyncio
class TestAsyncHeartbeatJoinSurvivesCancellation:
    """
    #140 item 34: the async variant's exit used to `await heartbeat_task`,
    which re-raises CancelledError the instant the enclosing task is
    cancelled -- abandoning a heartbeat's in-flight touch_resource() call
    exactly as a too-short bounded join would, and reopening the same
    "release proceeds while a touch is still running, which then
    re-establishes the lock under an abandoned holder id" leak the unbounded
    join closed for the sync variant.
    """

    async def test_join_completes_even_when_the_awaiting_task_is_cancelled(self):
        release_worker = threading.Event()
        worker_finished = threading.Event()

        def _in_flight_work():
            # Stands in for a heartbeat's touch_resource() call that is
            # already in flight when the cancellation arrives.
            release_worker.wait(timeout=5.0)
            worker_finished.set()

        heartbeat_thread = threading.Thread(target=_in_flight_work, daemon=True)
        heartbeat_thread.start()

        async def _joiner():
            await _join_heartbeat_thread_async(heartbeat_thread)

        task = asyncio.create_task(_joiner())
        await asyncio.sleep(0.05)  # let the joiner reach its await

        # Released from a real OS thread: once the cancellation lands, the
        # join finishes synchronously on the event-loop thread, so nothing
        # scheduled on the loop could unblock it.
        threading.Timer(0.2, release_worker.set).start()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # The cancellation propagated (above) only AFTER the in-flight work
        # actually finished -- so the caller's release can never overtake it.
        assert worker_finished.is_set()
        assert not heartbeat_thread.is_alive()

    async def test_join_does_not_block_the_event_loop_on_the_normal_path(self):
        release_worker = threading.Event()

        heartbeat_thread = threading.Thread(
            target=lambda: release_worker.wait(timeout=5.0), daemon=True
        )
        heartbeat_thread.start()

        ticks = []

        async def _ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks.append(1)

        ticker_task = asyncio.create_task(_ticker())
        threading.Timer(0.2, release_worker.set).start()
        await _join_heartbeat_thread_async(heartbeat_thread)
        ticker_task.cancel()

        # The loop kept scheduling other work for the whole ~0.2s join.
        assert len(ticks) >= 5
        # ...and the join actually waited: returning while the thread is still
        # running is the leak this helper exists to close.
        assert not heartbeat_thread.is_alive()

    async def test_the_normal_path_join_is_unbounded(self):
        """
        The invariant itself, asserted directly so no bound can slip back in:
        the whole reason this helper exists is that a bounded join (the
        original implementation used timeout=5) can return while a heartbeat's
        touch_resource() is still in flight, letting the caller's release be
        overtaken and the lock re-established under an abandoned holder id.
        A behavioral test can only ever catch a bound shorter than its own
        patience -- reintroducing `join(5.0)` passes every other case here.
        """
        release_worker = threading.Event()
        heartbeat_thread = threading.Thread(
            target=lambda: release_worker.wait(timeout=5.0), daemon=True
        )
        heartbeat_thread.start()
        probe = MagicMock(wraps=heartbeat_thread)

        threading.Timer(0.1, release_worker.set).start()
        await _join_heartbeat_thread_async(probe)

        probe.join.assert_called_once_with()  # no timeout argument, positional or keyword
        assert not heartbeat_thread.is_alive()


@pytest.mark.asyncio
class TestAsyncHeartbeatJoinSurvivesExecutorShutdown:
    """
    Regression found reviewing WI-1 (#146): the `except RuntimeError` fallback
    in _join_heartbeat_thread_async() was dead code, because
    BaseEventLoop.run_in_executor() raises its RuntimeErrors SYNCHRONOUSLY at
    call time (_check_closed()'s "Event loop is closed",
    _check_default_executor()'s "Executor shutdown has been called",
    ThreadPoolExecutor.submit()'s "cannot schedule new futures after
    shutdown") and the submission sat above the try.

    asyncio.run()'s own teardown calls shutdown_default_executor(), so a hold
    still unwinding at that point hit exactly this: the join was skipped (the
    caller's release could then be overtaken by an in-flight touch_resource(),
    re-establishing the lock under an abandoned holder id) AND the guarded
    body's real exception was replaced by a RuntimeError about asyncio
    internals.
    """

    async def test_the_join_still_happens_once_the_default_executor_is_shut_down(self):
        release_worker = threading.Event()
        heartbeat_thread = threading.Thread(
            target=lambda: release_worker.wait(timeout=5.0), daemon=True
        )
        heartbeat_thread.start()

        loop = asyncio.get_running_loop()
        await loop.shutdown_default_executor()

        threading.Timer(0.1, release_worker.set).start()
        await _join_heartbeat_thread_async(heartbeat_thread)  # must not raise

        assert not heartbeat_thread.is_alive()

    async def test_the_guarded_bodys_own_exception_is_not_replaced_by_an_executor_error(self):
        facade = MagicMock()
        facade.touch_resource.return_value = TouchResult.REFRESHED

        class _SentinelError(Exception):
            pass

        loop = asyncio.get_running_loop()
        with pytest.raises(_SentinelError):
            async with _held_with_heartbeat_async(
                facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=60.0
            ):
                await loop.shutdown_default_executor()
                raise _SentinelError("the body's real failure")

        assert "lock-heartbeat-proj_checkout-proj" not in [t.name for t in threading.enumerate()]

    async def test_the_lock_is_still_released_after_the_default_executor_is_shut_down(self):
        """
        Shape (c) in this module's docstring, defended rather than only
        argued. The final release in project_checkout_lock_async()'s finally
        is now OFFLOADED (#153 WI-8 review round: release_lock()'s acquire
        guard made an inline release cost up to two guard budgets of
        poll-sleeping on the shared event loop), which is exactly the "why is
        this one still inline?" cleanup this test was originally written to
        forbid -- because a naive offload raises RuntimeError out of
        _check_default_executor() once asyncio.run()'s teardown has shut the
        default executor down, skipping the release entirely (the lock stays
        `locked` under a synthetic holder id nothing in the next process
        knows, until TTL/staleness recovery, 7200s-14400s) AND replacing the
        guarded body's real exception with one about asyncio internals. So the
        offload carries _release_and_warn_async()'s synchronous fallback, and
        this test now pins that fallback rather than the inline call.
        """
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.return_value = TouchResult.REFRESHED
        facade.release_resource.return_value = True

        class _SentinelError(Exception):
            pass

        loop = asyncio.get_running_loop()
        # The pytest.raises half is as load-bearing as the release assertion:
        # it is what catches the exception-replacement half of the same bug.
        with pytest.raises(_SentinelError):
            async with project_checkout_lock_async("proj", facade=facade):
                await loop.shutdown_default_executor()
                raise _SentinelError("the body's real failure")

        facade.release_resource.assert_called_once()


class TestHeartbeatFailureEscalation(unittest.TestCase):
    """
    #140 item 30: a failed heartbeat refresh used to log the same WARNING
    whether it was one Redis blip or hours of sustained failure with the
    7200s Redis lock TTL about to lapse under a still-live holder. A run of
    failures now escalates to ERROR once it has lasted long enough to put
    that TTL genuinely at risk.
    """

    def test_a_brief_failure_run_logs_a_warning(self):
        with self.assertLogs('services.project_checkout_lock', level='WARNING') as captured:
            _log_heartbeat_failure("proj_checkout", "proj", 1, 60.0, "redis blip")

        self.assertEqual([r.levelno for r in captured.records], [logging.WARNING])
        self.assertIn("1 consecutive failure(s)", captured.records[0].getMessage())

    def test_a_sustained_failure_run_escalates_to_error(self):
        with self.assertLogs('services.project_checkout_lock', level='WARNING') as captured:
            _log_heartbeat_failure(
                "proj_checkout", "proj", 2, HEARTBEAT_FAILURE_ESCALATION_SECONDS, "redis down"
            )

        self.assertEqual([r.levelno for r in captured.records], [logging.ERROR])
        self.assertIn("Redis lock TTL", captured.records[0].getMessage())

    def test_repeated_refresh_failures_escalate_while_the_lock_is_still_held(self):
        facade = MagicMock()
        facade.touch_resource.side_effect = Exception("redis down")

        with patch('services.project_checkout_lock.HEARTBEAT_FAILURE_ESCALATION_SECONDS', 0.05):
            with self.assertLogs('services.project_checkout_lock', level='WARNING') as captured:
                with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
                    time.sleep(0.25)

        levels = [r.levelno for r in captured.records]
        self.assertEqual(levels[0], logging.WARNING, "the first blip must not cry wolf")
        self.assertIn(logging.ERROR, levels, "a sustained run must escalate")
        self.assertEqual(levels[-1], logging.ERROR)

    def test_a_recovered_heartbeat_resets_the_failure_run(self):
        facade = MagicMock()
        # One blip, then healthy forever after.
        facade.touch_resource.side_effect = [Exception("redis blip")] + [True] * 100

        with patch('services.project_checkout_lock.HEARTBEAT_FAILURE_ESCALATION_SECONDS', 0.05):
            with self.assertLogs('services.project_checkout_lock', level='INFO') as captured:
                with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
                    time.sleep(0.25)

        messages = [r.getMessage() for r in captured.records]
        self.assertTrue(any("recovered after 1 consecutive failed refresh" in m for m in messages))
        # No escalation, despite the run outlasting the (patched) threshold --
        # the clock restarts from the successful refresh, not from the hold.
        self.assertNotIn(logging.ERROR, [r.levelno for r in captured.records])


class TestHeartbeatDistinguishesLostFromUnrefreshed(unittest.TestCase):
    """
    Regression found reviewing WI-1 (#146): the escalation above was
    unreachable for the failure mode it was written for. It hung off
    `except Exception` around touch_resource(), but touch_lock() catches
    every store failure internally (Redis errors around hset/expire,
    _read_redis_lock_only/_read_yaml_lock_only, _save_lock_to_yaml) and
    RETURNS instead of raising -- so a sustained Redis+YAML outage, the exact
    case the escalation targets, produced no escalation at all.

    Worse, the same bare-False return also meant "another holder now owns
    this lock", so every tick of that outage logged an ERROR asserting the
    lock had been LOST and the guarded operation was racing a competing
    holder -- directly contradicting the ERROR touch_lock() itself logs one
    line earlier ("this is NOT confirmed loss to another holder either").

    touch_lock()/touch_resource() now return a tri-state TouchResult, and the
    heartbeat routes REFRESH_FAILED through the failure accounting while
    keeping the "lock lost" ERROR for a confirmed NOT_HELD.
    """

    def test_a_sustained_refresh_failure_escalates_without_claiming_the_lock_was_lost(self):
        facade = MagicMock()
        facade.touch_resource.return_value = TouchResult.REFRESH_FAILED

        with patch('services.project_checkout_lock.HEARTBEAT_FAILURE_ESCALATION_SECONDS', 0.05):
            with self.assertLogs('services.project_checkout_lock', level='WARNING') as captured:
                with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
                    time.sleep(0.25)

        levels = [r.levelno for r in captured.records]
        messages = [r.getMessage() for r in captured.records]
        self.assertEqual(levels[0], logging.WARNING, "the first blip must not cry wolf")
        self.assertIn(logging.ERROR, levels, "a sustained store outage must escalate")
        self.assertTrue(
            all("NO LONGER held" not in m for m in messages),
            "a store outage is not proof the lock was lost to a competing holder",
        )
        self.assertTrue(any("could not confirm or extend" in m for m in messages))

    def test_a_confirmed_loss_still_logs_the_lock_lost_error(self):
        facade = MagicMock()
        facade.touch_resource.return_value = TouchResult.NOT_HELD

        with self.assertLogs('services.project_checkout_lock', level='ERROR') as captured:
            with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
                time.sleep(0.05)

        self.assertTrue(any("NO LONGER held" in r.getMessage() for r in captured.records))

    def test_a_facade_still_returning_a_plain_bool_keeps_its_original_meaning(self):
        """TouchResult only has to be understood by this module; the facade is
        duck-typed (tests inject MagicMocks, and PipelineLockManager is not
        the only conceivable backing store). A plain False must still mean
        'lost', not be silently mistaken for a successful refresh."""
        facade = MagicMock()
        facade.touch_resource.return_value = False

        with self.assertLogs('services.project_checkout_lock', level='ERROR') as captured:
            with _held_with_heartbeat_sync(facade, "proj_checkout", "proj", holder_id=-42, heartbeat_interval_seconds=0.02):
                time.sleep(0.05)

        self.assertTrue(any("NO LONGER held" in r.getMessage() for r in captured.records))


class TestRealFacadeReportsStoreFailuresAsRefreshFailed(unittest.TestCase):
    """
    The half of the regression above a mock can't prove: against the REAL
    PipelineLockManager/ProjectResourceLockManager, a dual-store failure must
    surface as TouchResult.REFRESH_FAILED -- not as an exception (which is
    what the old `except Exception` escalation assumed) and not as NOT_HELD
    (which is what the old bare-False return collapsed it into).
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        # YAML-only, matching
        # test_project_resource_lock_manager.py::TestTouchResource: the whole
        # point here is what the REAL store layer returns, so the on-disk copy
        # is the one that has to be made to fail -- and it can only be the ONLY
        # configured store if there is genuinely no Redis behind it (#139).
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), use_redis=False)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_an_unreadable_store_returns_refresh_failed_rather_than_raising(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, -42)
        # Corrupt the only configured store's file: with no Redis to fall back
        # on, lock state is now genuinely unknown.
        self.lock_manager._get_state_file(
            "proj", f"__resource__{RESOURCE_NAME}"
        ).write_text("not: valid: yaml: [")

        result = self.facade.touch_resource("proj", RESOURCE_NAME, -42)

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertFalse(bool(result))

    def test_a_healthy_refresh_returns_refreshed(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, -42)

        result = self.facade.touch_resource("proj", RESOURCE_NAME, -42)

        self.assertIs(result, TouchResult.REFRESHED)
        self.assertTrue(bool(result))

    def test_a_lock_held_by_someone_else_returns_not_held(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, -42)

        result = self.facade.touch_resource("proj", RESOURCE_NAME, -43)

        self.assertIs(result, TouchResult.NOT_HELD)
        self.assertFalse(bool(result))


class _WriteFailingFakeRedis(ThreadSafeFakeRedis):
    """Reads keep answering while writes start failing -- the ordinary Redis
    states where exactly that happens (OOM under maxmemory-policy noeviction,
    MISCONF after a failed BGSAVE, READONLY after a failover). `writes_fail`
    is flipped mid-hold so the acquire itself still succeeds normally."""

    def __init__(self):
        super().__init__()
        self.writes_fail = False

    def hset(self, key, mapping):
        if self.writes_fail:
            raise RuntimeError("OOM command not allowed when used memory > 'maxmemory'")
        return super().hset(key, mapping)


class TestRedisWriteOnlyOutageIsNotReportedAsAHealthyRefresh(unittest.TestCase):
    """
    Regression found in a later WI-1 (#146) review round: touch_lock() OR-ed
    its two write legs, so a Redis-writes-fail/reads-succeed outage returned
    REFRESHED on the strength of the YAML write alone.

    Only the Redis key has a TTL (fixed 7200s), and extending it is the
    entire reason HEARTBEAT_INTERVAL_SECONDS exists -- the YAML copy never
    expires, and try_acquire_lock()'s Redis transaction reads a lapsed key
    back as an empty dict, which is falsy, so it hands the resource to a
    second caller without ever consulting that still-valid YAML copy. Under
    the old return, every tick of such an outage reset the heartbeat's
    failure run, so the sustained-failure escalation written for exactly
    this could never fire while the TTL ran down under a live holder.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.redis = _WriteFailingFakeRedis()
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=self.redis)
        self.facade = ProjectResourceLockManager(lock_manager=self.lock_manager)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_a_redis_write_failure_is_refresh_failed_even_though_yaml_succeeded(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, -42)
        self.redis.writes_fail = True

        result = self.facade.touch_resource("proj", RESOURCE_NAME, -42)

        self.assertIs(result, TouchResult.REFRESH_FAILED)
        self.assertFalse(bool(result))

    def test_a_sustained_redis_write_outage_escalates_instead_of_looking_healthy(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, -42)
        self.redis.writes_fail = True

        with patch('services.project_checkout_lock.HEARTBEAT_FAILURE_ESCALATION_SECONDS', 0.05):
            with self.assertLogs('services.project_checkout_lock', level='WARNING') as captured:
                with _held_with_heartbeat_sync(
                    self.facade, RESOURCE_NAME, "proj", holder_id=-42, heartbeat_interval_seconds=0.02
                ):
                    time.sleep(0.25)

        levels = [r.levelno for r in captured.records]
        messages = [r.getMessage() for r in captured.records]
        self.assertEqual(levels[0], logging.WARNING, "the first blip must not cry wolf")
        self.assertIn(logging.ERROR, levels, "a sustained Redis write outage must escalate")
        self.assertTrue(
            all("NO LONGER held" not in m for m in messages),
            "a write outage is not proof the lock was lost to a competing holder",
        )


@pytest.mark.asyncio
class TestAsyncAcquireRunsOffTheEventLoop:
    """
    #140 item 6: project_checkout_lock_async()'s poll loop called
    acquire_resource() -- a synchronous Redis transaction plus a YAML file
    read/write on the fallback path -- inline on the event loop, stalling the
    shared loop on every ~5s tick under contention despite the docstring's
    "never blocks the event loop" claim.
    """

    async def test_acquire_resource_is_called_off_the_event_loop_thread(self):
        loop_thread_id = threading.get_ident()
        calling_threads = []

        facade = MagicMock()
        facade.acquire_resource.side_effect = lambda *args: (
            calling_threads.append(threading.get_ident()), (True, "acquired")
        )[1]

        async with project_checkout_lock_async("proj", facade=facade):
            pass

        assert calling_threads, "acquire_resource() was never called"
        assert all(tid != loop_thread_id for tid in calling_threads)

    async def test_a_slow_acquire_attempt_does_not_stall_other_event_loop_tasks(self):
        ticks = []

        async def _ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks.append(1)

        facade = MagicMock()
        facade.acquire_resource.side_effect = lambda *args: (time.sleep(0.2), (True, "acquired"))[1]

        ticker_task = asyncio.create_task(_ticker())
        ticks_during_acquire = None
        async with project_checkout_lock_async("proj", facade=facade):
            ticks_during_acquire = len(ticks)
        ticker_task.cancel()

        assert ticks_during_acquire >= 5, (
            "the event loop was stalled for the whole acquire attempt"
        )

    async def test_cancelling_mid_acquire_releases_an_orphaned_success(self):
        """
        Offloading the attempt is what makes a cancellation able to interleave
        with it at all, so the fix has to clean up after itself: a
        concurrent.futures worker already running cannot be interrupted, and
        if it goes on to acquire the lock after its coroutine has unwound,
        nobody is left to release it (wedging the resource until
        PipelineLockManager's 7200s-14400s TTL/staleness recovery).
        """
        attempt_started = threading.Event()
        release_attempt = threading.Event()

        def _slow_acquire(*args):
            attempt_started.set()
            release_attempt.wait(timeout=5.0)
            return (True, "acquired")

        facade = MagicMock()
        facade.acquire_resource.side_effect = _slow_acquire
        facade.release_resource.return_value = True

        async def _holder():
            async with project_checkout_lock_async("proj", 42, facade=facade):
                pass  # pragma: no cover -- never reached; cancelled mid-acquire

        task = asyncio.create_task(_holder())
        await asyncio.to_thread(attempt_started.wait, 5.0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert not facade.release_resource.called  # nothing acquired yet

        release_attempt.set()  # the orphaned attempt now succeeds
        for _ in range(200):
            if facade.release_resource.called:
                break
            await asyncio.sleep(0.02)

        facade.release_resource.assert_called_once()
        assert facade.release_resource.call_args.args[:2] == ("proj", RESOURCE_NAME)

    async def test_an_orphaned_success_has_its_heartbeat_stopped_before_the_release(self):
        """The orphaned attempt now also STARTS a heartbeat (see
        TestAcquireAndHeartbeatStartAreAtomic), so the cleanup callback has to
        stop and join it first -- a refresh still in flight when the phantom
        holder is released would re-establish the lock right back under it."""
        attempt_started = threading.Event()
        release_attempt = threading.Event()
        order = []

        def _slow_acquire(*args):
            attempt_started.set()
            release_attempt.wait(timeout=5.0)
            return (True, "acquired")

        facade = MagicMock()
        facade.acquire_resource.side_effect = _slow_acquire
        facade.touch_resource.side_effect = lambda *a: (order.append("touched"), TouchResult.REFRESHED)[1]
        facade.release_resource.side_effect = lambda *a: (order.append("released"), True)[1]

        async def _holder():
            async with project_checkout_lock_async("proj", 42, facade=facade):
                pass  # pragma: no cover -- never reached; cancelled mid-acquire

        task = asyncio.create_task(_holder())
        await asyncio.to_thread(attempt_started.wait, 5.0)
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        release_attempt.set()  # the orphaned attempt now succeeds
        for _ in range(200):
            if facade.release_resource.called:
                break
            await asyncio.sleep(0.02)

        facade.release_resource.assert_called_once()
        assert order[-1] == "released", "a heartbeat refresh must not outlive the release"
        assert f"lock-heartbeat-{RESOURCE_NAME}-proj" not in [t.name for t in threading.enumerate()]


@pytest.mark.asyncio
class TestAcquireAndHeartbeatStartAreAtomic:
    """
    Regression found reviewing WI-1 (#146): offloading the acquire introduced a
    suspension point between "the worker thread won the lock" and "the awaiting
    coroutine is rescheduled and starts the heartbeat". A single-threaded loop
    blocked by some OTHER task's guarded body -- the case this whole module is
    designed around, and one that lasts the agent's entire runtime (agents.yaml
    allows up to 10800s) -- holds that gap open on a lock that is ALREADY held
    and not yet being refreshed, letting PipelineLockManager's fixed 7200s
    Redis lock TTL lapse under it. Past that point a second caller's acquire
    reads the expired key back as an empty dict and is granted the same
    resource: the exact double-acquisition this lock exists to close.

    So the acquire and the heartbeat start happen in ONE executor callable, and
    the held-duration context manager adopts that already-running thread.
    """

    async def test_the_heartbeat_is_already_running_when_the_acquire_await_returns(self):
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.return_value = TouchResult.REFRESHED

        can_execute, _, heartbeat = await _acquire_and_start_heartbeat_off_loop(
            facade, RESOURCE_NAME, "proj", -42, None, heartbeat_interval_seconds=60.0
        )

        assert can_execute
        assert heartbeat is not None, "the hold was granted with no heartbeat attached"
        stop_event, heartbeat_thread = heartbeat
        try:
            assert heartbeat_thread.is_alive()
        finally:
            stop_event.set()
            heartbeat_thread.join()

    async def test_the_heartbeat_thread_is_started_by_the_acquiring_worker_thread(self):
        loop_thread_id = threading.get_ident()
        acquired_from = []
        started_from = []
        real_start = project_checkout_lock._start_heartbeat_thread

        def _probe_start(*args, **kwargs):
            started_from.append(threading.get_ident())
            return real_start(*args, **kwargs)

        facade = MagicMock()
        facade.acquire_resource.side_effect = lambda *a: (
            acquired_from.append(threading.get_ident()), (True, "acquired")
        )[1]
        facade.touch_resource.return_value = TouchResult.REFRESHED

        with patch('services.project_checkout_lock._start_heartbeat_thread', _probe_start):
            async with project_checkout_lock_async("proj", facade=facade):
                pass

        assert started_from, "no heartbeat thread was ever started"
        assert started_from[0] != loop_thread_id
        assert started_from[0] == acquired_from[0], (
            "the heartbeat must start on the same worker thread that won the lock, "
            "with no event-loop scheduling in between"
        )

    async def test_a_failed_acquisition_starts_no_heartbeat(self):
        facade = MagicMock()
        facade.acquire_resource.return_value = (False, "held by someone else")

        can_execute, reason, heartbeat = await _acquire_and_start_heartbeat_off_loop(
            facade, RESOURCE_NAME, "proj", -42, None, heartbeat_interval_seconds=60.0
        )

        assert not can_execute
        assert reason == "held by someone else"
        assert heartbeat is None
        facade.touch_resource.assert_not_called()

    async def test_a_heartbeat_that_cannot_be_started_releases_the_lock_it_just_won(self):
        """The lock IS held once acquire_resource() returns, but the awaiting
        coroutine only ever sees the exception -- so nothing downstream knows
        to release it."""
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.release_resource.return_value = True

        with patch(
            'services.project_checkout_lock._start_heartbeat_thread',
            side_effect=RuntimeError("can't start new thread"),
        ):
            with pytest.raises(RuntimeError, match="can't start new thread"):
                await _acquire_and_start_heartbeat_off_loop(
                    facade, RESOURCE_NAME, "proj", -42, 42, heartbeat_interval_seconds=60.0
                )

        facade.release_resource.assert_called_once_with("proj", RESOURCE_NAME, -42)


@pytest.mark.asyncio
class TestReleaseNeverOvertakesAnInFlightHeartbeat:
    """
    The composed ordering guarantee, end to end through
    project_checkout_lock_async() rather than through the join helper alone:
    an in-flight touch_resource() that completed AFTER release_resource()
    would re-establish the lock under a holder_id nobody will ever release
    again, leaking it until TTL/staleness recovery (7200s-14400s).
    """

    async def test_release_resource_is_called_only_after_the_in_flight_touch_returns(self):
        order = []
        touch_entered = threading.Event()
        let_touch_finish = threading.Event()
        touch_calls = []

        def _touch(*args):
            touch_calls.append(1)
            if len(touch_calls) == 1:
                touch_entered.set()
                let_touch_finish.wait(timeout=5.0)
                order.append("touch_returned")
            return TouchResult.REFRESHED

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.side_effect = _touch
        facade.release_resource.side_effect = lambda *a: (order.append("released"), True)[1]

        real_start = project_checkout_lock._start_heartbeat_thread

        def _fast_start(facade_, resource_name, project, holder_id, _interval):
            # The production interval is 1800s; nothing else about the hold
            # changes, so this is the one knob the composed path doesn't expose.
            return real_start(facade_, resource_name, project, holder_id, 0.02)

        with patch('services.project_checkout_lock._start_heartbeat_thread', _fast_start):
            async def _hold():
                async with project_checkout_lock_async("proj", facade=facade):
                    await asyncio.to_thread(touch_entered.wait, 5.0)

            task = asyncio.create_task(_hold())
            await asyncio.to_thread(touch_entered.wait, 5.0)
            threading.Timer(0.2, let_touch_finish.set).start()
            await task

        assert order == ["touch_returned", "released"]


@pytest.mark.asyncio
class TestCancellationWhileHoldingTheAsyncLock:
    """
    #140 item 34 composed end to end: a cancellation arriving while the guarded
    body is INSIDE the `async with` (an agent timeout, a shutdown, an outer
    asyncio.wait_for), not while it is still acquiring.

    Found in review of #146 WI-1: the two halves were covered separately and
    never together -- TestAsyncHeartbeatJoinSurvivesCancellation drives
    _join_heartbeat_thread_async() with a synthetic thread that is not a
    heartbeat and has no facade behind it (so it never reaches
    _release_and_warn), and TestReleaseNeverOvertakesAnInFlightHeartbeat drives
    the composed context manager but exits its body normally. Nothing pinned
    the composition, and project_checkout_lock_async()'s release lives in an
    async-generator `finally` -- asyncio async-generator finalisation under
    cancellation is exactly where a release gets silently skipped. A skipped
    release here has no orphan-cleanup path at all: the holder_id is synthetic
    and process-local, so nothing else in the process would ever release it and
    the project's checkout stays wedged for 7200s-14400s.
    """

    @staticmethod
    def _facade_with_a_slow_first_touch(order, touch_entered, let_touch_finish):
        touch_calls = []

        def _touch(*args):
            touch_calls.append(1)
            if len(touch_calls) == 1:
                touch_entered.set()
                let_touch_finish.wait(timeout=5.0)
                order.append("touch_returned")
            return TouchResult.REFRESHED

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.side_effect = _touch
        facade.release_resource.side_effect = lambda *a: (order.append("released"), True)[1]
        return facade

    async def _cancel_mid_hold(self, facade, touch_entered, let_touch_finish, cancel_times=1):
        async def _hold():
            async with project_checkout_lock_async("proj", facade=facade):
                await asyncio.sleep(30)  # cancelled here, mid-hold

        with _heartbeat_interval_override(0.02):
            task = asyncio.create_task(_hold())
            await asyncio.to_thread(touch_entered.wait, 5.0)
            # Released from a real OS thread: once the cancellation lands the
            # join finishes synchronously, so nothing scheduled on the loop
            # could unblock it.
            threading.Timer(0.2, let_touch_finish.set).start()
            for _ in range(cancel_times):
                task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

    async def test_a_cancellation_mid_hold_joins_the_heartbeat_then_releases_once(self):
        order = []
        touch_entered = threading.Event()
        let_touch_finish = threading.Event()
        facade = self._facade_with_a_slow_first_touch(order, touch_entered, let_touch_finish)

        await self._cancel_mid_hold(facade, touch_entered, let_touch_finish)

        # The in-flight refresh finished BEFORE the release -- one completing
        # after it would re-establish the lock under a holder nobody releases.
        assert order == ["touch_returned", "released"]
        facade.release_resource.assert_called_once()
        assert facade.release_resource.call_args.args[:2] == ("proj", RESOURCE_NAME)
        assert f"lock-heartbeat-{RESOURCE_NAME}-proj" not in [t.name for t in threading.enumerate()]

    async def test_a_repeated_cancellation_mid_hold_still_releases_exactly_once(self):
        """asyncio.Task.cancel() can legitimately be called more than once (a
        shutdown sweep on top of an outer wait_for): the release must not be
        skipped, nor run twice."""
        order = []
        touch_entered = threading.Event()
        let_touch_finish = threading.Event()
        facade = self._facade_with_a_slow_first_touch(order, touch_entered, let_touch_finish)

        await self._cancel_mid_hold(facade, touch_entered, let_touch_finish, cancel_times=3)

        assert order == ["touch_returned", "released"]
        facade.release_resource.assert_called_once()
        assert f"lock-heartbeat-{RESOURCE_NAME}-proj" not in [t.name for t in threading.enumerate()]


@pytest.mark.asyncio
class TestDefaultFacadeConstructedOffTheEventLoop:
    """
    #140 item 7: ProjectResourceLockManager() defaults to
    get_pipeline_lock_manager(), whose double-checked-locking guard is held
    across a full PipelineLockManager() construction -- Redis connect +
    .ping() with socket_connect_timeout=5 -- so the event-loop thread could
    block for up to that timeout if it reached the guard while a background
    thread was mid-construction.
    """

    async def test_default_facade_construction_happens_off_the_loop(self):
        loop_thread_id = threading.get_ident()
        constructing_threads = []

        class _ProbeFacade:
            def __init__(self):
                constructing_threads.append(threading.get_ident())

        with patch('services.project_checkout_lock.ProjectResourceLockManager', _ProbeFacade):
            facade = await _default_facade_off_loop()

        assert isinstance(facade, _ProbeFacade)
        assert len(constructing_threads) == 1
        assert constructing_threads[0] != loop_thread_id


@pytest.mark.asyncio
class TestAsyncReleaseRunsOffTheEventLoop:
    """
    REGRESSION (#153 WI-8 review round). project_checkout_lock_async()'s and
    dev_container_build_lock_async()'s release was deliberately left inline on
    the event loop -- shape (c) in the module docstring -- justified by
    "bounded by PipelineLockManager's own Redis socket timeouts". That stopped
    being true in this work item: release_lock() gained a
    '<state>.yaml.acquire.lock' guard whose wait is a `time.sleep(0.1)` poll
    loop on the calling thread, for RELEASE_GUARD_TIMEOUT_SECONDS and then
    RELEASE_GUARD_RETRY_TIMEOUT_SECONDS.

    The trigger is the one that budget's own comment predicts: with Redis
    unavailable every try_acquire_lock() takes that same guard on its
    YAML-fallback path and burns 5s Redis socket timeouts inside it, while
    this module's waiters re-poll every DEFAULT_POLL_INTERVAL_SECONDS -- so the
    guard is held near-continuously. Every exit of an
    `async with project_checkout_lock_async(...)` (claude_integration,
    auto_commit, feature_branch_manager, repair_cycle) would then stall the
    whole orchestrator for that budget: no board polling, no dispatch, no
    board-lock heartbeat sweep, no progression, for every project at once,
    and concurrent releases across projects serialize.
    """

    async def test_release_resource_is_called_off_the_event_loop_thread(self):
        loop_thread_id = threading.get_ident()
        calling_threads = []

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.return_value = TouchResult.REFRESHED
        facade.release_resource.side_effect = lambda *args: (
            calling_threads.append(threading.get_ident()), True
        )[1]

        async with project_checkout_lock_async("proj", facade=facade):
            pass

        assert calling_threads, "release_resource() was never called"
        assert all(tid != loop_thread_id for tid in calling_threads)

    async def test_a_slow_release_does_not_stall_other_event_loop_tasks(self):
        """The behavioural half: a guard budget spent here is a budget the
        monitor loop is not polling boards in."""
        ticks = []

        async def _ticker():
            while True:
                await asyncio.sleep(0.01)
                ticks.append(1)

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.return_value = TouchResult.REFRESHED
        facade.release_resource.side_effect = lambda *a: (time.sleep(0.2), True)[1]

        ticker_task = asyncio.create_task(_ticker())
        ticks_before = len(ticks)
        async with project_checkout_lock_async("proj", facade=facade):
            pass
        ticks_during_release = len(ticks) - ticks_before
        ticker_task.cancel()

        assert ticks_during_release >= 5, (
            "the event loop was stalled for the whole release"
        )

    async def test_the_dev_container_build_lock_release_is_offloaded_too(self):
        """The two modules share every other helper; the release was the one
        place each spelled the call out itself, so each had to be moved."""
        from services.dev_container_build_lock import dev_container_build_lock_async

        loop_thread_id = threading.get_ident()
        calling_threads = []

        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.return_value = TouchResult.REFRESHED
        facade.release_resource.side_effect = lambda *args: (
            calling_threads.append(threading.get_ident()), True
        )[1]

        async with dev_container_build_lock_async("proj", facade=facade):
            pass

        assert calling_threads, "release_resource() was never called"
        assert all(tid != loop_thread_id for tid in calling_threads)

    async def test_a_cancellation_waits_for_the_in_flight_release_exactly_once(self):
        """
        Offloading must not weaken what shape (b) already guaranteed: a
        skipped release has no orphan-cleanup path, so the cancellation
        fallback waits for the call already in flight -- rather than returning
        early (leaking the hold) or making a second call (a spurious "may
        already be released" and the stall back on the loop).
        """
        release_entered = threading.Event()
        let_release_finish = threading.Event()
        calls = []

        def _slow_release(*args):
            calls.append(1)
            release_entered.set()
            let_release_finish.wait(timeout=5.0)
            return True

        facade = MagicMock()
        facade.release_resource.side_effect = _slow_release

        async def _releaser():
            await _release_and_warn_async(facade, RESOURCE_NAME, "proj", -42, 7)

        task = asyncio.create_task(_releaser())
        await asyncio.to_thread(release_entered.wait, 5.0)
        # From a real OS thread: once the cancellation lands, the wait is
        # synchronous on the loop, so nothing scheduled on the loop could
        # unblock it.
        threading.Timer(0.2, let_release_finish.set).start()
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        # The cancellation propagated only AFTER the release finished, and
        # nothing started a second one.
        assert let_release_finish.is_set()
        assert calls == [1]

    async def test_the_release_still_happens_once_the_default_executor_is_shut_down(self):
        """The submission is inside the try for the same reason it is in
        _join_heartbeat_thread_async(): run_in_executor() raises its
        RuntimeErrors synchronously at call time, so a submission above the try
        would skip the release outright during asyncio.run()'s teardown."""
        facade = MagicMock()
        facade.release_resource.return_value = True

        loop = asyncio.get_running_loop()
        await loop.shutdown_default_executor()

        await _release_and_warn_async(facade, RESOURCE_NAME, "proj", -42, 7)

        facade.release_resource.assert_called_once()


class TestReleaseAndWarnReportsWhyTheReleaseFailed(unittest.TestCase):
    """
    REGRESSION (#153 WI-8 review round): release_lock() now takes the lock's
    acquire guard, and a guard it cannot take used to come back as the same
    bare False as a considered-and-refused release. _release_and_warn() said
    "lock may already be released or retained" at WARNING for both -- but the
    guard case is the one with real consequences, and this module's own call
    site spells them out: nothing else in the process knows this holder_id,
    so an unreleased lock leaks until TTL/staleness recovery and blocks every
    acquisition of the resource for that project meanwhile.
    """

    def setUp(self):
        self.facade = MagicMock()

    def test_a_serialization_failure_is_an_error_naming_the_unreleased_lock(self):
        from services.pipeline_lock_manager import ReleaseResult

        self.facade.release_resource.return_value = ReleaseResult.SERIALIZATION_FAILED

        with self.assertLogs('services.project_checkout_lock', level='ERROR') as logs:
            _release_and_warn(self.facade, RESOURCE_NAME, "proj", -42, 7)

        text = "\n".join(logs.output)
        # "did NOT complete" rather than "still held": SERIALIZATION_FAILED now
        # also covers losing the INNER '<state>.yaml.lock' after the Redis leg
        # has already deleted its key, where the record is half gone rather
        # than untouched. Either way it is not a retained/failed lock.
        self.assertIn("did NOT complete", text)
        self.assertIn("-42", text)
        self.assertNotIn("may already be released or retained", text)

    def test_an_ordinary_refusal_stays_a_warning(self):
        from services.pipeline_lock_manager import ReleaseResult

        self.facade.release_resource.return_value = ReleaseResult.NOT_RELEASED

        with self.assertLogs('services.project_checkout_lock', level='WARNING') as logs:
            _release_and_warn(self.facade, RESOURCE_NAME, "proj", -42, 7)

        text = "\n".join(logs.output)
        self.assertIn("may already be released or retained", text)
        self.assertNotIn("ERROR", text)

    def test_a_successful_release_logs_nothing(self):
        from services.pipeline_lock_manager import ReleaseResult

        self.facade.release_resource.return_value = ReleaseResult.RELEASED

        with patch.object(project_checkout_lock.logger, 'warning') as warn, \
                patch.object(project_checkout_lock.logger, 'error') as err:
            _release_and_warn(self.facade, RESOURCE_NAME, "proj", -42, 7)

        warn.assert_not_called()
        err.assert_not_called()


if __name__ == '__main__':
    unittest.main()

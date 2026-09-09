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
from services.pipeline_lock_manager import PipelineLockManager, TouchResult
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
    HEARTBEAT_FAILURE_ESCALATION_SECONDS,
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
        # YAML-only (redis_client=None), matching
        # test_project_resource_lock_manager.py::TestTouchResource: the whole
        # point here is what the REAL store layer returns, so the on-disk copy
        # is the one that has to be made to fail.
        self.lock_manager = PipelineLockManager(state_dir=Path(self.test_dir), redis_client=None)
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


if __name__ == '__main__':
    unittest.main()

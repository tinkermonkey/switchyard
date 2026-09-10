"""
Tests for services/dev_container_build_lock.py (issue #56, Phase 2 of the
concurrency redesign, #88/#34).

Mirrors tests/unit/services/test_project_checkout_lock.py's own structure
(#54) for the equivalent module guarding a different resource:

1. Sequential unit coverage of dev_container_build_lock_sync()/_async()'s own
   poll/retry/timeout mechanics, through the REAL ProjectResourceLockManager
   facade (#53) backed by a real temp-dir PipelineLockManager + a small
   thread-safe fake Redis client (ThreadSafeFakeRedis below, duplicated from
   test_project_checkout_lock.py rather than imported cross-module -- see
   that file's own module docstring for why a real transactional fake,
   rather than a bare MagicMock, is needed for a genuine-concurrency test to
   mean anything).

2. TestConcurrentCollisionSerializes: the acceptance-criteria regression
   test itself -- two genuinely concurrent OS threads and two genuinely
   concurrent asyncio tasks acquiring the SAME project's dev_container_build
   lock, proving they serialize (never run their guarded section at the same
   time) rather than racing, standing in for "a pipeline-driven build/verify
   racing an admin-script-driven one against the same project" (#56's own
   acceptance criterion).

3. TestReusesProjectCheckoutLockHolderIdMinting: confirms this module reuses
   project_checkout_lock._mint_unique_holder_id() rather than re-deriving its
   own counter -- see this module's own docstring ("Why this reuses
   project_checkout_lock's holder id minting") for why that matters. The
   minting function's own correctness (negative, unique per call, unique
   across threads) is already covered by
   test_project_checkout_lock.py::TestMintUniqueHolderId and is not
   re-tested here.
"""

import asyncio
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import tempfile
import shutil

import pytest

from services.pipeline_lock_manager import PipelineLockManager, TouchResult
from services.project_resource_lock_manager import ProjectResourceLockManager
from services import project_checkout_lock
from services.dev_container_build_lock import (
    acquire_failure_is_contention,
    agent_holds_build_window,
    BUILD_WINDOW_AGENTS,
    dev_container_build_lock_async,
    dev_container_build_lock_if_free_async,
    dev_container_build_lock_if_free_sync,
    dev_container_build_lock_sync,
    DevContainerBuildLockTimeoutError,
    RESOURCE_NAME,
)


class ThreadSafeFakeRedis:
    """Minimal in-memory stand-in for a single Redis instance's hash + atomic
    transaction API, sufficient for PipelineLockManager's try_acquire_lock()/
    release_lock()/get_lock(). Duplicated from test_project_checkout_lock.py
    (see that module's docstring for the full rationale) rather than shared
    via cross-test-module import, since tests/unit has no package __init__.py
    making such an import reliable under pytest's collection."""

    def __init__(self):
        self._store = {}
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
        with self._global_lock:
            return func(ThreadSafeFakeRedis._Pipe(self))


def _make_facade(tmp_dir: str) -> ProjectResourceLockManager:
    lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), redis_client=ThreadSafeFakeRedis())
    return ProjectResourceLockManager(lock_manager=lock_manager)


def _make_yaml_only_facade(tmp_dir: str) -> ProjectResourceLockManager:
    """
    Facade over PipelineLockManager's documented YAML-only fallback -- see
    test_project_checkout_lock.py's helper of the same name for the full
    rationale. redis_client is cleared explicitly after construction rather
    than just passed as None, because None makes the constructor build a real
    client from REDIS_HOST, which succeeds inside the orchestrator container.
    """
    lock_manager = PipelineLockManager(state_dir=Path(tmp_dir), redis_client=None)
    lock_manager.redis_client = None
    return ProjectResourceLockManager(lock_manager=lock_manager)


class TestDevContainerBuildLockSyncMechanics(unittest.TestCase):
    """Sequential (single-thread) coverage of the sync context manager's own
    poll/retry/timeout/release mechanics."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_acquires_and_releases_when_uncontended(self):
        entered = []
        with dev_container_build_lock_sync("proj", 111, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
            entered.append(True)
            lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
            self.assertIsNotNone(lock)
            self.assertLess(lock.locked_by_issue, 0)

        self.assertEqual(entered, [True])
        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_releases_on_exception_inside_with_block(self):
        with self.assertRaises(ValueError):
            with dev_container_build_lock_sync("proj", 111, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
                raise ValueError("boom")

        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_waits_then_acquires_once_the_other_holder_releases(self):
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
            with dev_container_build_lock_sync("proj", 2, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.02):
                elapsed = time.monotonic() - start
                lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
                self.assertLess(lock.locked_by_issue, 0)
            self.assertGreaterEqual(elapsed, 0.15)
        finally:
            releaser.join()

    def test_raises_timeout_error_when_never_freed(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)  # never released

        with self.assertRaises(DevContainerBuildLockTimeoutError):
            with dev_container_build_lock_sync("proj", 2, facade=self.facade, timeout_seconds=0.1, poll_interval_seconds=0.02):
                pass  # pragma: no cover -- must never be entered

        lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
        self.assertEqual(lock.locked_by_issue, 1)


@pytest.mark.asyncio
class TestDevContainerBuildLockAsyncMechanics:
    """Async counterpart of the sequential mechanics tests above."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    async def test_acquires_and_releases_when_uncontended(self):
        async with dev_container_build_lock_async("proj", 111, facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
            lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
            assert lock.locked_by_issue < 0

        assert self.facade.get_resource_lock("proj", RESOURCE_NAME) is None

    async def test_raises_timeout_error_when_never_freed(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)

        with pytest.raises(DevContainerBuildLockTimeoutError):
            async with dev_container_build_lock_async("proj", 2, facade=self.facade, timeout_seconds=0.1, poll_interval_seconds=0.02):
                pass  # pragma: no cover


class TestConcurrentCollisionSerializes(unittest.TestCase):
    """
    THE regression test called for by issue #56's acceptance criteria: two
    genuinely concurrent operations against the same project's
    dev_container_build resource lock -- standing in for "a pipeline-driven
    build/verify racing an admin-script-driven one against the same
    project" -- must serialize (never overlap) instead of racing, and both
    must eventually complete (this is a mutex, not a one-wins-one-fails
    gate).
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_two_threads_racing_the_same_project_serialize(self):
        """Two real OS threads simulate 'rebuild_project_images.py racing a
        live pipeline-driven dev_environment_setup build' against the same
        project (#56's own named risk) -- both must be able to run their
        guarded section, but never at the same time."""
        concurrent_count = {"value": 0}
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = []
        errors = []

        def worker(issue_number):
            try:
                with dev_container_build_lock_sync(
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
        claude/claude_integration.py's run_claude_code() actually runs in for
        dev_environment_setup/verifier's local execution) instead of OS
        threads."""

        async def run():
            concurrent_count = {"value": 0}
            max_concurrent = {"value": 0}
            completed = []

            async def worker(issue_number):
                async with dev_container_build_lock_async(
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

    def test_two_callers_with_the_SAME_real_issue_number_still_serialize(self):
        """Same critical case test_project_checkout_lock.py's equivalent
        test covers: PipelineLockManager.try_acquire_lock() treats a
        MATCHING issue_number as reentrant with no other identity check. If
        this lock passed the caller's real issue_number straight through as
        the holder identity, two genuinely different concurrent operations
        sharing a real issue number would each be told they already hold the
        lock and both would proceed concurrently. Passing the SAME
        issue_number for both racing callers here proves the reused
        _mint_unique_holder_id()-based fix holds for this lock too."""
        concurrent_count = {"value": 0}
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = {"count": 0}
        SAME_ISSUE_NUMBER = 42

        def worker():
            with dev_container_build_lock_sync(
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
        self.assertEqual(max_concurrent["value"], 1)  # ...but never at the same time
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))


class TestYamlOnlyFallbackConcurrency(unittest.TestCase):
    """
    The same acceptance criterion as TestConcurrentCollisionSerializes, but
    over PipelineLockManager's YAML-only fallback rather than its atomic Redis
    transaction (see _make_yaml_only_facade, and
    test_project_checkout_lock.py's equivalent class for the full rationale).

    Both of #56's own named racers reach that branch: a pipeline-driven build
    through dev_container_build_lock_async()'s off-loop poll, and
    scripts/rebuild_project_images.py through dev_container_build_lock_sync().
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_yaml_only_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_asyncio_tasks_racing_the_same_project_serialize_without_redis(self):
        async def run():
            inside = []
            max_concurrent = {"value": 0}
            completed = []

            async def worker(n):
                async with dev_container_build_lock_async(
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

        self.assertEqual(sorted(completed), list(range(6)))
        self.assertEqual(max_concurrent, 1)
        self.assertIsNone(self.facade.get_resource_lock("shared-project", RESOURCE_NAME))

    def test_threads_racing_the_same_project_serialize_without_redis(self):
        inside = []
        max_concurrent = {"value": 0}
        count_lock = threading.Lock()
        completed = []
        errors = []

        def worker(n):
            try:
                with dev_container_build_lock_sync(
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


class TestDistinctFromProjectCheckoutResource(unittest.TestCase):
    """dev_container_build and project_checkout are distinct resources under
    the same facade -- holding one must never block the other for the same
    project (see claude/claude_integration.py, which nests them)."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_dev_container_build_lock_does_not_block_project_checkout_lock(self):
        with dev_container_build_lock_sync("proj", facade=self.facade, timeout_seconds=5, poll_interval_seconds=0.01):
            # A concurrent project_checkout acquisition for the SAME project
            # must succeed immediately -- different resource_name entirely.
            with project_checkout_lock.project_checkout_lock_sync(
                "proj", facade=self.facade, timeout_seconds=0.5, poll_interval_seconds=0.01,
            ):
                pass  # reaching here at all is the assertion


class TestReusesProjectCheckoutLockHolderIdMinting(unittest.TestCase):
    """Confirms dev_container_build_lock imports and reuses
    project_checkout_lock._mint_unique_holder_id() (see this module's own
    docstring, "Why this reuses project_checkout_lock's holder id minting")
    rather than re-deriving its own counter. The minting function's own
    correctness is already covered by test_project_checkout_lock.py."""

    def test_same_function_object_is_reused(self):
        import services.dev_container_build_lock as dcbl
        self.assertIs(dcbl._mint_unique_holder_id, project_checkout_lock._mint_unique_holder_id)

    def test_shared_off_loop_helpers_are_reused(self):
        """WI-1 (#146): the event-loop fixes for the async path -- an OS-thread
        heartbeat that fires even when the guarded body never yields (#141),
        and an acquire_resource() poll tick that runs off the loop and starts
        that heartbeat on the same worker thread (#140 item 6) -- live in
        project_checkout_lock.py and are imported here, so this module's async
        lock gets them too."""
        import services.dev_container_build_lock as dcbl
        self.assertIs(
            dcbl._acquire_and_start_heartbeat_off_loop,
            project_checkout_lock._acquire_and_start_heartbeat_off_loop,
        )
        self.assertIs(dcbl._default_facade_off_loop, project_checkout_lock._default_facade_off_loop)
        self.assertIs(dcbl._held_with_heartbeat_async, project_checkout_lock._held_with_heartbeat_async)


@pytest.mark.asyncio
class TestAsyncPathSharesTheEventLoopFixes:
    """
    WI-1 (#146): behavioral spot-checks that this module's async lock really
    gets the shared event-loop fixes at runtime, not just by import identity
    (see TestReusesProjectCheckoutLockHolderIdMinting for that). Their full
    behavior is covered in test_project_checkout_lock.py.
    """

    async def test_acquire_resource_is_called_off_the_event_loop_thread(self):
        loop_thread_id = threading.get_ident()
        calling_threads = []

        facade = MagicMock()
        facade.acquire_resource.side_effect = lambda *args: (
            calling_threads.append(threading.get_ident()), (True, "acquired")
        )[1]

        async with dev_container_build_lock_async("proj", facade=facade):
            pass

        assert calling_threads, "acquire_resource() was never called"
        assert all(tid != loop_thread_id for tid in calling_threads)

    async def test_heartbeat_fires_while_the_guarded_body_blocks_the_event_loop(self):
        """
        The local-execution path this lock wraps
        (claude/claude_integration.py's _run_claude_code_locally()) blocks the
        event loop for the subprocess's whole runtime -- see #141.

        Drives dev_container_build_lock_async() itself rather than the shared
        _held_with_heartbeat_async() helper: found in a later WI-1 (#146)
        review round that going through the helper directly re-tested code
        already covered in test_project_checkout_lock.py and proved nothing
        about THIS module's wiring -- dropping `heartbeat=heartbeat` from the
        call site below (which would leak one never-stopped heartbeat thread
        per build, and silently reopen the acquire-then-heartbeat gap
        _acquire_and_start_heartbeat() exists to close) left the whole file
        green.
        """
        touched = threading.Event()
        touch_args = []
        acquired_holder_ids = []
        thread_name = f"lock-heartbeat-{RESOURCE_NAME}-proj"

        facade = MagicMock()
        facade.acquire_resource.side_effect = lambda project, resource, holder_id: (
            acquired_holder_ids.append(holder_id), (True, "acquired")
        )[1]
        facade.touch_resource.side_effect = lambda *args: (
            touch_args.append(args), touched.set(), TouchResult.REFRESHED
        )[2]

        real_start = project_checkout_lock._start_heartbeat_thread

        def _fast_start(facade_, resource_name, project, holder_id, _interval):
            # The production interval is 1800s; nothing else about the hold
            # changes, so this is the one knob the composed path doesn't expose.
            return real_start(facade_, resource_name, project, holder_id, 0.02)

        with patch('services.project_checkout_lock._start_heartbeat_thread', _fast_start):
            async with dev_container_build_lock_async("proj", facade=facade):
                fired_during_the_block = touched.wait(timeout=5.0)  # no `await` here, deliberately
                # Exactly one: the thread the acquiring worker started and
                # this hold adopted, not that one plus a second started here.
                live = [t for t in threading.enumerate() if t.name == thread_name and t.is_alive()]
                assert len(live) == 1, f"expected exactly one adopted heartbeat thread, got {len(live)}"

        assert fired_during_the_block
        assert touch_args, "touch_resource() was never called"
        # Pins the adoption to the acquire's own holder id -- the id a
        # self-started thread would share, which is why the counts above
        # carry the rest of the weight.
        assert all(args == ("proj", RESOURCE_NAME, acquired_holder_ids[0]) for args in touch_args)
        assert not [t for t in threading.enumerate() if t.name == thread_name and t.is_alive()], (
            "the hold's heartbeat thread outlived the with block"
        )

    async def test_the_lock_is_still_released_after_the_default_executor_is_shut_down(self):
        """
        Mirror of test_project_checkout_lock.py's test of the same name: the
        final _release_and_warn() in dev_container_build_lock_async()'s
        finally must stay SYNCHRONOUS on the loop. Offloading it makes it
        raise RuntimeError out of _check_default_executor() once
        asyncio.run()'s teardown has shut the default executor down --
        skipping the release entirely (the build lock then stays `locked`
        under a holder id nothing in the next process knows, until
        TTL/staleness recovery) and replacing the guarded body's real
        exception with one about asyncio internals.
        """
        facade = MagicMock()
        facade.acquire_resource.return_value = (True, "acquired")
        facade.touch_resource.return_value = TouchResult.REFRESHED
        facade.release_resource.return_value = True

        class _SentinelError(Exception):
            pass

        loop = asyncio.get_running_loop()
        with pytest.raises(_SentinelError):
            async with dev_container_build_lock_async("proj", facade=facade):
                await loop.shutdown_default_executor()
                raise _SentinelError("the body's real failure")

        facade.release_resource.assert_called_once()

    async def test_a_cancellation_mid_hold_joins_the_heartbeat_then_releases_once(self):
        """
        Mirror of test_project_checkout_lock.py's
        TestCancellationWhileHoldingTheAsyncLock (#140 item 34, found still
        uncovered in review of #146 WI-1): a cancellation arriving while the
        guarded body is INSIDE the `async with`. dev_container_build_lock_async()
        has its own copy of the try/finally that owns the release, so the
        guarantee has to be pinned here too -- moving the release inside the
        `async with`, shielding the teardown, or offloading the release would
        skip it entirely on this path, wedging the build lock under a
        synthetic, process-local holder id nothing else will ever release.
        """
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
            return real_start(facade_, resource_name, project, holder_id, 0.02)

        async def _hold():
            async with dev_container_build_lock_async("proj", facade=facade):
                await asyncio.sleep(30)  # cancelled here, mid-hold

        with patch('services.project_checkout_lock._start_heartbeat_thread', _fast_start):
            task = asyncio.create_task(_hold())
            await asyncio.to_thread(touch_entered.wait, 5.0)
            threading.Timer(0.2, let_touch_finish.set).start()
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task

        assert order == ["touch_returned", "released"]
        facade.release_resource.assert_called_once()
        assert facade.release_resource.call_args.args[:2] == ("proj", RESOURCE_NAME)
        assert f"lock-heartbeat-{RESOURCE_NAME}-proj" not in [t.name for t in threading.enumerate()]


if __name__ == '__main__':
    unittest.main()


class TestBuildWindowAgentGate(unittest.TestCase):
    """
    #152 item B: which agents this lock is acquired FOR. The gate used to be
    claude_integration's `use_docker=False` branch, which five non-building
    callers also reach -- see agent_holds_build_window()'s docstring and
    tests/unit/test_claude_integration_dev_container_lock_gating.py for the
    call-site regression coverage.
    """

    def test_only_the_two_build_agents_are_members(self):
        self.assertEqual(
            BUILD_WINDOW_AGENTS,
            frozenset({"dev_environment_setup", "dev_environment_verifier"}),
        )

    def test_predicate_matches_the_set(self):
        for agent in BUILD_WINDOW_AGENTS:
            self.assertTrue(agent_holds_build_window(agent))
        for agent in ("pipeline_analysis", "strategy_generator", "unknown", None, ""):
            self.assertFalse(agent_holds_build_window(agent))


class TestIfFreeSyncVariant(unittest.TestCase):
    """
    #152 item A: the non-blocking variant used by bookkeeping writers of
    dev_container_state that own no build window (see this module's docstring,
    "Bookkeeping writers"). Never waits, never raises a lock timeout, and yields
    whether the lock was actually taken.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def test_yields_true_and_releases_when_free(self):
        with dev_container_build_lock_if_free_sync("proj", 7, facade=self.facade) as acquired:
            self.assertTrue(acquired)
            lock = self.facade.get_resource_lock("proj", RESOURCE_NAME)
            self.assertIsNotNone(lock)
            self.assertLess(lock.locked_by_issue, 0)

        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_yields_false_without_waiting_when_busy(self):
        """The whole point: a startup reconciliation must not block behind a
        build (or behind the dead previous process's still-TTL'd lock)."""
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)  # never released

        start = time.monotonic()
        with dev_container_build_lock_if_free_sync("proj", 7, facade=self.facade) as acquired:
            self.assertFalse(acquired)
        elapsed = time.monotonic() - start

        self.assertLess(elapsed, 1.0)
        # The other holder's lock is untouched -- nothing stolen, nothing released.
        self.assertEqual(self.facade.get_resource_lock("proj", RESOURCE_NAME).locked_by_issue, 1)

    def test_does_not_raise_a_lock_timeout_when_busy(self):
        """Contention is the expected outcome here, not a failure. Raising
        DevContainerBuildLockTimeoutError would misreport it to every consumer
        of services/resource_lock_errors.is_lock_timeout_error()."""
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)
        try:
            with dev_container_build_lock_if_free_sync("proj", facade=self.facade):
                pass
        except DevContainerBuildLockTimeoutError:  # pragma: no cover
            self.fail("non-blocking variant must not raise a lock timeout")

    def test_releases_on_exception_inside_the_body(self):
        with self.assertRaises(ValueError):
            with dev_container_build_lock_if_free_sync("proj", facade=self.facade) as acquired:
                self.assertTrue(acquired)
                raise ValueError("boom")

        self.assertIsNone(self.facade.get_resource_lock("proj", RESOURCE_NAME))

    def test_a_held_if_free_lock_blocks_the_blocking_variant(self):
        """It really is the same resource: a bookkeeping write in flight
        serializes a build that starts at the same moment, and vice versa."""
        with dev_container_build_lock_if_free_sync("proj", facade=self.facade) as acquired:
            self.assertTrue(acquired)
            with self.assertRaises(DevContainerBuildLockTimeoutError):
                with dev_container_build_lock_sync(
                    "proj", facade=self.facade, timeout_seconds=0.1, poll_interval_seconds=0.02
                ):
                    pass  # pragma: no cover


@pytest.mark.asyncio
class TestIfFreeAsyncVariant:
    """Async counterpart of TestIfFreeSyncVariant."""

    def setup_method(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def teardown_method(self):
        shutil.rmtree(self.test_dir)

    async def test_yields_true_and_releases_when_free(self):
        async with dev_container_build_lock_if_free_async("proj", 7, facade=self.facade) as acquired:
            assert acquired is True
            assert self.facade.get_resource_lock("proj", RESOURCE_NAME) is not None

        assert self.facade.get_resource_lock("proj", RESOURCE_NAME) is None

    async def test_yields_false_without_waiting_when_busy(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)

        start = time.monotonic()
        async with dev_container_build_lock_if_free_async("proj", 7, facade=self.facade) as acquired:
            assert acquired is False
        assert time.monotonic() - start < 1.0
        assert self.facade.get_resource_lock("proj", RESOURCE_NAME).locked_by_issue == 1

    async def test_does_not_raise_a_lock_timeout_when_busy(self):
        self.facade.acquire_resource("proj", RESOURCE_NAME, 1)
        async with dev_container_build_lock_if_free_async("proj", facade=self.facade) as acquired:
            assert acquired is False

    async def test_releases_on_exception_inside_the_body(self):
        with pytest.raises(ValueError):
            async with dev_container_build_lock_if_free_async("proj", facade=self.facade):
                raise ValueError("boom")

        assert self.facade.get_resource_lock("proj", RESOURCE_NAME) is None

    async def test_acquire_runs_off_the_event_loop_thread(self):
        """Same rule as the polling variant: no synchronous lock I/O on the
        event-loop thread (see project_checkout_lock.py's module docstring)."""
        loop_thread = threading.get_ident()
        seen = []
        real_acquire = self.facade.acquire_resource

        def recording_acquire(project, resource_name, holder_id):
            seen.append(threading.get_ident())
            return real_acquire(project, resource_name, holder_id)

        with patch.object(self.facade, "acquire_resource", side_effect=recording_acquire):
            async with dev_container_build_lock_if_free_async("proj", facade=self.facade) as acquired:
                assert acquired is True

        assert seen and all(t != loop_thread for t in seen)


class TestAcquireFailureClassification(unittest.TestCase):
    """
    #152 review: the non-blocking variant's callers were told every False meant
    "a build owns this project's state and is about to write a fresher status".
    PipelineLockManager.try_acquire_lock() also returns False fail-closed on
    unknown/degraded lock state and for a lock retained after a failed run -- in
    none of which is anybody holding a build window, so a caller that skips its
    write on one of those drops it for good while the log narrates a holder that
    does not exist.
    """

    def test_a_live_holder_is_contention(self):
        self.assertTrue(acquire_failure_is_contention("locked_by_issue_42"))

    def test_a_retained_lock_is_not_contention(self):
        """`locked_by_issue_N_failed` is a durable marker left for deliberate
        human recovery -- its 'holder' is a run that already ended."""
        self.assertFalse(acquire_failure_is_contention("locked_by_issue_42_failed"))

    def test_fail_closed_reasons_are_not_contention(self):
        for reason in (
            "lock_state_unknown_failing_closed",
            "lock_acquire_serialization_timeout",
            "lock_acquire_serialization_unavailable",
        ):
            with self.subTest(reason=reason):
                self.assertFalse(acquire_failure_is_contention(reason))

    def test_an_unrecognised_reason_is_not_assumed_to_be_a_holder(self):
        self.assertFalse(acquire_failure_is_contention("something_new"))
        self.assertFalse(acquire_failure_is_contention(None))

    def test_the_classified_reasons_really_are_what_the_lock_manager_returns(self):
        """Guard against the classifier drifting from its source: every string
        it special-cases must still be spelled that way in try_acquire_lock()."""
        import inspect
        from services.pipeline_lock_manager import PipelineLockManager as _PLM

        source = inspect.getsource(_PLM.try_acquire_lock) + inspect.getsource(
            _PLM._try_acquire_lock_yaml_unguarded
        )
        for reason in (
            "lock_state_unknown_failing_closed",
            "lock_acquire_serialization_timeout",
            "lock_acquire_serialization_unavailable",
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, source)


class TestDegradedSkipsAreLoggedAsErrors(unittest.TestCase):
    """A skipped bookkeeping write is a WARNING when a build really is running,
    and an ERROR when nothing is -- the log line is the only signal a dropped
    write has."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp()
        self.facade = _make_facade(self.test_dir)

    def tearDown(self):
        shutil.rmtree(self.test_dir)

    def _skip_with_reason(self, reason):
        with patch.object(self.facade, "acquire_resource", return_value=(False, reason)):
            with self.assertLogs("services.dev_container_build_lock") as captured:
                with dev_container_build_lock_if_free_sync("proj", 7, facade=self.facade) as acquired:
                    self.assertFalse(acquired)
        return captured

    def test_contention_stays_a_warning(self):
        captured = self._skip_with_reason("locked_by_issue_42")
        self.assertEqual(captured.records[0].levelname, "WARNING")

    def test_a_degraded_acquire_is_an_error_naming_the_real_reason(self):
        captured = self._skip_with_reason("lock_state_unknown_failing_closed")
        self.assertEqual(captured.records[0].levelname, "ERROR")
        self.assertIn("lock_state_unknown_failing_closed", captured.output[0])
        # And does NOT claim a holder exists.
        self.assertIn("NOT contention", captured.output[0])

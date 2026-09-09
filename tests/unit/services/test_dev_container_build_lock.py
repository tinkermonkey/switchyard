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
import tempfile
import shutil

import pytest

from services.pipeline_lock_manager import PipelineLockManager
from services.project_resource_lock_manager import ProjectResourceLockManager
from services import project_checkout_lock
from services.dev_container_build_lock import (
    dev_container_build_lock_async,
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


if __name__ == '__main__':
    unittest.main()

"""
Tests for services/docker_socket_access_gate.py (switchyard #51).

Covers:
- acquire()/release() succeed for the common single-holder case (capacity=1,
  nothing else contending) with no blocking -- a no-op in practice.
- release() frees the slot for a subsequent acquirer against the same
  project.
- A second concurrent acquire() attempt for the SAME project blocks until
  the first holder releases, then proceeds.
- The gate is scoped per-project: a concurrent acquire for a DIFFERENT
  project is unaffected by another project's held slot.
- _holder_id() is deterministic for a given task_id (acquire/release must
  agree on the same holder key).
"""

import asyncio

import pytest

from services.docker_socket_access_gate import DockerSocketAccessGate, _holder_id
from services.pipeline_semaphore_manager import PipelineSemaphoreManager


@pytest.fixture
def semaphore_manager(tmp_path):
    """A real PipelineSemaphoreManager backed only by YAML (redis_client=False
    is falsy, so every code path takes the YAML-only branch) writing into an
    isolated tmp_path -- exercises the real capacity/staleness logic, not a
    mocked-out no-op, without touching a real Redis or the repo's state dir."""
    return PipelineSemaphoreManager(state_dir=tmp_path, redis_client=False)


@pytest.fixture
def gate(semaphore_manager):
    """Fast poll interval so blocking tests run quickly."""
    return DockerSocketAccessGate(
        semaphore_manager=semaphore_manager,
        max_concurrent=1,
        poll_interval=0.02,
        max_wait_seconds=5.0,
    )


class TestHolderId:
    def test_deterministic_for_same_task_id(self):
        assert _holder_id("task-abc") == _holder_id("task-abc")

    def test_differs_for_different_task_ids(self):
        assert _holder_id("task-abc") != _holder_id("task-xyz")

    def test_returns_int(self):
        assert isinstance(_holder_id("task-abc"), int)


class TestAcquireReleaseNoContention:
    """At the default capacity=1 with nothing else contending, acquire must
    succeed immediately and release must free the slot -- the no-op-in-
    practice behavior the issue's acceptance criteria require."""

    @pytest.mark.asyncio
    async def test_acquire_succeeds_immediately(self, gate, semaphore_manager):
        holder_id = await gate.acquire("phone-home", "task-1")

        assert isinstance(holder_id, int)
        assert semaphore_manager.is_held_by("phone-home", "_docker_socket_access", holder_id)

    @pytest.mark.asyncio
    async def test_release_frees_the_slot(self, gate, semaphore_manager):
        holder_id = await gate.acquire("phone-home", "task-1")
        released = await gate.release("phone-home", holder_id)

        assert released is True
        assert semaphore_manager.get_holders("phone-home", "_docker_socket_access") == []

    @pytest.mark.asyncio
    async def test_release_then_reacquire_by_a_different_task(self, gate):
        holder_id_1 = await gate.acquire("phone-home", "task-1")
        await gate.release("phone-home", holder_id_1)

        # A second, unrelated launch must be able to acquire the now-free slot
        # without blocking.
        holder_id_2 = await asyncio.wait_for(gate.acquire("phone-home", "task-2"), timeout=1.0)
        assert holder_id_2 != holder_id_1

    @pytest.mark.asyncio
    async def test_release_is_safe_when_not_held(self, gate):
        """release() on a slot that was never acquired (or already released)
        must not raise -- mirrors PipelineSemaphoreManager.release()'s own
        idempotence, matters for the docker_runner finally-block wiring
        where release always runs regardless of whether acquire actually
        got called on some code path."""
        assert await gate.release("phone-home", 999999) is True


class TestConcurrentAcquireBlocks:
    @pytest.mark.asyncio
    async def test_second_acquire_for_same_project_blocks_until_release(self, gate):
        holder_id_1 = await gate.acquire("phone-home", "task-1")

        second_acquire_task = asyncio.create_task(gate.acquire("phone-home", "task-2"))

        # Give the second acquire several poll cycles to (incorrectly) succeed
        # if the gate weren't actually enforcing capacity.
        await asyncio.sleep(0.15)
        assert not second_acquire_task.done(), (
            "a second acquire for the same project must block while the first "
            "holder still holds the slot"
        )

        await gate.release("phone-home", holder_id_1)

        holder_id_2 = await asyncio.wait_for(second_acquire_task, timeout=1.0)
        assert holder_id_2 != holder_id_1

    @pytest.mark.asyncio
    async def test_acquire_for_a_different_project_is_not_blocked(self, gate):
        """The gate is scoped per-project: holding the slot for one project
        must not block a concurrent acquire for a different project."""
        await gate.acquire("phone-home", "task-1")

        other_project_holder_id = await asyncio.wait_for(
            gate.acquire("context-studio", "task-2"), timeout=1.0
        )
        assert isinstance(other_project_holder_id, int)

    @pytest.mark.asyncio
    async def test_times_out_if_never_released(self, semaphore_manager):
        """A pathologically short max_wait_seconds must surface as a
        TimeoutError rather than hanging forever, so callers can treat it as
        an ordinary launch failure."""
        gate = DockerSocketAccessGate(
            semaphore_manager=semaphore_manager,
            max_concurrent=1,
            poll_interval=0.02,
            max_wait_seconds=0.1,
        )
        await gate.acquire("phone-home", "task-1")

        with pytest.raises(TimeoutError):
            await gate.acquire("phone-home", "task-2")


class TestCancellationDuringAcquireCleansUpOrphanedHolder:
    """(#51 review, pass 2) loop.run_in_executor()'s underlying thread cannot
    actually be interrupted once it has started running -- cancelling the
    awaiting task only stops the caller from observing the result, the
    background try_acquire() call keeps going regardless. If it goes on to
    succeed after cancellation, nothing else would ever release that slot
    without the done-callback fix in acquire()'s except-CancelledError branch."""

    @pytest.mark.asyncio
    async def test_cancelled_acquire_releases_orphaned_slot_once_background_call_completes(
        self, gate, semaphore_manager
    ):
        import threading
        import time

        real_try_acquire = semaphore_manager.try_acquire
        started = threading.Event()

        def slow_try_acquire(*args, **kwargs):
            started.set()
            time.sleep(0.1)  # simulate a slow Redis/YAML round trip
            return real_try_acquire(*args, **kwargs)

        semaphore_manager.try_acquire = slow_try_acquire

        task = asyncio.create_task(gate.acquire("phone-home", "task-1"))
        # Wait until the background thread has actually started (so cancellation
        # below lands mid-flight, not before the thread call even begins).
        await asyncio.get_event_loop().run_in_executor(None, started.wait, 1.0)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        # The background try_acquire() call is still finishing despite the
        # cancellation -- give its done-callback a moment to run.
        await asyncio.sleep(0.3)

        # The orphaned holder must have been auto-released -- a fresh acquire for
        # the same project must succeed immediately (capacity=1), not block.
        holder_id = await asyncio.wait_for(gate.acquire("phone-home", "task-2"), timeout=1.0)
        assert isinstance(holder_id, int)

"""
Docker Socket Access Gate

A concurrency gate scoped specifically to agents that mount
/var/run/docker.sock into their container -- any agent with
docker_socket_access: true in project config, plus dev_environment_setup,
which always mounts it (see claude/docker_runner.py's docker_socket_access
check in run_agent_in_container / _build_docker_command).

This is DISTINCT from, and independent of, the general per-(project, board)
pipeline dispatch concurrency in PipelineSemaphoreManager
(services/pipeline_semaphore_manager.py) -- that gate limits how many
epics may be in-dispatch on a given board at once; this one limits how many
docker-socket-bearing agent CONTAINERS may be running against the same
PROJECT at once, regardless of which board or pipeline dispatched them.

Rationale (switchyard #51): docker-socket-bearing agents can launch their
own test-infrastructure containers (e.g. testcontainers-managed Postgres).
If a project's own test harness allocates fixed host ports rather than
ephemeral ones, two such agents running concurrently against the same
project can collide on the same port. Serializing docker-socket-bearing
agent launches per-project (capacity 1 by default) removes that risk at the
orchestrator level without switchyard needing to know or verify anything
about any individual project's own test-harness behavior (out of scope --
see docker_socket_access.md's port-allocation guidance for what projects
themselves should do).

Deliberately reuses PipelineSemaphoreManager itself -- the same Redis+YAML
durable counted-holder primitive, same staleness pruning, same fail-closed-
on-both-stores-down semantics -- rather than inventing new locking
infrastructure. It is scoped under a reserved synthetic "board" name
(DOCKER_SOCKET_ACCESS_BOARD) per project, so its holders live in a
completely separate (project, board) keyspace from any real pipeline board
and can never contend with, or be confused for, ordinary pipeline-dispatch
capacity.
"""

import asyncio
import hashlib
import logging
import os
from typing import Optional

from services.pipeline_semaphore_manager import PipelineSemaphoreManager, get_pipeline_semaphore_manager

logger = logging.getLogger(__name__)

# Reserved synthetic "board" name scoping this gate's holders within
# PipelineSemaphoreManager's (project, board) keyspace. Never a real board
# name (real boards come from config/foundations/workflows.yaml), so this
# gate's Redis key / YAML state file can never collide with an actual
# pipeline board's.
DOCKER_SOCKET_ACCESS_BOARD = "_docker_socket_access"

# How many docker-socket-bearing agent containers may run against the SAME
# project at once. 1 is the correct default per switchyard #51: two epics
# must not simultaneously run docker-socket-bearing agents against the same
# project until that project's own test-harness port allocation is
# confirmed collision-safe. Configurable (deployment-wide, not per-project)
# only because the issue asked for that if trivial -- 1 remains the
# intended default in every normal deployment.
DEFAULT_MAX_CONCURRENT = int(os.environ.get("DOCKER_SOCKET_ACCESS_MAX_CONCURRENT", "1"))

# Interval between poll attempts while blocked waiting for a slot.
_POLL_INTERVAL_SECONDS = 5.0

# Hard ceiling on total wait time. PipelineSemaphoreManager's own staleness
# pruning (DEFAULT_STALENESS_SECONDS, 4h) normally reclaims a dead holder's
# slot long before this fires, so actually hitting this ceiling means real,
# sustained contention (or a genuinely wedged holder outliving even
# staleness pruning), not a transient wait.
_MAX_WAIT_SECONDS = float(os.environ.get("DOCKER_SOCKET_ACCESS_MAX_WAIT_SECONDS", str(6 * 3600)))


def _holder_id(task_id: str) -> int:
    """Derive an integer holder id from a task_id.

    PipelineSemaphoreManager's holder key is typed/named as an issue number,
    but its Redis hash field and YAML dict key only ever need to be a stable
    hashable identifier for "who holds this slot" -- nothing about its
    storage requires it to be a real GitHub issue number (it's used as a
    string dict/hash-field key throughout PipelineSemaphoreManager, str()'d
    on write and int()'d back on read -- Python ints are arbitrary-precision,
    so nothing constrains this to fit in 32 bits). A per-launch task_id is
    what run_agent_in_container actually has in hand, so this derives a
    deterministic (not Python's randomized hash()) id from it.

    Uses a full SHA-256 digest, not a 32-bit checksum (e.g. zlib.crc32): a
    32-bit space makes an accidental collision between two genuinely
    concurrent, unrelated task_ids for the same project a real (if
    individually unlikely) risk over the orchestrator's operating lifetime --
    and per PipelineSemaphoreManager's idempotent-holder semantics, a
    colliding second task_id would be silently admitted as if it were the
    first task_id refreshing its own slot, defeating the exact mutual-
    exclusion guarantee this gate exists to provide (found in #51 review).
    SHA-256's 256-bit space makes that practically impossible.
    """
    return int(hashlib.sha256(str(task_id).encode("utf-8")).hexdigest(), 16)


class DockerSocketAccessGate:
    """Async acquire/release wrapper around PipelineSemaphoreManager, scoped
    to docker-socket-bearing agent launches. See module docstring."""

    def __init__(
        self,
        semaphore_manager: Optional[PipelineSemaphoreManager] = None,
        max_concurrent: Optional[int] = None,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        max_wait_seconds: float = _MAX_WAIT_SECONDS,
    ):
        self._semaphore = semaphore_manager or get_pipeline_semaphore_manager()
        self._max_concurrent = max_concurrent if max_concurrent is not None else DEFAULT_MAX_CONCURRENT
        self._poll_interval = poll_interval
        self._max_wait_seconds = max_wait_seconds

    async def acquire(self, project: str, task_id: str) -> int:
        """Block (async, non-busy -- sleeps between polls) until a
        docker-socket-access slot is free for `project`, then claim it.

        Returns the holder id to pass to release(). At the default
        max_concurrent=1 with nothing else contending, this acquires on the
        first attempt and returns immediately -- a no-op in practice.

        Raises TimeoutError if no slot could be claimed within
        max_wait_seconds; callers should treat that the same as any other
        launch failure (the task queue / pipeline retry path picks it up
        again later).
        """
        holder_id = _holder_id(task_id)
        waited = 0.0
        while True:
            # try_acquire() does synchronous I/O (a Redis round trip, or a YAML
            # file-lock read/write on fallback) -- offload each poll tick to a
            # thread so it can't stall the shared event loop for its duration
            # (found in #51 review: this poll can run for up to max_wait_seconds,
            # unlike a typical fail-fast single-attempt gate elsewhere in this
            # codebase).
            acquired, reason = await asyncio.to_thread(
                self._semaphore.try_acquire,
                project=project,
                board=DOCKER_SOCKET_ACCESS_BOARD,
                issue_number=holder_id,
                max_concurrent=self._max_concurrent,
            )
            if acquired:
                if waited:
                    logger.info(
                        f"docker-socket-access gate acquired for project={project} "
                        f"task_id={task_id} after waiting {waited:.0f}s"
                    )
                return holder_id

            if waited == 0:
                logger.info(
                    f"docker-socket-access gate at capacity for project={project} "
                    f"(max_concurrent={self._max_concurrent}, {reason}); "
                    f"task_id={task_id} waiting for a free slot"
                )
            if waited >= self._max_wait_seconds:
                raise TimeoutError(
                    f"Timed out after {waited:.0f}s waiting for a docker-socket-access "
                    f"gate slot for project={project} task_id={task_id} ({reason})"
                )
            await asyncio.sleep(self._poll_interval)
            waited += self._poll_interval

    def release(self, project: str, holder_id: int) -> bool:
        """Release a slot previously claimed by acquire() for `project`.

        Safe to call even if the slot isn't currently held (no-op) -- mirrors
        PipelineSemaphoreManager.release()'s own idempotence.
        """
        return self._semaphore.release(project, DOCKER_SOCKET_ACCESS_BOARD, holder_id)


# Singleton instance, mirroring get_pipeline_semaphore_manager()'s pattern.
_docker_socket_access_gate: Optional[DockerSocketAccessGate] = None


def get_docker_socket_access_gate() -> DockerSocketAccessGate:
    """Get singleton instance of DockerSocketAccessGate."""
    global _docker_socket_access_gate
    if _docker_socket_access_gate is None:
        _docker_socket_access_gate = DockerSocketAccessGate()
    return _docker_socket_access_gate

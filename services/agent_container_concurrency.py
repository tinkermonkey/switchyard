"""
Agent Container Concurrency Limiter

Provides a tier-0 global cap on how many agent Docker containers may be
launching/executing at once, across the whole orchestrator process (and,
because it is Redis-backed, across multiple orchestrator processes too).

Why this exists: uncoordinated parallel container launches (e.g. the raw
threading.Thread fan-out in services/project_monitor.py for review-cycle
resume, PR review, and conversational-loop resume) can pile up enough
concurrent Claude Code sessions to trip the account-global Claude rate-limit
circuit breaker on a single burst. This module is a hard safety-net ceiling,
independent of and in addition to that breaker.

Mechanism: a Redis sorted set (ZSET) "token bucket". Each holder registers a
unique token with score = acquisition unix timestamp via ZADD. Before every
capacity check, entries older than a max-age TTL are pruned via
ZREMRANGEBYSCORE — this makes a crashed/killed agent process (which never
gets to release its slot) self-heal instead of permanently consuming
capacity, with no separate cron/sweep process required.

Fail-open: if Redis is unavailable, this degrades to "no cap enforced" (logs
a warning) rather than blocking all agent execution — matching this
codebase's existing convention for Redis-backed coordination primitives
(CircuitBreaker, the in-memory task queue fallback).
"""

import asyncio
import logging
import os
import time
import uuid
from typing import Optional

import redis

logger = logging.getLogger(__name__)

# Redis key for the ZSET token bucket. Colon-separated namespacing, matching
# this codebase's convention (see circuit_breaker:{name}:state,
# pipeline_lock:{project}:{board}).
REDIS_KEY = "agent_container_slots:tokens"

# Longest configured agent timeout today is 10800s (3 hours; see
# config/foundations/agents.yaml). Default TTL is sized comfortably above
# that so a legitimately long-running agent's slot is never pruned out from
# under it while still genuinely running, while a crashed process's stale
# slot still eventually self-heals.
DEFAULT_TTL_SECONDS = 14400  # 4 hours

# How often to poll for a free slot while blocked at capacity.
DEFAULT_POLL_INTERVAL_SECONDS = 2

# Atomically prune stale entries, check capacity, and add the new token — all
# in one server-side operation, so there is no gap between checking capacity
# and claiming a slot for a concurrent caller to race into (see acquire()).
# KEYS[1] = REDIS_KEY, ARGV = [cutoff, max_concurrent, token, now]
_ACQUIRE_SCRIPT = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
local current = redis.call('ZCARD', KEYS[1])
if current < tonumber(ARGV[2]) then
    redis.call('ZADD', KEYS[1], ARGV[4], ARGV[3])
    return 1
else
    return 0
end
"""

# Sentinel distinguishing "no redis_client argument passed, go discover one
# via _get_redis_client()" from "redis_client=None passed explicitly, meaning
# Redis is known to be unavailable" (used by tests to simulate an outage
# without needing a real unreachable host).
_UNSET = object()


def _get_redis_client() -> Optional["redis.Redis"]:
    """
    Create a Redis client using this codebase's standard connection pattern
    (see services/circuit_breaker.py): REDIS_HOST/REDIS_PORT env vars with a
    host fallback list, decode_responses=True, and a ping() check. Returns
    None if no candidate host is reachable.
    """
    redis_host = os.environ.get('REDIS_HOST')
    redis_port = int(os.environ.get('REDIS_PORT', 6379))

    hosts_to_try = [redis_host] if redis_host else ['redis', 'localhost', '127.0.0.1']

    for host in hosts_to_try:
        try:
            client = redis.Redis(
                host=host,
                port=redis_port,
                decode_responses=True,
                socket_connect_timeout=2,
                socket_timeout=2
            )
            client.ping()
            logger.debug(f"agent_container_concurrency: connected to Redis at {host}:{redis_port}")
            return client
        except Exception as e:
            logger.debug(f"agent_container_concurrency: could not connect to Redis at {host}:{redis_port}: {e}")
            continue

    return None


class AgentContainerConcurrencyLimiter:
    """
    Redis-backed counting semaphore gating how many agent containers may run
    concurrently, implemented as a self-healing ZSET token bucket.

    Fails open (never blocks) if Redis is unreachable.
    """

    def __init__(
        self,
        max_concurrent: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        redis_client=_UNSET,
    ):
        if max_concurrent < 1:
            # ZCARD is never negative, so a limit <= 0 (e.g. a misconfigured
            # "0 means unlimited") makes `current < max_concurrent` always
            # false — every acquire() call would poll forever with no error,
            # wedging every future agent launch. 0/negative isn't a
            # meaningful "unlimited" here; clamp to the safe default instead
            # of hanging the whole orchestrator on a config typo.
            logger.error(
                f"agent_container_concurrency: max_concurrent={max_concurrent} is "
                f"invalid (must be >= 1) — clamping to 1 instead of wedging every "
                f"agent launch. Check MAX_CONCURRENT_AGENT_CONTAINERS."
            )
            max_concurrent = 1

        self.max_concurrent = max_concurrent
        self.ttl_seconds = ttl_seconds
        self.poll_interval_seconds = poll_interval_seconds

        # ORCHESTRATOR_WORKERS and MAX_CONCURRENT_AGENT_CONTAINERS are
        # independent settings that can silently contradict each other: a
        # deployment configured for N-way worker parallelism would have every
        # container launch serialize down to this cap regardless. Neither
        # setting is authoritative over the other, so just surface the
        # mismatch rather than silently throttling with no explanation.
        try:
            orchestrator_workers = int(os.environ.get('ORCHESTRATOR_WORKERS', 1))
            if orchestrator_workers > self.max_concurrent:
                logger.warning(
                    f"agent_container_concurrency: ORCHESTRATOR_WORKERS="
                    f"{orchestrator_workers} exceeds max_concurrent_agent_containers="
                    f"{self.max_concurrent} — container launches will serialize "
                    f"below the configured worker parallelism. Raise "
                    f"MAX_CONCURRENT_AGENT_CONTAINERS if that's not intended."
                )
        except (TypeError, ValueError):
            pass  # malformed env var — not this module's job to validate it

        if redis_client is _UNSET:
            self.redis_client = _get_redis_client()
        else:
            # Explicit value (including None, meaning "Redis is known to be
            # unavailable") — used directly, no discovery attempted.
            self.redis_client = redis_client

        if self.redis_client is None:
            logger.warning(
                "agent_container_concurrency: Redis unavailable — concurrency "
                "cap is fail-open (disabled) until Redis is reachable again"
            )

    async def acquire(self) -> str:
        """
        Block/poll until a slot is available, then claim it and return the
        holder token (needed for release()). If Redis is unavailable, returns
        immediately without enforcing any cap (fail-open) — the returned
        token is still valid to pass to release(), which is a no-op in that
        case too.
        """
        token = str(uuid.uuid4())

        if self.redis_client is None:
            return token

        while True:
            try:
                # Prune + capacity-check + add must happen as one atomic
                # server-side operation: a separate ZCARD-then-ZADD is a
                # classic TOCTOU race — two concurrent callers can both
                # observe capacity free and both add a token, silently
                # exceeding max_concurrent (the whole point of this
                # primitive). EVAL runs the script atomically on the Redis
                # server (single-threaded execution model), so there is no
                # window between the check and the add.
                cutoff = time.time() - self.ttl_seconds
                now = time.time()
                acquired = self.redis_client.eval(
                    _ACQUIRE_SCRIPT,
                    1,
                    REDIS_KEY,
                    cutoff,
                    self.max_concurrent,
                    token,
                    now,
                )
                if acquired:
                    return token
            except Exception as e:
                # Redis became unreachable mid-operation (or some other
                # transient error) — fail open rather than wedge agent
                # execution on a coordination primitive.
                logger.warning(
                    f"agent_container_concurrency: Redis error during acquire, "
                    f"failing open: {e}"
                )
                return token

            await asyncio.sleep(self.poll_interval_seconds)

    def release(self, token: str) -> None:
        """Release a previously acquired slot. Safe to call even if Redis is
        unavailable or the token was never actually registered."""
        if self.redis_client is None:
            return
        try:
            self.redis_client.zrem(REDIS_KEY, token)
        except Exception as e:
            logger.warning(f"agent_container_concurrency: Redis error during release: {e}")


class acquire_agent_container_slot:
    """
    Async context manager wrapping AgentContainerConcurrencyLimiter's
    acquire/release for use in a try/finally-shaped call site, e.g.:

        async with acquire_agent_container_slot(max_concurrent=cfg.max_concurrent_agent_containers):
            result = await self._execute_in_container(...)

    A fresh limiter (and Redis connection) is created per call by default —
    fine for occasional/low-frequency use, but callers on a hot path (like
    claude/docker_runner.py's run_agent_in_container(), invoked on every
    agent launch) should pass an existing limiter via `limiter=` instead, so
    a Redis connection is established once and reused rather than
    reconnecting (and, during an outage, re-paying the connect-timeout cost)
    on every single call.
    """

    def __init__(
        self,
        max_concurrent: int,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
        poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
        limiter: Optional[AgentContainerConcurrencyLimiter] = None,
    ):
        self._limiter = limiter or AgentContainerConcurrencyLimiter(
            max_concurrent=max_concurrent,
            ttl_seconds=ttl_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        self._token: Optional[str] = None

    async def __aenter__(self) -> "acquire_agent_container_slot":
        self._token = await self._limiter.acquire()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb) -> None:
        if self._token is not None:
            self._limiter.release(self._token)
        return None

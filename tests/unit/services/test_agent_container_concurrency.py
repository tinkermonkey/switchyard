"""
Unit tests for services/agent_container_concurrency.py's Redis-backed
tier-0 concurrency cap on agent container launches.

Covers:
(a) capacity=1 blocks a second concurrent acquire until the first releases
(b) a stale/expired token is pruned (TTL) and its slot reclaimed
(c) Redis-unavailable fails open (never blocks)
"""

import asyncio
import time

import pytest

from services.agent_container_concurrency import (
    AgentContainerConcurrencyLimiter,
    acquire_agent_container_slot,
)


class FakeRedisZSet:
    """Minimal in-memory stand-in for the subset of the Redis API this
    module uses (zadd, zcard, zrem, zremrangebyscore), so tests exercise the
    real capacity/pruning logic instead of a mocked-out no-op."""

    def __init__(self):
        self._scores = {}  # member -> score

    def zadd(self, key, mapping):
        self._scores.update(mapping)

    def zcard(self, key):
        return len(self._scores)

    def zrem(self, key, member):
        self._scores.pop(member, None)

    def zremrangebyscore(self, key, min_score, max_score):
        if min_score == "-inf":
            min_score = float("-inf")
        if max_score == "+inf":
            max_score = float("inf")
        stale = [m for m, s in self._scores.items() if min_score <= s <= max_score]
        for m in stale:
            del self._scores[m]

    def eval(self, script, numkeys, key, cutoff, max_concurrent, token, now):
        """Emulate _ACQUIRE_SCRIPT's atomic prune+check+add against this
        in-memory store (a real Redis server runs the Lua script itself;
        this fake has no Lua interpreter, so it replicates the same three
        steps directly — atomicity here comes from the test being
        single-threaded per await point, matching what the real EVAL
        guarantees on the server)."""
        self.zremrangebyscore(key, "-inf", cutoff)
        if self.zcard(key) < max_concurrent:
            self.zadd(key, {token: now})
            return 1
        return 0


class RaisingRedis:
    """Stand-in for a Redis client whose every call raises, simulating a
    connection that dies mid-operation."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise ConnectionError("simulated redis outage")
        return _raise


@pytest.mark.asyncio
class TestCapacityBlocking:
    async def test_second_acquire_blocks_until_first_releases_capacity_one(self):
        fake_redis = FakeRedisZSet()
        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=1,
            poll_interval_seconds=0.01,
            redis_client=fake_redis,
        )

        token1 = await limiter.acquire()
        assert fake_redis.zcard(None) == 1

        events = []

        async def second_acquirer():
            events.append("acquire_start")
            token2 = await limiter.acquire()
            events.append("acquire_done")
            return token2

        task = asyncio.create_task(second_acquirer())

        # Give the second acquirer a few poll cycles to prove it is blocked
        # while the first holder still has the slot.
        await asyncio.sleep(0.05)
        assert "acquire_done" not in events
        assert not task.done()

        # Release the first holder's slot -> second acquirer should proceed.
        limiter.release(token1)

        token2 = await asyncio.wait_for(task, timeout=2)
        assert "acquire_done" in events
        assert token2 != token1
        assert fake_redis.zcard(None) == 1  # only the second holder now

        limiter.release(token2)
        assert fake_redis.zcard(None) == 0

    async def test_many_concurrent_acquirers_never_exceed_max_concurrent(self):
        """Regression test for the ZCARD-then-ZADD TOCTOU race: acquire() must
        use one atomic operation (see _ACQUIRE_SCRIPT), not a separate
        check-then-act, so N concurrent callers racing for M < N slots never
        let more than M through at once."""
        fake_redis = FakeRedisZSet()
        max_concurrent = 3
        num_acquirers = 10
        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=max_concurrent,
            poll_interval_seconds=0.01,
            redis_client=fake_redis,
        )

        held_count = 0
        max_observed = 0

        async def holder():
            nonlocal held_count, max_observed
            token = await limiter.acquire()
            held_count += 1
            max_observed = max(max_observed, held_count)
            await asyncio.sleep(0.03)  # simulate doing work while holding the slot
            held_count -= 1
            limiter.release(token)

        await asyncio.gather(*(holder() for _ in range(num_acquirers)))

        assert max_observed == max_concurrent
        assert fake_redis.zcard(None) == 0  # everyone released cleanly

    async def test_context_manager_acquires_and_releases(self):
        fake_redis = FakeRedisZSet()

        async with acquire_agent_container_slot(
            max_concurrent=1,
            poll_interval_seconds=0.01,
            limiter=AgentContainerConcurrencyLimiter(
                max_concurrent=1, poll_interval_seconds=0.01, redis_client=fake_redis
            ),
        ):
            assert fake_redis.zcard(None) == 1

        assert fake_redis.zcard(None) == 0

    async def test_context_manager_releases_on_exception(self):
        fake_redis = FakeRedisZSet()
        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=1, poll_interval_seconds=0.01, redis_client=fake_redis
        )

        with pytest.raises(RuntimeError):
            async with acquire_agent_container_slot(max_concurrent=1, limiter=limiter):
                assert fake_redis.zcard(None) == 1
                raise RuntimeError("simulated agent execution failure")

        assert fake_redis.zcard(None) == 0


@pytest.mark.asyncio
class TestStaleTokenPruning:
    async def test_stale_token_is_pruned_and_slot_reclaimed(self):
        fake_redis = FakeRedisZSet()
        ttl_seconds = 100

        # Simulate a crashed holder: registered long enough ago to be past TTL.
        fake_redis.zadd(None, {"crashed-holder-token": time.time() - (ttl_seconds + 10)})
        assert fake_redis.zcard(None) == 1

        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=1,
            ttl_seconds=ttl_seconds,
            poll_interval_seconds=0.01,
            redis_client=fake_redis,
        )

        new_token = await asyncio.wait_for(limiter.acquire(), timeout=2)

        # The stale entry must have been pruned away, and the new holder
        # granted the reclaimed slot.
        assert "crashed-holder-token" not in fake_redis._scores
        assert new_token in fake_redis._scores
        assert fake_redis.zcard(None) == 1

    async def test_fresh_token_is_not_pruned(self):
        fake_redis = FakeRedisZSet()
        ttl_seconds = 100
        fake_redis.zadd(None, {"live-holder-token": time.time()})

        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=1,
            ttl_seconds=ttl_seconds,
            poll_interval_seconds=0.01,
            redis_client=fake_redis,
        )

        events = []

        async def acquirer():
            events.append("start")
            tok = await limiter.acquire()
            events.append("done")
            return tok

        task = asyncio.create_task(acquirer())
        await asyncio.sleep(0.05)

        # The live token is well within TTL, so capacity=1 stays full and the
        # new acquirer must still be blocked.
        assert "done" not in events
        assert "live-holder-token" in fake_redis._scores

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


class TestOrchestratorWorkersMismatchWarning:
    def test_warns_when_orchestrator_workers_exceeds_cap(self, monkeypatch, caplog):
        monkeypatch.setenv("ORCHESTRATOR_WORKERS", "4")
        with caplog.at_level("WARNING"):
            AgentContainerConcurrencyLimiter(max_concurrent=1, redis_client=None)
        assert any("ORCHESTRATOR_WORKERS=4" in r.message for r in caplog.records)

    def test_no_warning_when_cap_covers_orchestrator_workers(self, monkeypatch, caplog):
        monkeypatch.setenv("ORCHESTRATOR_WORKERS", "2")
        with caplog.at_level("WARNING"):
            AgentContainerConcurrencyLimiter(max_concurrent=4, redis_client=None)
        assert not any("ORCHESTRATOR_WORKERS" in r.message for r in caplog.records)


@pytest.mark.asyncio
class TestFailOpen:
    async def test_no_redis_client_fails_open(self):
        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=1,
            redis_client=None,
        )

        # Both acquires should succeed immediately without ever blocking,
        # even though max_concurrent=1, because Redis is unavailable.
        token1 = await asyncio.wait_for(limiter.acquire(), timeout=1)
        token2 = await asyncio.wait_for(limiter.acquire(), timeout=1)

        assert token1 != token2
        # release() must also be a safe no-op with no client configured.
        limiter.release(token1)
        limiter.release(token2)

    async def test_redis_error_mid_operation_fails_open(self):
        limiter = AgentContainerConcurrencyLimiter(
            max_concurrent=1,
            redis_client=RaisingRedis(),
        )

        # acquire() must not raise or block forever when Redis calls raise.
        token = await asyncio.wait_for(limiter.acquire(), timeout=1)
        assert token

        # release() must also swallow the error rather than propagating it.
        limiter.release(token)

    async def test_context_manager_fails_open_when_redis_client_construction_fails(self, monkeypatch):
        from services import agent_container_concurrency

        monkeypatch.setattr(
            agent_container_concurrency, "_get_redis_client", lambda: None
        )

        # Should complete without ever blocking despite max_concurrent=1 and
        # two overlapping holders.
        async with acquire_agent_container_slot(max_concurrent=1):
            async with acquire_agent_container_slot(max_concurrent=1):
                pass

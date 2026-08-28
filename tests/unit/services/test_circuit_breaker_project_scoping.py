"""
Unit tests for CircuitBreaker re-keying (switchyard issue #42).

Before this change, services/circuit_breaker.py persisted state to Redis under
circuit_breaker:{agent_name}:state — no project component. A fresh
CircuitBreaker object is constructed on every agent execution, but any two
executions of the same agent/stage name (e.g. "developer") shared the same
Redis key regardless of which project they belonged to. That meant a failure
in one project's "developer" stage could trip the breaker for every other
unrelated project's concurrent "developer" stage execution (last-writer-wins,
non-atomic save).

The fix re-keys the breaker's name to "{project_name}:{agent_name}" at its
construction sites (pipeline/base.py's PipelineStage fallback and
agents/orchestrator_integration.py's AgentStage custom circuit_breaker_config
path), mirroring PipelineLockManager's existing project:board granularity.

These tests use a fake in-memory Redis backing store (shared across
CircuitBreaker instances within a test) so we exercise real persistence —
not just "no Redis means everything is trivially independent" — and confirm
that project-scoped keys genuinely isolate failure counts between projects
while preserving state sharing *within* the same project/agent pair (which is
the whole point of a circuit breaker surviving across per-execution
instances).
"""

import pytest
from unittest.mock import patch
from services.circuit_breaker import CircuitBreaker, CircuitState


class FakeRedisClient:
    """Minimal fake Redis client backed by a plain dict, shared across
    CircuitBreaker instances within a test to simulate real cross-instance
    persistence via Redis."""

    def __init__(self, store: dict):
        self._store = store

    def ping(self):
        return True

    def setex(self, key, ttl, value):
        self._store[key] = value
        return True

    def get(self, key):
        return self._store.get(key)


def make_breaker(name: str, store: dict, failure_threshold: int = 3):
    with patch("redis.Redis") as mock_redis_cls:
        mock_redis_cls.return_value = FakeRedisClient(store)
        return CircuitBreaker(name=name, failure_threshold=failure_threshold)


async def _trip_to_open(breaker: CircuitBreaker, times: int):
    async def raises(*args, **kwargs):
        raise ValueError("simulated agent failure")

    for _ in range(times):
        with pytest.raises(ValueError):
            await breaker.call(raises)


class TestCircuitBreakerProjectScoping:
    def test_redis_key_includes_project_prefix(self):
        """The persisted Redis key must be namespaced by project, not just
        the bare agent/stage name."""
        store = {}
        breaker = make_breaker("proj_a:developer", store)
        breaker._save_state()

        assert "circuit_breaker:proj_a:developer:state" in store
        assert "circuit_breaker:developer:state" not in store

    @pytest.mark.asyncio
    async def test_two_projects_same_agent_have_independent_failure_counts(self):
        """The core cross-project bleed scenario from issue #42: project A's
        'developer' stage tripping its breaker must not affect project B's
        concurrently-running 'developer' stage, even though both share the
        same underlying Redis instance."""
        store = {}

        # Project A's "developer" stage fails enough times to open its circuit.
        breaker_a = make_breaker("proj_a:developer", store, failure_threshold=3)
        await _trip_to_open(breaker_a, 3)
        assert breaker_a.state == CircuitState.OPEN
        assert breaker_a.failure_count == 3

        # A fresh CircuitBreaker for project B's "developer" stage — as would
        # be constructed on project B's next concurrent agent execution —
        # must start clean, not inherit project A's tripped state.
        breaker_b = make_breaker("proj_b:developer", store, failure_threshold=3)
        assert breaker_b.state == CircuitState.CLOSED
        assert breaker_b.failure_count == 0

        # Project B can independently accumulate its own failures without
        # being affected by project A already being open.
        await _trip_to_open(breaker_b, 2)
        assert breaker_b.state == CircuitState.CLOSED  # threshold not yet reached
        assert breaker_b.failure_count == 2

        # Project A's breaker remains open and unaffected by B's activity.
        assert breaker_a.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_same_project_same_agent_state_persists_across_instances(self):
        """Regression guard: within the *same* project, a freshly constructed
        CircuitBreaker for the same agent must still pick up previously
        persisted state — the whole point of a circuit breaker surviving
        across the per-execution instances the orchestrator constructs."""
        store = {}

        breaker_1 = make_breaker("proj_a:developer", store, failure_threshold=3)
        await _trip_to_open(breaker_1, 3)
        assert breaker_1.state == CircuitState.OPEN

        # A second, independently-constructed instance for the same
        # project:agent key (e.g. the next execution of project A's
        # "developer" stage) must load the tripped state from Redis.
        breaker_2 = make_breaker("proj_a:developer", store, failure_threshold=3)
        assert breaker_2.state == CircuitState.OPEN
        assert breaker_2.failure_count == 3

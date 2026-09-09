"""
Unit tests for services/circuit_breaker.py's exemption of project resource-lock
timeouts from per-stage failure counting (#148, from #140 items 10/13).

A ProjectCheckoutLockTimeoutError / DevContainerBuildLockTimeoutError means the
guarded stage never ran at all: another holder owned the project's base clone
(or its dev-container build slot) for the entire, deliberately generous lock
timeout (~3h / ~1h). Without this exemption, three such waits — every one of
them the lock working exactly as designed — would open that agent+project
circuit and block ALL further dispatch of that agent for that project for
recovery_timeout, on top of the contention that caused it.

Mirrors test_circuit_breaker_claude_exemption.py, which covers the
ClaudeCodeRateLimitError exemption this one is modelled on.
"""

import pytest
from unittest.mock import patch

from services.circuit_breaker import CircuitBreaker, CircuitState
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


def make_breaker(name="test_stage", failure_threshold=3):
    with patch("redis.Redis") as mock_redis_cls:
        mock_redis_cls.return_value.ping.side_effect = Exception("no redis in tests")
        return CircuitBreaker(name=name, failure_threshold=failure_threshold)


class TestLockTimeoutExemption:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_lock_timeout_does_not_increment_failure_count(self, error_cls):
        breaker = make_breaker()

        async def raises_lock_timeout(*args, **kwargs):
            raise error_cls("could not acquire lock within 10900.0s")

        with pytest.raises(error_cls):
            await breaker.call(raises_lock_timeout)

        assert breaker.failure_count == 0
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_repeated_lock_timeouts_never_open_the_circuit(self):
        """Pure contention must never trip this agent+project's breaker — that
        would block every later dispatch of the agent for recovery_timeout."""
        breaker = make_breaker(failure_threshold=3)

        async def raises_lock_timeout(*args, **kwargs):
            raise ProjectCheckoutLockTimeoutError("busy")

        for _ in range(10):
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                await breaker.call(raises_lock_timeout)

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_wrapped_lock_timeout_is_also_exempt(self):
        """An agent that re-wraps the timeout (`raise Exception(...) from exc`)
        must not defeat the exemption — see resource_lock_errors.py."""
        breaker = make_breaker()

        async def raises_wrapped_lock_timeout(*args, **kwargs):
            try:
                raise ProjectCheckoutLockTimeoutError("busy")
            except Exception as exc:
                raise Exception(f"Business Analyst execution failed: {exc}") from exc

        with pytest.raises(Exception):
            await breaker.call(raises_wrapped_lock_timeout)

        assert breaker.failure_count == 0
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_normal_exception_still_counts_as_failure(self):
        """Regression guard: the exemption must be scoped to lock timeouts only —
        an ordinary agent failure must still open the circuit."""
        breaker = make_breaker(failure_threshold=2)

        async def raises_normal(*args, **kwargs):
            raise RuntimeError("agent produced garbage")

        for _ in range(2):
            with pytest.raises(RuntimeError):
                await breaker.call(raises_normal)

        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_lock_timeouts_do_not_push_real_failures_over_the_threshold(self):
        breaker = make_breaker(failure_threshold=3)

        async def raises_lock_timeout(*args, **kwargs):
            raise DevContainerBuildLockTimeoutError("busy")

        async def raises_normal(*args, **kwargs):
            raise RuntimeError("agent produced garbage")

        with pytest.raises(RuntimeError):
            await breaker.call(raises_normal)
        for _ in range(5):
            with pytest.raises(DevContainerBuildLockTimeoutError):
                await breaker.call(raises_lock_timeout)
        with pytest.raises(RuntimeError):
            await breaker.call(raises_normal)

        assert breaker.failure_count == 2
        assert breaker.state == CircuitState.CLOSED

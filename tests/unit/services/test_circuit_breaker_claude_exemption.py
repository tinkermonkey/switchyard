"""
Unit tests for services/circuit_breaker.py's exemption of ClaudeCodeRateLimitError
from per-stage failure counting.

A Claude Code usage-limit rejection is a systemic, account-wide issue that the
global ClaudeCodeBreaker already tracks. Without this exemption, the very first
live-detected rate limit (discovered mid-execute(), before the global breaker
has tripped) would count as one failure against whichever stage's own
CircuitBreaker happened to be running — and repeated incidents could eventually
trip that stage's breaker purely from outage noise, adding an unrelated 600s
lockout on top of the real one.
"""

import pytest
from unittest.mock import patch
from services.circuit_breaker import CircuitBreaker, CircuitState
from monitoring.claude_code_breaker import ClaudeCodeRateLimitError


def make_breaker(name="test_stage", failure_threshold=3):
    with patch("redis.Redis") as mock_redis_cls:
        mock_redis_cls.return_value.ping.side_effect = Exception("no redis in tests")
        return CircuitBreaker(name=name, failure_threshold=failure_threshold)


class TestClaudeCodeRateLimitExemption:
    @pytest.mark.asyncio
    async def test_rate_limit_error_does_not_increment_failure_count(self):
        breaker = make_breaker()

        async def raises_rate_limit(*args, **kwargs):
            raise ClaudeCodeRateLimitError("Claude Code rate limit confirmed")

        with pytest.raises(ClaudeCodeRateLimitError):
            await breaker.call(raises_rate_limit)

        assert breaker.failure_count == 0
        assert breaker.state == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_repeated_rate_limit_errors_never_open_the_circuit(self):
        """Even many rate-limit incidents in a row must not trip this stage's
        own breaker — that would add an unrelated lockout on top of the real,
        global one."""
        breaker = make_breaker(failure_threshold=3)

        async def raises_rate_limit(*args, **kwargs):
            raise ClaudeCodeRateLimitError("Claude Code rate limit confirmed")

        for _ in range(10):
            with pytest.raises(ClaudeCodeRateLimitError):
                await breaker.call(raises_rate_limit)

        assert breaker.state == CircuitState.CLOSED
        assert breaker.failure_count == 0

    @pytest.mark.asyncio
    async def test_normal_exception_still_counts_as_failure(self):
        """Regression guard: the exemption must be scoped to
        ClaudeCodeRateLimitError only — an ordinary agent failure must still
        count and still be able to open the circuit as before."""
        breaker = make_breaker(failure_threshold=3)

        async def raises_normal_error(*args, **kwargs):
            raise ValueError("some unrelated agent failure")

        for _ in range(3):
            with pytest.raises(ValueError):
                await breaker.call(raises_normal_error)

        assert breaker.failure_count == 3
        assert breaker.state == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_rate_limit_error_mixed_with_normal_failures(self):
        """A rate-limit incident interleaved with real failures must not
        contribute to the real-failure count."""
        breaker = make_breaker(failure_threshold=3)

        async def raises_normal_error(*args, **kwargs):
            raise ValueError("real failure")

        async def raises_rate_limit(*args, **kwargs):
            raise ClaudeCodeRateLimitError("rate limited")

        with pytest.raises(ValueError):
            await breaker.call(raises_normal_error)
        assert breaker.failure_count == 1

        # A rate-limit blip in between must not add to the count.
        with pytest.raises(ClaudeCodeRateLimitError):
            await breaker.call(raises_rate_limit)
        assert breaker.failure_count == 1

        with pytest.raises(ValueError):
            await breaker.call(raises_normal_error)
        assert breaker.failure_count == 2
        assert breaker.state == CircuitState.CLOSED

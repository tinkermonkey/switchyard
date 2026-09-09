"""
Unit tests for HumanFeedbackLoopExecutor._update_loop_heartbeat().

Issue #147: the two Redis keys that make up a conversational loop's DURABLE
liveness signal are the heartbeat (orchestrator:feedback_loop:heartbeat:*) and
the distributed loop lock (orchestrator:conversational_loop:*). Both are read
by ProjectMonitor's FAILSAFE and by the stranded-'active' queue sweep, and both
must stay fresh for as long as the loop is actually polling.

The lock was set once in _start_feedback_loop() with ex=1800 and never renewed,
so any conversation waiting more than 30 minutes for a human silently lost it
while the loop was still running.

These tests are skipped outside Docker because importing
HumanFeedbackLoopExecutor transitively imports modules that require the
container environment.
"""
import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import MagicMock, patch

from services.human_feedback_loop import HumanFeedbackLoopExecutor


def _beat(redis_client):
    executor = HumanFeedbackLoopExecutor()
    with patch('redis.Redis', return_value=redis_client):
        executor._update_loop_heartbeat('myproject', 42)
    return executor


class TestUpdateLoopHeartbeat:
    def test_sets_the_heartbeat_key_with_its_five_minute_ttl(self):
        redis_client = MagicMock()

        _beat(redis_client)

        key, ttl, _value = redis_client.setex.call_args[0]
        assert key == 'orchestrator:feedback_loop:heartbeat:myproject:42'
        assert ttl == 300

    def test_renews_the_conversational_loop_lock_ttl(self):
        """REGRESSION (#147): the loop lock's 30-minute TTL was never renewed,
        so on a conversation that waits longer than that for a human reply the
        key lapsed under a live loop. start_feedback_loop() then found neither
        the in-memory entry (cleared on every restart) nor the lock, and would
        start a SECOND loop on the same issue."""
        redis_client = MagicMock()

        _beat(redis_client)

        redis_client.expire.assert_called_once_with(
            'orchestrator:conversational_loop:myproject:42', 1800
        )

    def test_uses_expire_so_a_cleaned_up_lock_is_not_resurrected(self):
        """expire() only renews an existing key. Using set() here would
        re-create a lock that cleanup_loop()/teardown deliberately deleted."""
        redis_client = MagicMock()

        _beat(redis_client)

        assert redis_client.set.call_count == 0

    def test_redis_failure_does_not_propagate(self):
        """The heartbeat is monitoring state - a Redis blip must not take the
        conversational loop down with it."""
        redis_client = MagicMock()
        redis_client.setex.side_effect = ConnectionError("redis unreachable")

        _beat(redis_client)  # must not raise

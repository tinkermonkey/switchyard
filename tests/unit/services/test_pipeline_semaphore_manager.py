"""
Tests for services/pipeline_semaphore_manager.py — the minimal, correctly-shaped
per-(project, board) counted concurrency primitive built to replace #41's
container-layer cap with something that generalizes PipelineLockManager's
single-holder lock into an N-holder semaphore (see switchyard #55).

Covers:
- Capacity enforcement: at most max_concurrent distinct issues may hold a slot.
- Idempotent re-acquire: an existing holder re-acquiring doesn't consume a
  second slot, and refreshes its staleness timestamp.
- release() frees a slot for a new acquirer.
- Staleness pruning: an unreleased holder older than staleness_seconds is
  evicted on the next acquire attempt, self-healing a process that died
  without releasing.
- Redis-unavailable falls back to a YAML-only path with equivalent capacity
  and idempotency semantics (not just "doesn't crash").
- Redis and YAML stay in sync after a successful Redis-path acquire/release,
  so a later Redis outage can still fall back to a reasonably fresh view.
"""

import time
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from services.pipeline_semaphore_manager import PipelineSemaphoreManager


class FakeRedis:
    """Minimal in-memory stand-in for the subset of the Redis API this module
    uses (hgetall, hset, hdel, hexists, hlen, expire, eval), so tests exercise
    the real capacity/staleness logic instead of a mocked-out no-op."""

    def __init__(self):
        self._hashes: dict[str, dict[str, str]] = {}

    def hgetall(self, key):
        return dict(self._hashes.get(key, {}))

    def hset(self, key, field=None, value=None, mapping=None):
        h = self._hashes.setdefault(key, {})
        if mapping:
            h.update({k: str(v) for k, v in mapping.items()})
        else:
            h[field] = str(value)

    def hdel(self, key, field):
        self._hashes.get(key, {}).pop(field, None)

    def hexists(self, key, field):
        return field in self._hashes.get(key, {})

    def hlen(self, key):
        return len(self._hashes.get(key, {}))

    def expire(self, key, seconds):
        pass  # no-op: staleness is handled by the script's own pruning, not Redis TTL

    def eval(self, script, numkeys, key, cutoff, max_concurrent, issue_number, now):
        """Emulate _TRY_ACQUIRE_SCRIPT's atomic prune+check+add against this
        in-memory store (a real Redis server runs the Lua script itself; this
        fake replicates the same steps directly)."""
        h = self._hashes.setdefault(key, {})
        cutoff = float(cutoff)
        for field, value in list(h.items()):
            if float(value) < cutoff:
                del h[field]

        issue_number = str(issue_number)
        if issue_number in h:
            h[issue_number] = str(now)
            return 1

        if len(h) < int(max_concurrent):
            h[issue_number] = str(now)
            return 1
        return 0


class RaisingRedis:
    """Stand-in for a Redis client whose every call raises, simulating a
    connection that dies mid-operation."""

    def __getattr__(self, name):
        def _raise(*args, **kwargs):
            raise ConnectionError("simulated redis outage")
        return _raise


def make_manager(tmp_path, redis_client=None):
    return PipelineSemaphoreManager(state_dir=tmp_path, redis_client=redis_client)


class TestCapacityEnforcement:
    def test_admits_up_to_max_concurrent_distinct_holders(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())

        ok1, _ = manager.try_acquire("proj", "board", 1, max_concurrent=3)
        ok2, _ = manager.try_acquire("proj", "board", 2, max_concurrent=3)
        ok3, _ = manager.try_acquire("proj", "board", 3, max_concurrent=3)
        ok4, reason4 = manager.try_acquire("proj", "board", 4, max_concurrent=3)

        assert ok1 and ok2 and ok3
        assert not ok4
        assert "at_capacity" in reason4
        assert manager.get_holders("proj", "board") == [1, 2, 3]

    def test_default_max_concurrent_one_behaves_like_a_lock(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())

        ok1, _ = manager.try_acquire("proj", "board", 1)
        ok2, _ = manager.try_acquire("proj", "board", 2)

        assert ok1
        assert not ok2
        assert manager.get_holders("proj", "board") == [1]

    def test_release_frees_a_slot_for_a_new_acquirer(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())

        manager.try_acquire("proj", "board", 1, max_concurrent=1)
        ok_blocked, _ = manager.try_acquire("proj", "board", 2, max_concurrent=1)
        assert not ok_blocked

        released = manager.release("proj", "board", 1)
        assert released is True

        ok_after_release, _ = manager.try_acquire("proj", "board", 2, max_concurrent=1)
        assert ok_after_release
        assert manager.get_holders("proj", "board") == [2]

    def test_boards_are_independent(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())

        manager.try_acquire("proj", "BoardA", 1, max_concurrent=1)
        ok_other_board, _ = manager.try_acquire("proj", "BoardB", 1, max_concurrent=1)

        assert ok_other_board  # same issue, different board — independent capacity
        assert manager.get_holders("proj", "BoardA") == [1]
        assert manager.get_holders("proj", "BoardB") == [1]


class TestIdempotentReacquire:
    def test_existing_holder_reacquiring_does_not_consume_a_second_slot(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())

        ok1, _ = manager.try_acquire("proj", "board", 1, max_concurrent=1)
        ok1_again, reason = manager.try_acquire("proj", "board", 1, max_concurrent=1)

        assert ok1 and ok1_again
        assert reason == "acquired"
        assert manager.get_holders("proj", "board") == [1]  # not double-counted

    def test_reacquire_refreshes_staleness_timestamp(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        key = manager._get_semaphore_key("proj", "board")

        manager.try_acquire("proj", "board", 1, max_concurrent=1)
        old_ts = float(fake_redis._hashes[key]["1"])

        time.sleep(0.01)
        manager.try_acquire("proj", "board", 1, max_concurrent=1)
        new_ts = float(fake_redis._hashes[key]["1"])

        assert new_ts > old_ts


class TestStalenessPruning:
    def test_stale_holder_is_pruned_and_slot_reclaimed(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        key = manager._get_semaphore_key("proj", "board")

        # Simulate a crashed process: registered long enough ago to be stale.
        staleness_seconds = 100
        fake_redis._hashes[key] = {"1": str(time.time() - (staleness_seconds + 10))}

        ok, _ = manager.try_acquire(
            "proj", "board", 2, max_concurrent=1, staleness_seconds=staleness_seconds
        )

        assert ok
        assert manager.get_holders("proj", "board") == [2]  # 1 was pruned, 2 admitted

    def test_fresh_holder_is_not_pruned(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        key = manager._get_semaphore_key("proj", "board")

        staleness_seconds = 100
        fake_redis._hashes[key] = {"1": str(time.time())}

        ok, reason = manager.try_acquire(
            "proj", "board", 2, max_concurrent=1, staleness_seconds=staleness_seconds
        )

        assert not ok
        assert "at_capacity" in reason
        assert manager.get_holders("proj", "board") == [1]


class TestRedisUnavailableFallback:
    def test_no_redis_client_falls_back_to_yaml_with_same_capacity_semantics(self, tmp_path):
        manager = make_manager(tmp_path, redis_client=None)

        ok1, _ = manager.try_acquire("proj", "board", 1, max_concurrent=1)
        ok2, reason2 = manager.try_acquire("proj", "board", 2, max_concurrent=1)
        ok1_again, _ = manager.try_acquire("proj", "board", 1, max_concurrent=1)  # idempotent

        assert ok1
        assert not ok2
        assert "at_capacity" in reason2
        assert ok1_again
        assert manager.get_holders("proj", "board") == [1]

    def test_redis_error_mid_operation_falls_back_to_yaml(self, tmp_path):
        manager = make_manager(tmp_path, redis_client=RaisingRedis())

        ok, reason = manager.try_acquire("proj", "board", 1, max_concurrent=1)

        assert ok
        assert reason == "acquired_yaml_only"

    def test_yaml_release_after_redis_unavailable_acquire(self, tmp_path):
        manager = make_manager(tmp_path, redis_client=None)

        manager.try_acquire("proj", "board", 1, max_concurrent=1)
        released = manager.release("proj", "board", 1)
        ok_after, _ = manager.try_acquire("proj", "board", 2, max_concurrent=1)

        assert released
        assert ok_after
        assert manager.get_holders("proj", "board") == [2]


class TestYamlSyncFromRedis:
    def test_yaml_mirrors_redis_state_after_acquire(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)

        manager.try_acquire("proj", "board", 1, max_concurrent=2)
        manager.try_acquire("proj", "board", 2, max_concurrent=2)

        state_file = manager._get_state_file("proj", "board")
        assert state_file.exists()
        with open(state_file) as f:
            data = yaml.safe_load(f)
        assert sorted(data["holders"]) == [1, 2]

    def test_yaml_mirror_survives_redis_outage_as_fallback_read(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        manager.try_acquire("proj", "board", 1, max_concurrent=1)

        # Simulate Redis becoming unreachable for reads (e.g. connection drop).
        manager.redis_client = RaisingRedis()

        assert manager.get_holders("proj", "board") == [1]  # served from YAML mirror


class TestGetHoldersAndIsHeldBy:
    def test_get_holders_empty_when_never_acquired(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())
        assert manager.get_holders("proj", "board") == []

    def test_is_held_by(self, tmp_path):
        manager = make_manager(tmp_path, FakeRedis())
        manager.try_acquire("proj", "board", 1, max_concurrent=1)

        assert manager.is_held_by("proj", "board", 1) is True
        assert manager.is_held_by("proj", "board", 2) is False

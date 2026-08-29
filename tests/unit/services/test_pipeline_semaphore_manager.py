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
from datetime import datetime, timezone
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
        self.expire_calls: list[tuple[str, int]] = []  # (key, seconds) — asserted on directly

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
        self.expire_calls.append((key, int(seconds)))

    def eval(self, script, numkeys, key, cutoff, max_concurrent, issue_number, now, expire_seconds):
        """Emulate _TRY_ACQUIRE_SCRIPT's atomic prune+check+add against this
        in-memory store (a real Redis server runs the Lua script itself; this
        fake replicates the same steps directly), including its [acquired,
        pruned] return shape and its own EXPIRE call using expire_seconds."""
        h = self._hashes.setdefault(key, {})
        cutoff = float(cutoff)
        pruned = 0
        for field, value in list(h.items()):
            if float(value) < cutoff:
                del h[field]
                pruned = 1

        issue_number = str(issue_number)
        if issue_number in h:
            h[issue_number] = str(now)
            self.expire(key, expire_seconds)
            return [1, pruned]

        if len(h) < int(max_concurrent):
            h[issue_number] = str(now)
            self.expire(key, expire_seconds)
            return [1, pruned]
        return [0, pruned]


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


class TestExpireNeverShorterThanStaleness:
    """Regression tests for the review finding that the Redis hash's own
    EXPIRE was hardcoded shorter than the logical staleness window it's
    supposed to protect — Redis could silently drop the WHOLE hash (every
    holder, not just stale ones) before a genuinely-still-active holder's
    staleness deadline arrived, letting a later acquire exceed capacity with
    no fail-closed check to catch it."""

    def test_expire_is_always_at_least_staleness_seconds(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        key = manager._get_semaphore_key("proj", "board")

        manager.try_acquire("proj", "board", 1, max_concurrent=1, staleness_seconds=99999)

        assert fake_redis.expire_calls, "acquire must set an EXPIRE on the hash"
        for called_key, seconds in fake_redis.expire_calls:
            assert called_key == key
            assert seconds >= 99999, (
                f"EXPIRE={seconds} is shorter than staleness_seconds=99999 — "
                "Redis could drop the whole hash before logical pruning would"
            )

    def test_expire_scales_with_a_larger_default_staleness(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)

        manager.try_acquire("proj", "board", 1, max_concurrent=1)  # default staleness

        from services.pipeline_semaphore_manager import DEFAULT_STALENESS_SECONDS
        _, seconds = fake_redis.expire_calls[-1]
        assert seconds >= DEFAULT_STALENESS_SECONDS


class TestGetHoldersTrustsConfirmedEmptyRedis:
    """Regression tests for the review finding that get_holders() couldn't
    distinguish 'Redis read succeeded and confirmed zero holders' from 'no
    information available', and so needlessly (and incorrectly) fell back to
    a possibly-stale YAML mirror even when Redis had just given a fresh,
    authoritative empty answer."""

    def test_confirmed_empty_redis_is_trusted_over_stale_yaml(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)

        manager.try_acquire("proj", "board", 1, max_concurrent=1)
        manager.release("proj", "board", 1)  # Redis now confirmed-empty for this key

        # Simulate a stale YAML mirror that never got cleaned up (e.g. an
        # older bug, or a race) still claiming issue 1 holds a slot.
        state_file = manager._get_state_file("proj", "board")
        with open(state_file, "w") as f:
            yaml.dump(
                {"project": "proj", "board": "board", "holders": [1],
                 "holder_entries": {"1": "2020-01-01T00:00:00+00:00"}},
                f,
            )

        # Redis's fresh, confirmed-empty answer must win, not stale YAML.
        assert manager.get_holders("proj", "board") == []

    def test_still_falls_back_to_yaml_when_redis_read_actually_fails(self, tmp_path):
        manager = make_manager(tmp_path, redis_client=FakeRedis())
        manager.try_acquire("proj", "board", 1, max_concurrent=1)

        manager.redis_client = RaisingRedis()  # Redis becomes unreachable

        assert manager.get_holders("proj", "board") == [1]  # served from YAML


class TestYamlSyncsOnPruneEvenWhenRefused:
    """Regression test for the review finding that YAML was only resynced on
    a successful acquire — pruning a stale holder during a REFUSED acquire
    (other live holders still keep the board at capacity) never propagated
    to YAML, leaving a phantom already-pruned holder there indefinitely."""

    def test_pruning_during_a_refused_acquire_still_updates_yaml(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        key = manager._get_semaphore_key("proj", "board")

        staleness_seconds = 100
        # Two holders already at capacity=2: one stale, one fresh.
        fake_redis._hashes[key] = {
            "1": str(time.time() - (staleness_seconds + 10)),  # stale
            "2": str(time.time()),  # fresh
        }
        # Seed YAML with the pre-prune state so we can observe the resync.
        manager._sync_yaml_from_redis("proj", "board")
        assert sorted(manager._read_yaml_holders_only("proj", "board")[0]) == [1, 2]

        # issue 3 tries to acquire; capacity=2 means it's refused, but issue 1
        # gets pruned for staleness along the way (issue 2 alone still holds
        # the board at effective capacity=1 in this scenario... use
        # capacity=1 explicitly so the refusal is unambiguous after pruning).
        ok, reason = manager.try_acquire(
            "proj", "board", 3, max_concurrent=1, staleness_seconds=staleness_seconds
        )

        assert not ok
        assert "at_capacity" in reason
        # Redis-side pruning removed issue 1; YAML must reflect that even
        # though the overall acquire was refused.
        assert manager._read_yaml_holders_only("proj", "board")[0] == [2]


class TestYamlSyncSkipsUnchangedWrite:
    """Regression test for the review finding that every idempotent refresh
    (a holder re-acquiring on every poll, the module's own documented
    behavior) triggered a full YAML disk read+write+flock cycle even when
    the holder set hadn't changed at all."""

    def test_idempotent_reacquire_does_not_rewrite_yaml_file(self, tmp_path):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)

        manager.try_acquire("proj", "board", 1, max_concurrent=1)
        state_file = manager._get_state_file("proj", "board")
        mtime_after_first_acquire = state_file.stat().st_mtime_ns

        time.sleep(0.01)
        manager.try_acquire("proj", "board", 1, max_concurrent=1)  # idempotent refresh

        assert state_file.stat().st_mtime_ns == mtime_after_first_acquire, (
            "YAML file must not be rewritten when the holder set is unchanged"
        )


class TestReleaseReportsFailureAccurately:
    """Regression test for the review finding that release() initialized
    redis_ok = True instead of False: with no Redis client configured and a
    failing YAML write, the method incorrectly reported success (mirroring
    it as an implicit 'this store doesn't apply, so it trivially succeeded'
    instead of 'this store never confirmed anything'), leaving a slot held
    forever while callers believed it had been freed."""

    def test_release_reports_failure_when_no_redis_and_yaml_write_fails(self, tmp_path, monkeypatch):
        manager = make_manager(tmp_path, redis_client=None)
        manager.try_acquire("proj", "board", 1, max_concurrent=1)

        monkeypatch.setattr(manager, "_release_yaml_only", lambda *a, **kw: False)

        result = manager.release("proj", "board", 1)

        assert result is False

    def test_release_reports_success_when_redis_confirms_even_without_yaml(self, tmp_path, monkeypatch):
        fake_redis = FakeRedis()
        manager = make_manager(tmp_path, fake_redis)
        manager.try_acquire("proj", "board", 1, max_concurrent=1)

        monkeypatch.setattr(manager, "_release_yaml_only", lambda *a, **kw: False)

        result = manager.release("proj", "board", 1)

        assert result is True  # Redis-side hdel succeeded, so at least one store confirmed


class TestYamlOnlyPersistsPruningEvenOnRefusal:
    """Regression test mirroring TestYamlSyncsOnPruneEvenWhenRefused, but for
    the YAML-only fallback path (_try_acquire_yaml_only): pruning a stale
    holder during a refused acquire must be written back to disk, not just
    computed in memory and discarded when the function returns False."""

    def test_pruning_during_a_refused_yaml_only_acquire_is_persisted(self, tmp_path):
        manager = make_manager(tmp_path, redis_client=None)
        state_file = manager._get_state_file("proj", "board")
        staleness_seconds = 100

        # Seed the YAML file directly: one stale holder, one fresh — at
        # capacity=1 (using the fresh holder alone).
        now = time.time()
        state_file.parent.mkdir(parents=True, exist_ok=True)
        with open(state_file, "w") as f:
            yaml.dump({
                "project": "proj", "board": "board",
                "holders": [1, 2],
                "holder_entries": {
                    "1": datetime.fromtimestamp(now - (staleness_seconds + 10), tz=timezone.utc).isoformat(),
                    "2": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
                },
            }, f)

        ok, reason = manager.try_acquire(
            "proj", "board", 3, max_concurrent=1, staleness_seconds=staleness_seconds
        )

        assert not ok
        assert "at_capacity" in reason
        # Issue 1 was pruned for staleness; that must be persisted even
        # though the overall acquire (issue 3) was refused.
        assert manager.get_holders("proj", "board") == [2]

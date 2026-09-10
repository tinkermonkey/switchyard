"""
In-memory stand-ins for the slice of the Redis API PipelineLockManager uses.

Shared home for what used to be two byte-identical copies of
ThreadSafeFakeRedis (tests/unit/services/test_project_checkout_lock.py and
test_dev_container_build_lock.py). The second copy's docstring gave the reason
as "tests/unit has no package __init__.py making such an import reliable under
pytest's collection" -- that is true of tests/unit, but not of tests/utils,
which tests/conftest.py already imports from by package path. #139's audit
needed a stateful fake in three more modules, and a third and fourth copy is
worse than one shared one.

Why a fake at all rather than a MagicMock or the real client: try_acquire_lock()
/touch_lock()/release_lock() decide who holds a lock by reading the current
value inside a WATCH/MULTI transaction and writing a different one. A MagicMock
returns whatever the test told it to regardless of what was written, so it can
model a single decision but not a sequence of them, and never a race. The real
client would make these tests depend on a reachable Redis -- see
tests/conftest.py's service fail-fast guards (#174) for why the suite must not,
and note that inside the orchestrator container that Redis is the running
deployment's own.
"""

import threading
import time


class ThreadSafeFakeRedis:
    """Minimal in-memory stand-in for a single Redis instance's hash + atomic
    transaction API, sufficient for PipelineLockManager's try_acquire_lock()/
    release_lock()/get_lock()."""

    def __init__(self):
        self._store = {}
        # RLock, not Lock: transaction() holds this for the whole func(pipe)
        # call, and func (acquire_lock_tx/release_lock_tx in
        # pipeline_lock_manager.py) calls pipe.hgetall()/.hset()/.delete(),
        # which re-enter this same lock from the same thread -- a plain
        # non-reentrant Lock would self-deadlock there.
        self._global_lock = threading.RLock()

    def ping(self):
        return True

    def hgetall(self, key):
        with self._global_lock:
            return dict(self._store.get(key, {}))

    def hset(self, key, mapping):
        with self._global_lock:
            self._store.setdefault(key, {}).update(mapping)

    def delete(self, key):
        with self._global_lock:
            self._store.pop(key, None)

    def expire(self, key, seconds):
        pass  # TTL not needed for these tests -- see TtlFakeRedis for the ones that need it

    # Deliberately no keys(): get_all_locks()' Redis scan is the one caller,
    # and leaving it absent is what makes that method fall back to its YAML
    # glob here, exactly as it did when this class was two local copies. A
    # test that wants the Redis scan covered should say so with its own
    # subclass rather than have it appear under every existing user of this
    # one.

    class _Pipe:
        """Stands in for both Redis.pipeline()'s context-managed object
        (.watch()/.exists(), whose results this codebase's acquire_lock
        currently discards -- see pipeline_lock_manager.py's own comments)
        and the callable-transaction pipe passed to acquire_lock_tx/
        release_lock_tx (.hgetall()/.multi()/.hset()/.expire()/.delete())."""

        def __init__(self, redis):
            self._redis = redis

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def watch(self, key):
            return None

        def exists(self, key):
            with self._redis._global_lock:
                return key in self._redis._store

        def multi(self):
            return None

        def hgetall(self, key):
            return self._redis.hgetall(key)

        def hset(self, key, mapping):
            return self._redis.hset(key, mapping)

        def expire(self, key, seconds):
            return None

        def delete(self, key):
            return self._redis.delete(key)

    # type(self)._Pipe, not ThreadSafeFakeRedis._Pipe: resolving through the
    # MRO is what lets TtlFakeRedis below swap in its own TTL-honouring pipe.
    def pipeline(self):
        return type(self)._Pipe(self)

    def transaction(self, func, *keys, value_from_callable=False):
        # The whole read-decide-write sequence runs under one process-wide
        # lock -- see class docstring for why this is a faithful enough model
        # of real single-Redis-instance atomicity for these tests.
        with self._global_lock:
            return func(type(self)._Pipe(self))


class TtlFakeRedis(ThreadSafeFakeRedis):
    """
    ThreadSafeFakeRedis that actually HONOURS expire(), on a compressed clock.

    Found in review of #146 WI-1: the base fake's expire() is a no-op
    ("TTL not needed for these tests"), which makes every heartbeat test in
    test_project_checkout_lock.py structurally blind to the one outcome the
    heartbeat exists to produce. A hold that outlives PipelineLockManager's
    Redis lock-key TTL has its Redis copy silently vanish, and
    try_acquire_lock()'s transaction reads the missing key back as an empty
    dict -- so a SECOND caller is granted the same lock while the first still
    holds it. Asserting on touch_resource call counts cannot see that; only a
    real acquire attempt can.

    The real TTL is LOCK_TTL_SECONDS (7200s), far too long to sit through in a
    unit test, so ANY expire() maps onto `ttl_seconds` instead and the tests
    choose a heartbeat interval either side of it.
    """

    def __init__(self, ttl_seconds: float):
        super().__init__()
        self.ttl_seconds = ttl_seconds
        # Every (key, seconds) passed to expire(), so a test can assert the
        # TTL this codebase actually writes -- see
        # TestHeartbeatConstantsTrackTheRealRedisTtl.
        self.expire_calls = []
        self._expires_at = {}

    def expire(self, key, seconds):
        with self._global_lock:
            self.expire_calls.append((key, seconds))
            self._expires_at[key] = time.monotonic() + self.ttl_seconds

    def _drop_if_expired(self, key):
        """Caller must hold _global_lock."""
        expires_at = self._expires_at.get(key)
        if expires_at is not None and time.monotonic() >= expires_at:
            self._store.pop(key, None)
            self._expires_at.pop(key, None)

    def hgetall(self, key):
        with self._global_lock:
            self._drop_if_expired(key)
            return dict(self._store.get(key, {}))

    def delete(self, key):
        with self._global_lock:
            self._expires_at.pop(key, None)
            self._store.pop(key, None)

    class _Pipe(ThreadSafeFakeRedis._Pipe):
        def exists(self, key):
            with self._redis._global_lock:
                self._redis._drop_if_expired(key)
                return key in self._redis._store

        def expire(self, key, seconds):
            return self._redis.expire(key, seconds)

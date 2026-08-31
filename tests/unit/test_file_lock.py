"""
Unit tests for utils/file_lock.py's enforce_timeout mode.

Covers the bounded-wait behavior added alongside the review_cycle.py /
pr_review_state_manager.py locking fix (#37): when enforce_timeout=True,
file_lock() must raise TimeoutError instead of blocking forever if the
lock is already held.
"""

import threading
import time

import pytest

from utils.file_lock import file_lock


def test_enforce_timeout_raises_when_lock_held(tmp_path):
    lock_path = tmp_path / "state.yaml.lock"
    released = threading.Event()
    holder_acquired = threading.Event()

    def hold_lock():
        with file_lock(lock_path):
            holder_acquired.set()
            released.wait(timeout=5)

    holder = threading.Thread(target=hold_lock, daemon=True)
    holder.start()
    assert holder_acquired.wait(timeout=2), "holder thread never acquired the lock"

    start = time.monotonic()
    with pytest.raises(TimeoutError):
        with file_lock(lock_path, timeout=0.3, enforce_timeout=True):
            pass
    elapsed = time.monotonic() - start

    # Bounded: should time out close to the requested window, never block indefinitely.
    assert elapsed < 2.0

    released.set()
    holder.join(timeout=2)


def test_enforce_timeout_acquires_once_released(tmp_path):
    lock_path = tmp_path / "state.yaml.lock"
    released = threading.Event()
    holder_acquired = threading.Event()

    def hold_briefly():
        with file_lock(lock_path):
            holder_acquired.set()
            time.sleep(0.2)
        released.set()

    holder = threading.Thread(target=hold_briefly, daemon=True)
    holder.start()
    assert holder_acquired.wait(timeout=2)

    # Timeout is generous enough to outlast the brief hold above.
    with file_lock(lock_path, timeout=2, enforce_timeout=True):
        assert released.is_set()

    holder.join(timeout=2)


def test_default_blocking_mode_unchanged(tmp_path):
    """enforce_timeout defaults to False — existing callers keep blocking behavior."""
    lock_path = tmp_path / "state.yaml.lock"
    with file_lock(lock_path):
        pass  # no exception, no timeout param required

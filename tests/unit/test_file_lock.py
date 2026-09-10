"""
Unit tests for utils/file_lock.py's enforce_timeout mode and its re-entrancy
guard.

Covers the bounded-wait behavior added alongside the review_cycle.py /
pr_review_state_manager.py locking fix (#37): when enforce_timeout=True,
file_lock() must raise TimeoutError instead of blocking forever if the
lock is already held.

Also covers the re-entrancy guard (#150): fcntl.flock() locks are per
open-file-description, so a thread that takes the same lock path twice blocks
against itself forever, in the same thread, with no timeout. That is how
WorkExecutionStateTracker's empty-output sweep silently wedged its scheduler
thread on every single run for months.
"""

import threading
import time

import pytest

from utils.file_lock import ReentrantFileLockError, file_lock, safe_yaml_write


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


class TestReentrancyGuard:
    """A nested acquire of the same path must raise, not hang (#150)."""

    def test_nested_acquire_of_the_same_path_raises(self, tmp_path):
        lock_path = tmp_path / "state.yaml.lock"

        with file_lock(lock_path):
            with pytest.raises(ReentrantFileLockError):
                with file_lock(lock_path):
                    pytest.fail("re-entrant acquire should never have succeeded")

    def test_nested_acquire_through_safe_yaml_write_raises(self, tmp_path):
        """The lock path is the same whether it is reached directly or via
        safe_yaml_write()'s derived <file>.lock -- which is exactly how
        save_state() would have re-entered load_state()'s lock."""
        state_file = tmp_path / "state.yaml"

        with file_lock(state_file.with_suffix(state_file.suffix + '.lock')):
            with pytest.raises(ReentrantFileLockError):
                with safe_yaml_write(state_file):
                    pytest.fail("re-entrant acquire should never have succeeded")

    def test_different_paths_still_nest_freely(self, tmp_path):
        """The guard is per lock path -- PipelineLockManager's separate
        .acquire.lock guard file pattern must keep working."""
        outer = tmp_path / "state.yaml.acquire.lock"
        inner = tmp_path / "state.yaml.lock"

        with file_lock(outer):
            with file_lock(inner):
                pass

    def test_the_path_is_reusable_after_the_context_exits(self, tmp_path):
        lock_path = tmp_path / "state.yaml.lock"

        with file_lock(lock_path):
            pass
        with file_lock(lock_path):
            pass

    def test_a_raising_body_still_clears_the_guard(self, tmp_path):
        lock_path = tmp_path / "state.yaml.lock"

        with pytest.raises(ValueError):
            with file_lock(lock_path):
                raise ValueError("boom")

        with file_lock(lock_path):
            pass

    def test_another_thread_is_unaffected(self, tmp_path):
        """The guard is thread-local: it must never make a genuine cross-thread
        wait look like a programming error."""
        lock_path = tmp_path / "state.yaml.lock"
        acquired = threading.Event()

        def take_it():
            with file_lock(lock_path, timeout=2, enforce_timeout=True):
                acquired.set()

        with file_lock(lock_path):
            other = threading.Thread(target=take_it, daemon=True)
            other.start()
            # Still blocked on the real flock, not refused by the guard.
            assert not acquired.wait(timeout=0.3)

        other.join(timeout=3)
        assert acquired.is_set()

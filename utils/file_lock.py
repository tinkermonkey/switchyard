"""
Thread-safe file locking utilities.

Provides cross-process and cross-thread file locking using fcntl (POSIX systems).
Used to ensure YAML state files can be safely written from multiple worker threads.
"""

import fcntl
import contextlib
import logging
import os
import threading
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# Poll interval used when enforce_timeout=True.
_POLL_INTERVAL_SECONDS = 0.1

# Lock paths the current thread is already inside, used only to turn a
# re-entrant acquire into a loud error instead of a permanent hang (#150).
_thread_state = threading.local()


class ReentrantFileLockError(RuntimeError):
    """Raised when a thread tries to take a file_lock it already holds.

    fcntl.flock() locks are attached to the open file description, not to the
    process or the thread, so a second open() of the same lock path blocks
    against the first one -- in the same thread, with no timeout and no way
    out. This has bitten this codebase twice: PipelineLockManager works around
    it with a separate .acquire.lock guard file, and
    WorkExecutionStateTracker's empty-output sweep silently wedged its
    scheduler thread on every run by calling has_active_execution() (which
    re-reads the state file) from inside the state file's own lock. Detecting
    it here means the next one surfaces as a traceback rather than as a thread
    that never comes back.
    """


def _held_lock_paths() -> set:
    held = getattr(_thread_state, 'held_lock_paths', None)
    if held is None:
        held = set()
        _thread_state.held_lock_paths = held
    return held


@contextlib.contextmanager
def file_lock(lock_file_path: Union[str, Path], timeout: int = 10, enforce_timeout: bool = False):
    """
    Context manager for exclusive file locking.

    Uses fcntl.flock() to acquire an exclusive lock on a lock file.
    This prevents multiple processes or threads from writing to the same
    file simultaneously, avoiding corruption.

    Args:
        lock_file_path: Path to the lock file (typically .lock extension)
        timeout: Maximum time to wait for lock (seconds). Only enforced when
                enforce_timeout=True; otherwise unused (kept for logging/monitoring
                and for source compatibility with existing callers).
        enforce_timeout: If True, poll for a non-blocking lock and raise
                TimeoutError if it isn't acquired within `timeout` seconds,
                instead of blocking indefinitely. Defaults to False so existing
                callers keep today's blocking behavior unchanged; pass True for
                call sites that must not risk an unbounded wait (e.g. code
                reachable from an async event loop, or from startup).

    Usage:
        with file_lock('/path/to/file.lock'):
            # Critical section - only one process/thread can be here
            with open('/path/to/file.yaml', 'w') as f:
                yaml.dump(data, f)

    Raises:
        ReentrantFileLockError: if this thread already holds this lock path.

    Note:
        - The lock file is created if it doesn't exist
        - The lock is automatically released when exiting the context
        - Blocks until lock is acquired unless enforce_timeout=True
        - NOT re-entrant; see ReentrantFileLockError
    """
    lock_path = Path(lock_file_path)

    # Refuse a re-entrant acquire rather than hanging on it -- see
    # ReentrantFileLockError. Checked before the open() so a refused acquire
    # doesn't leak a file descriptor.
    lock_key = os.path.abspath(str(lock_path))
    held = _held_lock_paths()
    if lock_key in held:
        raise ReentrantFileLockError(
            f"Thread already holds {lock_key}; a nested file_lock() on it would "
            f"block forever against its own outer acquire. Use the non-locking "
            f"variant of whatever read/write is nested here (for example "
            f"WorkExecutionStateTracker.has_active_execution_for_state), or take "
            f"a separate guard file the way PipelineLockManager does."
        )

    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open/create the lock file
    lock_file = None
    registered = False
    try:
        lock_file = open(lock_path, 'a')

        logger.debug(f"Acquiring lock: {lock_path}")
        if enforce_timeout:
            start_time = time.monotonic()
            while True:
                try:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() - start_time > timeout:
                        raise TimeoutError(
                            f"Could not acquire lock on {lock_path} within {timeout} seconds"
                        )
                    time.sleep(_POLL_INTERVAL_SECONDS)
        else:
            # Acquire exclusive lock (blocks until available)
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        logger.debug(f"Lock acquired: {lock_path}")

        held.add(lock_key)
        registered = True

        yield

    finally:
        if registered:
            held.discard(lock_key)
        if lock_file:
            # Release lock
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            logger.debug(f"Lock released: {lock_path}")
            lock_file.close()


@contextlib.contextmanager
def safe_yaml_write(
    yaml_file_path: Union[str, Path], timeout: int = 10, enforce_timeout: bool = False
):
    """
    Context manager for thread-safe YAML file writing.

    Automatically creates and manages a .lock file alongside the YAML file.

    Args:
        yaml_file_path: Path to the YAML file to write
        timeout / enforce_timeout: forwarded to file_lock() unchanged, and
            defaulted the same way, so existing callers keep today's blocking
            behavior. Pass enforce_timeout=True where an unbounded wait here
            would park something that must not park -- e.g.
            PipelineLockManager._save_lock_to_yaml(), which runs inside that
            class's '<state>.yaml.acquire.lock' guard and so would otherwise
            make that guard's hold unbounded.

    Usage:
        with safe_yaml_write('/path/to/file.yaml'):
            with open('/path/to/file.yaml', 'w') as f:
                yaml.dump(data, f)

    Raises:
        TimeoutError: if enforce_timeout=True and the lock isn't acquired in time.

    Note:
        Creates a lock file at: /path/to/file.yaml.lock
    """
    yaml_path = Path(yaml_file_path)
    lock_path = yaml_path.with_suffix(yaml_path.suffix + '.lock')

    with file_lock(lock_path, timeout=timeout, enforce_timeout=enforce_timeout):
        yield

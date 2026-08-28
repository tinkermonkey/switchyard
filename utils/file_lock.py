"""
Thread-safe file locking utilities.

Provides cross-process and cross-thread file locking using fcntl (POSIX systems).
Used to ensure YAML state files can be safely written from multiple worker threads.
"""

import fcntl
import contextlib
import logging
import time
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)

# Poll interval used when enforce_timeout=True.
_POLL_INTERVAL_SECONDS = 0.1


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

    Note:
        - The lock file is created if it doesn't exist
        - The lock is automatically released when exiting the context
        - Blocks until lock is acquired unless enforce_timeout=True
    """
    lock_path = Path(lock_file_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open/create the lock file
    lock_file = None
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

        yield

    finally:
        if lock_file:
            # Release lock
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            logger.debug(f"Lock released: {lock_path}")
            lock_file.close()


@contextlib.contextmanager
def safe_yaml_write(yaml_file_path: Union[str, Path]):
    """
    Context manager for thread-safe YAML file writing.

    Automatically creates and manages a .lock file alongside the YAML file.

    Args:
        yaml_file_path: Path to the YAML file to write

    Usage:
        with safe_yaml_write('/path/to/file.yaml'):
            with open('/path/to/file.yaml', 'w') as f:
                yaml.dump(data, f)

    Note:
        Creates a lock file at: /path/to/file.yaml.lock
    """
    yaml_path = Path(yaml_file_path)
    lock_path = yaml_path.with_suffix(yaml_path.suffix + '.lock')

    with file_lock(lock_path):
        yield

"""
Thread-safe file locking utilities.

Provides cross-process and cross-thread file locking using fcntl (POSIX systems).
Used to ensure YAML state files can be safely written from multiple worker threads.
"""

import fcntl
import contextlib
import logging
from pathlib import Path
from typing import Union

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def file_lock(lock_file_path: Union[str, Path], timeout: int = 10):
    """
    Context manager for exclusive file locking.

    Uses fcntl.flock() to acquire an exclusive lock on a lock file.
    This prevents multiple processes or threads from writing to the same
    file simultaneously, avoiding corruption.

    Args:
        lock_file_path: Path to the lock file (typically .lock extension)
        timeout: Maximum time to wait for lock (seconds). Not enforced by fcntl,
                but can be used for logging/monitoring.

    Usage:
        with file_lock('/path/to/file.lock'):
            # Critical section - only one process/thread can be here
            with open('/path/to/file.yaml', 'w') as f:
                yaml.dump(data, f)

    Note:
        - The lock file is created if it doesn't exist
        - The lock is automatically released when exiting the context
        - Blocks until lock is acquired (no timeout enforcement)
    """
    lock_path = Path(lock_file_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    # Open/create the lock file
    lock_file = None
    try:
        lock_file = open(lock_path, 'a')

        # Acquire exclusive lock (blocks until available)
        logger.debug(f"Acquiring lock: {lock_path}")
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

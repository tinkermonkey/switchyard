"""
Redis utilities for Medic investigation queue management.

Provides common Redis operations used by both Docker and Claude investigation systems.
"""

import logging
import time
from typing import Optional
import redis

logger = logging.getLogger(__name__)


def acquire_lock(
    redis_client: redis.Redis,
    lock_key: str,
    ttl_seconds: int = 60,
    timeout_seconds: int = 10
) -> bool:
    """
    Acquire a distributed lock using Redis.

    Args:
        redis_client: Redis client
        lock_key: Key for the lock
        ttl_seconds: TTL for the lock in seconds
        timeout_seconds: How long to wait to acquire the lock

    Returns:
        True if lock acquired, False otherwise
    """
    start_time = time.time()
    identifier = str(time.time())  # Simple identifier

    while (time.time() - start_time) < timeout_seconds:
        if redis_client.set(lock_key, identifier, nx=True, ex=ttl_seconds):
            return True
        time.sleep(0.1)

    return False


def release_lock(
    redis_client: redis.Redis,
    lock_key: str
) -> bool:
    """
    Release a distributed lock.

    Args:
        redis_client: Redis client
        lock_key: Key for the lock

    Returns:
        True if lock released, False otherwise
    """
    try:
        redis_client.delete(lock_key)
        return True
    except Exception as e:
        logger.error(f"Failed to release lock {lock_key}: {e}")
        return False


def get_investigation_status(
    redis_client: redis.Redis,
    key_prefix: str,
    fingerprint_id: str
) -> Optional[str]:
    """
    Get investigation status from Redis.

    Args:
        redis_client: Redis client
        key_prefix: Key prefix (e.g., "medic:docker_investigation")
        fingerprint_id: Fingerprint ID

    Returns:
        Status string or None if not found
    """
    try:
        status_key = f"{key_prefix}:{fingerprint_id}:status"
        status = redis_client.get(status_key)
        return status if status else None
    except Exception as e:
        logger.error(f"Failed to get investigation status for {fingerprint_id}: {e}")
        return None


def set_investigation_status(
    redis_client: redis.Redis,
    key_prefix: str,
    fingerprint_id: str,
    status: str,
    ttl_seconds: Optional[int] = None
) -> bool:
    """
    Set investigation status in Redis.

    Args:
        redis_client: Redis client
        key_prefix: Key prefix (e.g., "medic:docker_investigation")
        fingerprint_id: Fingerprint ID
        status: Status to set
        ttl_seconds: Optional TTL in seconds

    Returns:
        True if successful, False otherwise
    """
    try:
        status_key = f"{key_prefix}:{fingerprint_id}:status"
        if ttl_seconds:
            redis_client.setex(status_key, ttl_seconds, status)
        else:
            redis_client.set(status_key, status)
        return True
    except Exception as e:
        logger.error(f"Failed to set investigation status for {fingerprint_id}: {e}")
        return False


def get_queue_length(
    redis_client: redis.Redis,
    queue_key: str
) -> int:
    """
    Get length of a Redis list queue.

    Args:
        redis_client: Redis client
        queue_key: Queue key

    Returns:
        Queue length
    """
    try:
        return redis_client.llen(queue_key)
    except Exception as e:
        logger.error(f"Failed to get queue length for {queue_key}: {e}")
        return 0


def clear_keys_by_pattern(
    redis_client: redis.Redis,
    pattern: str
) -> int:
    """
    Delete all keys matching a pattern.

    Args:
        redis_client: Redis client
        pattern: Key pattern (e.g., "medic:investigation:*")

    Returns:
        Number of keys deleted
    """
    try:
        keys = redis_client.keys(pattern)
        if keys:
            return redis_client.delete(*keys)
        return 0
    except Exception as e:
        logger.error(f"Failed to clear keys matching {pattern}: {e}")
        return 0

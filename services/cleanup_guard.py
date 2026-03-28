"""
Cleanup Coordination Guard

Prevents multiple cleanup mechanisms (zombie watchdog, Docker reconciliation,
stuck state cleanup) from acting on the same issue simultaneously.  Uses a
Redis SET NX lock with a short TTL so that only one mechanism "claims" cleanup
for a given (project, issue_number) at a time.
"""

import logging

logger = logging.getLogger(__name__)

_REDIS_PREFIX = "orchestrator:cleanup_guard"
_DEFAULT_TTL = 300  # 5 minutes

# Lazy-initialized module-level Redis client (avoids creating a new connection per call)
_redis_client = None


def _get_redis():
    """Get or create the module-level Redis client."""
    global _redis_client
    if _redis_client is None:
        import redis
        _redis_client = redis.Redis(
            host='redis', port=6379,
            decode_responses=True,
            socket_connect_timeout=2
        )
    return _redis_client


def try_claim_cleanup(project: str, issue_number: int, mechanism: str, ttl_seconds: int = _DEFAULT_TTL) -> bool:
    """
    Attempt to claim cleanup ownership for an issue.

    Returns True if claimed (caller should proceed with cleanup).
    Returns False if another mechanism already owns it (caller should skip).
    """
    try:
        rc = _get_redis()
        key = f"{_REDIS_PREFIX}:{project}:{issue_number}"
        claimed = rc.set(key, mechanism, nx=True, ex=ttl_seconds)
        if not claimed:
            existing = rc.get(key)
            logger.debug(
                f"Cleanup for {project}/#{issue_number} already claimed by {existing}, "
                f"skipping in {mechanism}"
            )
        return bool(claimed)
    except Exception as e:
        logger.error(
            f"Cleanup guard Redis unavailable for {project}/#{issue_number} "
            f"(mechanism={mechanism}) - proceeding without coordination: {e}"
        )
        return True  # Fail open to preserve existing behavior


def release_cleanup(project: str, issue_number: int) -> None:
    """Release cleanup ownership (best-effort; TTL handles expiration)."""
    try:
        rc = _get_redis()
        key = f"{_REDIS_PREFIX}:{project}:{issue_number}"
        rc.delete(key)
    except Exception as e:
        logger.debug(f"Could not release cleanup guard for {project}/#{issue_number} (TTL will expire): {e}")

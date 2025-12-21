"""
Base Investigation Queue

Base class for investigation queue management with Redis.
Uses parameterized key prefixes to support both Docker and Claude systems.
"""

import logging
import redis
from typing import Optional, List
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class BaseInvestigationQueue:
    """
    Redis-based investigation queue with distributed locking.

    All methods are concrete - only the key prefix differs between Docker and Claude systems.

    Redis Key Pattern:
        {key_prefix}:{fingerprint_id}:pid              # Process ID
        {key_prefix}:{fingerprint_id}:status           # Status
        {key_prefix}:{fingerprint_id}:lock             # Lock with TTL
        {key_prefix}:{fingerprint_id}:started_at       # ISO timestamp
        {key_prefix}:{fingerprint_id}:last_heartbeat   # ISO timestamp
        {key_prefix}:{fingerprint_id}:agent_output_lines  # Progress counter
        {key_prefix}:{fingerprint_id}:result           # Outcome
        {key_prefix}:{fingerprint_id}:completed_at     # ISO timestamp
        {key_prefix}:queue                             # List (queue)
        {key_prefix}:active                            # Set of active fingerprint IDs
    """

    # Status states
    STATUS_QUEUED = "queued"
    STATUS_STARTING = "starting"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_STALLED = "stalled"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"
    STATUS_IGNORED = "ignored"
    STATUS_TIMEOUT = "timeout"

    # Investigation results
    RESULT_SUCCESS = "success"
    RESULT_IGNORED = "ignored"
    RESULT_FAILED = "failed"
    RESULT_TIMEOUT = "timeout"

    # Timeouts
    LOCK_TTL = 4 * 3600  # 4 hours
    STALL_THRESHOLD = 10 * 60  # 10 minutes
    HEARTBEAT_INTERVAL = 30  # 30 seconds
    MAX_CONCURRENT = 3  # Maximum concurrent investigations

    def __init__(self, redis_client: redis.Redis, key_prefix: str):
        """
        Initialize investigation queue.

        Args:
            redis_client: Redis client
            key_prefix: Key prefix (e.g., "medic:docker_investigation" or "medic:claude_investigation")
        """
        self.redis = redis_client
        self.KEY_PREFIX = key_prefix
        self.QUEUE_KEY = f"{key_prefix}:queue"
        self.ACTIVE_SET_KEY = f"{key_prefix}:active"
        logger.info(f"BaseInvestigationQueue initialized with prefix: {key_prefix}")

    def _key(self, fingerprint_id: str, suffix: str) -> str:
        """Generate Redis key for investigation data."""
        return f"{self.KEY_PREFIX}:{fingerprint_id}:{suffix}"

    def enqueue(self, fingerprint_id: str, priority: str = "normal") -> bool:
        """
        Add investigation to queue if not already queued/in-progress.

        Args:
            fingerprint_id: Failure signature ID
            priority: "low", "normal", "high" (affects queue position)

        Returns:
            True if enqueued, False if already exists
        """
        # Check if already in queue or active
        status = self.get_status(fingerprint_id)
        if status in [
            self.STATUS_QUEUED,
            self.STATUS_STARTING,
            self.STATUS_IN_PROGRESS,
        ]:
            logger.info(f"Investigation {fingerprint_id} already {status}, not re-enqueuing")
            return False

        # Set initial status
        self.redis.set(self._key(fingerprint_id, "status"), self.STATUS_QUEUED)

        # Add to queue based on priority
        if priority == "high":
            self.redis.lpush(self.QUEUE_KEY, fingerprint_id)
        else:
            self.redis.rpush(self.QUEUE_KEY, fingerprint_id)

        logger.info(f"Enqueued investigation: {fingerprint_id} (priority: {priority})")
        return True

    async def dequeue(self) -> Optional[str]:
        """
        Get next investigation from queue (non-blocking check with timeout simulation).

        Returns:
            Fingerprint ID or None if queue is empty
        """
        # Use non-blocking lpop instead of blocking blpop to avoid blocking the event loop
        result = self.redis.lpop(self.QUEUE_KEY)
        if result:
            logger.info(f"Dequeued investigation: {result}")
            return result
        # If queue is empty, return None (caller will sleep and retry)
        return None

    def get_status(self, fingerprint_id: str) -> Optional[str]:
        """
        Get investigation status.

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Status string or None
        """
        status = self.redis.get(self._key(fingerprint_id, "status"))
        return status if status else None

    def update_status(self, fingerprint_id: str, status: str):
        """
        Update investigation status.

        Args:
            fingerprint_id: Fingerprint ID
            status: New status
        """
        self.redis.set(self._key(fingerprint_id, "status"), status)
        logger.debug(f"Updated status for {fingerprint_id}: {status}")

    def mark_started(self, fingerprint_id: str, pid: int = 0, container_name: str = None):
        """
        Mark investigation as started.

        Args:
            fingerprint_id: Fingerprint ID
            pid: Process ID (legacy, defaults to 0 for containerized execution)
            container_name: Docker container name (for containerized execution)
        """
        now = datetime.now(timezone.utc).isoformat()

        self.redis.set(self._key(fingerprint_id, "pid"), str(pid))
        if container_name:
            self.redis.set(self._key(fingerprint_id, "container_name"), container_name)
        self.redis.set(self._key(fingerprint_id, "status"), self.STATUS_IN_PROGRESS)
        self.redis.set(self._key(fingerprint_id, "started_at"), now)
        self.redis.set(self._key(fingerprint_id, "last_heartbeat"), now)
        self.redis.sadd(self.ACTIVE_SET_KEY, fingerprint_id)

        if container_name:
            logger.info(f"Marked investigation {fingerprint_id} as started (container: {container_name})")
        else:
            logger.info(f"Marked investigation {fingerprint_id} as started (PID: {pid})")

    def update_heartbeat(self, fingerprint_id: str, agent_output_lines: int = 0):
        """
        Update heartbeat timestamp and progress.

        Args:
            fingerprint_id: Fingerprint ID
            agent_output_lines: Number of agent output lines (progress indicator)
        """
        now = datetime.now(timezone.utc).isoformat()

        self.redis.set(self._key(fingerprint_id, "last_heartbeat"), now)
        if agent_output_lines > 0:
            self.redis.set(self._key(fingerprint_id, "agent_output_lines"), str(agent_output_lines))

    def mark_completed(
        self,
        fingerprint_id: str,
        result: str,
        output_path: Optional[str] = None
    ):
        """
        Mark investigation as completed.

        Args:
            fingerprint_id: Fingerprint ID
            result: Investigation result (success, failed, ignored, timeout)
            output_path: Optional path to investigation output
        """
        now = datetime.now(timezone.utc).isoformat()

        # Determine status based on result
        status = self.STATUS_COMPLETED if result == self.RESULT_SUCCESS else self.STATUS_FAILED
        if result == self.RESULT_TIMEOUT:
            status = self.STATUS_TIMEOUT
        elif result == self.RESULT_IGNORED:
            status = self.STATUS_IGNORED

        self.redis.set(self._key(fingerprint_id, "status"), status)
        self.redis.set(self._key(fingerprint_id, "result"), result)
        self.redis.set(self._key(fingerprint_id, "completed_at"), now)
        self.redis.srem(self.ACTIVE_SET_KEY, fingerprint_id)

        logger.info(f"Marked investigation {fingerprint_id} as {status} ({result})")

    def get_active_count(self) -> int:
        """
        Get count of active investigations.

        Returns:
            Number of active investigations
        """
        return self.redis.scard(self.ACTIVE_SET_KEY)

    def get_all_active(self) -> List[str]:
        """
        Get list of all active investigation fingerprint IDs.

        Returns:
            List of fingerprint IDs
        """
        active = self.redis.smembers(self.ACTIVE_SET_KEY)
        return list(active) if active else []

    def get_queue_length(self) -> int:
        """
        Get number of queued investigations.

        Returns:
            Queue length
        """
        return self.redis.llen(self.QUEUE_KEY)

    def get_investigation_info(self, fingerprint_id: str) -> dict:
        """
        Get all investigation information.

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Dict with investigation data
        """
        return {
            "fingerprint_id": fingerprint_id,
            "status": self.get_status(fingerprint_id),
            "pid": self.redis.get(self._key(fingerprint_id, "pid")),
            "container_name": self.redis.get(self._key(fingerprint_id, "container_name")),
            "started_at": self.redis.get(self._key(fingerprint_id, "started_at")),
            "last_heartbeat": self.redis.get(self._key(fingerprint_id, "last_heartbeat")),
            "agent_output_lines": self.redis.get(self._key(fingerprint_id, "agent_output_lines")),
            "result": self.redis.get(self._key(fingerprint_id, "result")),
            "completed_at": self.redis.get(self._key(fingerprint_id, "completed_at")),
        }

    def cleanup_investigation(self, fingerprint_id: str):
        """
        Clean up Redis keys for investigation.

        Args:
            fingerprint_id: Fingerprint ID
        """
        keys_to_delete = [
            self._key(fingerprint_id, "pid"),
            self._key(fingerprint_id, "status"),
            self._key(fingerprint_id, "lock"),
            self._key(fingerprint_id, "started_at"),
            self._key(fingerprint_id, "last_heartbeat"),
            self._key(fingerprint_id, "agent_output_lines"),
            self._key(fingerprint_id, "result"),
            self._key(fingerprint_id, "completed_at"),
        ]

        for key in keys_to_delete:
            self.redis.delete(key)

        self.redis.srem(self.ACTIVE_SET_KEY, fingerprint_id)

        logger.info(f"Cleaned up investigation keys for {fingerprint_id}")

    def cleanup_orphaned_keys(self, fingerprint_ids: List[str]) -> int:
        """
        Cleanup orphaned Redis keys for deleted signatures.

        Args:
            fingerprint_ids: List of fingerprint IDs to clean up

        Returns:
            Number of investigations cleaned
        """
        cleaned_count = 0
        for fp_id in fingerprint_ids:
            self.cleanup_investigation(fp_id)
            cleaned_count += 1

        logger.info(f"Cleaned up {cleaned_count} orphaned investigation keys")
        return cleaned_count

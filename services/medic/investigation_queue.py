"""
Investigation Queue Manager with Redis-based Locking

Manages the queue of investigations and process lifecycle tracking.
"""

import logging
import redis
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta
import json

logger = logging.getLogger(__name__)


class InvestigationQueue:
    """
    Redis-based investigation queue with distributed locking.

    Redis Keys:
        medic:investigation:{fingerprint_id}:pid              # Process ID
        medic:investigation:{fingerprint_id}:status           # Status
        medic:investigation:{fingerprint_id}:lock             # Lock with TTL
        medic:investigation:{fingerprint_id}:started_at       # ISO timestamp
        medic:investigation:{fingerprint_id}:last_heartbeat   # ISO timestamp
        medic:investigation:{fingerprint_id}:agent_output_lines  # Progress counter
        medic:investigation:{fingerprint_id}:result           # Outcome
        medic:investigation:{fingerprint_id}:completed_at     # ISO timestamp
        medic:investigation:queue                             # List (queue)
        medic:investigation:active                            # Set of active fingerprint IDs
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
    RESULT_SUCCESS = "success"          # Successfully diagnosed and created fix plan
    RESULT_IGNORED = "ignored"          # Marked as not actionable
    RESULT_FAILED = "failed"            # Investigation failed/error
    RESULT_TIMEOUT = "timeout"          # Exceeded 4 hour timeout

    # Timeouts
    LOCK_TTL = 4 * 3600  # 4 hours
    STALL_THRESHOLD = 10 * 60  # 10 minutes
    HEARTBEAT_INTERVAL = 30  # 30 seconds
    MAX_CONCURRENT = 3  # Maximum concurrent investigations

    def __init__(self, redis_client: redis.Redis):
        """Initialize investigation queue with Redis client"""
        self.redis = redis_client
        logger.info("InvestigationQueue initialized")

    def _key(self, fingerprint_id: str, suffix: str) -> str:
        """Generate Redis key for investigation data"""
        return f"medic:investigation:{fingerprint_id}:{suffix}"

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
            self.redis.lpush("medic:investigation:queue", fingerprint_id)
        else:
            self.redis.rpush("medic:investigation:queue", fingerprint_id)

        logger.info(f"Enqueued investigation: {fingerprint_id} (priority: {priority})")
        return True

    def dequeue(self) -> Optional[str]:
        """
        Get next investigation from queue (blocking with 5s timeout).

        Returns:
            fingerprint_id or None if queue empty
        """
        result = self.redis.blpop("medic:investigation:queue", timeout=5)
        if result:
            _, fingerprint_id = result
            # Already decoded since redis client has decode_responses=True
            return fingerprint_id
        return None

    def acquire_lock(self, fingerprint_id: str) -> bool:
        """
        Acquire distributed lock for investigation.

        Returns:
            True if lock acquired, False if already locked
        """
        lock_key = self._key(fingerprint_id, "lock")
        # SET with NX (only if not exists) and EX (expiry)
        acquired = self.redis.set(lock_key, "locked", nx=True, ex=self.LOCK_TTL)

        if acquired:
            logger.info(f"Acquired lock for {fingerprint_id}")
        else:
            logger.warning(f"Failed to acquire lock for {fingerprint_id}")

        return bool(acquired)

    def release_lock(self, fingerprint_id: str):
        """Release distributed lock"""
        lock_key = self._key(fingerprint_id, "lock")
        self.redis.delete(lock_key)
        logger.info(f"Released lock for {fingerprint_id}")

    def update_status(self, fingerprint_id: str, status: str):
        """Update investigation status"""
        self.redis.set(self._key(fingerprint_id, "status"), status)
        logger.debug(f"Updated status for {fingerprint_id}: {status}")

    def get_status(self, fingerprint_id: str) -> Optional[str]:
        """Get current investigation status"""
        status = self.redis.get(self._key(fingerprint_id, "status"))
        return status.decode("utf-8") if status else None

    def set_pid(self, fingerprint_id: str, pid: int):
        """Set process ID for investigation"""
        self.redis.set(self._key(fingerprint_id, "pid"), str(pid))
        self.redis.sadd("medic:investigation:active", fingerprint_id)
        logger.info(f"Set PID {pid} for {fingerprint_id}")

    def get_pid(self, fingerprint_id: str) -> Optional[int]:
        """Get process ID for investigation"""
        pid = self.redis.get(self._key(fingerprint_id, "pid"))
        return int(pid) if pid else None

    def mark_started(self, fingerprint_id: str):
        """Mark investigation as started"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "started_at"), now)
        self.update_heartbeat(fingerprint_id)
        self.update_status(fingerprint_id, self.STATUS_IN_PROGRESS)

    def get_started_at(self, fingerprint_id: str) -> Optional[datetime]:
        """Get investigation start time"""
        started = self.redis.get(self._key(fingerprint_id, "started_at"))
        if started:
            return datetime.fromisoformat(started.decode("utf-8"))
        return None

    def update_heartbeat(self, fingerprint_id: str):
        """Update last heartbeat timestamp"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "last_heartbeat"), now)

    def get_last_heartbeat(self, fingerprint_id: str) -> Optional[datetime]:
        """Get last heartbeat timestamp"""
        heartbeat = self.redis.get(self._key(fingerprint_id, "last_heartbeat"))
        if heartbeat:
            return datetime.fromisoformat(heartbeat.decode("utf-8"))
        return None

    def set_output_lines(self, fingerprint_id: str, line_count: int):
        """Update agent output line count (for progress tracking)"""
        self.redis.set(self._key(fingerprint_id, "agent_output_lines"), str(line_count))

    def get_output_lines(self, fingerprint_id: str) -> int:
        """Get agent output line count"""
        lines = self.redis.get(self._key(fingerprint_id, "agent_output_lines"))
        return int(lines) if lines else 0

    def mark_completed(
        self, fingerprint_id: str, result: str, error_message: Optional[str] = None
    ):
        """
        Mark investigation as completed.

        Args:
            fingerprint_id: Investigation ID
            result: One of RESULT_* constants
            error_message: Optional error details
        """
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "completed_at"), now)
        self.redis.set(self._key(fingerprint_id, "result"), result)

        if error_message:
            self.redis.set(self._key(fingerprint_id, "error"), error_message)

        # Set final status based on result
        if result == self.RESULT_SUCCESS:
            status = self.STATUS_COMPLETED
        elif result == self.RESULT_IGNORED:
            status = self.STATUS_IGNORED
        elif result == self.RESULT_TIMEOUT:
            status = self.STATUS_TIMEOUT
        else:
            status = self.STATUS_FAILED

        self.update_status(fingerprint_id, status)

        # Remove from active set
        self.redis.srem("medic:investigation:active", fingerprint_id)

        # Release lock
        self.release_lock(fingerprint_id)

        logger.info(f"Marked investigation {fingerprint_id} as {status} ({result})")

    def get_investigation_info(self, fingerprint_id: str) -> Dict:
        """Get all information about an investigation"""
        info = {
            "fingerprint_id": fingerprint_id,
            "status": self.get_status(fingerprint_id),
            "pid": self.get_pid(fingerprint_id),
            "started_at": None,
            "last_heartbeat": None,
            "completed_at": None,
            "result": None,
            "error": None,
            "output_lines": self.get_output_lines(fingerprint_id),
        }

        # Get timestamps
        started = self.get_started_at(fingerprint_id)
        if started:
            info["started_at"] = started.isoformat()

        heartbeat = self.get_last_heartbeat(fingerprint_id)
        if heartbeat:
            info["last_heartbeat"] = heartbeat.isoformat()

        completed = self.redis.get(self._key(fingerprint_id, "completed_at"))
        if completed:
            info["completed_at"] = completed.decode("utf-8")

        result = self.redis.get(self._key(fingerprint_id, "result"))
        if result:
            info["result"] = result.decode("utf-8")

        error = self.redis.get(self._key(fingerprint_id, "error"))
        if error:
            info["error"] = error.decode("utf-8")

        return info

    def get_all_active(self) -> List[str]:
        """Get list of all active investigation fingerprint IDs"""
        active = self.redis.smembers("medic:investigation:active")
        return [fp.decode("utf-8") for fp in active]

    def get_queue_length(self) -> int:
        """Get number of queued investigations"""
        return self.redis.llen("medic:investigation:queue")

    def check_stalled(self, fingerprint_id: str) -> bool:
        """
        Check if investigation is stalled (no heartbeat for >10 minutes).

        Returns:
            True if stalled
        """
        heartbeat = self.get_last_heartbeat(fingerprint_id)
        if not heartbeat:
            return True

        elapsed = datetime.now(timezone.utc) - heartbeat
        return elapsed.total_seconds() > self.STALL_THRESHOLD

    def check_timeout(self, fingerprint_id: str) -> bool:
        """
        Check if investigation has exceeded 4-hour timeout.

        Returns:
            True if timed out
        """
        started = self.get_started_at(fingerprint_id)
        if not started:
            return False

        elapsed = datetime.now(timezone.utc) - started
        return elapsed.total_seconds() > self.LOCK_TTL

    def cleanup_investigation(self, fingerprint_id: str):
        """Delete all Redis keys for an investigation"""
        keys_to_delete = [
            self._key(fingerprint_id, "pid"),
            self._key(fingerprint_id, "status"),
            self._key(fingerprint_id, "lock"),
            self._key(fingerprint_id, "started_at"),
            self._key(fingerprint_id, "last_heartbeat"),
            self._key(fingerprint_id, "agent_output_lines"),
            self._key(fingerprint_id, "result"),
            self._key(fingerprint_id, "completed_at"),
            self._key(fingerprint_id, "error"),
        ]

        self.redis.delete(*keys_to_delete)
        self.redis.srem("medic:investigation:active", fingerprint_id)
        logger.info(f"Cleaned up Redis keys for {fingerprint_id}")

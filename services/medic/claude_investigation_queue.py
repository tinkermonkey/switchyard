"""
Claude Investigation Queue Manager with Redis-based Locking

Manages the queue of Claude failure investigations and process lifecycle tracking.
"""

import logging
import redis
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta
import json

logger = logging.getLogger(__name__)


class ClaudeInvestigationQueue:
    """
    Redis-based Claude investigation queue with distributed locking.

    Redis Keys:
        medic:claude_investigation:{fingerprint_id}:pid              # Process ID
        medic:claude_investigation:{fingerprint_id}:status           # Status
        medic:claude_investigation:{fingerprint_id}:lock             # Lock with TTL
        medic:claude_investigation:{fingerprint_id}:started_at       # ISO timestamp
        medic:claude_investigation:{fingerprint_id}:last_heartbeat   # ISO timestamp
        medic:claude_investigation:{fingerprint_id}:agent_output_lines  # Progress counter
        medic:claude_investigation:{fingerprint_id}:result           # Outcome
        medic:claude_investigation:{fingerprint_id}:completed_at     # ISO timestamp
        medic:claude_investigation:queue                             # List (queue)
        medic:claude_investigation:active                            # Set of active fingerprint IDs
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
        """Initialize Claude investigation queue with Redis client"""
        self.redis = redis_client
        logger.info("ClaudeInvestigationQueue initialized")

    def _key(self, fingerprint_id: str, suffix: str) -> str:
        """Generate Redis key for investigation data"""
        return f"medic:claude_investigation:{fingerprint_id}:{suffix}"

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
            logger.info(f"Claude investigation {fingerprint_id} already {status}, not re-enqueuing")
            return False

        # Set initial status
        self.redis.set(self._key(fingerprint_id, "status"), self.STATUS_QUEUED)

        # Add to queue based on priority
        if priority == "high":
            self.redis.lpush("medic:claude_investigation:queue", fingerprint_id)
        else:
            self.redis.rpush("medic:claude_investigation:queue", fingerprint_id)

        logger.info(f"Enqueued Claude investigation: {fingerprint_id} (priority: {priority})")
        return True

    def dequeue(self) -> Optional[str]:
        """
        Get next investigation from queue (blocking with 5s timeout).

        Returns:
            fingerprint_id or None if queue empty
        """
        result = self.redis.blpop("medic:claude_investigation:queue", timeout=5)
        if result:
            _, fingerprint_id = result
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
            logger.info(f"Acquired lock for Claude investigation {fingerprint_id}")
        else:
            logger.warning(f"Failed to acquire lock for Claude investigation {fingerprint_id}")

        return bool(acquired)

    def release_lock(self, fingerprint_id: str):
        """Release distributed lock"""
        lock_key = self._key(fingerprint_id, "lock")
        self.redis.delete(lock_key)
        logger.info(f"Released lock for Claude investigation {fingerprint_id}")

    def update_status(self, fingerprint_id: str, status: str):
        """Update investigation status"""
        self.redis.set(self._key(fingerprint_id, "status"), status)
        logger.debug(f"Updated status for Claude investigation {fingerprint_id}: {status}")

    def get_status(self, fingerprint_id: str) -> Optional[str]:
        """Get current investigation status"""
        status = self.redis.get(self._key(fingerprint_id, "status"))
        return status if status else None

    def set_pid(self, fingerprint_id: str, pid: int):
        """Set process ID for investigation"""
        self.redis.set(self._key(fingerprint_id, "pid"), str(pid))
        self.redis.sadd("medic:claude_investigation:active", fingerprint_id)
        logger.info(f"Set PID {pid} for Claude investigation {fingerprint_id}")

    def get_pid(self, fingerprint_id: str) -> Optional[int]:
        """Get process ID for investigation"""
        pid = self.redis.get(self._key(fingerprint_id, "pid"))
        return int(pid) if pid else None

    def remove_from_active(self, fingerprint_id: str):
        """Remove from active set"""
        self.redis.srem("medic:claude_investigation:active", fingerprint_id)

    def get_active(self) -> List[str]:
        """Get list of active investigation fingerprint IDs"""
        active = self.redis.smembers("medic:claude_investigation:active")
        return list(active)

    def record_heartbeat(self, fingerprint_id: str):
        """Record heartbeat timestamp"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "last_heartbeat"), now)

    def get_last_heartbeat(self, fingerprint_id: str) -> Optional[datetime]:
        """Get last heartbeat timestamp"""
        hb = self.redis.get(self._key(fingerprint_id, "last_heartbeat"))
        if hb:
            return datetime.fromisoformat(hb)
        return None

    def set_started_at(self, fingerprint_id: str):
        """Record investigation start time"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "started_at"), now)

    def get_started_at(self, fingerprint_id: str) -> Optional[datetime]:
        """Get investigation start time"""
        started = self.redis.get(self._key(fingerprint_id, "started_at"))
        if started:
            return datetime.fromisoformat(started)
        return None

    def set_completed_at(self, fingerprint_id: str):
        """Record investigation completion time"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "completed_at"), now)

    def get_completed_at(self, fingerprint_id: str) -> Optional[datetime]:
        """Get investigation completion time"""
        completed = self.redis.get(self._key(fingerprint_id, "completed_at"))
        if completed:
            return datetime.fromisoformat(completed)
        return None

    def set_result(self, fingerprint_id: str, result: str):
        """Set investigation result"""
        self.redis.set(self._key(fingerprint_id, "result"), result)
        logger.info(f"Set result for Claude investigation {fingerprint_id}: {result}")

    def get_result(self, fingerprint_id: str) -> Optional[str]:
        """Get investigation result"""
        result = self.redis.get(self._key(fingerprint_id, "result"))
        return result if result else None

    def update_progress(self, fingerprint_id: str, lines: int):
        """Update agent output line count (progress indicator)"""
        self.redis.set(self._key(fingerprint_id, "agent_output_lines"), str(lines))

    def get_progress(self, fingerprint_id: str) -> int:
        """Get agent output line count"""
        lines = self.redis.get(self._key(fingerprint_id, "agent_output_lines"))
        return int(lines) if lines else 0

    def is_stalled(self, fingerprint_id: str) -> bool:
        """
        Check if investigation has stalled (no heartbeat for 10+ minutes).

        Returns:
            True if stalled, False otherwise
        """
        last_hb = self.get_last_heartbeat(fingerprint_id)
        if not last_hb:
            return False

        elapsed = (datetime.now(timezone.utc) - last_hb).total_seconds()
        return elapsed > self.STALL_THRESHOLD

    def get_active_count(self) -> int:
        """Get count of active investigations"""
        return self.redis.scard("medic:claude_investigation:active")

    def can_start_new(self) -> bool:
        """Check if we can start a new investigation (under MAX_CONCURRENT)"""
        return self.get_active_count() < self.MAX_CONCURRENT

    def get_queue_length(self) -> int:
        """Get number of queued investigations"""
        return self.redis.llen("medic:claude_investigation:queue")

    def get_investigation_info(self, fingerprint_id: str) -> Dict:
        """
        Get complete investigation information.

        Returns:
            Dictionary with all investigation data
        """
        status = self.get_status(fingerprint_id)
        if not status:
            return {}

        info = {
            "fingerprint_id": fingerprint_id,
            "status": status,
            "pid": self.get_pid(fingerprint_id),
            "started_at": self.get_started_at(fingerprint_id).isoformat() if self.get_started_at(fingerprint_id) else None,
            "last_heartbeat": self.get_last_heartbeat(fingerprint_id).isoformat() if self.get_last_heartbeat(fingerprint_id) else None,
            "completed_at": self.get_completed_at(fingerprint_id).isoformat() if self.get_completed_at(fingerprint_id) else None,
            "result": self.get_result(fingerprint_id),
            "progress_lines": self.get_progress(fingerprint_id),
        }

        # Calculate duration if started
        if info["started_at"]:
            start = self.get_started_at(fingerprint_id)
            if info["completed_at"]:
                end = self.get_completed_at(fingerprint_id)
            else:
                end = datetime.now(timezone.utc)
            info["duration_seconds"] = (end - start).total_seconds()

        return info

    def cleanup_completed(self, older_than_hours: int = 24):
        """
        Clean up Redis data for completed investigations older than specified hours.

        Args:
            older_than_hours: Remove data for investigations completed this many hours ago
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)

        # Get all active fingerprint IDs (to check completed ones)
        all_active = self.get_active()

        cleaned = 0
        for fingerprint_id in all_active:
            completed_at = self.get_completed_at(fingerprint_id)
            if completed_at and completed_at < cutoff:
                # Remove all keys for this investigation
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
                self.redis.delete(*keys_to_delete)
                self.remove_from_active(fingerprint_id)
                cleaned += 1

        if cleaned > 0:
            logger.info(f"Cleaned up {cleaned} completed Claude investigations older than {older_than_hours}h")

        return cleaned

    def cleanup_orphaned_keys(self, fingerprint_ids: List[str]) -> int:
        """
        Remove Redis keys for deleted signatures.

        This removes all investigation-related keys for signatures that have been
        deleted from Elasticsearch due to inactivity.

        Args:
            fingerprint_ids: List of fingerprint IDs that were deleted

        Returns:
            Number of keys deleted
        """
        if not fingerprint_ids:
            return 0

        keys_deleted = 0

        for fp_id in fingerprint_ids:
            # Delete all investigation keys for this fingerprint
            keys_to_delete = [
                self._key(fp_id, "pid"),
                self._key(fp_id, "status"),
                self._key(fp_id, "lock"),
                self._key(fp_id, "started_at"),
                self._key(fp_id, "last_heartbeat"),
                self._key(fp_id, "agent_output_lines"),
                self._key(fp_id, "result"),
                self._key(fp_id, "completed_at"),
            ]

            deleted = self.redis.delete(*keys_to_delete)
            keys_deleted += deleted

            # Remove from active set
            self.redis.srem("medic:claude_investigation:active", fp_id)

        if keys_deleted > 0:
            logger.info(f"Cleaned up {keys_deleted} orphaned Redis keys for {len(fingerprint_ids)} deleted signatures")

        return keys_deleted


# Export
__all__ = ['ClaudeInvestigationQueue']

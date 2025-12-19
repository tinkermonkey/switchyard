"""
Claude Fix Execution Queue Manager with Redis-based Locking

Manages the queue of Claude fix executions and process lifecycle tracking.
"""

import logging
import redis
from typing import Optional, Dict, List
from datetime import datetime, timezone, timedelta
import json

logger = logging.getLogger(__name__)


class ClaudeFixExecutionQueue:
    """
    Redis-based Claude fix execution queue with distributed locking.

    Redis Keys:
        medic:claude_fix:{fingerprint_id}:pid              # Process ID
        medic:claude_fix:{fingerprint_id}:status           # Status
        medic:claude_fix:{fingerprint_id}:lock             # Lock with TTL
        medic:claude_fix:{fingerprint_id}:started_at       # ISO timestamp
        medic:claude_fix:{fingerprint_id}:last_heartbeat   # ISO timestamp
        medic:claude_fix:{fingerprint_id}:agent_output_lines  # Progress counter
        medic:claude_fix:{fingerprint_id}:result           # Outcome
        medic:claude_fix:{fingerprint_id}:completed_at     # ISO timestamp
        medic:claude_fix:queue                             # List (queue)
        medic:claude_fix:active                            # Set of active fingerprint IDs
    """

    # Status states
    STATUS_QUEUED = "queued"
    STATUS_STARTING = "starting"
    STATUS_IN_PROGRESS = "in_progress"
    STATUS_COMPLETED = "completed"
    STATUS_FAILED = "failed"

    # Results
    RESULT_SUCCESS = "success"
    RESULT_FAILED = "failed"
    RESULT_PARTIAL = "partial"  # e.g. code applied but restart skipped

    # Timeouts
    LOCK_TTL = 2 * 3600  # 2 hours
    STALL_THRESHOLD = 10 * 60  # 10 minutes
    MAX_CONCURRENT = 2  # Conservative limit for fixes

    def __init__(self, redis_client: redis.Redis):
        """Initialize Claude fix execution queue with Redis client"""
        self.redis = redis_client
        logger.info("ClaudeFixExecutionQueue initialized")

    def _key(self, fingerprint_id: str, suffix: str) -> str:
        """Generate Redis key for fix execution data"""
        return f"medic:claude_fix:{fingerprint_id}:{suffix}"

    def enqueue(self, fingerprint_id: str, project: str = None, fix_plan_path: str = None) -> bool:
        """
        Add fix execution to queue if not already queued/in-progress.

        Args:
            fingerprint_id: Failure signature ID
            project: Project name (optional)
            fix_plan_path: Path to fix plan file (optional)

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
            logger.info(f"Claude fix {fingerprint_id} already {status}, not re-enqueuing")
            return False

        # Store details if provided
        if project:
            self.redis.set(self._key(fingerprint_id, "project"), project)
        if fix_plan_path:
            self.redis.set(self._key(fingerprint_id, "fix_plan_path"), fix_plan_path)

        # Set initial status
        self.redis.set(self._key(fingerprint_id, "status"), self.STATUS_QUEUED)

        # Add to queue
        self.redis.rpush("medic:claude_fix:queue", fingerprint_id)

        logger.info(f"Enqueued Claude fix execution: {fingerprint_id}")
        return True

    def get_next_pending_fix(self) -> Optional[Dict]:
        """
        Get next pending fix from queue with details.
        
        Returns:
            Dict with fix details or None if queue empty
        """
        fingerprint_id = self.dequeue()
        if not fingerprint_id:
            return None
            
        # Get details
        project = self.redis.get(self._key(fingerprint_id, "project"))
        fix_plan_path = self.redis.get(self._key(fingerprint_id, "fix_plan_path"))
        
        return {
            "fingerprint_id": fingerprint_id,
            "project": project,
            "fix_plan_path": fix_plan_path
        }

    def dequeue(self) -> Optional[str]:
        """
        Get next fix execution from queue (blocking with 5s timeout).

        Returns:
            fingerprint_id or None if queue empty
        """
        result = self.redis.blpop("medic:claude_fix:queue", timeout=5)
        if result:
            _, fingerprint_id = result
            return fingerprint_id
        return None

    def acquire_lock(self, fingerprint_id: str) -> bool:
        """
        Acquire distributed lock for fix execution.

        Returns:
            True if lock acquired, False if already locked
        """
        lock_key = self._key(fingerprint_id, "lock")
        # SET with NX (only if not exists) and EX (expiry)
        acquired = self.redis.set(lock_key, "locked", nx=True, ex=self.LOCK_TTL)

        if acquired:
            logger.info(f"Acquired lock for Claude fix {fingerprint_id}")
        else:
            logger.warning(f"Failed to acquire lock for Claude fix {fingerprint_id}")

        return bool(acquired)

    def release_lock(self, fingerprint_id: str):
        """Release distributed lock"""
        lock_key = self._key(fingerprint_id, "lock")
        self.redis.delete(lock_key)
        logger.info(f"Released lock for Claude fix {fingerprint_id}")

    def update_status(self, fingerprint_id: str, status: str, **kwargs):
        """
        Update fix status and optional metadata.
        
        Args:
            fingerprint_id: Failure signature ID
            status: New status
            **kwargs: Additional fields to update (e.g. error, log_file)
        """
        self.redis.set(self._key(fingerprint_id, "status"), status)
        
        # Store additional fields
        for key, value in kwargs.items():
            if value is not None:
                self.redis.set(self._key(fingerprint_id, key), str(value))
                
        logger.debug(f"Updated status for Claude fix {fingerprint_id}: {status}")

    def get_status(self, fingerprint_id: str) -> Optional[str]:
        """Get current fix status"""
        status = self.redis.get(self._key(fingerprint_id, "status"))
        return status if status else None

    def set_pid(self, fingerprint_id: str, pid: int):
        """Set process ID for fix execution"""
        self.redis.set(self._key(fingerprint_id, "pid"), str(pid))
        self.redis.sadd("medic:claude_fix:active", fingerprint_id)
        logger.info(f"Set PID {pid} for Claude fix {fingerprint_id}")

    def get_pid(self, fingerprint_id: str) -> Optional[int]:
        """Get process ID for fix execution"""
        pid = self.redis.get(self._key(fingerprint_id, "pid"))
        return int(pid) if pid else None

    def remove_from_active(self, fingerprint_id: str):
        """Remove from active set"""
        self.redis.srem("medic:claude_fix:active", fingerprint_id)

    def get_active(self) -> List[str]:
        """Get list of active fix fingerprint IDs"""
        active = self.redis.smembers("medic:claude_fix:active")
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
        """Record fix start time"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "started_at"), now)

    def get_started_at(self, fingerprint_id: str) -> Optional[datetime]:
        """Get fix start time"""
        started = self.redis.get(self._key(fingerprint_id, "started_at"))
        if started:
            return datetime.fromisoformat(started)
        return None

    def set_completed_at(self, fingerprint_id: str):
        """Record fix completion time"""
        now = datetime.now(timezone.utc).isoformat()
        self.redis.set(self._key(fingerprint_id, "completed_at"), now)

    def get_completed_at(self, fingerprint_id: str) -> Optional[datetime]:
        """Get fix completion time"""
        completed = self.redis.get(self._key(fingerprint_id, "completed_at"))
        if completed:
            return datetime.fromisoformat(completed)
        return None

    def set_result(self, fingerprint_id: str, result: str):
        """Set fix result"""
        self.redis.set(self._key(fingerprint_id, "result"), result)
        logger.info(f"Set result for Claude fix {fingerprint_id}: {result}")

    def get_result(self, fingerprint_id: str) -> Optional[str]:
        """Get fix result"""
        result = self.redis.get(self._key(fingerprint_id, "result"))
        return result if result else None

    def update_progress(self, fingerprint_id: str, lines: int):
        """Update agent output line count (progress indicator)"""
        self.redis.set(self._key(fingerprint_id, "agent_output_lines"), str(lines))

    def get_progress(self, fingerprint_id: str) -> int:
        """Get agent output line count"""
        lines = self.redis.get(self._key(fingerprint_id, "agent_output_lines"))
        return int(lines) if lines else 0

    def get_active_count(self) -> int:
        """Get count of active fixes"""
        return self.redis.scard("medic:claude_fix:active")

    def can_start_new(self) -> bool:
        """Check if we can start a new fix (under MAX_CONCURRENT)"""
        return self.get_active_count() < self.MAX_CONCURRENT

    def get_fix_info(self, fingerprint_id: str) -> Dict:
        """
        Get complete fix execution information.

        Returns:
            Dictionary with all fix data
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
            "log_file": self.redis.get(self._key(fingerprint_id, "log_file")),
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
        Clean up Redis data for completed fixes older than specified hours.

        Args:
            older_than_hours: Remove data for fixes completed this many hours ago
        """
        cutoff = datetime.now(timezone.utc) - timedelta(hours=older_than_hours)

        # Get all active fingerprint IDs (to check completed ones)
        all_active = self.get_active()

        cleaned = 0
        for fingerprint_id in all_active:
            completed_at = self.get_completed_at(fingerprint_id)
            if completed_at and completed_at < cutoff:
                # Remove all keys for this fix
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
            logger.info(f"Cleaned up {cleaned} completed Claude fixes older than {older_than_hours}h")

        return cleaned


# Export
__all__ = ['ClaudeFixExecutionQueue']

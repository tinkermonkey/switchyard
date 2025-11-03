"""
Pipeline Lock Manager

Manages exclusive locks for pipeline execution to prevent concurrent work
on multiple issues within the same pipeline (project + board).

Only ONE issue can hold the pipeline lock at a time. Other issues wait in queue.
"""

import yaml
import redis
import logging
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class PipelineLock:
    """Exclusive lock for pipeline execution"""
    project: str
    board: str
    locked_by_issue: int
    lock_acquired_at: str
    lock_status: str  # 'locked', 'unlocked'


class PipelineLockManager:
    """Manages pipeline execution locks with Redis + YAML persistence"""

    def __init__(self, state_dir: Path = None, redis_client=None):
        """
        Initialize pipeline lock manager.

        Args:
            state_dir: Directory for YAML state persistence
            redis_client: Optional Redis client (will create if not provided)
        """
        if state_dir is None:
            import os
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "pipeline_locks"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Initialize Redis client
        self.redis_client = redis_client
        if self.redis_client is None:
            try:
                self.redis_client = redis.Redis(
                    host='redis',
                    port=6379,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                self.redis_client.ping()
                logger.info("Connected to Redis for pipeline locks")
            except Exception as e:
                logger.warning(f"Redis connection failed for locks, using YAML only: {e}")
                self.redis_client = None

        logger.info(f"PipelineLockManager initialized with state_dir: {state_dir}")

    def _get_lock_key(self, project: str, board: str) -> str:
        """Get Redis key for lock"""
        return f"pipeline_lock:{project}:{board}"

    def _get_state_file(self, project: str, board: str) -> Path:
        """Get YAML state file path for lock"""
        return self.state_dir / f"{project}_{board}.yaml"

    def get_lock(self, project: str, board: str) -> Optional[PipelineLock]:
        """
        Get current lock state for a pipeline.

        Returns:
            PipelineLock if locked, None if unlocked
        """
        # Try Redis first
        if self.redis_client:
            try:
                lock_data = self.redis_client.hgetall(self._get_lock_key(project, board))
                if lock_data and lock_data.get('lock_status') == 'locked':
                    return PipelineLock(
                        project=lock_data['project'],
                        board=lock_data['board'],
                        locked_by_issue=int(lock_data['locked_by_issue']),
                        lock_acquired_at=lock_data['lock_acquired_at'],
                        lock_status=lock_data['lock_status']
                    )
            except Exception as e:
                logger.warning(f"Failed to get lock from Redis: {e}")

        # Fallback to YAML
        state_file = self._get_state_file(project, board)
        if state_file.exists():
            try:
                with open(state_file, 'r') as f:
                    lock_data = yaml.safe_load(f)
                    if lock_data and lock_data.get('lock_status') == 'locked':
                        return PipelineLock(**lock_data)
            except Exception as e:
                logger.error(f"Failed to load lock state from YAML: {e}")

        return None

    def try_acquire_lock(
        self,
        project: str,
        board: str,
        issue_number: int
    ) -> Tuple[bool, str]:
        """
        Attempt to acquire pipeline lock with safety checks.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number attempting to acquire lock

        Returns:
            (can_execute: bool, reason: str)
        """
        lock = self.get_lock(project, board)

        # Case 1: No existing lock - acquire immediately
        if not lock or lock.lock_status == 'unlocked':
            self._create_lock(project, board, issue_number)
            return True, "lock_acquired"

        # Case 2: Lock held by THIS issue - already has access
        if lock.locked_by_issue == issue_number:
            logger.debug(f"Issue #{issue_number} already holds lock for {project}/{board}")
            return True, "already_holds_lock"

        # Case 3: Lock held by another issue - check if lock is stale
        try:
            lock_acquired_time = datetime.fromisoformat(lock.lock_acquired_at)
            lock_age = datetime.now(timezone.utc) - lock_acquired_time

            # Stale lock threshold: 2 hours
            if lock_age > timedelta(hours=2):
                logger.warning(
                    f"Stale lock detected for {project}/{board} "
                    f"(held by #{lock.locked_by_issue} for {lock_age})"
                )

                # Auto-release stale lock and acquire
                logger.info(
                    f"Auto-releasing stale lock (issue #{lock.locked_by_issue})"
                )
                self.release_lock(project, board, lock.locked_by_issue)
                self._create_lock(project, board, issue_number)
                return True, "stale_lock_recovered"
        except Exception as e:
            logger.warning(f"Failed to check lock age: {e}")

        # Case 4: Lock held by another issue (not stale)
        logger.debug(
            f"Pipeline {project}/{board} locked by issue #{lock.locked_by_issue}, "
            f"issue #{issue_number} must wait"
        )
        return False, f"locked_by_issue_{lock.locked_by_issue}"

    def _create_lock(self, project: str, board: str, issue_number: int):
        """Create a new lock"""
        lock = PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked'
        )

        # Write to Redis with 2 hour TTL
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                lock_data = asdict(lock)
                self.redis_client.hset(lock_key, mapping=lock_data)
                self.redis_client.expire(lock_key, 7200)  # 2 hours
                logger.debug(f"Created lock in Redis: {lock_key}")
            except Exception as e:
                logger.error(f"Failed to create lock in Redis: {e}")

        # Write to YAML for persistence
        self._save_lock_to_yaml(lock)

        logger.info(
            f"Pipeline lock acquired: {project}/{board} by issue #{issue_number}"
        )

    def release_lock(self, project: str, board: str, issue_number: int) -> bool:
        """
        Release pipeline lock.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number releasing the lock

        Returns:
            True if lock was released, False if not held by this issue
        """
        lock = self.get_lock(project, board)

        # Safety check: Verify this issue actually holds the lock
        if lock and lock.locked_by_issue != issue_number:
            logger.warning(
                f"Issue #{issue_number} attempted to release lock for {project}/{board} "
                f"but lock is held by #{lock.locked_by_issue}"
            )
            return False

        # Remove from Redis
        if self.redis_client:
            try:
                self.redis_client.delete(self._get_lock_key(project, board))
                logger.debug(f"Deleted lock from Redis: {project}/{board}")
            except Exception as e:
                logger.error(f"Failed to delete lock from Redis: {e}")

        # Update YAML to unlocked state
        state_file = self._get_state_file(project, board)
        if state_file.exists():
            try:
                state_file.unlink()
                logger.debug(f"Deleted lock YAML file: {state_file}")
            except Exception as e:
                logger.error(f"Failed to delete lock YAML: {e}")

        logger.info(
            f"Pipeline lock released: {project}/{board} by issue #{issue_number}"
        )
        return True

    def _save_lock_to_yaml(self, lock: PipelineLock):
        """Save lock state to YAML file"""
        state_file = self._get_state_file(lock.project, lock.board)
        try:
            with open(state_file, 'w') as f:
                yaml.dump(asdict(lock), f, default_flow_style=False, sort_keys=False)
            logger.debug(f"Saved lock to YAML: {state_file}")
        except Exception as e:
            logger.error(f"Failed to save lock to YAML: {e}")

    def get_all_locks(self) -> list[PipelineLock]:
        """
        Get all active locks (for monitoring/recovery).

        Returns:
            List of all active PipelineLock objects
        """
        locks = []

        # Scan YAML files
        for state_file in self.state_dir.glob("*.yaml"):
            try:
                with open(state_file, 'r') as f:
                    lock_data = yaml.safe_load(f)
                    if lock_data and lock_data.get('lock_status') == 'locked':
                        locks.append(PipelineLock(**lock_data))
            except Exception as e:
                logger.error(f"Failed to load lock from {state_file}: {e}")

        return locks


# Singleton instance
_pipeline_lock_manager = None


def get_pipeline_lock_manager() -> PipelineLockManager:
    """Get singleton instance of PipelineLockManager"""
    global _pipeline_lock_manager
    if _pipeline_lock_manager is None:
        _pipeline_lock_manager = PipelineLockManager()
    return _pipeline_lock_manager

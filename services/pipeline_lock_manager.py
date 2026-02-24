"""
Pipeline Lock Manager

Manages exclusive locks for pipeline execution to prevent concurrent work
on multiple issues within the same pipeline (project + board).

Only ONE issue can hold the pipeline lock at a time. Other issues wait in queue.
"""

import yaml
import redis
import logging
import os
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
                redis_host = os.environ.get('REDIS_HOST', 'redis')
                redis_port = int(os.environ.get('REDIS_PORT', 6379))
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                self.redis_client.ping()
                logger.info(f"Connected to Redis at {redis_host}:{redis_port} for pipeline locks")
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
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if state_file.exists():
            try:
                # Use file lock when reading to prevent reading partial writes
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(lock_file):
                    if state_file.exists():  # Check again inside lock
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
        # Try to acquire via Redis using atomic transaction (WATCH/MULTI)
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                
                # Use pipeline for optimistic locking
                with self.redis_client.pipeline() as pipe:
                    while True:
                        try:
                            # Watch the lock key for changes
                            pipe.watch(lock_key)
                            
                            # Check if lock exists
                            if pipe.exists(lock_key):
                                # Lock exists - check who owns it
                                # We must execute this read immediately (not in transaction)
                                # But pipe is in watch mode, so commands are buffered? 
                                # No, in redis-py pipeline, commands are buffered unless we call execute()
                                # But we need to read the value to decide.
                                # Standard pattern: pipe.watch(key); val = pipe.get(key); ...
                                
                                # We need a separate client or break out of pipeline to read?
                                # No, the pipeline object acts as a client.
                                # But in redis-py, calling methods on pipeline buffers them.
                                # EXCEPT when using watch, we can read before multi().
                                
                                # Actually, let's just read it.
                                # pipe.watch(lock_key) puts us in watch mode.
                                # We can't read with 'pipe' and get result immediately?
                                # Yes we can, before multi().
                                
                                # Wait, redis-py pipeline behavior:
                                # "When using a pipeline, commands are buffered..."
                                # But we need to read.
                                # Correct pattern:
                                # pipe.watch(key)
                                # current_value = pipe.hgetall(key) # This might return the pipeline object, not result?
                                # No, standard redis-py pipeline does not return results immediately.
                                
                                # We should use the callback form of transaction or just use the client for reading.
                                # But we need to watch.
                                pass
                                
                            # Let's use the transaction method which is cleaner in redis-py
                            # self.redis_client.transaction(func, *keys)
                            
                            def acquire_lock_tx(pipe):
                                lock_data = pipe.hgetall(lock_key)
                                
                                if lock_data and lock_data.get('lock_status') == 'locked':
                                    # Lock exists
                                    locked_by = int(lock_data.get('locked_by_issue', 0))
                                    if locked_by == issue_number:
                                        # Already held by us - refresh TTL
                                        pipe.multi()
                                        pipe.expire(lock_key, 7200)
                                        return "already_holds_lock"
                                    
                                    # Check for stale lock
                                    try:
                                        lock_acquired_at = lock_data.get('lock_acquired_at')
                                        if lock_acquired_at:
                                            acquired_time = datetime.fromisoformat(lock_acquired_at)
                                            lock_age = datetime.now(timezone.utc) - acquired_time
                                            if lock_age > timedelta(hours=4):
                                                # Stale - overwrite it
                                                # Proceed to acquire logic below
                                                pass
                                            else:
                                                # Locked by someone else
                                                return f"locked_by_issue_{locked_by}"
                                        else:
                                            return f"locked_by_issue_{locked_by}"
                                    except Exception:
                                        return f"locked_by_issue_{locked_by}"
                                
                                # Not locked or stale - acquire it
                                new_lock = PipelineLock(
                                    project=project,
                                    board=board,
                                    locked_by_issue=issue_number,
                                    lock_acquired_at=datetime.now(timezone.utc).isoformat(),
                                    lock_status='locked'
                                )
                                
                                pipe.multi()
                                pipe.hset(lock_key, mapping=asdict(new_lock))
                                pipe.expire(lock_key, 7200)
                                return "lock_acquired"

                            result = self.redis_client.transaction(acquire_lock_tx, lock_key, value_from_callable=True)
                            
                            # If we got here, transaction succeeded (or returned early)
                            if result in ["lock_acquired", "already_holds_lock", "stale_lock_recovered"]:
                                # We acquired/held the lock in Redis. Now sync to YAML.
                                # Note: There is a small window where Redis has lock but YAML doesn't.
                                # This is acceptable as Redis is primary.
                                self._create_lock_yaml_only(project, board, issue_number)
                                return True, result
                            else:
                                return False, result

                        except redis.WatchError:
                            # Lock changed while we were watching - retry loop
                            continue
                            
            except Exception as e:
                logger.warning(f"Redis lock acquisition failed, falling back to YAML: {e}")
                # Fall through to YAML fallback

        # Fallback to YAML (original logic, but only if Redis failed or not available)
        # Note: If Redis is available but we failed to acquire (locked by other), we returned False above.
        # We only reach here if self.redis_client is None or Redis threw an exception (connection error).
        
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

            # Stale lock threshold: 4 hours
            if lock_age > timedelta(hours=4):
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

    def _create_lock_yaml_only(self, project: str, board: str, issue_number: int):
        """Create lock in YAML only (helper for Redis sync)"""
        lock = PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked'
        )
        self._save_lock_to_yaml(lock)

    def _create_lock(self, project: str, board: str, issue_number: int):
        """Create a new lock (Legacy/Fallback method)"""
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
        Release pipeline lock safely.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number releasing the lock

        Returns:
            True if lock was released, False if not held by this issue
        """
        # Atomic release via Redis
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                
                with self.redis_client.pipeline() as pipe:
                    while True:
                        try:
                            pipe.watch(lock_key)
                            
                            # Check if lock exists and who owns it
                            # We need to read inside the watch block
                            # Since we can't easily read-and-branch in a pipeline without custom logic,
                            # we'll use the transaction callback pattern again or just read.
                            
                            # Read directly (watched)
                            # Note: In redis-py, if we watch a key, we can read it with the client (not pipe)
                            # or use the transaction callback.
                            
                            def release_lock_tx(pipe):
                                lock_data = pipe.hgetall(lock_key)
                                if not lock_data:
                                    # Lock doesn't exist - nothing to release
                                    return "not_found"
                                
                                locked_by = int(lock_data.get('locked_by_issue', 0))
                                if locked_by != issue_number:
                                    # Held by someone else
                                    return "held_by_other"
                                
                                # Held by us - delete it
                                pipe.multi()
                                pipe.delete(lock_key)
                                return "released"

                            result = self.redis_client.transaction(release_lock_tx, lock_key, value_from_callable=True)
                            
                            if result == "held_by_other":
                                logger.warning(
                                    f"Issue #{issue_number} attempted to release lock for {project}/{board} "
                                    f"but it is held by another issue"
                                )
                                return False
                            elif result == "not_found":
                                # Already gone, consider it success (idempotent)
                                logger.debug(f"Lock for {project}/{board} already gone during release by #{issue_number}")
                                # Fall through to clean up YAML just in case
                                break
                            else:
                                logger.debug(f"Deleted lock from Redis: {project}/{board}")
                                break
                                
                        except redis.WatchError:
                            continue
                            
            except Exception as e:
                logger.error(f"Failed to delete lock from Redis: {e}")

        # Update YAML to unlocked state (Fallback/Sync)
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if state_file.exists():
            try:
                # Use file lock when deleting to prevent race with writers
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(lock_file):
                    if state_file.exists():  # Check again inside lock
                        # Double check ownership in YAML if we didn't check Redis (e.g. Redis down)
                        # If Redis was up, we already validated ownership or deleted it.
                        # If Redis was down, we must validate against YAML.
                        
                        should_delete = True
                        if not self.redis_client: # Only check YAML content if Redis wasn't used
                            try:
                                with open(state_file, 'r') as f:
                                    lock_data = yaml.safe_load(f)
                                    if lock_data and int(lock_data.get('locked_by_issue', 0)) != issue_number:
                                        should_delete = False
                                        logger.warning(f"YAML lock held by {lock_data.get('locked_by_issue')} != {issue_number}")
                            except Exception:
                                pass # If read fails, assume safe to delete or let it be?
                        
                        if should_delete:
                            state_file.unlink()
                            logger.debug(f"Deleted lock YAML file: {state_file}")
                        else:
                            return False
                            
            except Exception as e:
                logger.error(f"Failed to delete lock YAML: {e}")

        logger.info(
            f"Pipeline lock released: {project}/{board} by issue #{issue_number}"
        )
        return True

    def _save_lock_to_yaml(self, lock: PipelineLock):
        """Save lock state to YAML file with thread-safe file locking"""
        from utils.file_lock import safe_yaml_write

        state_file = self._get_state_file(lock.project, lock.board)
        try:
            with safe_yaml_write(state_file):
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

    def sync_yaml_locks_to_redis(self) -> int:
        """
        Sync all YAML locks to Redis during startup recovery.

        This ensures Redis (source of truth) matches YAML persistence
        after orchestrator restart when Redis may have been cleared.

        IMPORTANT: This has a known race condition if multiple orchestrator
        instances start simultaneously. Proper fix requires leader election.

        Returns:
            Number of locks synced to Redis
        """
        if not self.redis_client:
            logger.warning("Cannot sync locks to Redis - Redis not available")
            return 0

        synced_count = 0
        skipped_count = 0
        locks = self.get_all_locks()  # Read from YAML

        for lock in locks:
            try:
                lock_key = self._get_lock_key(lock.project, lock.board)

                # Validate lock age - don't sync stale locks (older than 4 hours)
                from datetime import datetime, timezone, timedelta
                lock_age_threshold = datetime.now(timezone.utc) - timedelta(hours=4)
                lock_acquired_time = datetime.fromisoformat(lock.lock_acquired_at.replace('Z', '+00:00'))

                if lock_acquired_time < lock_age_threshold:
                    logger.warning(
                        f"Skipping stale lock sync: {lock.project}/{lock.board} "
                        f"held by issue #{lock.locked_by_issue} (age: {datetime.now(timezone.utc) - lock_acquired_time})"
                    )
                    skipped_count += 1
                    continue

                # Check if lock already exists in Redis
                existing_lock = self.redis_client.hgetall(lock_key)

                if not existing_lock:
                    # Lock missing in Redis - sync it
                    lock_data = asdict(lock)
                    self.redis_client.hset(lock_key, mapping=lock_data)
                    self.redis_client.expire(lock_key, 7200)  # 2 hour TTL
                    logger.info(
                        f"Synced lock to Redis: {lock.project}/{lock.board} "
                        f"held by issue #{lock.locked_by_issue}"
                    )
                    synced_count += 1
                else:
                    # Lock exists in Redis - don't overwrite to avoid conflicts
                    existing_holder = existing_lock.get(b'locked_by_issue', existing_lock.get('locked_by_issue'))
                    logger.warning(
                        f"Lock already in Redis: {lock.project}/{lock.board} "
                        f"(Redis: issue #{existing_holder}, YAML: issue #{lock.locked_by_issue}) - "
                        f"not overwriting to prevent race condition"
                    )
                    skipped_count += 1
            except Exception as e:
                logger.error(f"Failed to sync lock to Redis: {e}")

        if skipped_count > 0:
            logger.info(f"Lock sync summary: {synced_count} synced, {skipped_count} skipped")

        return synced_count

    def get_lock_holder(self, project: str, board: str) -> Optional[int]:
        """
        Get the issue number that currently holds the lock for a pipeline.

        Args:
            project: Project name
            board: Board name

        Returns:
            Issue number holding the lock, or None if unlocked
        """
        lock = self.get_lock(project, board)
        return lock.locked_by_issue if lock else None

    def is_locked_by_issue(self, project: str, board: str, issue_number: int) -> bool:
        """
        Check if a specific issue currently holds the lock.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number to check

        Returns:
            True if the issue holds the lock, False otherwise
        """
        lock_holder = self.get_lock_holder(project, board)
        return lock_holder == issue_number if lock_holder is not None else False

    def get_lock_status_for_issue(
        self,
        project: str,
        board: str,
        issue_number: int
    ) -> str:
        """
        Get the lock status for a specific issue in a pipeline.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number to check

        Returns:
            'holding_lock' - Issue currently holds the lock
            'waiting_for_lock' - Issue is waiting, another issue holds lock
            'no_lock' - Pipeline is unlocked
        """
        lock = self.get_lock(project, board)

        if not lock:
            return 'no_lock'

        if lock.locked_by_issue == issue_number:
            return 'holding_lock'
        else:
            return 'waiting_for_lock'


# Singleton instance
_pipeline_lock_manager = None


def get_pipeline_lock_manager() -> PipelineLockManager:
    """Get singleton instance of PipelineLockManager"""
    global _pipeline_lock_manager
    if _pipeline_lock_manager is None:
        _pipeline_lock_manager = PipelineLockManager()
    return _pipeline_lock_manager

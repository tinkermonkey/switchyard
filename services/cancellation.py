"""
Cancellation Signal Registry

Provides a shared cancellation mechanism for deliberately stopping work on an issue.
When a user kills a pipeline run, kills an agent, or moves a ticket to an exit column,
this registry ensures all retry/recovery layers respect the stop signal and don't
respawn agents.

Key design:
- Cancellation unit is (project, issue_number) — not container or pipeline_run_id
- Redis-backed with in-memory fallback for current-process cancellations
- CancellationError bypasses all retry loops and is never counted as a circuit breaker failure
- Signal TTL of 1 hour is a safety net; normal flow clears signals explicitly
"""

import logging
import subprocess
import json
from typing import Optional

logger = logging.getLogger(__name__)


class CancellationError(Exception):
    """Raised when work is deliberately cancelled.

    This exception propagates through all retry loops without being caught
    by generic `except Exception` retry handlers (callers must catch it first).
    It is never counted as a circuit breaker failure.
    """
    pass


class CancellationSignal:
    """Central registry for cancellation signals, keyed by (project, issue_number).

    Uses Redis as primary store with in-memory set as fallback when Redis is unavailable.
    """

    _REDIS_KEY_PREFIX = "cancelled"
    _TTL_SECONDS = 3600  # 1 hour safety net

    def __init__(self):
        self._in_memory: set = set()
        self._redis = None

    def _get_redis(self):
        """Lazy Redis connection (tolerates Redis being unavailable)."""
        if self._redis is None:
            try:
                import redis
                self._redis = redis.Redis(host='redis', port=6379, decode_responses=True)
                self._redis.ping()
            except Exception:
                self._redis = None
        return self._redis

    def _key(self, project: str, issue_number: int) -> str:
        return f"{self._REDIS_KEY_PREFIX}:{project}:{issue_number}"

    def cancel(self, project: str, issue_number: int, reason: str = "") -> None:
        """Set cancellation signal for an issue."""
        key = self._key(project, issue_number)
        self._in_memory.add((project, issue_number))

        redis_client = self._get_redis()
        if redis_client:
            try:
                redis_client.setex(key, self._TTL_SECONDS, reason or "cancelled")
                logger.info(f"Set cancellation signal in Redis: {key} (reason: {reason})")
            except Exception as e:
                logger.warning(f"Failed to set cancellation signal in Redis: {e}")
        else:
            logger.info(f"Set cancellation signal in memory: {project}/#{issue_number} (reason: {reason})")

    def is_cancelled(self, project: str, issue_number: int) -> bool:
        """Check if an issue has been cancelled."""
        # Check in-memory first (fast path)
        if (project, issue_number) in self._in_memory:
            return True

        # Check Redis
        redis_client = self._get_redis()
        if redis_client:
            try:
                if redis_client.exists(self._key(project, issue_number)):
                    # Sync to in-memory for faster future checks
                    self._in_memory.add((project, issue_number))
                    return True
            except Exception as e:
                logger.warning(f"Failed to check cancellation signal in Redis: {e}")

        return False

    def clear(self, project: str, issue_number: int) -> None:
        """Remove cancellation signal, allowing work to be re-triggered."""
        self._in_memory.discard((project, issue_number))

        redis_client = self._get_redis()
        if redis_client:
            try:
                redis_client.delete(self._key(project, issue_number))
                logger.info(f"Cleared cancellation signal: {project}/#{issue_number}")
            except Exception as e:
                logger.warning(f"Failed to clear cancellation signal in Redis: {e}")


# Singleton
_cancellation_signal: Optional[CancellationSignal] = None


def get_cancellation_signal() -> CancellationSignal:
    """Get the singleton CancellationSignal instance."""
    global _cancellation_signal
    if _cancellation_signal is None:
        _cancellation_signal = CancellationSignal()
    return _cancellation_signal


def kill_containers_for_issue(project: str, issue_number: int) -> int:
    """Find and kill all Docker containers for the given issue.

    Searches both Redis tracking keys and Docker labels to find containers.

    Returns:
        Number of containers killed.
    """
    killed = 0

    # Strategy 1: Find containers via Redis agent:container:* keys
    try:
        signal = get_cancellation_signal()
        redis_client = signal._get_redis()
        if not redis_client:
            raise Exception("Redis unavailable via singleton")

        for key in redis_client.scan_iter(match='agent:container:*', count=100):
            try:
                data = redis_client.hgetall(key)
                container_project = data.get('project', '')
                container_issue = data.get('issue_number', '')

                if container_project == project and str(container_issue) == str(issue_number):
                    container_name = data.get('container_name', key.split(':')[-1])
                    logger.info(f"Killing container {container_name} for {project}/#{issue_number} (found via Redis)")
                    try:
                        subprocess.run(
                            ['docker', 'rm', '-f', container_name],
                            capture_output=True, text=True, timeout=10
                        )
                        redis_client.delete(key)
                        killed += 1
                    except Exception as e:
                        logger.warning(f"Failed to kill container {container_name}: {e}")
            except Exception as e:
                logger.warning(f"Error processing Redis key {key}: {e}")

        # Also check repair cycle containers
        for key in redis_client.scan_iter(match=f'repair_cycle:container:{project}:{issue_number}', count=10):
            try:
                container_name = redis_client.get(key)
                if container_name:
                    logger.info(f"Killing repair cycle container {container_name} for {project}/#{issue_number}")
                    subprocess.run(
                        ['docker', 'rm', '-f', container_name],
                        capture_output=True, text=True, timeout=10
                    )
                    redis_client.delete(key)
                    killed += 1
            except Exception as e:
                logger.warning(f"Error killing repair cycle container: {e}")

    except Exception as e:
        logger.warning(f"Redis unavailable for container lookup, falling back to Docker labels: {e}")

    # Strategy 2: Find containers via Docker labels (fallback / catches missed containers)
    try:
        result = subprocess.run(
            ['docker', 'ps', '-q',
             '--filter', f'label=org.clauditoreum.project={project}',
             '--filter', f'label=org.clauditoreum.issue_number={issue_number}'],
            capture_output=True, text=True, timeout=10
        )

        if result.returncode == 0 and result.stdout.strip():
            container_ids = result.stdout.strip().split('\n')
            for container_id in container_ids:
                container_id = container_id.strip()
                if container_id:
                    logger.info(f"Killing container {container_id} for {project}/#{issue_number} (found via Docker labels)")
                    try:
                        subprocess.run(
                            ['docker', 'rm', '-f', container_id],
                            capture_output=True, text=True, timeout=10
                        )
                        killed += 1
                    except Exception as e:
                        logger.warning(f"Failed to kill container {container_id}: {e}")
    except Exception as e:
        logger.warning(f"Docker label-based container lookup failed: {e}")

    logger.info(f"Killed {killed} containers for {project}/#{issue_number}")
    return killed


def cancel_issue_work(project: str, issue_number: int, reason: str) -> None:
    """Full orchestrated cancellation for an issue.

    1. Set cancellation signal
    2. Kill all Docker containers for the issue
    3. Remove active review cycle
    4. Mark in-progress executions as cancelled
    """
    logger.warning(f"CANCELLING work for {project}/#{issue_number}: {reason}")

    # 1. Set cancellation signal (must be first — stops retry loops)
    signal = get_cancellation_signal()
    signal.cancel(project, issue_number, reason)

    # 2. Kill all Docker containers
    kill_containers_for_issue(project, issue_number)

    # 3. Remove active review cycle (if any)
    try:
        from services.review_cycle import review_cycle_executor
        if issue_number in review_cycle_executor.active_cycles:
            del review_cycle_executor.active_cycles[issue_number]
            logger.info(f"Removed active review cycle for issue #{issue_number}")
    except Exception as e:
        logger.warning(f"Failed to remove active review cycle: {e}")

    # 4. Mark in-progress executions as cancelled
    try:
        from services.work_execution_state import work_execution_tracker
        execution_history = work_execution_tracker.get_execution_history(project, issue_number)

        for execution in reversed(execution_history):
            if execution.get('outcome') == 'in_progress':
                agent = execution.get('agent')
                column = execution.get('column')
                work_execution_tracker.record_execution_outcome(
                    issue_number=issue_number,
                    column=column,
                    agent=agent,
                    outcome='cancelled',
                    project_name=project,
                    error=reason
                )
                logger.info(f"Marked execution as cancelled: {agent} in {column}")
    except Exception as e:
        logger.warning(f"Failed to mark executions as cancelled: {e}")

    logger.info(f"Cancellation complete for {project}/#{issue_number}")

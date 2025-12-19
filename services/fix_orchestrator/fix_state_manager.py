"""
Fix State Manager

Manages startup state recovery for the fix orchestrator service.
Detects and cleans up stale state from previous runs (orphaned containers, stale locks, etc.).
"""

import logging
import docker

logger = logging.getLogger(__name__)


class FixStateManager:
    """Manages startup state recovery for fix orchestrator."""

    def __init__(self, queue, runner, redis_client):
        """
        Initialize state manager.

        Args:
            queue: ClaudeFixExecutionQueue instance
            runner: ClaudeFixAgentRunner instance
            redis_client: Redis client instance
        """
        self.queue = queue
        self.runner = runner
        self.redis_client = redis_client
        self.docker_client = docker.from_env()

    def recover_on_startup(self):
        """
        Detect and clean stale state from previous runs.

        Returns:
            dict: Recovery statistics
        """
        logger.info("Starting state recovery...")

        stats = {
            "stale_in_progress": 0,
            "orphaned_containers": 0,
            "stale_locks": 0
        }

        # 1. Find stale in_progress fixes
        try:
            active_keys = self.redis_client.keys("fix:active:*")
            logger.info(f"Found {len(active_keys)} active fix keys in Redis")

            for key in active_keys:
                try:
                    fix_info = self.redis_client.hgetall(key)
                    container_name = fix_info.get('container_name', '')
                    fingerprint_id = fix_info.get('fingerprint_id')

                    if not fingerprint_id:
                        # Invalid entry, delete it
                        logger.warning(f"Invalid active fix entry: {key}")
                        self.redis_client.delete(key)
                        stats["stale_in_progress"] += 1
                        continue

                    # Check if container still exists and is running
                    if container_name and not self._is_container_running(container_name):
                        # Container gone but Redis state says active
                        logger.info(f"Stale in_progress fix detected: {fingerprint_id} (container {container_name} not running)")
                        self.queue.update_status(fingerprint_id, 'failed', error="Service restart - orphaned state")
                        self.queue.release_lock(fingerprint_id)
                        self.redis_client.delete(key)
                        stats["stale_in_progress"] += 1

                except Exception as e:
                    logger.error(f"Error processing active fix {key}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error finding stale in_progress fixes: {e}", exc_info=True)

        # 2. Find orphaned containers (no Redis state)
        try:
            running_fix_containers = self.docker_client.containers.list(
                filters={"name": "fix_agent_"}
            )
            logger.info(f"Found {len(running_fix_containers)} fix agent containers")

            for container in running_fix_containers:
                try:
                    container_name = container.name
                    fingerprint_id = self._extract_fingerprint_from_container_name(container_name)

                    if fingerprint_id and not self.redis_client.exists(f"fix:active:{fingerprint_id}"):
                        # Container running but no Redis state
                        logger.info(f"Orphaned container detected: {container_name}")
                        container.stop(timeout=10)
                        stats["orphaned_containers"] += 1

                except Exception as e:
                    logger.error(f"Error processing container {container.name}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error finding orphaned containers: {e}", exc_info=True)

        # 3. Release stale locks (older than 2 hours or with no TTL)
        try:
            lock_keys = self.redis_client.keys("medic:fix:execution:lock:*")
            logger.info(f"Found {len(lock_keys)} fix execution locks")

            for key in lock_keys:
                try:
                    ttl = self.redis_client.ttl(key)
                    if ttl < 0:  # No TTL or expired
                        logger.info(f"Releasing stale lock: {key}")
                        self.redis_client.delete(key)
                        stats["stale_locks"] += 1

                except Exception as e:
                    logger.error(f"Error processing lock {key}: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Error releasing stale locks: {e}", exc_info=True)

        logger.info(f"State recovery complete: {stats}")
        return stats

    def _is_container_running(self, container_name):
        """
        Check if a container is currently running.

        Args:
            container_name: Name of the container

        Returns:
            bool: True if container exists and is running
        """
        try:
            container = self.docker_client.containers.get(container_name)
            return container.status == 'running'
        except docker.errors.NotFound:
            return False
        except Exception as e:
            logger.warning(f"Error checking container {container_name}: {e}")
            return False

    def _extract_fingerprint_from_container_name(self, container_name):
        """
        Extract fingerprint ID from container name.

        Container names follow pattern: fix_agent_{short_id}
        We need to look up the full fingerprint from Redis.

        Args:
            container_name: Container name (e.g., "fix_agent_ab021abfeeac")

        Returns:
            str: Fingerprint ID or None if not found
        """
        try:
            # Check if there's a mapping in Redis
            # We'll store this mapping when creating containers
            mapping_key = f"container:{container_name}"
            fingerprint_id = self.redis_client.get(mapping_key)
            return fingerprint_id
        except Exception as e:
            logger.warning(f"Error extracting fingerprint from {container_name}: {e}")
            return None

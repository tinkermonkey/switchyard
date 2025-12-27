"""
Base Investigation Orchestrator

Abstract base class for investigation orchestration with recovery and monitoring.
Provides unified lifecycle management for both Docker and Claude investigation systems.
"""

import logging
import asyncio
import subprocess
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from elasticsearch import Elasticsearch
import redis

from monitoring.observability import get_observability_manager, EventType
from monitoring.timestamp_utils import utc_now, parse_iso_timestamp, utc_isoformat

logger = logging.getLogger(__name__)


class BaseInvestigationOrchestrator(ABC):
    """
    Abstract base class for investigation orchestration.

    Provides common lifecycle management, queue processing, heartbeat monitoring,
    and recovery logic. Subclasses customize investigation context and validation.

    Responsibilities:
    - Process investigation queue
    - Launch and monitor investigation processes
    - Track progress via heartbeats
    - Detect stalls and timeouts
    - Perform startup recovery
    - Trigger auto-investigations based on thresholds
    - Emit observability events
    """

    # Recovery thresholds
    WAIT_THRESHOLD = 30 * 60  # 30 minutes - wait before re-launch
    TIMEOUT_THRESHOLD = 4 * 3600  # 4 hours - mark as timeout
    RELAUNCH_GRACE_PERIOD = 5 * 60  # 5 minutes - grace period after restart

    def __init__(
        self,
        redis_client: redis.Redis,
        es_client: Elasticsearch,
        queue,
        agent_runner,
        report_manager,
        failure_store,
        workspace_root: str = "/workspace/clauditoreum",
        medic_dir: str = "/medic",
    ):
        """
        Initialize investigation orchestrator.

        Args:
            redis_client: Redis client for queue management
            es_client: Elasticsearch client for signature data
            queue: Investigation queue instance (BaseInvestigationQueue subclass)
            agent_runner: Agent runner instance (BaseInvestigationAgentRunner subclass)
            report_manager: Report manager instance (BaseReportManager subclass)
            failure_store: Failure signature store instance (BaseFailureSignatureStore subclass)
            workspace_root: Path to Clauditoreum codebase
            medic_dir: Base directory for investigation reports
        """
        self.redis = redis_client
        self.es = es_client
        self.queue = queue
        self.agent_runner = agent_runner
        self.report_manager = report_manager
        self.failure_store = failure_store
        self.workspace_root = workspace_root
        self.medic_dir = medic_dir
        self.observability = get_observability_manager()

        self.running = True  # Set to True immediately to avoid race condition
        self.active_processes = {}  # fingerprint_id -> investigation_info

        logger.info(f"{self.__class__.__name__} initialized")

    def _check_container_running(self, container_name: str) -> bool:
        """
        Verify container is actually running in Docker.

        Args:
            container_name: Name of the container to check

        Returns:
            True if container is running, False otherwise
        """
        try:
            result = subprocess.run(
                ['docker', 'ps', '-q', '-f', f'name=^{container_name}$'],
                capture_output=True,
                text=True,
                timeout=5
            )
            return bool(result.stdout.strip())
        except Exception as e:
            logger.error(f"Failed to check container {container_name}: {e}")
            return False

    async def _cleanup_orphaned_investigation(self, fingerprint_id: str):
        """
        Clean up investigation marked active but container doesn't exist.

        Args:
            fingerprint_id: Fingerprint ID of orphaned investigation
        """
        logger.warning(f"Orphaned investigation detected: {fingerprint_id}")

        try:
            # Check if reports exist (maybe it completed but state not updated)
            status = self.report_manager.get_report_status(fingerprint_id)
            if status.get('has_diagnosis'):
                # Reports exist - mark as completed
                self.queue.mark_completed(fingerprint_id, "success", es_store=self.failure_store)
                logger.info(f"Orphaned investigation {fingerprint_id} had reports - marked completed")

                # Emit event
                try:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_COMPLETED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "result": "success",
                            "orphaned_cleanup": True,
                            "had_reports": True
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit observability event: {e}")
            else:
                # No reports - mark as failed
                self.queue.mark_completed(fingerprint_id, "failed", es_store=self.failure_store)
                logger.error(f"Orphaned investigation {fingerprint_id} failed - no reports found")

                # Emit event
                try:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_FAILED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "result": "failed",
                            "orphaned_cleanup": True,
                            "had_reports": False
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit observability event: {e}")

            # Remove from active_processes if present
            if fingerprint_id in self.active_processes:
                del self.active_processes[fingerprint_id]

        except Exception as e:
            logger.error(f"Error cleaning up orphaned investigation {fingerprint_id}: {e}", exc_info=True)

    async def rebuild_redis_from_es(self):
        """
        Rebuild all Redis state from Elasticsearch on startup (ES-FIRST architecture).

        ES is the source of truth - Redis is just a performance cache.
        This method clears Redis and rebuilds queues/active sets from ES data.
        """
        logger.info("Rebuilding Redis state from Elasticsearch (ES-first)...")

        try:
            # Clear all Redis state
            self.redis.delete(self.queue.QUEUE_KEY)
            self.redis.delete(self.queue.ACTIVE_SET_KEY)
            logger.info("Cleared Redis queue and active set")

            # Query ES for all investigations with non-terminal statuses
            query = {
                "query": {
                    "terms": {
                        "investigation_status": ["queued", "starting", "in_progress", "stalled"]
                    }
                },
                "size": 1000,
                "_source": ["fingerprint_id", "investigation_status", "investigation_metadata"]
            }

            # Search across all index patterns
            index_patterns = [f"{self.failure_store.INDEX_PREFIX}-*"]

            queued_count = 0
            active_count = 0

            for index_pattern in index_patterns:
                try:
                    result = self.es.search(index=index_pattern, body=query, ignore=[404])

                    for hit in result.get('hits', {}).get('hits', []):
                        fp_id = hit['_source']['fingerprint_id']
                        status = hit['_source']['investigation_status']
                        metadata = hit['_source'].get('investigation_metadata', {})

                        if status == 'queued':
                            # Add to queue (status is in ES, no need to update Redis)
                            self.redis.rpush(self.queue.QUEUE_KEY, fp_id)
                            queued_count += 1
                            logger.debug(f"Rebuilt queued: {fp_id[:16]}...")

                        elif status in ['starting', 'in_progress', 'stalled']:
                            # Add to active set (status is in ES, no need to update Redis)
                            self.redis.sadd(self.queue.ACTIVE_SET_KEY, fp_id)
                            active_count += 1

                            # Check if container actually running
                            container_name = metadata.get('container_name')
                            if container_name:
                                # Restore container metadata to Redis
                                if metadata.get('started_at'):
                                    self.redis.set(self.queue._key(fp_id, "started_at"), metadata['started_at'])
                                if metadata.get('last_heartbeat'):
                                    self.redis.set(self.queue._key(fp_id, "last_heartbeat"), metadata['last_heartbeat'])
                                self.redis.set(self.queue._key(fp_id, "container_name"), container_name)

                                # Verify container is actually running
                                if not self._check_container_running(container_name):
                                    logger.warning(
                                        f"Rebuilt {fp_id[:16]}... but container {container_name} not running - will clean up"
                                    )
                                    await self._mark_investigation_failed(
                                        fp_id,
                                        reason="container_not_found_on_startup"
                                    )
                                else:
                                    logger.debug(f"Rebuilt active: {fp_id[:16]}... (container: {container_name})")
                            else:
                                logger.warning(f"Rebuilt {fp_id[:16]}... but no container_name - will mark failed")
                                await self._mark_investigation_failed(
                                    fp_id,
                                    reason="no_container_name_in_metadata"
                                )

                except Exception as e:
                    logger.debug(f"Could not search {index_pattern}: {e}")

            logger.info(f"Rebuilt Redis from ES: {queued_count} queued, {active_count} active")

        except Exception as e:
            logger.error(f"Error rebuilding Redis from ES: {e}", exc_info=True)

    async def _mark_investigation_failed(self, fingerprint_id: str, reason: str):
        """
        Mark investigation as failed in both ES and Redis (ES-first).

        Args:
            fingerprint_id: Fingerprint ID
            reason: Failure reason
        """
        from monitoring.timestamp_utils import utc_isoformat

        # ES-first update
        success = self.failure_store.update_investigation_status_es_first(
            fingerprint_id,
            status="failed",
            metadata={
                "result": "failed",
                "error_message": reason,
                "completed_at": utc_isoformat()
            }
        )

        if success:
            # Update Redis tracking data (best effort)
            try:
                self.redis.set(self.queue._key(fingerprint_id, "result"), self.queue.RESULT_FAILED)
                self.redis.set(self.queue._key(fingerprint_id, "completed_at"), utc_isoformat())
                self.redis.srem(self.queue.ACTIVE_SET_KEY, fingerprint_id)
            except Exception as e:
                logger.warning(f"Redis update failed for {fingerprint_id}, will be reconciled: {e}")
        else:
            logger.error(f"Failed to mark {fingerprint_id} as failed in ES")

    async def _restore_active_processes(self):
        """
        Restore active investigations from Redis into active_processes dict.

        Called on startup to resume monitoring investigations that were running
        when the orchestrator last shut down.
        """
        active_fps = self.queue.get_all_active()
        logger.info(f"Restoring {len(active_fps)} active investigations from Redis")

        restored = 0
        cleaned = 0

        for fp_id in active_fps:
            try:
                info = self.queue.get_investigation_info(fp_id)
                container_name = info.get('container_name')

                if not container_name:
                    logger.warning(f"Investigation {fp_id} has no container_name - marking as failed")
                    self.queue.mark_completed(fp_id, "failed", es_store=self.failure_store)
                    cleaned += 1
                    continue

                # Check if container is actually running
                if not self._check_container_running(container_name):
                    logger.warning(f"Investigation {fp_id} container {container_name} not running - cleaning up")
                    await self._cleanup_orphaned_investigation(fp_id)
                    cleaned += 1
                    continue

                # Container is running - restore to active_processes
                # Note: We can't restore the asyncio Task, but monitoring will still work
                self.active_processes[fp_id] = {
                    'container_name': container_name,
                    'started_at': info.get('started_at'),
                    'task': None,  # Task can't be restored, but monitoring will still work
                }
                restored += 1
                logger.info(f"Restored active investigation {fp_id} (container: {container_name})")

            except Exception as e:
                logger.error(f"Error restoring investigation {fp_id}: {e}", exc_info=True)
                cleaned += 1

        logger.info(f"Restoration complete: {restored} restored, {cleaned} cleaned")

    @abstractmethod
    def _prepare_investigation_context(
        self, fingerprint_id: str, signature: Dict[str, Any]
    ) -> Tuple[str, str]:
        """
        Prepare investigation context file and output path.

        Args:
            fingerprint_id: Fingerprint ID
            signature: Signature document from Elasticsearch

        Returns:
            Tuple of (context_file_path, output_log_path)
        """
        pass

    @abstractmethod
    def _validate_investigation_result(self, fingerprint_id: str) -> Tuple[str, str]:
        """
        Validate investigation result and determine outcome.

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Tuple of (result, status) where:
            - result: "success", "ignored", "failed", "timeout"
            - status: "completed", "ignored", "failed", "timeout"
        """
        pass

    @abstractmethod
    def _get_observability_data(
        self, fingerprint_id: str, signature: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Get observability event data for this investigation.

        Args:
            fingerprint_id: Fingerprint ID
            signature: Signature document

        Returns:
            Dict with observability event data
        """
        pass

    async def start(self):
        """Start the investigation orchestrator"""
        logger.info(f"Starting {self.__class__.__name__}...")

        # Check Claude Code CLI availability
        claude_version = self.agent_runner.get_claude_version()
        if not claude_version:
            logger.error("Claude Code CLI not available - investigations will fail")
        else:
            logger.info(f"Claude Code CLI available: {claude_version}")

        # NEW ES-FIRST ARCHITECTURE: Rebuild Redis from Elasticsearch
        logger.info("Rebuilding Redis from Elasticsearch (ES-first architecture)...")
        await self.rebuild_redis_from_es()

        # Restore active processes from Redis (after rebuild)
        logger.info("Restoring active processes from Redis...")
        await self._restore_active_processes()

        # Perform startup recovery for any remaining issues
        logger.info("Performing startup recovery...")
        await self._recover_stalled_investigations()

        # Reconcile stuck queued investigations
        logger.info("Reconciling queued investigations...")
        await self._reconcile_queued_investigations()

        # Reconcile stale Elasticsearch investigation statuses
        logger.info("Reconciling Elasticsearch investigation statuses...")
        await self._reconcile_elasticsearch_statuses()

        self.running = True
        logger.info(f"{self.__class__.__name__} initialized and ready")

        # Create background tasks using native asyncio instead of APScheduler
        # This avoids event loop context issues
        asyncio.create_task(self._queue_processor_loop())
        asyncio.create_task(self._heartbeat_monitor_loop())
        asyncio.create_task(self._auto_trigger_checker_loop())
        asyncio.create_task(self._reconciliation_loop())
        logger.info(f"Started 4 background tasks")

        # Keep this task alive with infinite wait
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled")
            self.running = False
            self.scheduler.shutdown()
            raise

    async def stop(self):
        """Stop the orchestrator and cleanup"""
        logger.info(f"Stopping {self.__class__.__name__}...")
        self.running = False

        # Cancel all active investigation tasks
        for fingerprint_id, investigation_info in list(self.active_processes.items()):
            try:
                task = investigation_info.get('task')
                if task and not task.done():
                    logger.info(f"Cancelling investigation {fingerprint_id}")
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        logger.info(f"Investigation {fingerprint_id} cancelled")

                # Mark as completed (updates ES)
                self.queue.mark_completed(
                    fingerprint_id,
                    self.queue.RESULT_FAILED,
                    es_store=self.failure_store
                )
            except Exception as e:
                logger.error(f"Error stopping investigation {fingerprint_id}: {e}")

        self.active_processes.clear()
        logger.info(f"{self.__class__.__name__} stopped")

    async def queue_processor(self):
        """Legacy method - background tasks now use scheduled iteration methods"""
        logger.warning("queue_processor() called but tasks are now scheduled - this should not be called")
        pass

    async def _queue_processor_iteration(self):
        """Process investigation queue (single iteration)"""
        try:
            # Check concurrent limit
            active_count = self.queue.get_active_count()

            if active_count >= self.queue.MAX_CONCURRENT:
                logger.debug(f"Max concurrent investigations reached ({active_count}), waiting...")
                return

            # Get next investigation from queue
            fingerprint_id = await self.queue.dequeue()

            if not fingerprint_id:
                # Queue empty
                return

            # Start investigation
            logger.info(f"Dequeued investigation: {fingerprint_id}")
            await self._start_investigation(fingerprint_id)

        except Exception as e:
            logger.error(f"Queue processor error: {e}", exc_info=True)

    async def _queue_processor_loop(self):
        """Background loop for queue processing"""
        while self.running:
            try:
                await self._queue_processor_iteration()
            except Exception as e:
                logger.error(f"Queue processor loop error: {e}", exc_info=True)
            await asyncio.sleep(1)

    async def _heartbeat_monitor_loop(self):
        """Background loop for heartbeat monitoring"""
        while self.running:
            try:
                await self._heartbeat_monitor_iteration()
            except Exception as e:
                logger.error(f"Heartbeat monitor loop error: {e}", exc_info=True)
            await asyncio.sleep(60)

    async def _auto_trigger_checker_loop(self):
        """Background loop for auto trigger checking"""
        while self.running:
            try:
                await self._auto_trigger_checker_iteration()
            except Exception as e:
                logger.error(f"Auto trigger checker loop error: {e}", exc_info=True)
            await asyncio.sleep(30)

    async def _reconciliation_loop(self):
        """Background loop for reconciliation (runs every 30 seconds)"""
        while self.running:
            try:
                await self._reconciliation_iteration()
            except Exception as e:
                logger.error(f"Reconciliation loop error: {e}", exc_info=True)
            await asyncio.sleep(30)

    def _update_investigation_status_atomic(
        self,
        fingerprint_id: str,
        redis_status: str,
        es_status: str,
        add_to_active: bool = False,
        remove_from_active: bool = False
    ) -> bool:
        """
        Atomically update investigation status in both Redis and Elasticsearch.

        Updates Redis first (source of truth), then updates Elasticsearch with retry.
        If Elasticsearch update fails after retries, creates an inconsistency marker
        in Redis for later reconciliation.

        Args:
            fingerprint_id: Fingerprint ID
            redis_status: Status value for Redis status key
            es_status: Status value for Elasticsearch investigation_status field
            add_to_active: Whether to add to Redis active set
            remove_from_active: Whether to remove from Redis active set

        Returns:
            True if both Redis and ES updated successfully, False if ES failed
        """
        try:
            # Phase 1: Update Redis (source of truth)
            self.queue.update_status(fingerprint_id, redis_status)

            if add_to_active:
                self.redis.sadd(f"{self.queue.KEY_PREFIX}:active", fingerprint_id)

            if remove_from_active:
                self.redis.srem(f"{self.queue.KEY_PREFIX}:active", fingerprint_id)

            logger.debug(
                f"Updated Redis status for {fingerprint_id[:16]}... to {redis_status}"
            )

            # Phase 2: Update Elasticsearch with retry
            es_success = self.failure_store.update_investigation_status(
                fingerprint_id, es_status
            )

            if es_success:
                # Both updates succeeded
                return True
            else:
                # Elasticsearch update failed after retries
                logger.warning(
                    f"INCONSISTENT STATE: Redis updated but Elasticsearch failed for {fingerprint_id[:16]}... "
                    f"(Redis: {redis_status}, ES target: {es_status})"
                )

                # Create inconsistency marker for reconciliation
                marker_key = f"medic:inconsistent_status:{fingerprint_id}"
                self.redis.setex(marker_key, 300, es_status)  # 5 minute TTL

                return False

        except Exception as e:
            logger.error(
                f"Error in atomic status update for {fingerprint_id[:16]}...: {e}",
                exc_info=True
            )
            return False

    async def _start_investigation(self, fingerprint_id: str):
        """Start an investigation for a fingerprint (ES-FIRST)"""
        logger.info(f"Starting investigation for {fingerprint_id}")

        try:
            # ES-FIRST: Update to "starting" in ES before doing anything
            now = utc_isoformat()
            success = self.failure_store.update_investigation_status_es_first(
                fingerprint_id,
                status="starting",
                metadata={"started_at": now},
                expected_current=["queued"]
            )

            if not success:
                logger.error(f"Failed to update ES to 'starting' for {fingerprint_id}")
                return

            # Redis tracking is best-effort and not authoritative
            # (Status is in ES only)

            # Get signature data from Elasticsearch
            signature = self.failure_store.get_signature(fingerprint_id)
            if not signature:
                logger.error(f"Signature not found: {fingerprint_id}")
                await self._mark_investigation_failed(fingerprint_id, "signature_not_found")
                return

            # Prepare context (subclass-specific)
            context_file, output_log = self._prepare_investigation_context(
                fingerprint_id, signature
            )

            # Emit agent_initialized event and get execution_id for tracking
            agent_execution_id = self.observability.emit_agent_initialized(
                agent=self.agent_runner._get_investigation_agent_name(),
                task_id=fingerprint_id[:16],
                project="medic",
                config={
                    "fingerprint_id": fingerprint_id,
                    "investigation_type": self.__class__.__name__,
                    "model": self.agent_runner._get_claude_model()
                },
                container_name=f"claude-agent-clauditoreum-{fingerprint_id[:16]}"
            )

            # Launch investigation process with execution_id
            investigation_info = await self.agent_runner.launch_investigation(
                fingerprint_id=fingerprint_id,
                context_file=context_file,
                output_log=output_log,
                observability_manager=self.observability,
                agent_execution_id=agent_execution_id,
            )

            if not investigation_info:
                logger.error(f"Failed to launch investigation for {fingerprint_id}")
                await self._mark_investigation_failed(fingerprint_id, "launch_failed")
                return

            # Track active investigation
            self.active_processes[fingerprint_id] = investigation_info
            logger.info(f"Tracked active investigation {fingerprint_id}")

            # ES-FIRST: Update to "in_progress" with container info
            container_name = investigation_info.get('container_name')
            now = utc_isoformat()

            success = self.failure_store.update_investigation_status_es_first(
                fingerprint_id,
                status="in_progress",
                metadata={
                    "container_name": container_name,
                    "last_heartbeat": now
                },
                expected_current=["starting"]
            )

            if not success:
                logger.error(f"Failed to update ES to 'in_progress' for {fingerprint_id}")
                return

            # Update Redis tracking metadata (best effort, not authoritative)
            try:
                self.redis.set(self.queue._key(fingerprint_id, "pid"), "0")
                if container_name:
                    self.redis.set(self.queue._key(fingerprint_id, "container_name"), container_name)
                if agent_execution_id:
                    self.redis.set(self.queue._key(fingerprint_id, "agent_execution_id"), agent_execution_id)
                self.redis.set(self.queue._key(fingerprint_id, "started_at"), now)
                self.redis.set(self.queue._key(fingerprint_id, "last_heartbeat"), now)
                self.redis.sadd(self.queue.ACTIVE_SET_KEY, fingerprint_id)
            except Exception as e:
                logger.warning(f"Redis update failed: {e}, will be reconciled")

            # Set up completion callback
            def done_callback(task):
                """Schedule completion handler in the event loop"""
                try:
                    # Pass investigation_info to completion handler for agent_execution_id
                    asyncio.create_task(self._handle_investigation_completion(fingerprint_id, task, investigation_info))
                except Exception as e:
                    logger.error(f"Error scheduling investigation completion for {fingerprint_id}: {e}", exc_info=True)

            investigation_info['task'].add_done_callback(done_callback)

            # Emit event
            try:
                event_data = self._get_observability_data(fingerprint_id, signature)
                self.observability.emit(
                    EventType.MEDIC_INVESTIGATION_STARTED,
                    agent="medic-investigator",
                    task_id=fingerprint_id,
                    project=event_data.get("project", "medic"),
                    data=event_data,
                )
            except Exception as e:
                logger.warning(f"Failed to emit observability event: {e}")

            logger.info(f"Investigation started: {fingerprint_id}")

        except Exception as e:
            logger.error(f"Error starting investigation {fingerprint_id}: {e}", exc_info=True)
            # ES-FIRST: Mark as failed
            await self._mark_investigation_failed(
                fingerprint_id,
                reason=f"Exception during start: {str(e)}"
            )

    async def _handle_investigation_completion(self, fingerprint_id: str, task: asyncio.Task, investigation_info: Dict = None):
        """
        Handle completion of an investigation (ES-FIRST).

        Args:
            fingerprint_id: Fingerprint ID
            task: Completed task
            investigation_info: Optional dict with investigation metadata including agent_execution_id
        """
        try:
            # Remove from active processes
            if fingerprint_id in self.active_processes:
                del self.active_processes[fingerprint_id]

            # Check if task succeeded or failed
            if task.exception():
                logger.error(f"Investigation {fingerprint_id} failed with exception: {task.exception()}")
                result = self.queue.RESULT_FAILED
                es_status = "failed"
            else:
                # Validate result (subclass-specific)
                result, redis_status = self._validate_investigation_result(fingerprint_id)
                logger.info(f"Investigation {fingerprint_id} completed with result: {result}")

                # Map to ES status
                es_status_map = {
                    self.queue.STATUS_COMPLETED: "completed",
                    self.queue.STATUS_IGNORED: "ignored",
                    self.queue.STATUS_FAILED: "failed",
                    self.queue.STATUS_TIMEOUT: "timeout",
                }
                es_status = es_status_map.get(redis_status, "failed")

            # ES-FIRST: Update ES with completion metadata
            now = utc_isoformat()
            success = self.failure_store.update_investigation_status_es_first(
                fingerprint_id,
                status=es_status,
                metadata={
                    "result": result,
                    "completed_at": now
                },
                expected_current=["in_progress", "stalled", "starting"]
            )

            if not success:
                logger.error(f"Failed to update ES for completed investigation {fingerprint_id}")

            # Update Redis tracking data (best effort)
            try:
                self.redis.set(self.queue._key(fingerprint_id, "result"), result)
                self.redis.set(self.queue._key(fingerprint_id, "completed_at"), now)
                self.redis.srem(self.queue.ACTIVE_SET_KEY, fingerprint_id)
            except Exception as e:
                logger.warning(f"Redis update failed: {e}, will be reconciled")

            # Emit completion event
            try:
                if result in [self.queue.RESULT_SUCCESS, self.queue.RESULT_IGNORED]:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_COMPLETED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "result": result,
                        },
                    )
                else:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_FAILED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "reason": result,
                        },
                    )

                # Emit agent execution completion event for UI tracking
                if investigation_info and investigation_info.get('agent_execution_id'):
                    agent_execution_id = investigation_info['agent_execution_id']
                    if result in [self.queue.RESULT_SUCCESS, self.queue.RESULT_IGNORED]:
                        self.observability.emit_agent_completed(
                            agent_execution_id=agent_execution_id,
                            outputs={"result": result, "status": es_status}
                        )
                    else:
                        self.observability.emit_agent_failed(
                            agent_execution_id=agent_execution_id,
                            error=result
                        )
            except Exception as e:
                logger.warning(f"Failed to emit observability event: {e}")

        except Exception as e:
            logger.error(
                f"Error handling completion for {fingerprint_id}: {e}", exc_info=True
            )

    async def heartbeat_monitor(self):
        """Legacy method - background tasks now use scheduled iteration methods"""
        logger.warning("heartbeat_monitor() called but tasks are now scheduled - this should not be called")
        pass

    async def _heartbeat_monitor_iteration(self):
        """Monitor active investigations (single iteration)"""
        try:
            # Check Redis active set (persistent), not active_processes (volatile)
            active_fps = self.queue.get_all_active()

            if not active_fps:
                return

            logger.debug(f"Monitoring {len(active_fps)} active investigations")

            # Check each active investigation
            for fingerprint_id in active_fps:
                try:
                    await self._check_investigation_progress(fingerprint_id)
                except Exception as e:
                    logger.error(f"Error monitoring investigation {fingerprint_id}: {e}")

            # Check for stalls
            stalled = self.check_stalled_investigations()
            if stalled:
                logger.warning(f"Stalled investigations: {stalled}")

            # Check for timeouts
            timed_out = self.check_timeouts()
            if timed_out:
                logger.warning(f"Timed out investigations: {timed_out}")

        except Exception as e:
            logger.error(f"Heartbeat monitor error: {e}", exc_info=True)

    async def reconciliation_loop(self):
        """Legacy method - background tasks now use scheduled iteration methods"""
        logger.warning("reconciliation_loop() called but tasks are now scheduled - this should not be called")
        pass

    async def _detect_stuck_starting(self):
        """
        Find investigations stuck in 'starting' state (ES-first check).

        These should transition to 'in_progress' within 5 minutes.
        """
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"investigation_status": "starting"}},
                            {"range": {"updated_at": {"lte": "now-5m"}}}
                        ]
                    }
                },
                "size": 100,
                "_source": ["fingerprint_id", "investigation_metadata", "updated_at"]
            }

            index_pattern = f"{self.failure_store.INDEX_PREFIX}-*"
            result = self.es.search(index=index_pattern, body=query, ignore=[404])

            for hit in result.get('hits', {}).get('hits', []):
                fp_id = hit['_source']['fingerprint_id']
                updated_at = hit['_source'].get('updated_at')
                metadata = hit['_source'].get('investigation_metadata', {})
                container_name = metadata.get('container_name')

                logger.error(f"Investigation {fp_id[:16]}... stuck in 'starting' since {updated_at}")

                # Check if container exists
                if container_name and self._check_container_running(container_name):
                    # Container is running - update to in_progress (ES-first)
                    logger.info(f"Container {container_name} running, updating to in_progress")
                    success = self.failure_store.update_investigation_status_es_first(
                        fp_id,
                        status="in_progress",
                        metadata={"last_heartbeat": utc_isoformat()},
                        expected_current=["starting"]
                    )
                    # Redis tracking is best-effort and not authoritative
                    # (Status is in ES only)
                else:
                    # Container not running - mark as failed (ES-first)
                    logger.info(f"No container found, marking as failed")
                    await self._mark_investigation_failed(
                        fp_id,
                        reason="Investigation stuck in starting state"
                    )

        except Exception as e:
            logger.error(f"Error detecting stuck starting investigations: {e}", exc_info=True)

    async def _detect_stuck_in_progress(self):
        """
        Find investigations stuck in 'in_progress' state (ES-first check).

        Uses last_heartbeat in investigation_metadata to detect stalls (2+ hours without heartbeat).
        """
        try:
            query = {
                "query": {
                    "bool": {
                        "must": [
                            {"term": {"investigation_status": "in_progress"}},
                            {"range": {"investigation_metadata.last_heartbeat": {"lte": "now-2h"}}}
                        ]
                    }
                },
                "size": 100,
                "_source": ["fingerprint_id", "investigation_metadata"]
            }

            index_pattern = f"{self.failure_store.INDEX_PREFIX}-*"
            result = self.es.search(index=index_pattern, body=query, ignore=[404])

            for hit in result.get('hits', {}).get('hits', []):
                fp_id = hit['_source']['fingerprint_id']
                metadata = hit['_source'].get('investigation_metadata', {})
                container_name = metadata.get('container_name')
                last_heartbeat = metadata.get('last_heartbeat')

                logger.error(
                    f"Investigation {fp_id[:16]}... stuck in 'in_progress' "
                    f"(last heartbeat: {last_heartbeat})"
                )

                # Check if container still running
                if container_name and self._check_container_running(container_name):
                    # Container running but no heartbeat - mark as stalled (ES-first)
                    logger.warning(f"Container {container_name} running but stalled")
                    success = self.failure_store.update_investigation_status_es_first(
                        fp_id,
                        status="stalled",
                        expected_current=["in_progress"]
                    )
                    # Redis tracking is best-effort and not authoritative
                    # (Status is in ES only)
                else:
                    # Container not running - mark as failed (ES-first)
                    logger.info(f"Container not running, marking as failed")
                    await self._mark_investigation_failed(
                        fp_id,
                        reason="Investigation stuck in progress, container not found"
                    )

        except Exception as e:
            logger.error(f"Error detecting stuck in-progress investigations: {e}", exc_info=True)

    async def _reconciliation_iteration(self):
        """Reconciliation: verify Redis state matches Docker reality (single iteration)"""
        try:
            # Get what Redis claims is active
            redis_active_fps = set(self.queue.get_all_active())

            # Check active investigations if any exist
            if redis_active_fps:
                logger.debug(f"Reconciliation: Checking {len(redis_active_fps)} investigations")

                # Check each active investigation
                reconciled = 0
                cleaned = 0

                for fp_id in redis_active_fps:
                    try:
                        info = self.queue.get_investigation_info(fp_id)
                        container_name = info.get('container_name')

                        if not container_name:
                            logger.warning(f"Reconciliation: {fp_id} has no container_name")
                            await self._cleanup_orphaned_investigation(fp_id)
                            cleaned += 1
                            continue

                        # Verify container is running
                        if not self._check_container_running(container_name):
                            logger.warning(f"Reconciliation: {fp_id} container not running")
                            await self._cleanup_orphaned_investigation(fp_id)
                            cleaned += 1
                        else:
                            reconciled += 1

                    except Exception as e:
                        logger.error(f"Error reconciling {fp_id}: {e}", exc_info=True)

                if cleaned > 0:
                    logger.info(f"Reconciliation: {reconciled} healthy, {cleaned} cleaned")

            # Run stuck state detection (ES-first checks)
            await self._detect_stuck_starting()
            await self._detect_stuck_in_progress()

            # Always reconcile Elasticsearch investigation statuses (even if no active investigations)
            await self._reconcile_elasticsearch_statuses()

        except Exception as e:
            logger.error(f"Reconciliation error: {e}", exc_info=True)

    async def _reconcile_queued_investigations(self):
        """
        Clean up investigations stuck in 'queued' status but not actually in queue.
        This handles crash scenarios where dequeue happened but mark_started() didn't.
        """
        try:
            # Get all fingerprint IDs that have status="queued"
            queued_fps = []

            # Scan all keys matching the status pattern
            for key in self.redis.scan_iter(match=f"{self.queue.KEY_PREFIX}*:status"):
                status = self.redis.get(key)
                if status == self.queue.STATUS_QUEUED:
                    # Extract fingerprint ID from key
                    fp_id = key.replace(f"{self.queue.KEY_PREFIX}", "").replace(":status", "")
                    queued_fps.append(fp_id)

            if not queued_fps:
                return

            # Get actual queue contents
            queue_list = set(self.queue.get_all_queued())

            # Check for stuck investigations
            cleaned = 0
            for fp_id in queued_fps:
                if fp_id not in queue_list:
                    # Not in queue but has queued status - check how long
                    started_at_str = self.redis.get(self.queue._key(fp_id, "started_at"))

                    if not started_at_str:
                        # No started_at - definitely stuck, clean up
                        logger.warning(f"Reconciling stuck queued investigation (no started_at): {fp_id}")
                        self.queue.mark_completed(fp_id, self.queue.RESULT_FAILED, es_store=self.failure_store)
                        cleaned += 1
                        continue

                    # Check elapsed time
                    try:
                        from datetime import datetime, timedelta
                        started_at = datetime.fromisoformat(started_at_str.replace('Z', '+00:00'))
                        elapsed = datetime.now().astimezone() - started_at

                        # Grace period of 1 hour
                        if elapsed > timedelta(hours=1):
                            logger.warning(f"Reconciling stuck queued investigation (stuck {elapsed}): {fp_id}")

                            # Check if it has reports (maybe it actually completed)
                            status = self.report_manager.get_report_status(fp_id)
                            if status.get('has_diagnosis'):
                                self.queue.mark_completed(fp_id, self.queue.RESULT_SUCCESS, es_store=self.failure_store)
                            else:
                                self.queue.mark_completed(fp_id, self.queue.RESULT_FAILED, es_store=self.failure_store)
                            cleaned += 1

                    except Exception as e:
                        logger.error(f"Error checking elapsed time for {fp_id}: {e}")

            if cleaned > 0:
                logger.info(f"Queued reconciliation: cleaned {cleaned} stuck investigations")

        except Exception as e:
            logger.error(f"Queued reconciliation error: {e}", exc_info=True)

    async def _reconcile_elasticsearch_statuses(self):
        """
        Two-phase reconciliation to sync investigation status between Redis and Elasticsearch.

        Phase 1 (Proactive): Fix known inconsistencies from atomic update failures
        Phase 2 (Reactive): Scan for stale statuses and correct them
        """
        try:
            total_fixed = 0

            # === PHASE 1: Proactive - Fix known inconsistencies ===
            proactive_fixed = 0
            try:
                # Get all inconsistency markers from Redis
                marker_pattern = "medic:inconsistent_status:*"
                inconsistent_keys = []

                # Scan for inconsistency markers (using SCAN to avoid blocking)
                cursor = 0
                while True:
                    cursor, keys = self.redis.scan(cursor, match=marker_pattern, count=100)
                    inconsistent_keys.extend(keys)
                    if cursor == 0:
                        break

                # Fix each known inconsistency
                for marker_key in inconsistent_keys:
                    try:
                        # Extract fingerprint_id from key
                        fp_id = marker_key.replace("medic:inconsistent_status:", "")

                        # Get expected ES status from marker
                        expected_es_status = self.redis.get(marker_key)
                        if not expected_es_status:
                            continue

                        logger.info(
                            f"Proactively fixing known inconsistency for {fp_id[:16]}... "
                            f"(ES target: {expected_es_status})"
                        )

                        # Retry ES update
                        es_success = self.failure_store.update_investigation_status(
                            fp_id, expected_es_status
                        )

                        if es_success:
                            # Fixed - delete marker
                            self.redis.delete(marker_key)
                            proactive_fixed += 1
                            logger.info(f"Fixed inconsistency for {fp_id[:16]}...")
                        else:
                            logger.warning(
                                f"Still cannot update ES for {fp_id[:16]}... "
                                f"(marker will retry later)"
                            )

                    except Exception as e:
                        logger.error(f"Error fixing inconsistency for {marker_key}: {e}")

                if proactive_fixed > 0:
                    logger.info(f"Proactive reconciliation: fixed {proactive_fixed} known inconsistencies")

            except Exception as e:
                logger.error(f"Proactive reconciliation error: {e}", exc_info=True)

            # === PHASE 2: Reactive - Scan for stale statuses ===
            reactive_fixed = 0
            try:
                # Get current state from Redis (source of truth)
                active_fps = set(self.queue.get_all_active())
                queued_fps = set(self.queue.get_all_queued())

                # Query Elasticsearch for signatures with potentially stale statuses
                stale_statuses = ["in_progress", "queued", "starting", "running"]

                # Search across index patterns
                index_patterns = [
                    "medic-failure-signatures-*",
                    "medic-docker-failures-*",
                    "medic-claude-failures-*"
                ]

                for index_pattern in index_patterns:
                    try:
                        result = self.es.search(
                            index=index_pattern,
                            body={
                                "size": 1000,
                                "query": {
                                    "terms": {
                                        "investigation_status": stale_statuses
                                    }
                                },
                                "_source": ["fingerprint_id", "investigation_status"]
                            },
                            ignore=[404]
                        )

                        for hit in result.get("hits", {}).get("hits", []):
                            fp_id = hit["_source"]["fingerprint_id"]
                            es_status = hit["_source"]["investigation_status"]

                            # Determine correct status from Redis (source of truth)
                            correct_status = None

                            if fp_id in queued_fps:
                                correct_status = "queued"
                            elif fp_id in active_fps:
                                # Check Redis status key for more precision
                                redis_status = self.queue.get_status(fp_id)
                                if redis_status == self.queue.STATUS_IN_PROGRESS:
                                    correct_status = "in_progress"
                                elif redis_status == self.queue.STATUS_STARTING:
                                    correct_status = "starting"
                                else:
                                    # In active set but status doesn't match - trust status key
                                    correct_status = "in_progress"
                            else:
                                # Not in queue or active - should be completed/failed
                                redis_status = self.queue.get_status(fp_id)
                                if redis_status:
                                    # Map Redis status to ES status
                                    status_map = {
                                        self.queue.STATUS_COMPLETED: "completed",
                                        self.queue.STATUS_FAILED: "failed",
                                        self.queue.STATUS_IGNORED: "ignored",
                                        self.queue.STATUS_TIMEOUT: "timeout",
                                        self.queue.STATUS_STALLED: "failed",
                                    }
                                    correct_status = status_map.get(redis_status, "failed")
                                else:
                                    # No Redis status - mark as failed
                                    correct_status = "failed"

                            # Update ES if status doesn't match
                            if correct_status and correct_status != es_status:
                                logger.warning(
                                    f"Reactive reconciliation: {fp_id[:16]}... "
                                    f"ES:{es_status} → Redis:{correct_status}"
                                )

                                # Update in Elasticsearch directly
                                self.es.update(
                                    index=hit["_index"],
                                    id=hit["_id"],
                                    body={
                                        "doc": {
                                            "investigation_status": correct_status
                                        }
                                    },
                                    refresh=True
                                )
                                reactive_fixed += 1

                    except Exception as e:
                        logger.debug(f"Could not search {index_pattern}: {e}")

                if reactive_fixed > 0:
                    logger.info(f"Reactive reconciliation: fixed {reactive_fixed} stale statuses")

            except Exception as e:
                logger.error(f"Reactive reconciliation error: {e}", exc_info=True)

            # Emit observability event if any fixes were made
            total_fixed = proactive_fixed + reactive_fixed
            if total_fixed > 0:
                try:
                    from monitoring.timestamp_utils import utc_isoformat
                    self.observability.emit(
                        EventType.MEDIC_STATUS_RECONCILIATION,
                        agent="medic-reconciler",
                        task_id="status-reconciliation",
                        project="medic",
                        data={
                            "proactive_fixed": proactive_fixed,
                            "reactive_fixed": reactive_fixed,
                            "total_fixed": total_fixed,
                            "timestamp": utc_isoformat()
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit reconciliation event: {e}")

        except Exception as e:
            logger.error(f"Elasticsearch reconciliation error: {e}", exc_info=True)

    async def reconcile_signature_status_now(self, fingerprint_id: str) -> bool:
        """
        Immediately reconcile a specific signature's status between Redis and Elasticsearch.

        Useful for API calls or user-triggered actions that need instant reconciliation.

        Args:
            fingerprint_id: Fingerprint ID to reconcile

        Returns:
            True if status was reconciled successfully, False otherwise
        """
        try:
            logger.info(f"On-demand reconciliation for {fingerprint_id[:16]}...")

            # Get current state from Redis (source of truth)
            redis_status = self.queue.get_status(fingerprint_id)
            in_active = fingerprint_id in self.queue.get_all_active()
            in_queue = fingerprint_id in self.queue.get_all_queued()

            # Determine correct ES status from Redis state
            if in_queue:
                correct_es_status = "queued"
            elif in_active:
                # Map Redis status to ES status
                if redis_status == self.queue.STATUS_IN_PROGRESS:
                    correct_es_status = "in_progress"
                elif redis_status == self.queue.STATUS_STARTING:
                    correct_es_status = "starting"
                else:
                    correct_es_status = "in_progress"  # Default for active
            else:
                # Not in queue or active
                if redis_status:
                    # Map Redis status to ES status
                    status_map = {
                        self.queue.STATUS_COMPLETED: "completed",
                        self.queue.STATUS_FAILED: "failed",
                        self.queue.STATUS_IGNORED: "ignored",
                        self.queue.STATUS_TIMEOUT: "timeout",
                        self.queue.STATUS_STALLED: "failed",
                    }
                    correct_es_status = status_map.get(redis_status, "failed")
                else:
                    # No Redis state - mark as not_started
                    correct_es_status = "not_started"

            # Update Elasticsearch to match Redis
            es_success = self.failure_store.update_investigation_status(
                fingerprint_id, correct_es_status
            )

            if es_success:
                logger.info(
                    f"Successfully reconciled {fingerprint_id[:16]}... to {correct_es_status}"
                )
                return True
            else:
                logger.warning(
                    f"Failed to reconcile {fingerprint_id[:16]}... (ES update failed)"
                )
                return False

        except Exception as e:
            logger.error(
                f"Error reconciling {fingerprint_id[:16]}...: {e}", exc_info=True
            )
            return False

    async def _check_investigation_progress(self, fingerprint_id: str):
        """Check progress AND verify container is still running"""
        try:
            investigation_info = self.active_processes.get(fingerprint_id)
            if not investigation_info:
                # Not in active_processes - get from Redis and verify
                info = self.queue.get_investigation_info(fingerprint_id)
                if not info:
                    return

                container_name = info.get('container_name')
                if container_name:
                    # CRITICAL: Verify container is actually running
                    if not self._check_container_running(container_name):
                        logger.error(f"Container {container_name} for investigation {fingerprint_id} is not running")
                        await self._cleanup_orphaned_investigation(fingerprint_id)
                        return

                    # Container running - update heartbeat
                    line_count = self.report_manager.count_log_lines(fingerprint_id)
                    self.queue.update_heartbeat(fingerprint_id, line_count)
                return

            # In active_processes - check both task and container
            container_name = investigation_info.get('container_name')

            # CRITICAL: Verify container is actually running
            if container_name and not self._check_container_running(container_name):
                logger.error(f"Container {container_name} for investigation {fingerprint_id} is not running")
                # Container died - mark investigation as failed
                task = investigation_info.get('task')
                await self._handle_investigation_completion(fingerprint_id, task, investigation_info)
                return

            # Get the asyncio task
            task = investigation_info.get('task')
            if not task:
                logger.warning(f"No task found for {fingerprint_id}")
                return

            # Task still running - update heartbeat based on output
            line_count = self.report_manager.count_log_lines(fingerprint_id)
            self.queue.update_heartbeat(fingerprint_id, line_count)

        except Exception as e:
            logger.error(
                f"Error checking progress for {fingerprint_id}: {e}", exc_info=True
            )

    def check_stalled_investigations(self) -> List[str]:
        """
        Check all active investigations for stalls.

        Returns:
            List of fingerprint IDs that are stalled
        """
        stalled = []
        active_fps = self.queue.get_all_active()

        for fingerprint_id in active_fps:
            info = self.queue.get_investigation_info(fingerprint_id)
            last_heartbeat = info.get("last_heartbeat")

            if not last_heartbeat:
                continue

            heartbeat_time = parse_iso_timestamp(last_heartbeat)
            elapsed = (utc_now() - heartbeat_time).total_seconds()

            if elapsed > self.queue.STALL_THRESHOLD:
                logger.warning(f"{fingerprint_id}: Investigation stalled ({int(elapsed/60)}m)")
                stalled.append(fingerprint_id)
                # Status updates now happen via ES, not Redis

        return stalled

    def check_timeouts(self) -> List[str]:
        """
        Check all active investigations for timeouts.

        Returns:
            List of fingerprint IDs that timed out
        """
        timed_out = []
        active_fps = self.queue.get_all_active()

        for fingerprint_id in active_fps:
            info = self.queue.get_investigation_info(fingerprint_id)
            started_at = info.get("started_at")

            if not started_at:
                continue

            started_time = parse_iso_timestamp(started_at)
            elapsed = (utc_now() - started_time).total_seconds()

            if elapsed > self.queue.LOCK_TTL:
                logger.warning(f"{fingerprint_id}: Investigation timed out ({int(elapsed/3600)}h)")

                # Cancel task if still running
                if fingerprint_id in self.active_processes:
                    investigation_info = self.active_processes[fingerprint_id]
                    task = investigation_info.get('task')
                    if task and not task.done():
                        task.cancel()

                    del self.active_processes[fingerprint_id]

                # Mark as timeout (updates ES)
                self.queue.mark_completed(
                    fingerprint_id,
                    self.queue.RESULT_TIMEOUT,
                    es_store=self.failure_store
                )
                timed_out.append(fingerprint_id)

                # Emit timeout event
                try:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_FAILED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "reason": "timeout",
                        },
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit observability event: {e}")

        return timed_out

    async def _recover_stalled_investigations(self):
        """Recover from previous crashes - clean up stalled investigations"""
        try:
            active_fps = self.queue.get_all_active()
            logger.info(f"Found {len(active_fps)} active investigations from previous run")

            recovery_stats = {
                "recovered": 0,
                "completed": 0,
                "failed": 0,
                "waiting": 0,
                "timeout": 0,
            }

            for fingerprint_id in active_fps:
                try:
                    result = await self._recover_investigation(fingerprint_id)
                    recovery_stats[result] += 1
                except Exception as e:
                    logger.error(f"Error recovering investigation {fingerprint_id}: {e}")
                    recovery_stats["failed"] += 1

            logger.info(f"Recovery complete: {recovery_stats}")

        except Exception as e:
            logger.error(f"Error in startup recovery: {e}", exc_info=True)

    async def _recover_investigation(self, fingerprint_id: str) -> str:
        """
        Recover a single investigation.

        Returns:
            One of: "recovered", "completed", "failed", "waiting", "timeout"
        """
        info = self.queue.get_investigation_info(fingerprint_id)
        status = info.get("status")
        started_at = info.get("started_at")

        logger.info(f"Recovering {fingerprint_id}: status={status}, started={started_at}")

        # Calculate elapsed time
        elapsed_seconds = None
        if started_at:
            started_time = parse_iso_timestamp(started_at)
            elapsed = utc_now() - started_time
            elapsed_seconds = elapsed.total_seconds()

        # Check if reports exist
        result, _ = self._validate_investigation_result(fingerprint_id)
        has_reports = result in [self.queue.RESULT_SUCCESS, self.queue.RESULT_IGNORED]

        if has_reports:
            # Reports exist - mark as completed (updates ES)
            logger.info(f"{fingerprint_id}: Reports exist, marking as completed")
            self.queue.mark_completed(fingerprint_id, result, es_store=self.failure_store)
            return "completed"

        # No reports - decide based on elapsed time
        if not elapsed_seconds:
            # No start time - shouldn't happen, but mark as failed (updates ES)
            logger.warning(f"{fingerprint_id}: No start time, marking as failed")
            self.queue.mark_completed(fingerprint_id, self.queue.RESULT_FAILED, es_store=self.failure_store)
            return "failed"

        if elapsed_seconds < self.WAIT_THRESHOLD:
            # Recently started - wait a bit more
            logger.info(f"{fingerprint_id}: Started {int(elapsed_seconds/60)}m ago, waiting")
            return "waiting"

        if elapsed_seconds > self.TIMEOUT_THRESHOLD:
            # Exceeded timeout - mark as timeout (updates ES)
            logger.warning(f"{fingerprint_id}: Exceeded timeout ({int(elapsed_seconds/3600)}h)")
            self.queue.mark_completed(fingerprint_id, self.queue.RESULT_TIMEOUT, es_store=self.failure_store)
            return "timeout"

        # Between 30min and 4hr - mark as failed (can't re-launch without process context, updates ES)
        logger.info(f"{fingerprint_id}: Stalled at {int(elapsed_seconds/60)}m, marking as failed")
        self.queue.mark_completed(fingerprint_id, self.queue.RESULT_FAILED, es_store=self.failure_store)
        return "failed"

    async def auto_trigger_checker(self):
        """Legacy method - background tasks now use scheduled iteration methods"""
        logger.warning("auto_trigger_checker() called but tasks are now scheduled - this should not be called")
        pass

    async def _auto_trigger_checker_iteration(self):
        """Check for signatures that should auto-trigger investigation (single iteration)"""
        try:
            logger.debug("Checking for auto-trigger conditions...")
            triggered = await self._check_auto_trigger_conditions()

            for fingerprint_id in triggered:
                logger.info(f"Auto-triggering investigation for {fingerprint_id}")
                # ES-first enqueue (ES is updated in enqueue method)
                self.queue.enqueue(fingerprint_id, priority="high", es_store=self.failure_store)

                # Emit event
                try:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_QUEUED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "trigger": "auto",
                        },
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit observability event: {e}")

            if triggered:
                logger.info(f"Auto-triggered {len(triggered)} investigations")

        except Exception as e:
            logger.error(f"Auto-trigger checker error: {e}", exc_info=True)

    async def _check_auto_trigger_conditions(self) -> List[str]:
        """
        Check for signatures that should auto-trigger investigation.

        Returns:
            List of fingerprint IDs to auto-trigger
        """
        # Get unresolved signatures with investigation_status = "not_started"
        signatures = self.failure_store.get_unresolved_signatures(max_count=100)
        triggered = []

        for signature in signatures:
            investigation_status = signature.get("investigation_status", "not_started")
            status = signature.get("status")

            # Skip if already investigated or ignored/resolved
            if investigation_status != "not_started":
                continue
            if status in ["ignored", "resolved"]:
                continue

            # Auto-trigger based on occurrence count or severity
            occurrence_count = signature.get("occurrence_count", 0)
            severity = signature.get("severity", "ERROR")

            # Trigger if: high severity with 5+ occurrences, or any severity with 20+ occurrences
            if (severity == "CRITICAL" and occurrence_count >= 5) or occurrence_count >= 20:
                triggered.append(signature["fingerprint_id"])

        return triggered

    def trigger_investigation(self, fingerprint_id: str, priority: str = "normal") -> bool:
        """
        Manually trigger investigation for a signature (ES-first).

        Args:
            fingerprint_id: Signature ID to investigate
            priority: "low", "normal", "high"

        Returns:
            True if enqueued, False if already queued/in-progress
        """
        return self.queue.enqueue(fingerprint_id, priority=priority, es_store=self.failure_store)

    def get_investigation_status(self, fingerprint_id: str) -> Dict:
        """
        Get investigation status and progress.

        Returns:
            Dictionary with status, progress, timestamps
        """
        return self.queue.get_investigation_info(fingerprint_id)

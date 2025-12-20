"""
Base Investigation Orchestrator

Abstract base class for investigation orchestration with recovery and monitoring.
Provides unified lifecycle management for both Docker and Claude investigation systems.
"""

import logging
import asyncio
from abc import ABC, abstractmethod
from typing import Dict, Any, Optional, List, Tuple
from datetime import datetime, timezone, timedelta
from elasticsearch import Elasticsearch
import redis

from monitoring.observability import get_observability_manager, EventType
from monitoring.timestamp_utils import utc_now, parse_iso_timestamp

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

        # Perform startup recovery
        logger.info("Performing startup recovery...")
        await self._recover_stalled_investigations()

        self.running = True
        logger.info(f"{self.__class__.__name__} initialized and ready")

        # Background tasks will be started by main.py
        # Keep this task alive with infinite wait
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Orchestrator cancelled")
            self.running = False
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

                # Update status
                self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                self.queue.mark_completed(
                    fingerprint_id,
                    self.queue.RESULT_FAILED,
                )
            except Exception as e:
                logger.error(f"Error stopping investigation {fingerprint_id}: {e}")

        self.active_processes.clear()
        logger.info(f"{self.__class__.__name__} stopped")

    async def queue_processor(self):
        """Process investigation queue (main loop)"""
        logger.info("Queue processor started")

        while self.running:
            try:
                logger.info("Queue processor loop iteration starting")
                # Check concurrent limit
                active_count = self.queue.get_active_count()
                logger.info(f"Active investigation count: {active_count}, MAX_CONCURRENT: {self.queue.MAX_CONCURRENT}")

                if active_count >= self.queue.MAX_CONCURRENT:
                    logger.info(
                        f"Max concurrent investigations reached ({active_count}), waiting..."
                    )
                    await asyncio.sleep(10)
                    continue

                # Get next investigation from queue
                logger.info("Calling queue.dequeue()...")
                fingerprint_id = await self.queue.dequeue()
                logger.info(f"dequeue() returned: {fingerprint_id}")

                if not fingerprint_id:
                    # Queue empty, wait a bit
                    logger.info("Queue empty, sleeping for 1 second")
                    await asyncio.sleep(1)
                    continue

                # Start investigation
                logger.info(f"Dequeued investigation: {fingerprint_id}")
                await self._start_investigation(fingerprint_id)

            except Exception as e:
                logger.error(f"Queue processor error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _start_investigation(self, fingerprint_id: str):
        """Start an investigation for a fingerprint"""
        logger.info(f"Starting investigation for {fingerprint_id}")

        try:
            # Update status
            self.queue.update_status(fingerprint_id, self.queue.STATUS_STARTING)

            # Get signature data from Elasticsearch
            signature = self.failure_store.get_signature(fingerprint_id)
            if not signature:
                logger.error(f"Signature not found: {fingerprint_id}")
                self.queue.mark_completed(
                    fingerprint_id,
                    self.queue.RESULT_FAILED,
                )
                return

            # Prepare context (subclass-specific)
            context_file, output_log = self._prepare_investigation_context(
                fingerprint_id, signature
            )

            # Launch investigation process
            investigation_info = await self.agent_runner.launch_investigation(
                fingerprint_id=fingerprint_id,
                context_file=context_file,
                output_log=output_log,
                observability_manager=self.observability,
            )

            if not investigation_info:
                logger.error(f"Failed to launch investigation for {fingerprint_id}")
                self.queue.mark_completed(
                    fingerprint_id,
                    self.queue.RESULT_FAILED,
                )
                return

            # Track active investigation
            self.active_processes[fingerprint_id] = investigation_info
            logger.info(f"Tracked active investigation {fingerprint_id}")

            # Mark as started with container name tracking
            container_name = investigation_info.get('container_name')
            self.queue.mark_started(fingerprint_id, pid=0, container_name=container_name)

            # Update Elasticsearch signature status to in_progress
            self.failure_store.update_investigation_status(fingerprint_id, "in_progress")

            # Set up completion callback
            def done_callback(task):
                """Schedule completion handler in the event loop"""
                try:
                    asyncio.create_task(self._handle_investigation_completion(fingerprint_id, task))
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
            self.queue.mark_completed(
                fingerprint_id,
                self.queue.RESULT_FAILED,
            )
            # Mark signature as failed to allow re-queueing
            self.failure_store.update_investigation_status(fingerprint_id, "failed")

    async def _handle_investigation_completion(self, fingerprint_id: str, task: asyncio.Task):
        """
        Handle completion of an investigation.

        Args:
            fingerprint_id: Fingerprint ID
            task: Completed task
        """
        try:
            # Remove from active processes
            if fingerprint_id in self.active_processes:
                del self.active_processes[fingerprint_id]

            # Check if task succeeded or failed
            if task.exception():
                logger.error(f"Investigation {fingerprint_id} failed with exception: {task.exception()}")
                result = self.queue.RESULT_FAILED
                status = self.queue.STATUS_FAILED
            else:
                # Validate result (subclass-specific)
                result, status = self._validate_investigation_result(fingerprint_id)
                logger.info(f"Investigation {fingerprint_id} completed with result: {result}")

            # Mark as completed
            self.queue.mark_completed(fingerprint_id, result)

            # Update Elasticsearch
            es_status_map = {
                self.queue.STATUS_COMPLETED: "completed",
                self.queue.STATUS_IGNORED: "ignored",
                self.queue.STATUS_FAILED: "failed",
                self.queue.STATUS_TIMEOUT: "timeout",
            }
            es_status = es_status_map.get(status, "failed")
            self.failure_store.update_investigation_status(fingerprint_id, es_status)

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
            except Exception as e:
                logger.warning(f"Failed to emit observability event: {e}")

        except Exception as e:
            logger.error(
                f"Error handling completion for {fingerprint_id}: {e}", exc_info=True
            )

    async def heartbeat_monitor(self):
        """Monitor active investigations for progress and completion"""
        logger.info("Heartbeat monitor started")

        while self.running:
            try:
                await asyncio.sleep(self.queue.HEARTBEAT_INTERVAL)

                # Check each active investigation
                active_fps = list(self.active_processes.keys())
                for fingerprint_id in active_fps:
                    await self._check_investigation_progress(fingerprint_id)

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

    async def _check_investigation_progress(self, fingerprint_id: str):
        """Check progress of a single investigation"""
        try:
            investigation_info = self.active_processes.get(fingerprint_id)
            if not investigation_info:
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
                self.queue.update_status(fingerprint_id, self.queue.STATUS_STALLED)

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

                # Mark as timeout
                self.queue.mark_completed(
                    fingerprint_id,
                    self.queue.RESULT_TIMEOUT,
                )
                self.failure_store.update_investigation_status(fingerprint_id, "timeout")
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
            # Reports exist - mark as completed
            logger.info(f"{fingerprint_id}: Reports exist, marking as completed")
            self.queue.mark_completed(fingerprint_id, result)
            es_status = "completed" if result == self.queue.RESULT_SUCCESS else "ignored"
            self.failure_store.update_investigation_status(fingerprint_id, es_status)
            return "completed"

        # No reports - decide based on elapsed time
        if not elapsed_seconds:
            # No start time - shouldn't happen, but mark as failed
            logger.warning(f"{fingerprint_id}: No start time, marking as failed")
            self.queue.mark_completed(fingerprint_id, self.queue.RESULT_FAILED)
            self.failure_store.update_investigation_status(fingerprint_id, "failed")
            return "failed"

        if elapsed_seconds < self.WAIT_THRESHOLD:
            # Recently started - wait a bit more
            logger.info(f"{fingerprint_id}: Started {int(elapsed_seconds/60)}m ago, waiting")
            return "waiting"

        if elapsed_seconds > self.TIMEOUT_THRESHOLD:
            # Exceeded timeout - mark as timeout
            logger.warning(f"{fingerprint_id}: Exceeded timeout ({int(elapsed_seconds/3600)}h)")
            self.queue.mark_completed(fingerprint_id, self.queue.RESULT_TIMEOUT)
            self.failure_store.update_investigation_status(fingerprint_id, "timeout")
            return "timeout"

        # Between 30min and 4hr - mark as failed (can't re-launch without process context)
        logger.info(f"{fingerprint_id}: Stalled at {int(elapsed_seconds/60)}m, marking as failed")
        self.queue.mark_completed(fingerprint_id, self.queue.RESULT_FAILED)
        self.failure_store.update_investigation_status(fingerprint_id, "failed")
        return "failed"

    async def auto_trigger_checker(self):
        """Periodically check for signatures that should auto-trigger investigation"""
        logger.info("Auto-trigger checker started")

        while self.running:
            try:
                # Run every 5 minutes
                await asyncio.sleep(300)

                logger.debug("Checking for auto-trigger conditions...")
                triggered = await self._check_auto_trigger_conditions()

                for fingerprint_id in triggered:
                    logger.info(f"Auto-triggering investigation for {fingerprint_id}")
                    self.failure_store.update_investigation_status(fingerprint_id, "queued")
                    self.queue.enqueue(fingerprint_id, priority="high")

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
        Manually trigger investigation for a signature.

        Args:
            fingerprint_id: Signature ID to investigate
            priority: "low", "normal", "high"

        Returns:
            True if enqueued, False if already queued/in-progress
        """
        return self.queue.enqueue(fingerprint_id, priority=priority)

    def get_investigation_status(self, fingerprint_id: str) -> Dict:
        """
        Get investigation status and progress.

        Returns:
            Dictionary with status, progress, timestamps
        """
        return self.queue.get_investigation_info(fingerprint_id)

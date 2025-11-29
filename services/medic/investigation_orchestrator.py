"""
Investigation Orchestrator

Main service that processes the investigation queue and manages investigation lifecycle.
"""

import logging
import asyncio
import redis
import os
from typing import Optional
from elasticsearch import Elasticsearch

from .investigation_queue import InvestigationQueue
from .investigation_agent_runner import InvestigationAgentRunner
from .investigation_recovery import InvestigationRecovery
from .report_manager import ReportManager
from .failure_signature_store import FailureSignatureStore
from monitoring.observability import get_observability_manager, EventType

logger = logging.getLogger(__name__)


class InvestigationOrchestrator:
    """
    Main orchestrator service for Medic investigations.

    Responsibilities:
    - Process investigation queue
    - Launch and monitor investigation processes
    - Track progress via heartbeats
    - Detect stalls and timeouts
    - Trigger auto-investigations based on thresholds
    - Emit observability events
    """

    def __init__(
        self,
        redis_client: redis.Redis,
        es_client: Elasticsearch,
        workspace_root: str = "/workspace/clauditoreum",
        medic_dir: str = "/medic",
    ):
        """
        Initialize investigation orchestrator.

        Args:
            redis_client: Redis client for queue management
            es_client: Elasticsearch client for signature data
            workspace_root: Path to Clauditoreum codebase
            medic_dir: Base directory for investigation reports
        """
        self.queue = InvestigationQueue(redis_client)
        self.agent_runner = InvestigationAgentRunner(workspace_root)
        self.report_manager = ReportManager(medic_dir)
        self.recovery = InvestigationRecovery(self.queue, self.agent_runner, self.report_manager)
        self.failure_store = FailureSignatureStore(es_client)
        self.observability = get_observability_manager()

        self.running = False
        self.active_processes = {}  # fingerprint_id -> subprocess.Popen

        logger.info("InvestigationOrchestrator initialized")

    async def start(self):
        """Start the investigation orchestrator"""
        logger.info("Starting Investigation Orchestrator...")

        # Check Claude Code CLI availability
        claude_version = self.agent_runner.get_claude_version()
        if not claude_version:
            logger.error("Claude Code CLI not available - investigations will fail")

        # Perform startup recovery
        logger.info("Performing startup recovery...")
        recovery_stats = self.recovery.recover_all()
        logger.info(f"Recovery complete: {recovery_stats}")

        self.running = True

        # Start background tasks
        tasks = [
            asyncio.create_task(self._queue_processor()),
            asyncio.create_task(self._heartbeat_monitor()),
            asyncio.create_task(self._auto_trigger_checker()),
        ]

        try:
            await asyncio.gather(*tasks)
        except Exception as e:
            logger.error(f"Orchestrator error: {e}", exc_info=True)
            await self.stop()

    async def stop(self):
        """Stop the orchestrator and cleanup"""
        logger.info("Stopping Investigation Orchestrator...")
        self.running = False

        # Cancel all active investigation tasks
        for fingerprint_id, investigation in self.active_processes.items():
            task = investigation.get('task')
            if task and not task.done():
                logger.info(f"Cancelling investigation {fingerprint_id}")
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.info(f"Investigation {fingerprint_id} cancelled")
                except Exception as e:
                    logger.warning(f"Error during task cancellation: {e}")

        logger.info("Investigation Orchestrator stopped")

    async def _queue_processor(self):
        """Process investigation queue (main loop)"""
        logger.info("Queue processor started")

        while self.running:
            try:
                # Check concurrent limit
                active_count = len(self.queue.get_all_active())
                if active_count >= InvestigationQueue.MAX_CONCURRENT:
                    logger.debug(
                        f"Max concurrent investigations reached ({active_count}), waiting..."
                    )
                    await asyncio.sleep(10)
                    continue

                # Get next investigation from queue (blocking with timeout)
                fingerprint_id = await asyncio.get_event_loop().run_in_executor(
                    None, self.queue.dequeue
                )

                if not fingerprint_id:
                    # Queue empty, wait a bit
                    await asyncio.sleep(1)
                    continue

                # Start investigation
                await self._start_investigation(fingerprint_id)

            except Exception as e:
                logger.error(f"Queue processor error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _start_investigation(self, fingerprint_id: str):
        """Start an investigation for a fingerprint"""
        logger.info(f"Starting investigation for {fingerprint_id}")

        try:
            # Acquire lock
            if not self.queue.acquire_lock(fingerprint_id):
                logger.warning(f"Failed to acquire lock for {fingerprint_id}")
                return

            # Update status
            self.queue.update_status(fingerprint_id, InvestigationQueue.STATUS_STARTING)

            # Get signature data from Elasticsearch
            signature = await self.failure_store._get_signature(fingerprint_id)
            if not signature:
                logger.error(f"Signature not found: {fingerprint_id}")
                self.queue.mark_completed(
                    fingerprint_id,
                    InvestigationQueue.RESULT_FAILED,
                    "Signature not found in Elasticsearch",
                )
                return

            # Prepare context
            sample_logs = signature.get("sample_log_entries", [])[:10]  # Top 10 samples
            context_file = self.report_manager.write_context(
                fingerprint_id, signature, sample_logs
            )

            # Get output log path
            output_log = self.report_manager.get_investigation_log_path(fingerprint_id)

            # Launch investigation process (now async)
            investigation = await self.agent_runner.launch_investigation(
                fingerprint_id, context_file, output_log, self.observability
            )

            if not investigation:
                logger.error(f"Failed to launch investigation for {fingerprint_id}")
                self.queue.mark_completed(
                    fingerprint_id,
                    InvestigationQueue.RESULT_FAILED,
                    "Failed to launch investigation process",
                )
                return

            # Track task (instead of process)
            self.active_processes[fingerprint_id] = investigation
            # Note: No PID with run_claude_code, using task-based monitoring
            self.queue.set_pid(fingerprint_id, 0)  # Placeholder for compatibility
            self.queue.mark_started(fingerprint_id)

            # Emit event
            try:
                self.observability.emit(
                    EventType.MEDIC_INVESTIGATION_STARTED,
                    agent="medic-investigator",
                    task_id=fingerprint_id,
                    project="medic",
                    data={
                        "fingerprint_id": fingerprint_id,
                        "method": "run_claude_code",  # Indicate new method
                        "severity": signature.get("severity"),
                        "error_type": signature.get("signature", {}).get("error_type"),
                    },
                )
            except Exception as e:
                logger.warning(f"Failed to emit observability event: {e}")

            logger.info(f"Investigation started: {fingerprint_id} (using run_claude_code)")

        except Exception as e:
            logger.error(f"Error starting investigation {fingerprint_id}: {e}", exc_info=True)
            self.queue.mark_completed(
                fingerprint_id,
                InvestigationQueue.RESULT_FAILED,
                str(e),
            )
            # Mark signature as failed to allow re-queueing
            await self.failure_store.update_investigation_status(fingerprint_id, "failed")

    async def _heartbeat_monitor(self):
        """Monitor active investigations for progress and completion"""
        logger.info("Heartbeat monitor started")

        while self.running:
            try:
                await asyncio.sleep(InvestigationQueue.HEARTBEAT_INTERVAL)

                # Check each active investigation
                for fingerprint_id in list(self.active_processes.keys()):
                    await self._check_investigation_progress(fingerprint_id)

                # Check for stalls
                stalled = self.recovery.check_stalled_investigations()
                if stalled:
                    logger.warning(f"Stalled investigations: {stalled}")

                # Check for timeouts
                timed_out = self.recovery.check_timeouts()
                for fingerprint_id in timed_out:
                    if fingerprint_id in self.active_processes:
                        del self.active_processes[fingerprint_id]

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

            except Exception as e:
                logger.error(f"Heartbeat monitor error: {e}", exc_info=True)

    async def _check_investigation_progress(self, fingerprint_id: str):
        """Check progress of a single investigation"""
        try:
            investigation = self.active_processes.get(fingerprint_id)
            if not investigation:
                return

            # Get the asyncio task
            task = investigation.get('task')
            if not task:
                logger.warning(f"No task found for {fingerprint_id}")
                return

            # Check if task is done
            if task.done():
                # Task finished
                try:
                    result = task.result()  # This will raise if task failed
                    logger.info(f"Investigation {fingerprint_id} completed successfully")
                    await self._handle_investigation_completion(fingerprint_id, 0)
                except Exception as e:
                    logger.error(f"Investigation {fingerprint_id} failed with exception: {e}")
                    await self._handle_investigation_completion(fingerprint_id, 1)
                return

            # Task still running - update heartbeat based on output
            line_count = self.report_manager.count_log_lines(fingerprint_id)
            previous_count = self.queue.get_output_lines(fingerprint_id)

            if line_count > previous_count:
                # Progress detected
                self.queue.set_output_lines(fingerprint_id, line_count)
                self.queue.update_heartbeat(fingerprint_id)
                logger.debug(
                    f"{fingerprint_id}: Progress detected ({line_count} lines)"
                )

        except Exception as e:
            logger.error(
                f"Error checking progress for {fingerprint_id}: {e}", exc_info=True
            )

    async def _handle_investigation_completion(self, fingerprint_id: str, returncode: int):
        """Handle completion of an investigation"""
        try:
            # Remove from active processes
            if fingerprint_id in self.active_processes:
                del self.active_processes[fingerprint_id]

            # Check for reports
            report_status = self.report_manager.get_report_status(fingerprint_id)

            has_diagnosis = report_status.get("has_diagnosis", False)
            has_fix_plan = report_status.get("has_fix_plan", False)
            has_ignored = report_status.get("has_ignored", False)

            # Determine result
            if has_diagnosis and has_fix_plan:
                result = InvestigationQueue.RESULT_SUCCESS
                logger.info(f"{fingerprint_id}: Completed successfully with diagnosis and fix plan")
            elif has_ignored:
                result = InvestigationQueue.RESULT_IGNORED
                logger.info(f"{fingerprint_id}: Marked as ignored")
            elif returncode == 0:
                result = InvestigationQueue.RESULT_FAILED
                error = "Investigation completed but no reports generated"
                logger.warning(f"{fingerprint_id}: {error}")
            else:
                result = InvestigationQueue.RESULT_FAILED
                error = f"Investigation process failed with code {returncode}"
                logger.error(f"{fingerprint_id}: {error}")

            # Mark as completed
            self.queue.mark_completed(fingerprint_id, result)

            # Update Elasticsearch
            if result == InvestigationQueue.RESULT_SUCCESS:
                await self.failure_store.update_investigation_status(
                    fingerprint_id, "completed"
                )
            elif result == InvestigationQueue.RESULT_IGNORED:
                await self.failure_store.update_investigation_status(
                    fingerprint_id, "ignored"
                )
            else:
                await self.failure_store.update_investigation_status(
                    fingerprint_id, "failed"
                )

            # Emit completion event
            try:
                if result in [InvestigationQueue.RESULT_SUCCESS, InvestigationQueue.RESULT_IGNORED]:
                    self.observability.emit(
                        EventType.MEDIC_INVESTIGATION_COMPLETED,
                        agent="medic-investigator",
                        task_id=fingerprint_id,
                        project="medic",
                        data={
                            "fingerprint_id": fingerprint_id,
                            "result": result,
                            "has_diagnosis": has_diagnosis,
                            "has_fix_plan": has_fix_plan,
                            "has_ignored": has_ignored,
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
                            "returncode": returncode,
                        },
                    )
            except Exception as e:
                logger.warning(f"Failed to emit observability event: {e}")

        except Exception as e:
            logger.error(
                f"Error handling completion for {fingerprint_id}: {e}", exc_info=True
            )

    async def _auto_trigger_checker(self):
        """Periodically check for signatures that should auto-trigger investigation"""
        logger.info("Auto-trigger checker started")

        while self.running:
            try:
                # Run every 5 minutes
                await asyncio.sleep(300)

                logger.debug("Checking for auto-trigger conditions...")
                triggered = await self.failure_store.check_auto_trigger_conditions()

                for fingerprint_id in triggered:
                    logger.info(f"Auto-triggering investigation for {fingerprint_id}")
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


# Main entry point
async def main():
    """Main entry point for investigation orchestrator service"""
    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    # Get configuration from environment
    redis_host = os.getenv("REDIS_HOST", "localhost")
    redis_port = int(os.getenv("REDIS_PORT", 6379))
    es_hosts = os.getenv("ELASTICSEARCH_HOSTS", "http://localhost:9200")
    workspace_root = os.getenv("WORKSPACE_ROOT", "/workspace/clauditoreum")
    medic_dir = os.getenv("MEDIC_DIR", "/medic")

    # Initialize clients
    redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=False)
    es_client = Elasticsearch([es_hosts])

    # Create and start orchestrator
    orchestrator = InvestigationOrchestrator(
        redis_client, es_client, workspace_root, medic_dir
    )

    try:
        await orchestrator.start()
    except KeyboardInterrupt:
        logger.info("Received shutdown signal")
        await orchestrator.stop()


if __name__ == "__main__":
    asyncio.run(main())

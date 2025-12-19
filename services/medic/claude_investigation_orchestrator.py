"""
Claude Investigation Orchestrator

Main service that processes the Claude investigation queue and manages investigation lifecycle.
"""

import logging
import asyncio
import redis
import os
from typing import Optional, Dict
from elasticsearch import Elasticsearch

from .claude_investigation_queue import ClaudeInvestigationQueue
from .claude_investigation_agent_runner import ClaudeInvestigationAgentRunner
from .claude_report_manager import ClaudeReportManager
from .claude_failure_signature_store import ClaudeFailureSignatureStore
from monitoring.observability import get_observability_manager, EventType

logger = logging.getLogger(__name__)


class ClaudeInvestigationOrchestrator:
    """
    Main orchestrator service for Claude Medic investigations.

    Responsibilities:
    - Process Claude investigation queue
    - Launch and monitor Claude investigation processes
    - Track progress via heartbeats
    - Detect stalls and timeouts
    - Update investigation status in Elasticsearch
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
        Initialize Claude investigation orchestrator.

        Args:
            redis_client: Redis client for queue management
            es_client: Elasticsearch client for signature data
            workspace_root: Path to Clauditoreum codebase
            medic_dir: Base directory for investigation reports
        """
        self.queue = ClaudeInvestigationQueue(redis_client)
        self.agent_runner = ClaudeInvestigationAgentRunner(workspace_root)
        self.report_manager = ClaudeReportManager(medic_dir)
        self.failure_store = ClaudeFailureSignatureStore(es_client)
        self.observability = get_observability_manager()

        self.running = False
        self.active_processes = {}  # fingerprint_id -> investigation_info

        logger.info("ClaudeInvestigationOrchestrator initialized")

    async def start(self):
        """Start the Claude investigation orchestrator"""
        logger.info("Starting Claude Investigation Orchestrator...")

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
        logger.info("Claude Investigation Orchestrator initialized and ready")

        # Background tasks will be started by main.py
        # Keep this task alive with infinite wait
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Claude orchestrator cancelled")
            self.running = False
            raise

    async def stop(self):
        """Stop the orchestrator and cleanup"""
        logger.info("Stopping Claude Investigation Orchestrator...")
        self.running = False

        # Cancel all active investigation tasks
        for fingerprint_id, investigation_info in self.active_processes.items():
            try:
                await self.agent_runner.kill_investigation(investigation_info)
                self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                self.queue.release_lock(fingerprint_id)
            except Exception as e:
                logger.error(f"Error stopping investigation {fingerprint_id}: {e}")

        self.active_processes.clear()
        logger.info("Claude Investigation Orchestrator stopped")

    async def queue_processor(self):
        """Process investigation queue continuously"""
        logger.info("Claude queue processor started")

        while self.running:
            try:
                # Check if we can start new investigations
                if not self.queue.can_start_new():
                    await asyncio.sleep(5)
                    continue

                # Get next investigation from queue
                fingerprint_id = self.queue.dequeue()

                if fingerprint_id:
                    logger.info(f"Dequeued Claude investigation: {fingerprint_id}")
                    await self._start_investigation(fingerprint_id)

            except Exception as e:
                logger.error(f"Error in queue processor: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _start_investigation(self, fingerprint_id: str):
        """
        Start a new Claude investigation.

        Args:
            fingerprint_id: Failure signature ID
        """
        try:
            # Acquire lock
            if not self.queue.acquire_lock(fingerprint_id):
                logger.warning(f"Failed to acquire lock for {fingerprint_id}, skipping")
                return

            # Update status
            self.queue.update_status(fingerprint_id, self.queue.STATUS_STARTING)
            self.queue.set_started_at(fingerprint_id)

            # Get signature data from Elasticsearch
            signature = self.failure_store.get_signature(fingerprint_id)
            if not signature:
                logger.error(f"Signature not found for {fingerprint_id}")
                self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                self.queue.release_lock(fingerprint_id)
                return

            project = signature.get('project', 'unknown')
            sample_clusters = signature.get('sample_clusters', [])

            # Create investigation context file
            context_file = self.report_manager.write_context(
                fingerprint_id=fingerprint_id,
                signature_data=signature,
                sample_clusters=sample_clusters,
                project=project
            )

            # Get output log path
            output_log = self.report_manager.get_investigation_log_path(fingerprint_id)

            # Launch investigation
            investigation_info = await self.agent_runner.launch_investigation(
                fingerprint_id=fingerprint_id,
                context_file=context_file,
                output_log=output_log,
                project=project,
                observability_manager=self.observability
            )

            if not investigation_info:
                logger.error(f"Failed to launch investigation for {fingerprint_id}")
                self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                self.queue.release_lock(fingerprint_id)
                return

            # Track active investigation
            self.active_processes[fingerprint_id] = investigation_info
            self.queue.update_status(fingerprint_id, self.queue.STATUS_IN_PROGRESS)
            self.queue.record_heartbeat(fingerprint_id)

            # Add to active set (since we're not using set_pid anymore with async tasks)
            from services.medic.claude_investigation_queue import ClaudeInvestigationQueue
            self.queue.redis.sadd("medic:claude_investigation:active", fingerprint_id)

            # Set up completion callback
            def done_callback(task):
                """Schedule completion handler in the event loop"""
                try:
                    # Use asyncio.create_task directly (requires Python 3.7+)
                    # This properly schedules in the current running loop
                    asyncio.create_task(self._investigation_completed(fingerprint_id, task))
                except Exception as e:
                    logger.error(f"Error scheduling investigation completion for {fingerprint_id}: {e}", exc_info=True)

            investigation_info['task'].add_done_callback(done_callback)

            logger.info(f"Claude investigation started for {fingerprint_id} (project: {project})")

            # TODO: Emit event for medic investigation started
            # self.observability.emit_event(EventType.MEDIC_CLAUDE_INVESTIGATION_STARTED, {
            #     "fingerprint_id": fingerprint_id,
            #     "project": project
            # })

        except Exception as e:
            logger.error(f"Failed to start investigation for {fingerprint_id}: {e}", exc_info=True)
            self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
            self.queue.release_lock(fingerprint_id)

    async def _investigation_completed(self, fingerprint_id: str, task: asyncio.Task):
        """
        Handle investigation completion.

        Args:
            fingerprint_id: Failure signature ID
            task: Completed task
        """
        try:
            # Check if task succeeded or failed
            if task.exception():
                logger.error(f"Claude investigation {fingerprint_id} failed with exception: {task.exception()}")
                self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                self.queue.set_result(fingerprint_id, self.queue.RESULT_FAILED)
            else:
                logger.info(f"Claude investigation {fingerprint_id} completed successfully")

                # Determine result based on reports created
                report_status = self.report_manager.get_status(fingerprint_id)

                if report_status == "diagnosed":
                    self.queue.set_result(fingerprint_id, self.queue.RESULT_SUCCESS)
                    self.queue.update_status(fingerprint_id, self.queue.STATUS_COMPLETED)

                    # Update signature investigation status in Elasticsearch
                    self.failure_store.update_investigation_status(
                        fingerprint_id,
                        "completed"
                    )

                    # TODO: Emit event for medic investigation completed
                    # self.observability.emit_event(EventType.MEDIC_CLAUDE_INVESTIGATION_COMPLETED, {
                    #     "fingerprint_id": fingerprint_id,
                    #     "result": "success"
                    # })

                elif report_status == "ignored":
                    self.queue.set_result(fingerprint_id, self.queue.RESULT_IGNORED)
                    self.queue.update_status(fingerprint_id, self.queue.STATUS_IGNORED)

                    # Update signature
                    self.failure_store.update_investigation_status(
                        fingerprint_id,
                        "ignored"
                    )

                    # TODO: Emit event for medic investigation completed
                    # self.observability.emit_event(EventType.MEDIC_CLAUDE_INVESTIGATION_COMPLETED, {
                    #     "fingerprint_id": fingerprint_id,
                    #     "result": "ignored"
                    # })

                else:
                    # Investigation ran but didn't produce expected reports
                    logger.warning(f"Claude investigation {fingerprint_id} completed but no reports found")
                    self.queue.set_result(fingerprint_id, self.queue.RESULT_FAILED)
                    self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)

            # Cleanup
            self.queue.set_completed_at(fingerprint_id)
            self.queue.remove_from_active(fingerprint_id)
            self.queue.release_lock(fingerprint_id)

            if fingerprint_id in self.active_processes:
                del self.active_processes[fingerprint_id]

        except Exception as e:
            logger.error(f"Error handling investigation completion for {fingerprint_id}: {e}", exc_info=True)

    async def heartbeat_monitor(self):
        """Monitor active investigations for stalls and timeouts"""
        logger.info("Claude heartbeat monitor started")

        while self.running:
            try:
                await asyncio.sleep(30)  # Check every 30 seconds

                active_fps = list(self.active_processes.keys())

                for fingerprint_id in active_fps:
                    try:
                        # Update progress (log line count)
                        line_count = self.report_manager.count_log_lines(fingerprint_id)
                        self.queue.update_progress(fingerprint_id, line_count)

                        # Check for stalls (NOTE: Investigation process should record its own heartbeats)
                        if self.queue.is_stalled(fingerprint_id):
                            logger.warning(f"Claude investigation {fingerprint_id} appears stalled (no heartbeat)")

                            # Mark as failed and cleanup
                            investigation_info = self.active_processes.get(fingerprint_id)
                            if investigation_info:
                                # Cancel the task
                                task = investigation_info.get('task')
                                if task and not task.done():
                                    task.cancel()

                                # Remove from active
                                del self.active_processes[fingerprint_id]

                            self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                            self.queue.set_result(fingerprint_id, self.queue.RESULT_FAILED)
                            self.queue.set_completed_at(fingerprint_id)
                            self.queue.remove_from_active(fingerprint_id)
                            self.queue.release_lock(fingerprint_id)

                            logger.info(f"Cleaned up stalled investigation {fingerprint_id}")

                    except Exception as e:
                        logger.error(f"Error monitoring investigation {fingerprint_id}: {e}")

            except Exception as e:
                logger.error(f"Error in heartbeat monitor: {e}", exc_info=True)

    async def _recover_stalled_investigations(self):
        """Recover from previous crashes - clean up stalled investigations"""
        try:
            active_fps = self.queue.get_active()

            logger.info(f"Found {len(active_fps)} active Claude investigations from previous run")

            for fingerprint_id in active_fps:
                try:
                    # Check if still has lock
                    status = self.queue.get_status(fingerprint_id)

                    if status in [self.queue.STATUS_QUEUED, self.queue.STATUS_STARTING]:
                        # Re-enqueue
                        logger.info(f"Re-enqueuing Claude investigation {fingerprint_id}")
                        self.queue.enqueue(fingerprint_id, priority="high")

                    elif status == self.queue.STATUS_IN_PROGRESS:
                        # Mark as failed (process was interrupted)
                        logger.info(f"Marking interrupted Claude investigation {fingerprint_id} as failed")
                        self.queue.update_status(fingerprint_id, self.queue.STATUS_FAILED)
                        self.queue.set_result(fingerprint_id, self.queue.RESULT_FAILED)
                        self.queue.set_completed_at(fingerprint_id)
                        self.queue.release_lock(fingerprint_id)
                        self.queue.remove_from_active(fingerprint_id)

                    else:
                        # Already completed/failed - just cleanup
                        self.queue.remove_from_active(fingerprint_id)
                        self.queue.release_lock(fingerprint_id)

                except Exception as e:
                    logger.error(f"Error recovering investigation {fingerprint_id}: {e}")

        except Exception as e:
            logger.error(f"Error in startup recovery: {e}", exc_info=True)

    async def auto_trigger_checker(self):
        """Periodically check for signatures that should auto-trigger investigation"""
        logger.info("Claude auto-trigger checker started")

        while self.running:
            try:
                # Run every 5 minutes
                await asyncio.sleep(300)

                logger.debug("Checking for Claude auto-trigger conditions...")

                # Get unresolved signatures with investigation_status = "not_started"
                try:
                    from elasticsearch.helpers import scan
                    query = {
                        "query": {
                            "bool": {
                                "must": [
                                    {"term": {"investigation_status": "not_started"}},
                                    {"bool": {
                                        "must_not": [
                                            {"term": {"status": "resolved"}},
                                            {"term": {"status": "ignored"}}
                                        ]
                                    }}
                                ]
                            }
                        },
                        "_source": ["fingerprint_id"]
                    }

                    results = self.failure_store.es.search(
                        index="medic-claude-failures-*",
                        body=query,
                        size=100
                    )

                    triggered = [hit['_source']['fingerprint_id'] for hit in results['hits']['hits']]

                    for fingerprint_id in triggered:
                        logger.info(f"Auto-triggering Claude investigation for {fingerprint_id}")
                        # Update investigation status to queued
                        self.failure_store.update_investigation_status(fingerprint_id, "queued")
                        # Enqueue investigation
                        self.queue.enqueue(fingerprint_id, priority="normal")

                    if triggered:
                        logger.info(f"Auto-triggered {len(triggered)} Claude investigations")

                except Exception as e:
                    logger.error(f"Error querying signatures for auto-trigger: {e}", exc_info=True)

            except Exception as e:
                logger.error(f"Claude auto-trigger checker error: {e}", exc_info=True)

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


async def main():
    """Main entry point for Claude investigation orchestrator"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration from environment
    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    es_hosts = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200").split(",")
    workspace_root = os.getenv("WORKSPACE_ROOT", "/workspace/clauditoreum")
    medic_dir = os.getenv("MEDIC_DIR", "/medic")

    # Create clients
    redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
    es_client = Elasticsearch(es_hosts)

    # Create and run orchestrator
    orchestrator = ClaudeInvestigationOrchestrator(
        redis_client=redis_client,
        es_client=es_client,
        workspace_root=workspace_root,
        medic_dir=medic_dir
    )

    await orchestrator.start()


if __name__ == "__main__":
    asyncio.run(main())

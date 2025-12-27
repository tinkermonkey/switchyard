"""
Simplified Investigation Orchestrator

Instead of launching containers directly, this creates tasks in the orchestrator's
task queue and lets the proven agent execution system handle the investigations.
"""

import logging
import json
import sys
import asyncio
from pathlib import Path
from typing import Dict, Optional

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from task_queue.task_queue import TaskQueue, TaskPriority
from services.medic.base.base_investigation_queue import BaseInvestigationQueue

logger = logging.getLogger(__name__)


class SimplifiedInvestigationOrchestrator:
    """
    Simplified investigation orchestrator that delegates to the main orchestrator.

    Instead of managing Docker containers and async tasks, this simply:
    1. Detects when an investigation should be triggered
    2. Creates an orchestrator task for the medic_investigator agent
    3. Monitors task completion
    """

    def __init__(
        self,
        redis_client,
        es_client,
        failure_store,
        queue: BaseInvestigationQueue,
        workspace_root: str = "/workspace/clauditoreum",
        medic_dir: str = "/medic",
    ):
        """
        Initialize simplified orchestrator.

        Args:
            redis_client: Redis client
            es_client: Elasticsearch client
            failure_store: Failure signature store
            queue: Investigation queue
            workspace_root: Orchestrator workspace root
            medic_dir: Directory for investigation reports
        """
        self.redis = redis_client
        self.es = es_client
        self.failure_store = failure_store
        self.queue = queue
        self.workspace_root = workspace_root
        self.medic_dir = medic_dir
        self.running = False

        # Initialize orchestrator task queue
        self.task_queue = TaskQueue(redis_client)

        logger.info(f"{self.__class__.__name__} initialized")

    async def start(self):
        """Start the orchestrator"""
        logger.info(f"Starting {self.__class__.__name__}...")
        self.running = True
        logger.info(f"{self.__class__.__name__} initialized and ready")

    async def stop(self):
        """Stop the orchestrator"""
        logger.info(f"Stopping {self.__class__.__name__}...")
        self.running = False
        logger.info(f"{self.__class__.__name__} stopped")

    async def queue_processor(self):
        """
        Process investigation queue by creating orchestrator tasks.

        This is much simpler than the original - just dequeue investigations
        and create tasks for the orchestrator to handle.
        """
        logger.info("Queue processor started")

        while self.running:
            try:
                # Check concurrent limit
                active_count = self.queue.get_active_count()
                logger.debug(f"Active investigations: {active_count}, MAX: {self.queue.MAX_CONCURRENT}")

                if active_count >= self.queue.MAX_CONCURRENT:
                    logger.debug(f"Max concurrent investigations reached, waiting...")
                    await asyncio.sleep(10)
                    continue

                # Get next investigation
                fingerprint_id = await self.queue.dequeue()

                if not fingerprint_id:
                    # Queue empty, wait
                    await asyncio.sleep(5)
                    continue

                logger.info(f"Dequeued investigation: {fingerprint_id}")

                # Start investigation by creating orchestrator task
                await self._start_investigation(fingerprint_id)

            except Exception as e:
                logger.error(f"Queue processor error: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _start_investigation(self, fingerprint_id: str):
        """
        Start investigation by creating an orchestrator task.

        Args:
            fingerprint_id: Failure signature ID
        """
        try:
            logger.info(f"Starting investigation for {fingerprint_id}")

            # Get signature data
            signature = self.failure_store.get_signature(fingerprint_id)
            if not signature:
                logger.error(f"Signature not found: {fingerprint_id}")
                self.queue.mark_completed(fingerprint_id, self.queue.RESULT_FAILED, es_store=self.failure_store)
                return

            # Prepare investigation context
            investigation_dir = Path(self.medic_dir) / fingerprint_id
            investigation_dir.mkdir(parents=True, exist_ok=True)

            context_file = investigation_dir / "context.json"
            with open(context_file, "w") as f:
                json.dump(signature, f, indent=2)

            # Create task for orchestrator
            task_description = f"Investigate failure pattern: {signature.get('error_pattern', 'unknown')[:100]}"

            task_context = {
                "failure_signature": signature,
                "investigation_dir": str(investigation_dir),
                "fingerprint_id": fingerprint_id,
            }

            # Enqueue task for medic_investigator agent
            task_id = self.task_queue.enqueue_task(
                agent_name="medic_investigator",
                project_name="clauditoreum",  # Investigating orchestrator itself
                description=task_description,
                context=task_context,
                priority=TaskPriority.HIGH,  # Investigations are important
                metadata={
                    "medic_investigation": True,
                    "fingerprint_id": fingerprint_id,
                }
            )

            logger.info(f"Created orchestrator task {task_id} for investigation {fingerprint_id}")

            # Mark as started (orchestrator will handle it from here, also updates ES)
            self.queue.mark_started(
                fingerprint_id,
                pid=0,
                container_name=f"medic-task-{task_id}",  # Track by task ID
                es_store=self.failure_store
            )

            logger.info(f"Investigation {fingerprint_id} delegated to orchestrator task {task_id}")

        except Exception as e:
            logger.error(f"Error starting investigation {fingerprint_id}: {e}", exc_info=True)
            self.queue.mark_completed(fingerprint_id, self.queue.RESULT_FAILED, es_store=self.failure_store)

    async def heartbeat_monitor(self):
        """
        Monitor investigation heartbeats (check task status in orchestrator).

        Simpler version - just check if orchestrator tasks are still running.
        """
        logger.info("Heartbeat monitor started")

        while self.running:
            try:
                # Check all active investigations
                active_fps = self.queue.get_all_active()

                for fp_id in active_fps:
                    # Get investigation info
                    info = self.queue.get_investigation_info(fp_id)
                    container_name = info.get("container_name", "")

                    # Extract task ID from container name
                    if container_name and container_name.startswith("medic-task-"):
                        task_id = container_name.replace("medic-task-", "")

                        # Check if task is still in queue or completed
                        task_info = self.task_queue.get_task(task_id)

                        if not task_info:
                            # Task not found - might have been cleaned up
                            logger.warning(f"Task {task_id} not found for investigation {fp_id}")
                            continue

                        task_status = task_info.get("status")

                        if task_status in ["completed", "failed"]:
                            logger.info(f"Investigation {fp_id} task completed with status: {task_status}")

                            result = self.queue.RESULT_SUCCESS if task_status == "completed" else self.queue.RESULT_FAILED
                            self.queue.mark_completed(fp_id, result, es_store=self.failure_store)

                # Sleep before next check
                await asyncio.sleep(30)

            except Exception as e:
                logger.error(f"Heartbeat monitor error: {e}", exc_info=True)
                await asyncio.sleep(30)

    async def auto_trigger_checker(self):
        """
        Check for signatures that should trigger investigations.

        Same as before - identifies high-occurrence failures.
        """
        logger.info("Auto-trigger checker started")

        while self.running:
            try:
                # Query for signatures that need investigation
                query = {
                    "query": {
                        "bool": {
                            "must": [
                                {"term": {"investigation_status": "pending"}},
                                {"range": {"occurrence_count": {"gte": 3}}},  # At least 3 occurrences
                            ]
                        }
                    },
                    "size": 10,
                    "sort": [{"occurrence_count": {"order": "desc"}}],
                }

                result = self.es.search(
                    index=self.failure_store.index_pattern,
                    body=query
                )

                for hit in result["hits"]["hits"]:
                    fingerprint_id = hit["_source"]["fingerprint_id"]

                    # Enqueue for investigation
                    if self.queue.enqueue(fingerprint_id, priority="high"):
                        logger.info(f"Auto-triggered investigation for {fingerprint_id}")
                        self.failure_store.update_investigation_status(fingerprint_id, "queued")

                # Check every 5 minutes
                await asyncio.sleep(300)

            except Exception as e:
                logger.error(f"Auto-trigger checker error: {e}", exc_info=True)
                await asyncio.sleep(300)

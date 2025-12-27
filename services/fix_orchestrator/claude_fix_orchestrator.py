"""
Claude Fix Orchestrator Service

Manages the execution of automated fixes for Claude tool failures.
Polls the fix execution queue and dispatches tasks to the fix agent runner.
"""

import asyncio
import logging
import signal
import sys
import os
import json
import redis
from pathlib import Path
from datetime import datetime
from functools import partial
from elasticsearch import Elasticsearch

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from services.fix_orchestrator.fix_execution_queue import ClaudeFixExecutionQueue
from services.fix_orchestrator.claude_fix_agent_runner import ClaudeFixAgentRunner
from monitoring.observability import get_observability_manager, EventType
from services.medic.claude_failure_signature_store import ClaudeFailureSignatureStore

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("ClaudeFixOrchestrator")


class ClaudeFixOrchestrator:
    """
    Orchestrates the execution of Claude Medic fixes.
    """

    def __init__(self, redis_client=None):
        """Initialize the orchestrator."""
        print("PRINT: ClaudeFixOrchestrator.__init__() called", flush=True)
        self.running = False
        print(f"PRINT: Set self.running = {self.running}", flush=True)

        # Use provided Redis client or create one
        if redis_client is None:
            redis_host = os.getenv('REDIS_HOST', 'redis')
            redis_port = int(os.getenv('REDIS_PORT', 6379))
            print(f"PRINT: Creating own Redis client at {redis_host}:{redis_port}", flush=True)
            self.redis_client = redis.Redis(
                host=redis_host,
                port=redis_port,
                decode_responses=True
            )
        else:
            print("PRINT: Using provided Redis client", flush=True)
            self.redis_client = redis_client
        print("PRINT: Redis client ready", flush=True)

        self.queue = ClaudeFixExecutionQueue(self.redis_client)
        print("PRINT: Queue created", flush=True)
        self.runner = ClaudeFixAgentRunner()
        print("PRINT: Runner created", flush=True)

        # Initialize Elasticsearch client for signature store
        es_hosts = os.getenv('ELASTICSEARCH_HOSTS', 'http://elasticsearch:9200').split(',')
        self.es_client = Elasticsearch(es_hosts)
        self.signature_store = ClaudeFailureSignatureStore(self.es_client)
        print("PRINT: Signature store created", flush=True)
        self.active_fixes = {}  # fingerprint_id -> fix_info
        self.poll_interval = 5.0  # seconds
        print(f"PRINT: Poll interval = {self.poll_interval}", flush=True)

        # Ensure directories exist
        self.logs_dir = Path("/workspace/orchestrator_data/medic/fix_logs")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        print(f"PRINT: Logs dir = {self.logs_dir}", flush=True)

        # Initialize observability manager
        self.observability = get_observability_manager()
        print("PRINT: Observability manager initialized", flush=True)
        print("PRINT: ClaudeFixOrchestrator.__init__() completed", flush=True)

    async def start(self):
        """Start the orchestrator service (initialization only)."""
        print("PRINT: ClaudeFixOrchestrator.start() called", flush=True)
        logger.info("ClaudeFixOrchestrator.start() called")
        self.running = True
        logger.info("Claude Fix Orchestrator started")

        # Background tasks (queue_processor, monitor) will be started by main.py
        # Keep this task alive with infinite wait
        try:
            while True:
                await asyncio.sleep(3600)
        except asyncio.CancelledError:
            logger.info("Fix orchestrator cancelled")
            self.running = False
            raise

    async def queue_processor(self):
        """Background task to process fix queue."""
        print("PRINT: ClaudeFixOrchestrator.queue_processor() called", flush=True)
        logger.info("ClaudeFixOrchestrator.queue_processor() called")
        logger.info("Fix queue processor started")
        try:
            while self.running:
                await self.process_queue()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            logger.info("Fix queue processor cancelled")
            raise
        except Exception as e:
            logger.error(f"Fix queue processor failed: {e}", exc_info=True)

    async def monitor_loop(self):
        """Background task to monitor active fixes."""
        logger.info("ClaudeFixOrchestrator.monitor_loop() called")
        logger.info("Fix monitor loop started")
        try:
            while self.running:
                await self.monitor_active_fixes()
                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:
            logger.info("Fix monitor loop cancelled")
            raise
        except Exception as e:
            logger.error(f"Fix monitor loop failed: {e}", exc_info=True)

    async def stop(self):
        """Stop the orchestrator service."""
        logger.info("Stopping Claude Fix Orchestrator...")
        self.running = False
        loop = asyncio.get_running_loop()

        # Cancel active fixes
        for fingerprint_id, fix_info in list(self.active_fixes.items()):
            logger.info(f"Cancelling active fix for {fingerprint_id}")
            await self.runner.kill_fix(fix_info)

            # Release lock
            await loop.run_in_executor(None, self.queue.release_lock, fingerprint_id)

        logger.info("Claude Fix Orchestrator stopped")

    async def process_queue(self):
        """Check queue for pending fixes and start them."""
        try:
            # Get next pending fix (blocking call, must run in executor to not block event loop)
            # Note: get_next_pending_fix is synchronous and blocking (5s timeout)
            loop = asyncio.get_running_loop()
            pending_fix = await loop.run_in_executor(None, self.queue.get_next_pending_fix)
            
            if not pending_fix:
                return

            fingerprint_id = pending_fix['fingerprint_id']
            project = pending_fix.get('project', 'unknown')
            fix_plan_path = pending_fix.get('fix_plan_path')

            logger.info(f"Found pending fix for {fingerprint_id} (project: {project})")

            # Validate fix plan exists
            if not fix_plan_path or not os.path.exists(fix_plan_path):
                logger.error(f"Fix plan not found for {fingerprint_id}: {fix_plan_path}")
                await loop.run_in_executor(
                    None,
                    partial(self.queue.update_status, fingerprint_id, 'failed', error="Fix plan file not found")
                )
                return

            # Acquire lock
            lock_acquired = await loop.run_in_executor(None, self.queue.acquire_lock, fingerprint_id)
            if not lock_acquired:
                logger.warning(f"Could not acquire lock for {fingerprint_id}, skipping")
                return

            # Prepare log file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = self.logs_dir / f"fix_{fingerprint_id}_{timestamp}.jsonl"

            # Emit agent_initialized event and get execution_id for tracking
            agent_execution_id = self.observability.emit_agent_initialized(
                agent="claude-fix-executor",
                task_id=f"fix-{fingerprint_id[:16]}",
                project=project,
                config={
                    "fingerprint_id": fingerprint_id,
                    "fix_plan_path": fix_plan_path,
                    "log_file": str(log_file)
                },
                container_name=None  # Fix runs on host, not in container
            )

            # Launch fix
            fix_info = await self.runner.launch_fix(
                fingerprint_id=fingerprint_id,
                fix_plan_file=fix_plan_path,
                output_log=str(log_file),
                project=project,
                observability_manager=self.observability,
                agent_execution_id=agent_execution_id
            )

            if fix_info:
                # Update status to in_progress
                await loop.run_in_executor(
                    None,
                    partial(self.queue.update_status, fingerprint_id, 'in_progress', log_file=str(log_file))
                )

                # Track active fix
                self.active_fixes[fingerprint_id] = fix_info
                logger.info(f"Started fix execution for {fingerprint_id}")

                # Track active fix in Redis
                container_name = fix_info.get('container_name', '')
                self.redis_client.hset(
                    f"fix:active:{fingerprint_id}",
                    mapping={
                        "fingerprint_id": fingerprint_id,
                        "status": "in_progress",
                        "started_at": datetime.now().isoformat(),
                        "log_file": str(log_file),
                        "container_name": container_name,
                        "project": project,
                        "agent_execution_id": agent_execution_id or ""
                    }
                )
                self.redis_client.expire(f"fix:active:{fingerprint_id}", 7200)  # 2 hour TTL

                # Store container name mapping for recovery
                if container_name:
                    self.redis_client.set(f"container:{container_name}", fingerprint_id)
                    self.redis_client.expire(f"container:{container_name}", 7200)

                # Emit event
                try:
                    self.observability.emit(
                        EventType.MEDIC_FIX_STARTED,
                        agent="medic-fix-executor",
                        task_id=fingerprint_id,
                        project=project,
                        data={
                            "fingerprint_id": fingerprint_id,
                            "fix_plan_path": fix_plan_path,
                            "log_file": str(log_file)
                        }
                    )
                except Exception as e:
                    logger.warning(f"Failed to emit observability event: {e}")
            else:
                logger.error(f"Failed to launch fix for {fingerprint_id}")
                await loop.run_in_executor(
                    None,
                    partial(self.queue.update_status, fingerprint_id, 'failed', error="Failed to launch runner process")
                )
                await loop.run_in_executor(None, self.queue.release_lock, fingerprint_id)

        except Exception as e:
            logger.error(f"Error processing queue: {e}", exc_info=True)

    async def monitor_active_fixes(self):
        """Monitor running fixes and handle completion."""
        completed_fixes = []
        loop = asyncio.get_running_loop()

        for fingerprint_id, fix_info in self.active_fixes.items():
            try:
                # Check for kill signal
                kill_signal = self.redis_client.get(f"fix:kill:{fingerprint_id}")
                if kill_signal:
                    logger.info(f"Kill signal received for {fingerprint_id}")
                    await self.runner.kill_fix(fix_info)
                    completed_fixes.append(fingerprint_id)

                    await loop.run_in_executor(
                        None,
                        partial(self.queue.update_status, fingerprint_id, 'failed', error="Killed by user request")
                    )
                    await loop.run_in_executor(None, self.queue.release_lock, fingerprint_id)
                    self.redis_client.delete(f"fix:kill:{fingerprint_id}")
                    self.redis_client.delete(f"fix:active:{fingerprint_id}")
                    container_name = fix_info.get('container_name', '')
                    if container_name:
                        self.redis_client.delete(f"container:{container_name}")

                    # Emit event
                    try:
                        self.observability.emit(
                            EventType.MEDIC_FIX_FAILED,
                            agent="medic-fix-executor",
                            task_id=fingerprint_id,
                            project=fix_info.get('project', 'unknown'),
                            data={"fingerprint_id": fingerprint_id, "reason": "killed_by_user"}
                        )

                        # Emit agent execution failure event for UI tracking
                        agent_execution_id = fix_info.get('agent_execution_id')
                        if agent_execution_id:
                            self.observability.emit_agent_failed(
                                agent_execution_id=agent_execution_id,
                                error="Killed by user request"
                            )
                    except Exception as e:
                        logger.warning(f"Failed to emit observability event: {e}")

                    logger.info(f"Fix {fingerprint_id} killed by user request")
                    continue

                if not self.runner.is_fix_running(fix_info):
                    completed_fixes.append(fingerprint_id)

                    # Check task result
                    task = fix_info.get('task')
                    success = False
                    error_msg = None

                    try:
                        if task:
                            result = task.result()
                            # Simple success check - if it finished without exception
                            success = True
                    except Exception as e:
                        error_msg = str(e)
                        logger.error(f"Fix task for {fingerprint_id} failed with exception: {e}")

                    # Update status
                    status = 'completed' if success else 'failed'
                    await loop.run_in_executor(
                        None,
                        partial(self.queue.update_status, fingerprint_id, status, error=error_msg)
                    )

                    # Release lock
                    await loop.run_in_executor(None, self.queue.release_lock, fingerprint_id)

                    logger.info(f"Fix execution for {fingerprint_id} finished: {status}")

                    # Remove from active tracking in Redis
                    self.redis_client.delete(f"fix:active:{fingerprint_id}")
                    container_name = fix_info.get('container_name', '')
                    if container_name:
                        self.redis_client.delete(f"container:{container_name}")

                    # Emit event
                    try:
                        project = fix_info.get('project', 'unknown')
                        if success:
                            self.observability.emit(
                                EventType.MEDIC_FIX_COMPLETED,
                                agent="medic-fix-executor",
                                task_id=fingerprint_id,
                                project=project,
                                data={"fingerprint_id": fingerprint_id}
                            )

                            # Mark signature as resolved when fix completes successfully
                            try:
                                self.signature_store.update_status(fingerprint_id, "resolved")
                                logger.info(f"Marked signature {fingerprint_id} as resolved after successful fix")
                            except Exception as e:
                                logger.warning(f"Failed to update signature status to resolved: {e}")
                        else:
                            self.observability.emit(
                                EventType.MEDIC_FIX_FAILED,
                                agent="medic-fix-executor",
                                task_id=fingerprint_id,
                                project=project,
                                data={"fingerprint_id": fingerprint_id, "error": error_msg}
                            )

                        # Emit agent execution completion event for UI tracking
                        agent_execution_id = fix_info.get('agent_execution_id')
                        if agent_execution_id:
                            if success:
                                self.observability.emit_agent_completed(
                                    agent_execution_id=agent_execution_id,
                                    outputs={"result": "success", "status": status}
                                )
                            else:
                                self.observability.emit_agent_failed(
                                    agent_execution_id=agent_execution_id,
                                    error=error_msg or "Fix execution failed"
                                )
                    except Exception as e:
                        logger.warning(f"Failed to emit observability event: {e}")

            except Exception as e:
                logger.error(f"Error monitoring fix {fingerprint_id}: {e}", exc_info=True)

        # Remove completed from tracking
        for fingerprint_id in completed_fixes:
            self.active_fixes.pop(fingerprint_id, None)


if __name__ == "__main__":
    orchestrator = ClaudeFixOrchestrator()
    asyncio.run(orchestrator.start())

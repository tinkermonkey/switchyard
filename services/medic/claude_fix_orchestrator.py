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

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.parent))

from services.medic.fix_execution_queue import ClaudeFixExecutionQueue
from services.medic.claude_fix_agent_runner import ClaudeFixAgentRunner

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

    def __init__(self):
        """Initialize the orchestrator."""
        self.running = False
        
        # Initialize Redis client
        redis_host = os.getenv('REDIS_HOST', 'redis')
        redis_port = int(os.getenv('REDIS_PORT', 6379))
        self.redis_client = redis.Redis(
            host=redis_host, 
            port=redis_port, 
            decode_responses=True
        )
        
        self.queue = ClaudeFixExecutionQueue(self.redis_client)
        self.runner = ClaudeFixAgentRunner()
        self.active_fixes = {}  # fingerprint_id -> fix_info
        self.poll_interval = 5.0  # seconds

        # Ensure directories exist
        self.logs_dir = Path("/workspace/orchestrator_data/medic/fix_logs")
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    async def start(self):
        """Start the orchestrator service."""
        self.running = True
        logger.info("Claude Fix Orchestrator started")

        # Register signal handlers
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        try:
            while self.running:
                await self.process_queue()
                await self.monitor_active_fixes()
                await asyncio.sleep(self.poll_interval)
        except Exception as e:
            logger.error(f"Orchestrator main loop failed: {e}", exc_info=True)
        finally:
            await self.stop()

    async def stop(self):
        """Stop the orchestrator service."""
        logger.info("Stopping Claude Fix Orchestrator...")
        self.running = False
        
        # Cancel active fixes
        for fingerprint_id, fix_info in list(self.active_fixes.items()):
            logger.info(f"Cancelling active fix for {fingerprint_id}")
            await self.runner.kill_fix(fix_info)
            
            # Release lock
            await self.queue.release_lock(fingerprint_id)
            
        logger.info("Claude Fix Orchestrator stopped")

    async def process_queue(self):
        """Check queue for pending fixes and start them."""
        try:
            # Get next pending fix (blocking call, run in executor if needed, but for now direct call)
            # Note: get_next_pending_fix is synchronous and blocking (5s timeout)
            pending_fix = self.queue.get_next_pending_fix()
            
            if not pending_fix:
                return

            fingerprint_id = pending_fix['fingerprint_id']
            project = pending_fix.get('project', 'unknown')
            fix_plan_path = pending_fix.get('fix_plan_path')

            logger.info(f"Found pending fix for {fingerprint_id} (project: {project})")

            # Validate fix plan exists
            if not fix_plan_path or not os.path.exists(fix_plan_path):
                logger.error(f"Fix plan not found for {fingerprint_id}: {fix_plan_path}")
                self.queue.update_status(
                    fingerprint_id, 
                    'failed', 
                    error="Fix plan file not found"
                )
                return

            # Acquire lock
            if not self.queue.acquire_lock(fingerprint_id):
                logger.warning(f"Could not acquire lock for {fingerprint_id}, skipping")
                return

            # Prepare log file
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_file = self.logs_dir / f"fix_{fingerprint_id}_{timestamp}.jsonl"

            # Launch fix
            fix_info = await self.runner.launch_fix(
                fingerprint_id=fingerprint_id,
                fix_plan_file=fix_plan_path,
                output_log=str(log_file),
                project=project
            )

            if fix_info:
                # Update status to in_progress
                self.queue.update_status(
                    fingerprint_id, 
                    'in_progress',
                    log_file=str(log_file)
                )
                
                # Track active fix
                self.active_fixes[fingerprint_id] = fix_info
                logger.info(f"Started fix execution for {fingerprint_id}")
            else:
                logger.error(f"Failed to launch fix for {fingerprint_id}")
                self.queue.update_status(
                    fingerprint_id, 
                    'failed',
                    error="Failed to launch runner process"
                )
                self.queue.release_lock(fingerprint_id)

        except Exception as e:
            logger.error(f"Error processing queue: {e}", exc_info=True)

    async def monitor_active_fixes(self):
        """Monitor running fixes and handle completion."""
        completed_fixes = []

        for fingerprint_id, fix_info in self.active_fixes.items():
            try:
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
                    self.queue.update_status(
                        fingerprint_id, 
                        status,
                        error=error_msg
                    )
                    
                    # Release lock
                    self.queue.release_lock(fingerprint_id)
                    
                    logger.info(f"Fix execution for {fingerprint_id} finished: {status}")

            except Exception as e:
                logger.error(f"Error monitoring fix {fingerprint_id}: {e}", exc_info=True)

        # Remove completed from tracking
        for fingerprint_id in completed_fixes:
            self.active_fixes.pop(fingerprint_id, None)


if __name__ == "__main__":
    orchestrator = ClaudeFixOrchestrator()
    asyncio.run(orchestrator.start())

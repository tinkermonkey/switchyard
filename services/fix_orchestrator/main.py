"""
Fix Orchestrator Service Entry Point

Standalone service for executing Claude-generated fixes for medic-identified failures.
"""

import asyncio
import logging
import os
import sys
import signal
import redis
from pathlib import Path

# Add parent directory to path for shared imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from services.fix_orchestrator.claude_fix_orchestrator import ClaudeFixOrchestrator
from services.fix_orchestrator.fix_state_manager import FixStateManager

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("FixOrchestratorService")

async def main():
    logger.info("Starting Fix Orchestrator Service...")

    # Configuration
    redis_host = os.environ.get("REDIS_HOST", "redis")
    redis_port = int(os.environ.get("REDIS_PORT", 6379))

    # Initialize Redis client
    try:
        redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        redis_client.ping()
        logger.info("Redis client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Redis client: {e}")
        return

    # Create orchestrator
    orchestrator = ClaudeFixOrchestrator(redis_client=redis_client)

    # Perform startup recovery
    logger.info("Performing startup recovery...")
    state_manager = FixStateManager(orchestrator.queue, orchestrator.runner, orchestrator.redis_client)
    recovery_stats = state_manager.recover_on_startup()
    logger.info(f"Recovery complete: {recovery_stats}")

    # Setup graceful shutdown
    def signal_handler(signum, frame):
        logger.info(f"Received signal {signum}, initiating shutdown...")
        orchestrator.running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Start orchestrator (initialization + background tasks)
    tasks = [
        asyncio.create_task(orchestrator.start(), name="FixOrchestrator"),
        asyncio.create_task(orchestrator.queue_processor(), name="QueueProcessor"),
        asyncio.create_task(orchestrator.monitor_loop(), name="MonitorLoop")
    ]

    logger.info("Fix Orchestrator Service running...")

    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except asyncio.CancelledError:
        logger.info("Service cancelled")
    except Exception as e:
        logger.error(f"Service failed: {e}", exc_info=True)
    finally:
        await orchestrator.stop()
        logger.info("Fix Orchestrator Service stopped")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

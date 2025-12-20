"""
Medic Unified Service

Runs all Medic-related monitoring services in a single container:
- Docker Log Monitor
- Docker Investigation Orchestrator
- Claude Failure Monitor
- Claude Investigation Orchestrator
- Claude Signature Curator

Note: Claude Fix Orchestrator runs as a separate service (fix-orchestrator).
"""

import asyncio
import logging
import os
import sys
import docker
import redis
from elasticsearch import Elasticsearch
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent.parent))

# Docker system
from services.medic.docker import (
    DockerLogMonitor,
    FingerprintEngine,
    DockerFailureSignatureStore,
    DockerInvestigationOrchestrator,
)

# Claude system
from services.medic.claude import (
    ClaudeFailureMonitor,
    ClaudeInvestigationOrchestrator,
    ClaudeSignatureCurator,
)

# Configure logging
logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("MedicUnifiedService")

async def main():
    logger.info("Starting Medic Unified Service...")

    # --- Configuration ---
    redis_host = os.environ.get("REDIS_HOST", "redis")
    redis_port = int(os.environ.get("REDIS_PORT", 6379))
    es_hosts = os.environ.get("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200").split(",")
    # Handle single URL vs list for ES
    if len(es_hosts) == 1 and "," not in es_hosts[0]:
        es_hosts_list = [es_hosts[0]]
    else:
        es_hosts_list = es_hosts

    workspace_root = os.environ.get("WORKSPACE_ROOT", "/workspace/clauditoreum")
    medic_dir = os.environ.get("MEDIC_DIR", "/medic")

    # --- Shared Clients ---
    try:
        redis_client = redis.Redis(host=redis_host, port=redis_port, decode_responses=True)
        redis_client.ping()
        logger.info("Redis client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Redis client: {e}")
        return

    try:
        es_client = Elasticsearch(es_hosts_list)
        if not es_client.ping():
             logger.warning("Elasticsearch ping failed, but continuing...")
        logger.info(f"Elasticsearch client initialized: {es_hosts_list}")
    except Exception as e:
        logger.error(f"Failed to initialize Elasticsearch client: {e}")
        return

    try:
        docker_client = docker.from_env()
        logger.info("Docker client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Docker client: {e}")
        # We might want to continue even if Docker fails, but DockerLogMonitor won't work
        docker_client = None

    # --- Initialize Services ---

    tasks = []

    # 1. Docker Log Monitor
    if docker_client:
        fingerprint_engine = FingerprintEngine()
        failure_store = DockerFailureSignatureStore(es_client)
        docker_monitor = DockerLogMonitor(docker_client, fingerprint_engine, failure_store, redis_client)
        tasks.append(asyncio.create_task(docker_monitor.start_monitoring(), name="DockerLogMonitor"))
        logger.info("Initialized DockerLogMonitor with historical scan support")

    # 2. Claude Failure Monitor
    claude_failure_monitor = ClaudeFailureMonitor(
        redis_host=redis_host,
        redis_port=redis_port,
        elasticsearch_hosts=es_hosts_list
    )
    tasks.append(asyncio.create_task(claude_failure_monitor.run(), name="ClaudeFailureMonitor"))
    logger.info("Initialized ClaudeFailureMonitor")

    # 3. Docker Investigation Orchestrator
    docker_investigation_orchestrator = DockerInvestigationOrchestrator(
        redis_client=redis_client,
        es_client=es_client,
        workspace_root=workspace_root,
        medic_dir=medic_dir
    )
    tasks.append(asyncio.create_task(docker_investigation_orchestrator.start(), name="DockerInvestigationOrchestrator"))
    logger.info("Initialized DockerInvestigationOrchestrator")

    # Create background tasks for Docker Investigation Orchestrator at top level (FIX for event loop issue)
    tasks.append(asyncio.create_task(docker_investigation_orchestrator.queue_processor(), name="DockerInvestigationQueueProcessor"))
    tasks.append(asyncio.create_task(docker_investigation_orchestrator.heartbeat_monitor(), name="DockerInvestigationHeartbeatMonitor"))
    tasks.append(asyncio.create_task(docker_investigation_orchestrator.auto_trigger_checker(), name="DockerInvestigationAutoTrigger"))
    logger.info("Started Docker Investigation Orchestrator background tasks")

    # 4. Claude Investigation Orchestrator
    claude_investigation_orchestrator = ClaudeInvestigationOrchestrator(
        redis_client=redis_client,
        es_client=es_client,
        workspace_root=workspace_root,
        medic_dir=medic_dir
    )
    tasks.append(asyncio.create_task(claude_investigation_orchestrator.start(), name="ClaudeInvestigationOrchestrator"))
    logger.info("Initialized ClaudeInvestigationOrchestrator")

    # Create background tasks for Claude Investigation Orchestrator at top level (FIX for event loop issue)
    tasks.append(asyncio.create_task(claude_investigation_orchestrator.queue_processor(), name="ClaudeInvestigationQueueProcessor"))
    tasks.append(asyncio.create_task(claude_investigation_orchestrator.heartbeat_monitor(), name="ClaudeInvestigationHeartbeatMonitor"))
    tasks.append(asyncio.create_task(claude_investigation_orchestrator.auto_trigger_checker(), name="ClaudeInvestigationAutoTrigger"))
    logger.info("Started Claude Investigation Orchestrator background tasks")

    # 5. Claude Signature Curator
    # Note: It initializes its own clients.
    claude_signature_curator = ClaudeSignatureCurator()
    tasks.append(asyncio.create_task(claude_signature_curator.start(), name="ClaudeSignatureCurator"))
    logger.info("Initialized ClaudeSignatureCurator")

    # --- Run All ---
    logger.info(f"Running {len(tasks)} services concurrently...")
    try:
        await asyncio.gather(*tasks, return_exceptions=False)
    except asyncio.CancelledError:
        logger.info("Medic Unified Service stopping...")
    except Exception as e:
        logger.error(f"Medic Unified Service failed: {e}", exc_info=True)
        # Log which task failed
        for i, task in enumerate(tasks):
            if task.done() and task.exception():
                logger.error(f"Task {i} ({task.get_name()}) failed: {task.exception()}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

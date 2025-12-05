"""
Medic Unified Service

Runs all Medic-related services in a single container:
- Docker Log Monitor
- Claude Failure Monitor
- Investigation Orchestrator (Standard)
- Claude Investigation Orchestrator
- Claude Fix Orchestrator
- Claude Signature Curator
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

from services.medic.docker_log_monitor import DockerLogMonitor
from services.medic.fingerprint_engine import FingerprintEngine
from services.medic.failure_signature_store import FailureSignatureStore

from services.medic.claude_failure_monitor import ClaudeFailureMonitor
from services.medic.investigation_orchestrator import InvestigationOrchestrator
from services.medic.claude_investigation_orchestrator import ClaudeInvestigationOrchestrator
from services.medic.claude_fix_orchestrator import ClaudeFixOrchestrator
from services.medic.claude_signature_curator import ClaudeSignatureCurator

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
        failure_store = FailureSignatureStore(es_client)
        docker_monitor = DockerLogMonitor(docker_client, fingerprint_engine, failure_store)
        tasks.append(asyncio.create_task(docker_monitor.start_monitoring(), name="DockerLogMonitor"))
        logger.info("Initialized DockerLogMonitor")

    # 2. Claude Failure Monitor
    claude_failure_monitor = ClaudeFailureMonitor(
        redis_host=redis_host,
        redis_port=redis_port,
        elasticsearch_hosts=es_hosts_list
    )
    tasks.append(asyncio.create_task(claude_failure_monitor.run(), name="ClaudeFailureMonitor"))
    logger.info("Initialized ClaudeFailureMonitor")

    # 3. Investigation Orchestrator (Standard)
    investigation_orchestrator = InvestigationOrchestrator(
        redis_client=redis_client,
        es_client=es_client,
        workspace_root=workspace_root,
        medic_dir=medic_dir
    )
    tasks.append(asyncio.create_task(investigation_orchestrator.start(), name="InvestigationOrchestrator"))
    logger.info("Initialized InvestigationOrchestrator")

    # 4. Claude Investigation Orchestrator
    claude_investigation_orchestrator = ClaudeInvestigationOrchestrator(
        redis_client=redis_client,
        es_client=es_client,
        workspace_root=workspace_root,
        medic_dir=medic_dir
    )
    tasks.append(asyncio.create_task(claude_investigation_orchestrator.start(), name="ClaudeInvestigationOrchestrator"))
    logger.info("Initialized ClaudeInvestigationOrchestrator")

    # 5. Claude Fix Orchestrator
    # Note: It initializes its own clients, which is fine.
    claude_fix_orchestrator = ClaudeFixOrchestrator()
    tasks.append(asyncio.create_task(claude_fix_orchestrator.start(), name="ClaudeFixOrchestrator"))
    logger.info("Initialized ClaudeFixOrchestrator")

    # 6. Claude Signature Curator
    # Note: It initializes its own clients.
    claude_signature_curator = ClaudeSignatureCurator()
    tasks.append(asyncio.create_task(claude_signature_curator.start(), name="ClaudeSignatureCurator"))
    logger.info("Initialized ClaudeSignatureCurator")

    # --- Run All ---
    logger.info(f"Running {len(tasks)} services concurrently...")
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Medic Unified Service stopping...")
    except Exception as e:
        logger.error(f"Medic Unified Service failed: {e}", exc_info=True)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass

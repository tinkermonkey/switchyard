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
import yaml
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

# AI normalization
from services.medic.claude_normalizer import ClaudeCodeNormalizer

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

    # --- Load Configuration ---
    config_path = Path(__file__).parent.parent.parent / "config" / "medic.yaml"
    medic_config = {}
    if config_path.exists():
        try:
            with open(config_path, 'r') as f:
                full_config = yaml.safe_load(f)
                medic_config = full_config.get("medic", {})
            logger.info(f"Loaded medic configuration from {config_path}")
        except Exception as e:
            logger.warning(f"Failed to load medic config: {e}, using defaults")
    else:
        logger.warning(f"Config file not found at {config_path}, using defaults")

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

    # Initialize AI Normalization (if enabled)
    ai_config = medic_config.get("fingerprinting", {}).get("ai_normalization", {})
    use_ai = ai_config.get("enabled", False)
    claude_normalizer = None

    if use_ai:
        try:
            claude_normalizer = ClaudeCodeNormalizer(
                redis_client=redis_client,
                cache_ttl=ai_config.get("cache_ttl", 86400)
            )
            logger.info("AI normalization enabled with Claude Code")
        except Exception as e:
            logger.warning(f"Failed to initialize Claude normalizer: {e}, AI normalization disabled")
            use_ai = False

    # 1. Docker Log Monitor
    if docker_client:
        fingerprint_engine = FingerprintEngine(
            claude_normalizer=claude_normalizer,
            use_ai=use_ai,
            confidence_threshold=ai_config.get("confidence_threshold", 0.8)
        )
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
    logger.info("Initialized DockerInvestigationOrchestrator (background tasks use APScheduler)")

    # 4. Claude Investigation Orchestrator
    claude_investigation_orchestrator = ClaudeInvestigationOrchestrator(
        redis_client=redis_client,
        es_client=es_client,
        workspace_root=workspace_root,
        medic_dir=medic_dir
    )
    tasks.append(asyncio.create_task(claude_investigation_orchestrator.start(), name="ClaudeInvestigationOrchestrator"))
    logger.info("Initialized ClaudeInvestigationOrchestrator (background tasks use APScheduler)")

    # 5. Claude Signature Curator
    # Note: It initializes its own clients.
    claude_signature_curator = ClaudeSignatureCurator()
    tasks.append(asyncio.create_task(claude_signature_curator.start(), name="ClaudeSignatureCurator"))
    logger.info("Initialized ClaudeSignatureCurator")

    # Add diagnostic task
    async def diagnostic_loop():
        logger.info("Diagnostic loop started")
        for i in range(10):
            logger.info(f"Diagnostic iteration {i+1} - before sleep")
            await asyncio.sleep(2)
            logger.info(f"Diagnostic iteration {i+1} - after sleep")
        logger.info("Diagnostic loop completed")

    tasks.append(asyncio.create_task(diagnostic_loop(), name="Diagnostic"))

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

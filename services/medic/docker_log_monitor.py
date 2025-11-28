"""
Docker Log Monitor Service

Continuously monitors Docker container logs for ERROR and WARNING patterns.
Streams logs from all orchestrator containers in real-time.
"""

import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Optional, Dict
import docker
from docker.models.containers import Container
from elasticsearch import Elasticsearch

from monitoring.timestamp_utils import utc_now, utc_isoformat
from .fingerprint_engine import FingerprintEngine
from .failure_signature_store import FailureSignatureStore

logger = logging.getLogger(__name__)


class DockerLogMonitor:
    """
    Monitors Docker container logs for ERROR and WARNING patterns.
    Streams logs from all orchestrator containers in real-time.
    """

    # Container patterns to monitor
    MONITORED_CONTAINER_PATTERNS = [
        "clauditoreum-orchestrator",
        "clauditoreum-observability-server",
        "clauditoreum-pattern-ingestion",
        "clauditoreum-elasticsearch",
        "clauditoreum-redis",
    ]

    # Log levels to monitor
    MONITORED_LEVELS = ["ERROR", "CRITICAL", "WARNING", "FATAL"]

    def __init__(
        self,
        docker_client: docker.DockerClient,
        fingerprint_engine: FingerprintEngine,
        failure_store: FailureSignatureStore,
    ):
        self.docker = docker_client
        self.fingerprint_engine = fingerprint_engine
        self.failure_store = failure_store
        self.streams = {}
        self.running = False

    async def start_monitoring(self):
        """Start monitoring all containers"""
        self.running = True
        logger.info("Starting Docker log monitor...")

        try:
            # Find and monitor containers
            for pattern in self.MONITORED_CONTAINER_PATTERNS:
                await self._monitor_container_pattern(pattern)

            logger.info(f"Monitoring {len(self.streams)} containers")

            # Keep running
            while self.running:
                await asyncio.sleep(10)

        except Exception as e:
            logger.error(f"Docker log monitor failed: {e}", exc_info=True)
        finally:
            self.running = False

    async def _monitor_container_pattern(self, pattern: str):
        """Monitor containers matching pattern"""
        try:
            containers = self.docker.containers.list(
                filters={"name": pattern}, all=False
            )

            for container in containers:
                if container.id not in self.streams:
                    logger.info(f"Starting to monitor container: {container.name}")
                    # Create async task to stream logs
                    task = asyncio.create_task(
                        self._stream_container_logs(container)
                    )
                    self.streams[container.id] = task

        except Exception as e:
            logger.error(f"Failed to find containers matching {pattern}: {e}")

    async def _stream_container_logs(self, container: Container):
        """Stream logs from a single container"""
        container_name = container.name
        container_id = container.id

        logger.info(f"Streaming logs from {container_name}")

        try:
            # Stream logs starting from now
            for log_line in container.logs(
                stream=True, follow=True, since=datetime.now()
            ):
                if not self.running:
                    break

                # Process log line
                await self._process_log_line(
                    container_name, container_id, log_line
                )

        except Exception as e:
            logger.error(
                f"Error streaming logs from {container_name}: {e}", exc_info=True
            )

    async def _process_log_line(
        self, container_name: str, container_id: str, log_line: bytes
    ):
        """Process a single log line"""
        try:
            # Decode log line
            line_str = log_line.decode("utf-8", errors="replace").strip()

            if not line_str:
                return

            # Parse log entry
            parsed = self._parse_log_line(line_str)

            # Check if this is an error/warning
            if parsed["level"] not in self.MONITORED_LEVELS:
                return

            logger.debug(
                f"Detected {parsed['level']} in {container_name}: {parsed['message'][:100]}"
            )

            # Generate fingerprint
            fingerprint = self.fingerprint_engine.generate(
                container_name=container_name, log_entry=parsed
            )

            # Store/update failure signature
            container_info = {"id": container_id, "name": container_name}
            await self.failure_store.record_occurrence(
                fingerprint, parsed, container_info
            )

        except Exception as e:
            logger.error(f"Failed to process log line: {e}", exc_info=True)

    def _parse_log_line(self, line: str) -> dict:
        """
        Parse log line to extract structured data.

        Supports multiple log formats:
        - JSON logs (pythonjsonlogger)
        - Standard Python logging format
        - Plain text with log level
        """
        # Try to parse as JSON first (pythonjsonlogger format)
        if line.startswith("{"):
            try:
                data = json.loads(line)
                return {
                    "timestamp": data.get("asctime", data.get("timestamp", utc_isoformat())),
                    "level": data.get("levelname", data.get("level", "INFO")),
                    "message": data.get("message", line),
                    "name": data.get("name", ""),
                    "traceback": data.get("exc_text") or data.get("exception"),
                    "context": {
                        k: v
                        for k, v in data.items()
                        if k
                        not in [
                            "asctime",
                            "levelname",
                            "message",
                            "name",
                            "exc_text",
                            "exception",
                            "timestamp",
                            "level",
                        ]
                    },
                }
            except json.JSONDecodeError:
                pass

        # Try standard Python logging format
        # Format: "2025-11-28 12:45:23 - name - LEVEL - message"
        match = re.match(
            r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+-\s+(\S+)\s+-\s+(ERROR|WARNING|CRITICAL|FATAL|INFO|DEBUG)\s+-\s+(.+)",
            line,
        )
        if match:
            timestamp, name, level, message = match.groups()
            return {
                "timestamp": timestamp,
                "level": level,
                "message": message,
                "name": name,
                "traceback": None,
                "context": {},
            }

        # Try to extract log level from plain text
        # Look for patterns like: "ERROR:", "[ERROR]", etc.
        level = "INFO"  # Default
        for monitored_level in self.MONITORED_LEVELS:
            if (
                f"{monitored_level}:" in line
                or f"[{monitored_level}]" in line
                or f"{monitored_level} -" in line
            ):
                level = monitored_level
                break

        # Check for Python traceback
        traceback = None
        if "Traceback (most recent call last):" in line:
            traceback = line

        return {
            "timestamp": utc_isoformat(),
            "level": level,
            "message": line,
            "name": "",
            "traceback": traceback,
            "context": {},
        }

    def stop(self):
        """Stop monitoring"""
        logger.info("Stopping Docker log monitor...")
        self.running = False


async def main():
    """Main entry point for Medic Docker log monitor service"""
    import os

    # Setup logging
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )

    logger.info("Initializing Medic Docker Log Monitor...")

    # Initialize Docker client
    try:
        docker_client = docker.from_env()
        logger.info("Docker client initialized")
    except Exception as e:
        logger.error(f"Failed to initialize Docker client: {e}")
        return

    # Initialize Elasticsearch
    es_hosts = os.environ.get("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    try:
        es_client = Elasticsearch([es_hosts])
        logger.info(f"Elasticsearch client initialized: {es_hosts}")
    except Exception as e:
        logger.error(f"Failed to initialize Elasticsearch client: {e}")
        return

    # Initialize components
    fingerprint_engine = FingerprintEngine()
    failure_store = FailureSignatureStore(es_client)

    # Create and start monitor
    monitor = DockerLogMonitor(docker_client, fingerprint_engine, failure_store)

    try:
        await monitor.start_monitoring()
    except KeyboardInterrupt:
        logger.info("Received interrupt signal")
    finally:
        monitor.stop()
        logger.info("Medic Docker Log Monitor stopped")


if __name__ == "__main__":
    asyncio.run(main())

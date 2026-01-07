"""
Docker Log Monitor Service

Continuously monitors Docker container logs for ERROR and WARNING patterns.
Streams logs from all orchestrator containers in real-time.

Features:
- Historical log scanning on startup (configurable lookback period)
- Redis-based checkpoint tracking to avoid reprocessing
- Duplicate detection to prevent signature pollution
- Real-time streaming of new logs
"""

import asyncio
import json
import logging
import re
import hashlib
import os
import yaml
from datetime import datetime, timedelta
from typing import Optional, Dict
import docker
from docker.models.containers import Container
from elasticsearch import Elasticsearch
import redis

from monitoring.timestamp_utils import utc_now, utc_isoformat
from .fingerprint_engine import FingerprintEngine
from .docker_signature_store import DockerFailureSignatureStore

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

    # Redis keys
    REDIS_CHECKPOINT_KEY_PREFIX = "medic:docker_logs:checkpoint:"
    REDIS_PROCESSED_KEY_PREFIX = "medic:docker_logs:processed:"
    PROCESSED_TTL_DAYS = 7  # Keep processed hashes for 7 days

    def __init__(
        self,
        docker_client: docker.DockerClient,
        fingerprint_engine: FingerprintEngine,
        failure_store: DockerFailureSignatureStore,
        redis_client: Optional[redis.Redis] = None,
    ):
        self.docker = docker_client
        self.fingerprint_engine = fingerprint_engine
        self.failure_store = failure_store
        self.redis = redis_client
        self.streams = {}
        self.running = False

        # Load configuration
        self.config = self._load_config()

        # Stats
        self.historical_logs_processed = 0
        self.duplicates_skipped = 0

    def _load_config(self) -> dict:
        """Load medic configuration from YAML file"""
        config_path = "/app/config/medic.yaml"
        default_config = {
            "medic": {
                "monitoring": {
                    "historical_scan": {
                        "enabled": True,
                        "lookback_hours": 24,
                        "max_lookback_hours": 168
                    }
                }
            }
        }

        if not os.path.exists(config_path):
            logger.warning(f"Config file {config_path} not found, using defaults")
            return default_config

        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
                return config if config else default_config
        except Exception as e:
            logger.error(f"Failed to load config from {config_path}: {e}")
            return default_config

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
        """
        Stream logs from a single container.

        Two-phase approach:
        1. Historical scan: Process logs from lookback period
        2. Real-time stream: Monitor new logs as they appear
        """
        container_name = container.name
        container_id = container.id

        logger.info(f"Starting log monitoring for {container_name}")

        try:
            # Phase 1: Historical log scanning
            scan_start_time = await self._scan_historical_logs(container_name, container_id, container)

            # Phase 2: Real-time streaming from checkpoint
            logger.info(f"Starting real-time log streaming for {container_name}")

            # Run blocking Docker logs iteration in a thread to avoid blocking event loop
            await asyncio.to_thread(
                self._stream_logs_blocking,
                container,
                container_name,
                container_id,
                scan_start_time
            )

        except Exception as e:
            logger.error(
                f"Error streaming logs from {container_name}: {e}", exc_info=True
            )

    def _stream_logs_blocking(self, container: Container, container_name: str, container_id: str, scan_start_time):
        """
        Blocking synchronous method to stream Docker logs.
        Runs in a thread via asyncio.to_thread() to avoid blocking the event loop.
        """
        try:
            for log_line in container.logs(stream=True, follow=True, since=scan_start_time):
                if not self.running:
                    break

                # Skip non-bytes data (Docker SDK sometimes returns timestamps as integers)
                if not isinstance(log_line, bytes):
                    continue

                # Process log line synchronously
                self._process_log_line_sync(container_name, container_id, log_line, is_historical=False)

        except Exception as e:
            logger.error(f"Error in blocking log stream for {container_name}: {e}", exc_info=True)

    def _process_log_line_sync(self, container_name: str, container_id: str, log_line: bytes, is_historical: bool):
        """
        Synchronous version of log line processing for use in thread.
        """
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

            # Duplicate detection for historical logs
            if is_historical and self.redis:
                log_hash = hashlib.md5(f"{container_name}:{line_str}".encode()).hexdigest()
                cache_key = f"docker_log_monitor:seen:{log_hash}"

                if self.redis.get(cache_key):
                    return  # Already processed

                # Mark as seen (expire after 1 hour)
                self.redis.setex(cache_key, 3600, "1")

            # Generate fingerprint
            fingerprint = self.fingerprint_engine.generate(
                container_name=container_name, log_entry=parsed
            )

            # Store/update failure signature
            # Use asyncio.run() to call async storage from sync context
            try:
                import asyncio
                container_info = {"id": container_id, "name": container_name}
                asyncio.run(self.failure_store.record_occurrence(
                    fingerprint, parsed, container_info
                ))
                logger.info(f"Stored failure fingerprint {fingerprint.fingerprint_id} for {container_name}")
            except Exception as storage_error:
                logger.error(f"Failed to store failure to Elasticsearch: {storage_error}", exc_info=True)

        except Exception as e:
            logger.error(f"Failed to process log line: {e}")

    async def _scan_historical_logs(self, container_name: str, container_id: str, container: Container) -> datetime:
        """
        Scan historical logs from configured lookback period.

        Returns:
            datetime: The start time for real-time streaming (either checkpoint or now)
        """
        # Get configuration
        historical_config = self.config.get("medic", {}).get("monitoring", {}).get("historical_scan", {})
        enabled = historical_config.get("enabled", True)
        lookback_hours = historical_config.get("lookback_hours", 24)
        max_lookback_hours = historical_config.get("max_lookback_hours", 168)

        if not enabled:
            logger.info(f"Historical scan disabled for {container_name}, starting real-time stream")
            return datetime.now()

        # Validate lookback period
        lookback_hours = min(lookback_hours, max_lookback_hours)

        # Check for existing checkpoint in Redis
        checkpoint_key = f"{self.REDIS_CHECKPOINT_KEY_PREFIX}{container_name}"
        checkpoint = None

        if self.redis:
            try:
                checkpoint_str = self.redis.get(checkpoint_key)
                if checkpoint_str:
                    checkpoint = datetime.fromisoformat(checkpoint_str)
                    logger.info(f"Found checkpoint for {container_name}: {checkpoint}")
            except Exception as e:
                logger.warning(f"Failed to read checkpoint for {container_name}: {e}")

        # Determine scan start time
        if checkpoint:
            # Resume from checkpoint
            scan_start = checkpoint
            logger.info(f"Resuming from checkpoint for {container_name}: {scan_start}")
        else:
            # Start from lookback period
            scan_start = datetime.now() - timedelta(hours=lookback_hours)
            logger.info(f"Starting historical scan for {container_name} from {lookback_hours}h ago: {scan_start}")

        # Scan historical logs
        historical_count = 0
        try:
            for log_line in container.logs(since=scan_start, until=datetime.now()):
                if not self.running:
                    break

                await self._process_log_line(
                    container_name, container_id, log_line, is_historical=True
                )
                historical_count += 1

            logger.info(f"Historical scan complete for {container_name}: processed {historical_count} log lines")
            self.historical_logs_processed += historical_count

        except Exception as e:
            logger.error(f"Error during historical scan for {container_name}: {e}", exc_info=True)

        # Update checkpoint to now
        if self.redis:
            try:
                self.redis.set(checkpoint_key, datetime.now().isoformat())
            except Exception as e:
                logger.warning(f"Failed to update checkpoint for {container_name}: {e}")

        # Return current time for real-time streaming
        return datetime.now()

    async def _process_log_line(
        self, container_name: str, container_id: str, log_line: bytes, is_historical: bool = False
    ):
        """
        Process a single log line.

        Args:
            container_name: Name of the container
            container_id: ID of the container
            log_line: Raw log line bytes
            is_historical: True if processing historical logs (enables duplicate detection)
        """
        try:
            # Skip non-bytes data (Docker SDK sometimes returns timestamps as integers)
            if not isinstance(log_line, bytes):
                return

            # Decode log line
            line_str = log_line.decode("utf-8", errors="replace").strip()

            if not line_str:
                return

            # Parse log entry
            parsed = self._parse_log_line(line_str)

            # Check if this is an error/warning
            if parsed["level"] not in self.MONITORED_LEVELS:
                return

            # Duplicate detection for historical logs
            if is_historical and self.redis:
                if self._is_duplicate(container_name, parsed):
                    self.duplicates_skipped += 1
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

            # Mark as processed for duplicate detection
            if is_historical and self.redis:
                self._mark_as_processed(container_name, parsed)

        except Exception as e:
            logger.error(f"Failed to process log line: {e}", exc_info=True)

    def _is_duplicate(self, container_name: str, parsed: dict) -> bool:
        """
        Check if log entry has already been processed.

        Uses hash of (container, timestamp, message) to detect duplicates.
        """
        try:
            # Generate hash from key components
            hash_input = f"{container_name}:{parsed.get('timestamp', '')}:{parsed.get('message', '')}"
            log_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

            # Check Redis
            processed_key = f"{self.REDIS_PROCESSED_KEY_PREFIX}{log_hash}"
            return self.redis.exists(processed_key) > 0

        except Exception as e:
            logger.warning(f"Failed to check duplicate: {e}")
            return False  # On error, process anyway to be safe

    def _mark_as_processed(self, container_name: str, parsed: dict):
        """
        Mark log entry as processed.

        Stores hash in Redis with TTL to prevent reprocessing.
        """
        try:
            # Generate hash from key components
            hash_input = f"{container_name}:{parsed.get('timestamp', '')}:{parsed.get('message', '')}"
            log_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:16]

            # Store in Redis with TTL
            processed_key = f"{self.REDIS_PROCESSED_KEY_PREFIX}{log_hash}"
            ttl_seconds = self.PROCESSED_TTL_DAYS * 24 * 60 * 60
            self.redis.setex(processed_key, ttl_seconds, "1")

        except Exception as e:
            logger.warning(f"Failed to mark as processed: {e}")

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
        # Also handle format with milliseconds: "2025-11-28 12:45:23,123 - name - LEVEL - message"
        match = re.match(
            r"(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:,\d+)?)\s+-\s+(\S+)\s+-\s+(ERROR|WARNING|CRITICAL|FATAL|INFO|DEBUG)\s+-\s+(.+)",
            line,
        )
        if match:
            timestamp, name, level, message = match.groups()
            # Convert timestamp to ISO format
            # Handle both "2025-11-28 12:45:23" and "2025-11-28 12:45:23,123"
            timestamp_clean = timestamp.replace(',', '.').replace(' ', 'T')
            if '.' not in timestamp_clean:
                timestamp_clean += '.000000'
            timestamp_iso = timestamp_clean + 'Z'

            return {
                "timestamp": timestamp_iso,
                "level": level,
                "message": message,
                "name": name,
                "traceback": None,
                "context": {"logger": name},
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
    failure_store = DockerFailureSignatureStore(es_client)

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

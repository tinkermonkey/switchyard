"""
Claude Failure Monitor

Main service that queries Elasticsearch for Claude Code tool execution failures,
clusters them, fingerprints them, and stores signatures.

Runs periodically (every 5 minutes) to detect new failures.
"""

import asyncio
import logging
import os
import redis
from datetime import datetime, timedelta
from elasticsearch import Elasticsearch
from typing import List, Dict, Tuple

from .claude_clustering_engine import FailureClusteringEngine
from .claude_fingerprint_engine import ClaudeFingerprintEngine
from .claude_failure_signature_store import ClaudeFailureSignatureStore
from monitoring.observability import ObservabilityManager

logger = logging.getLogger(__name__)


class ClaudeFailureMonitor:
    """
    Monitors claude-streams-* indices for tool execution failures.

    Process:
    1. Query Elasticsearch for sessions with failures since last checkpoint
    2. For each session, retrieve ALL tool events (successes + failures)
    3. Cluster contiguous failures
    4. Fingerprint each cluster
    5. Create or update signatures in Elasticsearch
    """

    CHECK_INTERVAL_SECONDS = 300  # 5 minutes
    REDIS_LAST_PROCESSED_KEY = "claude_medic:last_processed_timestamp"
    REDIS_LOCK_KEY = "claude_medic:processing_lock"
    LOCK_TIMEOUT_SECONDS = 60

    def __init__(
        self,
        redis_host: str = "redis",
        redis_port: int = 6379,
        elasticsearch_hosts: List[str] = None
    ):
        self.logger = logger

        # Redis for state tracking
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )

        # Elasticsearch
        if elasticsearch_hosts is None:
            elasticsearch_hosts = ["http://elasticsearch:9200"]
        self.es = Elasticsearch(elasticsearch_hosts)

        # Components
        self.clustering_engine = FailureClusteringEngine()
        self.fingerprint_engine = ClaudeFingerprintEngine()
        self.signature_store = ClaudeFailureSignatureStore(self.es)

        # Observability
        self.obs = ObservabilityManager()

        # Metrics
        self.sessions_processed = 0
        self.clusters_created = 0
        self.signatures_created = 0
        self.signatures_updated = 0

    async def run(self):
        """Main entry point - setup and run monitor loop"""
        self.logger.info("Starting Claude Failure Monitor...")

        # Wait for services
        await self._wait_for_services()

        # Setup Elasticsearch template
        self.signature_store.setup_index_template()

        # Run monitoring loop
        while True:
            try:
                await self._monitoring_cycle()
            except Exception as e:
                self.logger.error(f"Error in monitoring cycle: {e}", exc_info=True)

            # Sleep until next cycle
            await asyncio.sleep(self.CHECK_INTERVAL_SECONDS)

    async def _monitoring_cycle(self):
        """Single monitoring cycle"""
        # Try to acquire lock
        lock_acquired = self.redis.set(
            self.REDIS_LOCK_KEY,
            "1",
            nx=True,
            ex=self.LOCK_TIMEOUT_SECONDS
        )

        if not lock_acquired:
            self.logger.debug("Another monitor instance is running, skipping cycle")
            return

        try:
            # Get last processed timestamp
            last_processed = self.redis.get(self.REDIS_LAST_PROCESSED_KEY)
            if not last_processed:
                # Default to 1 hour ago
                last_processed = (datetime.utcnow() - timedelta(hours=1)).isoformat() + 'Z'

            self.logger.info(f"Processing failures since {last_processed}")

            # Find sessions with failures
            sessions = self._find_sessions_with_failures(last_processed)

            self.logger.info(f"Found {len(sessions)} sessions with failures")

            # Process each session
            max_timestamp = last_processed
            for project, session_id, timestamp_range in sessions:
                try:
                    self._process_session(project, session_id, last_processed, datetime.utcnow().isoformat() + 'Z')
                    self.sessions_processed += 1

                    # Track max timestamp
                    if timestamp_range['max'] > max_timestamp:
                        max_timestamp = timestamp_range['max']

                except Exception as e:
                    self.logger.error(f"Failed to process session {session_id}: {e}", exc_info=True)

            # Update checkpoint
            self.redis.set(self.REDIS_LAST_PROCESSED_KEY, max_timestamp)

            self.logger.info(f"Monitoring cycle complete. Sessions: {len(sessions)}, Clusters: {self.clusters_created}, Signatures created: {self.signatures_created}, updated: {self.signatures_updated}")

        finally:
            # Release lock
            self.redis.delete(self.REDIS_LOCK_KEY)

    def _find_sessions_with_failures(self, since: str) -> List[Tuple[str, str, Dict]]:
        """
        Find sessions that have tool failures since given timestamp.

        Returns:
            List of (project, session_id, {min_timestamp, max_timestamp}) tuples
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"event_category": "tool_result"}},
                        {"term": {"success": False}},
                        {"range": {"timestamp": {"gt": since}}}
                    ]
                }
            },
            "aggs": {
                "by_session": {
                    "composite": {
                        "size": 100,
                        "sources": [
                            {"project": {"terms": {"field": "project"}}},
                            {"session_id": {"terms": {"field": "raw_event.event.session_id.keyword"}}}
                        ]
                    },
                    "aggs": {
                        "min_timestamp": {"min": {"field": "timestamp"}},
                        "max_timestamp": {"max": {"field": "timestamp"}}
                    }
                }
            },
            "size": 0
        }

        try:
            result = self.es.search(
                index="claude-streams-*",
                body=query
            )

            buckets = result.get('aggregations', {}).get('by_session', {}).get('buckets', [])

            sessions = []
            for bucket in buckets:
                project = bucket['key']['project']
                session_id = bucket['key']['session_id']
                min_ts = bucket['min_timestamp']['value_as_string']
                max_ts = bucket['max_timestamp']['value_as_string']

                sessions.append((project, session_id, {'min': min_ts, 'max': max_ts}))

            return sessions

        except Exception as e:
            self.logger.error(f"Failed to find sessions with failures: {e}")
            return []

    def _process_session(self, project: str, session_id: str, start_time: str, end_time: str):
        """Process a single session to create failure signatures"""
        # Cluster failures
        clusters = self.clustering_engine.cluster_failures_for_session(
            self.es,
            project,
            session_id,
            start_time,
            end_time
        )

        if not clusters:
            self.logger.debug(f"No failure clusters found for session {session_id}")
            return

        self.logger.info(f"Found {len(clusters)} failure clusters in session {session_id}")

        # Process each cluster
        for cluster in clusters:
            try:
                # Generate fingerprint
                fingerprint = self.fingerprint_engine.generate_from_cluster(cluster)

                # Create or update signature
                signature = self.signature_store.create_or_update_signature(
                    fingerprint,
                    cluster
                )

                # Track metrics
                self.clusters_created += 1
                if signature.get('cluster_count', 0) == 1:
                    self.signatures_created += 1
                    # TODO: Emit event for real-time UI updates
                    # self.obs.emit requires agent, task_id, project - need to adapt for medic events
                else:
                    self.signatures_updated += 1

                # Check auto-trigger thresholds
                self._check_auto_trigger(fingerprint.fingerprint_id, signature)

            except Exception as e:
                self.logger.error(f"Failed to process cluster {cluster.cluster_id}: {e}", exc_info=True)

    def _check_auto_trigger(self, fingerprint_id: str, signature: dict):
        """
        Check if signature meets thresholds for auto-investigation.

        Args:
            fingerprint_id: Failure signature ID
            signature: Signature document from Elasticsearch
        """
        try:
            # Check if already investigated
            investigation_status = signature.get('investigation_status', 'not_started')
            if investigation_status in ['queued', 'in_progress', 'completed', 'ignored']:
                return

            # Get signature metrics
            cluster_count = signature.get('cluster_count', 0)
            total_failures = signature.get('total_failures', 0)
            clusters_last_hour = signature.get('clusters_last_hour', 0)
            # Note: We don't have failures_last_hour, use total_failures as approximation

            # Load thresholds from config
            import yaml
            config_path = "/app/config/medic.yaml"
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = yaml.safe_load(f)

                auto_trigger_config = config.get('claude_failures', {}).get('auto_trigger', {})
                if not auto_trigger_config.get('enabled', False):
                    return

                thresholds = auto_trigger_config.get('thresholds', {})
            else:
                # Default thresholds
                thresholds = {
                    'cluster_count': {'total': 5, 'per_hour': 3},
                    'total_failures': {'total': 15, 'per_hour': 10}
                }

            # Check cluster-based thresholds
            cluster_threshold_total = thresholds.get('cluster_count', {}).get('total', 5)
            cluster_threshold_hour = thresholds.get('cluster_count', {}).get('per_hour', 3)

            # Check failure-based thresholds
            failure_threshold_total = thresholds.get('total_failures', {}).get('total', 15)

            triggered = False
            trigger_reason = None

            if cluster_count >= cluster_threshold_total:
                triggered = True
                trigger_reason = f"{cluster_count} clusters total (threshold: {cluster_threshold_total})"
            elif clusters_last_hour >= cluster_threshold_hour:
                triggered = True
                trigger_reason = f"{clusters_last_hour} clusters in last hour (threshold: {cluster_threshold_hour})"
            elif total_failures >= failure_threshold_total:
                triggered = True
                trigger_reason = f"{total_failures} total failures (threshold: {failure_threshold_total})"

            if triggered:
                # Enqueue investigation
                from .claude_investigation_queue import ClaudeInvestigationQueue
                queue = ClaudeInvestigationQueue(self.redis)

                enqueued = queue.enqueue(fingerprint_id, priority="normal")

                if enqueued:
                    self.logger.info(f"Auto-triggered Claude investigation for {fingerprint_id}: {trigger_reason}")

                    # Update signature investigation status
                    self.signature_store.update_investigation_status(fingerprint_id, 'queued')

                    # TODO: Emit event for real-time UI updates
                    # self.obs.emit requires agent, task_id, project - need to adapt for medic events

        except Exception as e:
            self.logger.error(f"Failed to check auto-trigger for {fingerprint_id}: {e}", exc_info=True)

    async def _wait_for_services(self):
        """Wait for Redis and Elasticsearch to be ready"""
        self.logger.info("Waiting for services to be ready...")

        # Wait for Redis
        max_retries = 30
        for i in range(max_retries):
            try:
                self.redis.ping()
                self.logger.info("Redis is ready")
                break
            except:
                if i == max_retries - 1:
                    raise Exception("Redis not available after 30 retries")
                await asyncio.sleep(1)

        # Wait for Elasticsearch
        for i in range(max_retries):
            try:
                self.es.ping()
                self.logger.info("Elasticsearch is ready")
                break
            except:
                if i == max_retries - 1:
                    raise Exception("Elasticsearch not available after 30 retries")
                await asyncio.sleep(1)


async def main():
    """Main entry point"""
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration from environment
    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    es_hosts = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200").split(",")

    # Create and run monitor
    monitor = ClaudeFailureMonitor(
        redis_host=redis_host,
        redis_port=redis_port,
        elasticsearch_hosts=es_hosts
    )

    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())

"""
Log Collector Service for Pattern Detection

Consumes events from Redis streams and publishes to Elasticsearch
for long-term storage and pattern analysis.
"""

import asyncio
import logging
import time
import json
from datetime import datetime
from typing import Optional, Dict, Any
import redis
from elasticsearch import Elasticsearch, helpers

from services.pattern_detection_schema import (
    AGENT_LOGS_MAPPING,
    AGENT_LOGS_TEMPLATE,
    AGENT_LOGS_ILM_POLICY,
    AGENT_EVENTS_TEMPLATE,
    CLAUDE_OTEL_ILM_POLICY,
    CLAUDE_OTEL_LOGS_TEMPLATE,
    CLAUDE_OTEL_METRICS_TEMPLATE,
    get_index_name,
    enrich_event,
)
from services.circuit_breaker import CircuitBreaker, CircuitBreakerOpen

logger = logging.getLogger(__name__)


class LogCollector:
    """
    Consumes logs from Redis streams and publishes to Elasticsearch
    """

    def __init__(
        self,
        redis_host: str = "redis",
        redis_port: int = 6379,
        elasticsearch_hosts: list = None,
        batch_size: int = 50,
        batch_timeout: float = 5.0
    ):
        """
        Initialize log collector

        Args:
            redis_host: Redis host
            redis_port: Redis port
            elasticsearch_hosts: List of Elasticsearch hosts
            batch_size: Number of events to batch before indexing
            batch_timeout: Max seconds to wait before flushing batch
        """
        self.redis_host = redis_host
        self.redis_port = redis_port
        self.batch_size = batch_size
        self.batch_timeout = batch_timeout

        # Initialize Redis connection
        self.redis = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )

        # Initialize Elasticsearch connection
        if elasticsearch_hosts is None:
            elasticsearch_hosts = ["http://elasticsearch:9200"]

        self.es = Elasticsearch(elasticsearch_hosts)

        # Stream keys and consumer group info
        self.event_stream = "orchestrator:event_stream"
        self.consumer_group = "log_collector"
        self.consumer_name = f"log_collector_{int(time.time())}"

        # Track last processed IDs for recovery
        self.last_event_id = "0-0"

        # Batch buffer
        self.batch = []
        self.last_flush = time.time()

        # Metrics
        self.events_processed = 0
        self.events_indexed = 0
        self.errors = 0

        # Circuit breakers
        self.redis_breaker = CircuitBreaker(
            name="redis_streams",
            failure_threshold=5,
            recovery_timeout=30,
            expected_exception=redis.ResponseError
        )
        self.elasticsearch_breaker = CircuitBreaker(
            name="elasticsearch_indexing",
            failure_threshold=3,
            recovery_timeout=60,
            expected_exception=Exception
        )

        logger.info(f"LogCollector initialized (consumer: {self.consumer_name})")

    def setup_elasticsearch(self):
        """Setup Elasticsearch indices and templates"""
        logger.info("Setting up Elasticsearch indices...")

        try:
            # Create ILM policy for agent logs (7-day retention)
            self.es.ilm.put_lifecycle(
                name="agent-logs-ilm-policy",
                body=AGENT_LOGS_ILM_POLICY
            )
            logger.info("Created ILM policy: agent-logs-ilm-policy (7-day retention)")
            
            # Create index templates for both new indices
            self.es.indices.put_index_template(
                name="agent-events-template",
                body=AGENT_EVENTS_TEMPLATE
            )
            logger.info("Created index template: agent-events-template")

            # Keep old template for migration period (with ILM policy)
            self.es.indices.put_index_template(
                name="agent-logs-template",
                body=AGENT_LOGS_TEMPLATE
            )
            logger.info("Created index template: agent-logs-template (legacy, with ILM policy)")

            # OTEL data stream ILM policy and override templates
            self.es.ilm.put_lifecycle(
                name="claude-otel-ilm-policy",
                body=CLAUDE_OTEL_ILM_POLICY
            )
            logger.info("Created ILM policy: claude-otel-ilm-policy (7-day retention)")

            self.es.indices.put_index_template(
                name="claude-otel-logs-ilm",
                body=CLAUDE_OTEL_LOGS_TEMPLATE
            )
            logger.info("Created index template: claude-otel-logs-ilm")

            self.es.indices.put_index_template(
                name="claude-otel-metrics-ilm",
                body=CLAUDE_OTEL_METRICS_TEMPLATE
            )
            logger.info("Created index template: claude-otel-metrics-ilm")

            # Create today's agent-events index
            today_events = get_index_name(event_category='agent_lifecycle')
            if not self.es.indices.exists(index=today_events):
                self.es.indices.create(index=today_events, body=AGENT_EVENTS_TEMPLATE['template'])
                logger.info(f"Created index: {today_events}")
            else:
                logger.info(f"Index already exists: {today_events}")

            return True

        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch: {e}")
            return False

    def setup_consumer_groups(self):
        """Setup Redis consumer groups for reliable consumption"""
        for stream_key in [self.event_stream]:
            try:
                # Try to create consumer group
                self.redis.xgroup_create(
                    name=stream_key,
                    groupname=self.consumer_group,
                    id="0",  # Start from beginning
                    mkstream=True
                )
                logger.info(f"Created consumer group '{self.consumer_group}' for stream '{stream_key}'")
            except redis.ResponseError as e:
                if "BUSYGROUP" in str(e):
                    # Group already exists
                    logger.info(f"Consumer group '{self.consumer_group}' already exists for '{stream_key}'")
                else:
                    logger.error(f"Error creating consumer group: {e}")

    async def consume_events(self):
        """
        Main consumption loop for both event streams
        """
        logger.info("Starting event consumption...")

        while True:
            try:
                # Read from both streams with circuit breaker protection
                try:
                    await self.redis_breaker.call(self._consume_agent_events)
                except CircuitBreakerOpen as e:
                    logger.debug(f"Redis circuit open for agent events: {e}")

                # Flush batch if timeout reached
                if time.time() - self.last_flush >= self.batch_timeout:
                    await self._flush_batch()

                # Small sleep to prevent tight loop
                await asyncio.sleep(0.1)

            except Exception as e:
                logger.error(f"Error in consumption loop: {e}")
                self.errors += 1
                await asyncio.sleep(1)  # Back off on errors

    async def _consume_agent_events(self):
        """Consume from agent event stream"""
        try:
            # Read from stream using consumer group
            messages = self.redis.xreadgroup(
                groupname=self.consumer_group,
                consumername=self.consumer_name,
                streams={self.event_stream: ">"},
                count=10,
                block=100  # Block for 100ms
            )

            if not messages:
                return

            for stream_name, events in messages:
                for event_id, event_data in events:
                    try:
                        # Parse event JSON
                        event_json = event_data.get("event")
                        if not event_json:
                            continue

                        event = json.loads(event_json)

                        # Enrich event
                        enriched = enrich_event(event)
                        enriched["_id"] = event_id  # Use Redis ID as Elasticsearch doc ID

                        # Add to batch
                        self.batch.append(enriched)
                        self.events_processed += 1

                        # Acknowledge message
                        self.redis.xack(self.event_stream, self.consumer_group, event_id)

                        # Flush batch if full
                        if len(self.batch) >= self.batch_size:
                            await self._flush_batch()

                    except Exception as e:
                        logger.error(f"Error processing event {event_id}: {e}")
                        self.errors += 1

        except redis.ResponseError as e:
            if "NOGROUP" in str(e):
                # Consumer group doesn't exist yet, recreate it
                try:
                    self.redis.xgroup_create(
                        name=self.event_stream,
                        groupname=self.consumer_group,
                        id="0",
                        mkstream=True
                    )
                    logger.debug(f"Created consumer group for {self.event_stream}")
                except redis.ResponseError as create_error:
                    if "BUSYGROUP" not in str(create_error):
                        logger.error(f"Failed to create consumer group: {create_error}")
            else:
                logger.error(f"Redis error consuming agent events: {e}")
        except Exception as e:
            logger.error(f"Error consuming agent events: {e}")

    async def _flush_batch(self):
        """Flush batch to Elasticsearch with circuit breaker protection"""
        if not self.batch:
            return

        try:
            await self.elasticsearch_breaker.call(self._do_flush_batch)
        except CircuitBreakerOpen as e:
            logger.warning(f"Elasticsearch circuit open, keeping batch buffered: {e}")
            # Keep batch buffered until circuit closes
        except Exception as e:
            logger.error(f"Error flushing batch to Elasticsearch: {e}")
            self.errors += 1
            # Keep batch for retry
            await asyncio.sleep(5)

    async def _do_flush_batch(self):
        """Perform the actual batch flush to Elasticsearch"""
        # Group documents by index based on event_category
        docs_by_index = {}

        for doc in self.batch:
            event_category = doc.get('event_category', 'other')
            index_name = get_index_name(event_category=event_category)

            if index_name not in docs_by_index:
                docs_by_index[index_name] = []

            doc_id = doc.pop("_id", None)
            action = {
                "_index": index_name,
                "_source": doc
            }
            if doc_id:
                action["_id"] = doc_id

            docs_by_index[index_name].append(action)

        # Bulk index to each index separately
        total_success = 0
        total_errors = 0

        for index_name, actions in docs_by_index.items():
            success_count, errors = helpers.bulk(
                self.es,
                actions,
                raise_on_error=False,
                raise_on_exception=False
            )

            total_success += success_count
            total_errors += len(errors) if errors else 0

            if errors:
                logger.warning(f"Bulk index to {index_name} had {len(errors)} errors")
                for error in errors[:3]:  # Log first 3 errors
                    logger.warning(f"Index error: {error}")

            logger.debug(f"Indexed {success_count} events to {index_name}")

        self.events_indexed += total_success
        self.errors += total_errors

        logger.debug(f"Batch flush complete: {total_success} indexed (total: {self.events_indexed})")

        # Clear batch
        self.batch = []
        self.last_flush = time.time()

    def get_stats(self) -> Dict[str, Any]:
        """Get collector statistics including circuit breaker state"""
        return {
            "events_processed": self.events_processed,
            "events_indexed": self.events_indexed,
            "errors": self.errors,
            "batch_size": len(self.batch),
            "circuit_breakers": {
                "redis": self.redis_breaker.get_state(),
                "elasticsearch": self.elasticsearch_breaker.get_state()
            }
        }

    async def run(self):
        """
        Main entry point - setup and run collector
        """
        logger.info("Starting Log Collector Service...")

        # Wait for services to be ready
        await self._wait_for_services()

        # Setup Elasticsearch
        if not self.setup_elasticsearch():
            logger.error("Failed to setup Elasticsearch, exiting")
            return

        # Setup consumer groups
        self.setup_consumer_groups()

        # Start consumption
        try:
            await self.consume_events()
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            # Flush remaining batch
            await self._flush_batch()

    async def _wait_for_services(self):
        """Wait for Redis and Elasticsearch to be ready"""
        logger.info("Waiting for services to be ready...")

        # Wait for Redis
        max_retries = 30
        for i in range(max_retries):
            try:
                self.redis.ping()
                logger.info("Redis is ready")
                break
            except:
                if i == max_retries - 1:
                    raise Exception("Redis not available after 30 retries")
                await asyncio.sleep(1)

        # Wait for Elasticsearch
        for i in range(max_retries):
            try:
                self.es.ping()
                logger.info("Elasticsearch is ready")
                break
            except:
                if i == max_retries - 1:
                    raise Exception("Elasticsearch not available after 30 retries")
                await asyncio.sleep(1)


async def main():
    """Main entry point"""
    import os

    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration from environment
    redis_host = os.getenv("REDIS_HOST", "redis")
    redis_port = int(os.getenv("REDIS_PORT", "6379"))
    es_hosts = os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200").split(",")

    # Create and run collector
    collector = LogCollector(
        redis_host=redis_host,
        redis_port=redis_port,
        elasticsearch_hosts=es_hosts
    )

    await collector.run()


if __name__ == "__main__":
    asyncio.run(main())

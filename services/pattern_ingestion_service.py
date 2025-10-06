"""
Pattern Ingestion Service (Consolidated)

Single service that handles the complete ingestion pipeline:
1. Log Collection: Redis streams → Elasticsearch
2. Pattern Detection: Elasticsearch → Pattern occurrences

Consolidates log-collector and pattern-detector into one efficient service.
"""

import asyncio
import logging
import os
import json
from typing import List
from elasticsearch import Elasticsearch
import redis

# Import the two services to consolidate
from services.log_collector import LogCollector
from services.pattern_detector_es import PatternDetector
from services.logging_config import setup_service_logging

# Setup logging with reduced verbosity
logger = setup_service_logging('pattern_ingestion')


class PatternIngestionService:
    """Consolidated log collection and pattern detection service"""

    def __init__(
        self,
        # Redis config (for log collection)
        redis_host: str = "redis",
        redis_port: int = 6379,
        # Elasticsearch config (shared)
        elasticsearch_hosts: List[str] = None,
        # Log collector config
        batch_size: int = 50,
        batch_timeout: float = 5.0,
        # Pattern detector config
        patterns_dir: str = "config/patterns",
        detection_interval: int = 60,
        lookback_minutes: int = 5
    ):
        """
        Initialize consolidated ingestion service

        Args:
            redis_host: Redis host for log collection
            redis_port: Redis port
            elasticsearch_hosts: Elasticsearch hosts (shared by both)
            batch_size: Log batch size
            batch_timeout: Log batch timeout
            patterns_dir: Pattern rules directory
            detection_interval: Pattern detection interval (seconds)
            lookback_minutes: Pattern detection lookback window
        """
        if elasticsearch_hosts is None:
            elasticsearch_hosts = ["http://elasticsearch:9200"]

        # Initialize shared Elasticsearch client
        self.es = Elasticsearch(elasticsearch_hosts)

        # Initialize Redis for publishing stats
        self.redis_client = redis.Redis(
            host=redis_host,
            port=redis_port,
            decode_responses=True
        )
        self.redis_stats_key = "orchestrator:pattern_ingestion_stats"

        # Initialize log collector
        self.log_collector = LogCollector(
            redis_host=redis_host,
            redis_port=redis_port,
            elasticsearch_hosts=elasticsearch_hosts,
            batch_size=batch_size,
            batch_timeout=batch_timeout
        )

        # Initialize pattern detector
        self.pattern_detector = PatternDetector(
            elasticsearch_hosts=elasticsearch_hosts,
            patterns_dir=patterns_dir,
            detection_interval=detection_interval,
            lookback_minutes=lookback_minutes
        )

        logger.info(
            "PatternIngestionService initialized "
            "(log collection + pattern detection consolidated)"
        )

    async def publish_stats_loop(self):
        """Periodically publish stats to Redis for observability"""
        while True:
            try:
                stats = self.get_stats()
                self.redis_client.setex(
                    self.redis_stats_key,
                    30,  # Expire after 30 seconds
                    json.dumps(stats)
                )
                await asyncio.sleep(5)  # Publish every 5 seconds
            except Exception as e:
                logger.error(f"Error publishing stats: {e}")
                await asyncio.sleep(5)

    async def run(self):
        """Start both services concurrently"""
        logger.info("Starting consolidated Pattern Ingestion Service...")

        # Initialize pattern detector (loads rules, waits for ES)
        await self.pattern_detector.initialize()

        # Run all services concurrently
        # log_collector.run() handles its own setup
        # pattern_detector.run() runs detection loop
        # publish_stats_loop() publishes health to Redis
        await asyncio.gather(
            self.log_collector.run(),
            self.pattern_detector.run(),
            self.publish_stats_loop(),
            return_exceptions=True
        )

    def get_stats(self):
        """Get aggregated stats from both services including circuit breaker state"""
        log_collector_stats = self.log_collector.get_stats()
        pattern_detector_stats = self.pattern_detector.get_stats()

        return {
            "service": "pattern_ingestion_consolidated",
            "log_collector": log_collector_stats,
            "pattern_detector": pattern_detector_stats,
            "health": {
                "redis_circuit": log_collector_stats["circuit_breakers"]["redis"]["state"],
                "elasticsearch_indexing_circuit": log_collector_stats["circuit_breakers"]["elasticsearch"]["state"],
                "pattern_detection_circuit": pattern_detector_stats["circuit_breaker"]["state"]
            }
        }


async def main():
    """Main entry point"""
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    # Get configuration from environment
    elasticsearch_hosts = [
        os.getenv("ELASTICSEARCH_HOSTS", "http://elasticsearch:9200")
    ]

    # Create and run consolidated service
    service = PatternIngestionService(
        # Redis
        redis_host=os.getenv("REDIS_HOST", "redis"),
        redis_port=int(os.getenv("REDIS_PORT", "6379")),
        # Elasticsearch (shared)
        elasticsearch_hosts=elasticsearch_hosts,
        # Log collector
        batch_size=int(os.getenv("LOG_BATCH_SIZE", "50")),
        batch_timeout=float(os.getenv("LOG_BATCH_TIMEOUT", "5.0")),
        # Pattern detector
        patterns_dir=os.getenv("PATTERNS_DIR", "config/patterns"),
        detection_interval=int(os.getenv("DETECTION_INTERVAL", "60")),
        lookback_minutes=int(os.getenv("LOOKBACK_MINUTES", "5"))
    )

    await service.run()


if __name__ == "__main__":
    asyncio.run(main())

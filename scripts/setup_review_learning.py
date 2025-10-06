#!/usr/bin/env python3
"""
Setup Review Learning System

Initializes Elasticsearch indices for the review feedback loop.
Run this once to set up the learning infrastructure.
"""

import sys
import asyncio
import logging
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from elasticsearch import Elasticsearch
from services.review_learning_schema import setup_review_learning_indices

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def main():
    """Setup review learning indices in Elasticsearch"""

    logger.info("=" * 80)
    logger.info("REVIEW LEARNING SYSTEM SETUP")
    logger.info("=" * 80)

    # Connect to Elasticsearch
    es_client = Elasticsearch(["http://elasticsearch:9200"])

    # Check connection
    if not es_client.ping():
        logger.error("Failed to connect to Elasticsearch at http://elasticsearch:9200")
        logger.error("Make sure Elasticsearch is running: docker-compose up elasticsearch")
        sys.exit(1)

    logger.info("Connected to Elasticsearch")

    try:
        # Setup indices
        logger.info("Creating review learning indices...")
        setup_review_learning_indices(es_client)

        logger.info("\n" + "=" * 80)
        logger.info("SETUP COMPLETE!")
        logger.info("=" * 80)
        logger.info("\nCreated indices:")
        logger.info("  - review-outcomes-YYYY.MM (monthly rotation)")
        logger.info("  - review-filters")
        logger.info("  - agent-performance")

        logger.info("\nNext steps:")
        logger.info("  1. Review cycles will automatically populate review-outcomes")
        logger.info("  2. Scheduled task runs daily at 3 AM to detect patterns")
        logger.info("  3. View learned filters in review-filters index")
        logger.info("  4. Monitor agent performance in agent-performance index")

        logger.info("\nManual trigger for testing:")
        logger.info("  from services.scheduled_tasks import get_scheduled_tasks_service")
        logger.info("  get_scheduled_tasks_service().run_review_learning_now()")

    except Exception as e:
        logger.error(f"Error setting up indices: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

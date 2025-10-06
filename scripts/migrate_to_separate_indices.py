#!/usr/bin/env python3
"""
Migration script to separate agent-logs-* into agent-events-* and claude-streams-*

This script:
1. Reads all documents from agent-logs-*
2. Routes them to the correct index based on event_category
3. Bulk reindexes with proper mappings
"""

import asyncio
import logging
from elasticsearch import Elasticsearch, helpers
from datetime import datetime
from collections import defaultdict

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def get_new_index_name(doc, original_index):
    """
    Determine the new index name based on event_category

    Args:
        doc: Document from Elasticsearch
        original_index: Original index name (e.g., agent-logs-2025-10-05)

    Returns:
        New index name (e.g., agent-events-2025-10-05 or claude-streams-2025-10-05)
    """
    event_category = doc.get('event_category', 'other')

    # Extract date from original index
    date_part = original_index.replace('agent-logs-', '')

    # Route based on category
    if event_category in ['agent_lifecycle', 'claude_api']:
        return f'agent-events-{date_part}'
    elif event_category in ['claude_stream', 'tool_call', 'tool_result', 'other', 'agent_output', 'agent_thinking']:
        return f'claude-streams-{date_part}'
    else:
        # Unknown category - default to agent-events
        logger.warning(f"Unknown event_category: {event_category}, defaulting to agent-events")
        return f'agent-events-{date_part}'


async def migrate_data():
    """Main migration function"""
    # Connect to Elasticsearch
    es = Elasticsearch(['http://elasticsearch:9200'])

    # Check if ES is available
    if not es.ping():
        logger.error("Cannot connect to Elasticsearch")
        return False

    logger.info("Connected to Elasticsearch")

    # Get all agent-logs indices
    indices = es.indices.get(index='agent-logs-*')
    logger.info(f"Found {len(indices)} agent-logs indices to migrate")

    total_migrated = 0
    stats = defaultdict(int)

    for index_name in sorted(indices.keys()):
        logger.info(f"Processing index: {index_name}")

        # Get document count
        count_result = es.count(index=index_name)
        total_docs = count_result['count']
        logger.info(f"  Total documents: {total_docs}")

        if total_docs == 0:
            logger.info(f"  Skipping empty index")
            continue

        # Group documents by new index
        docs_by_index = defaultdict(list)
        processed = 0

        # Scroll through all documents
        page = es.search(
            index=index_name,
            scroll='2m',
            size=100,
            body={"query": {"match_all": {}}}
        )

        scroll_id = page['_scroll_id']
        hits = page['hits']['hits']

        while hits:
            for hit in hits:
                source = hit['_source']
                doc_id = hit['_id']

                # Determine new index
                new_index = get_new_index_name(source, index_name)

                # Prepare document for reindex
                action = {
                    "_index": new_index,
                    "_id": doc_id,
                    "_source": source
                }

                docs_by_index[new_index].append(action)
                processed += 1
                stats[new_index] += 1

                # Bulk index when batch is full
                if len(docs_by_index[new_index]) >= 500:
                    success, errors = helpers.bulk(
                        es,
                        docs_by_index[new_index],
                        raise_on_error=False,
                        raise_on_exception=False
                    )
                    if errors:
                        logger.warning(f"  Bulk index errors: {len(errors)}")
                    logger.info(f"  Indexed {success} docs to {new_index}")
                    docs_by_index[new_index] = []

            # Get next page
            page = es.scroll(scroll_id=scroll_id, scroll='2m')
            scroll_id = page['_scroll_id']
            hits = page['hits']['hits']

            if processed % 1000 == 0:
                logger.info(f"  Processed {processed}/{total_docs} documents...")

        # Clear scroll
        es.clear_scroll(scroll_id=scroll_id)

        # Flush remaining documents
        for new_index, actions in docs_by_index.items():
            if actions:
                success, errors = helpers.bulk(
                    es,
                    actions,
                    raise_on_error=False,
                    raise_on_exception=False
                )
                if errors:
                    logger.warning(f"  Bulk index errors: {len(errors)}")
                logger.info(f"  Indexed {success} docs to {new_index}")

        total_migrated += processed
        logger.info(f"  Completed {index_name}: {processed} documents migrated")

    # Print summary
    logger.info("\n" + "="*60)
    logger.info("MIGRATION SUMMARY")
    logger.info("="*60)
    logger.info(f"Total documents migrated: {total_migrated}")
    logger.info("\nDocuments by new index:")
    for index_name, count in sorted(stats.items()):
        logger.info(f"  {index_name}: {count} documents")
    logger.info("="*60)

    # Refresh new indices
    logger.info("\nRefreshing new indices...")
    es.indices.refresh(index='agent-events-*')
    es.indices.refresh(index='claude-streams-*')
    logger.info("Refresh complete")

    return True


if __name__ == "__main__":
    success = asyncio.run(migrate_data())
    if success:
        logger.info("Migration completed successfully")
    else:
        logger.error("Migration failed")
        exit(1)

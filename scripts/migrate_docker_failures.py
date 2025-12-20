#!/usr/bin/env python3
"""
Elasticsearch Data Migration Script: Docker Failure Signatures

Migrates data from old index pattern to new unified schema:
- Old: medic-failure-signatures-*
- New: medic-docker-failures-*

Changes:
- Add type: "docker" field
- Add project: "orchestrator" field
- Rename sample_log_entries → sample_entries
- Add total_failures = occurrence_count
- Preserve all other fields

Usage:
    python scripts/migrate_docker_failures.py --dry-run      # Preview changes
    python scripts/migrate_docker_failures.py --execute      # Run migration
    python scripts/migrate_docker_failures.py --verify       # Verify migration
"""

import argparse
import json
import logging
import sys
from pathlib import Path
from typing import Dict, Any, List, Tuple
from datetime import datetime

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))

from elasticsearch import Elasticsearch, helpers
from elasticsearch.exceptions import NotFoundError

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class DockerFailureMigration:
    """Handles migration of Docker failure signatures to new schema."""

    OLD_INDEX_PATTERN = "medic-failure-signatures-*"
    NEW_INDEX_PATTERN = "medic-docker-failures-*"

    def __init__(self, es_hosts: List[str] = None):
        """
        Initialize migration tool.

        Args:
            es_hosts: List of Elasticsearch host URLs
        """
        if es_hosts is None:
            es_hosts = ["http://localhost:9200"]

        self.es = Elasticsearch(es_hosts)
        logger.info(f"Connected to Elasticsearch: {es_hosts}")

    def get_old_indices(self) -> List[str]:
        """Get list of old index names."""
        try:
            indices = self.es.indices.get(index=self.OLD_INDEX_PATTERN)
            return list(indices.keys())
        except NotFoundError:
            logger.warning(f"No indices found matching {self.OLD_INDEX_PATTERN}")
            return []

    def get_new_indices(self) -> List[str]:
        """Get list of new index names."""
        try:
            indices = self.es.indices.get(index=self.NEW_INDEX_PATTERN)
            return list(indices.keys())
        except NotFoundError:
            return []

    def count_documents(self, index_pattern: str) -> int:
        """Count total documents in index pattern."""
        try:
            result = self.es.count(index=index_pattern)
            return result['count']
        except Exception as e:
            logger.error(f"Failed to count documents in {index_pattern}: {e}")
            return 0

    def transform_document(self, doc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Transform old document to new schema.

        Args:
            doc: Old document from medic-failure-signatures-*

        Returns:
            Transformed document for medic-docker-failures-*
        """
        # Start with existing document
        new_doc = doc.copy()

        # Add new required fields
        new_doc['type'] = 'docker'
        new_doc['project'] = 'orchestrator'

        # Rename sample_log_entries to sample_entries if it exists
        if 'sample_log_entries' in new_doc:
            new_doc['sample_entries'] = new_doc.pop('sample_log_entries')

        # Add total_failures field (same as occurrence_count for Docker)
        if 'occurrence_count' in new_doc:
            new_doc['total_failures'] = new_doc['occurrence_count']

        return new_doc

    def generate_new_index_name(self, old_index_name: str) -> str:
        """
        Generate new index name from old index name.

        Args:
            old_index_name: e.g., "medic-failure-signatures-2025.01.15"

        Returns:
            New index name: e.g., "medic-docker-failures-2025.01.15"
        """
        # Extract date suffix if present
        if old_index_name.startswith("medic-failure-signatures-"):
            suffix = old_index_name.replace("medic-failure-signatures-", "")
            return f"medic-docker-failures-{suffix}"
        else:
            # Fallback: use timestamp
            timestamp = datetime.utcnow().strftime("%Y.%m.%d")
            return f"medic-docker-failures-{timestamp}"

    def dry_run(self) -> Tuple[int, List[str]]:
        """
        Preview migration without making changes.

        Returns:
            Tuple of (total_docs, index_names)
        """
        logger.info("=" * 80)
        logger.info("DRY RUN: Preview Migration")
        logger.info("=" * 80)

        old_indices = self.get_old_indices()
        if not old_indices:
            logger.warning("No old indices found to migrate")
            return 0, []

        total_docs = 0
        new_index_names = []

        for old_index in old_indices:
            new_index = self.generate_new_index_name(old_index)
            doc_count = self.count_documents(old_index)
            total_docs += doc_count

            logger.info(f"  {old_index} ({doc_count} docs)")
            logger.info(f"    → {new_index}")
            new_index_names.append(new_index)

        logger.info("=" * 80)
        logger.info(f"Total documents to migrate: {total_docs}")
        logger.info(f"Total indices to create: {len(new_index_names)}")
        logger.info("=" * 80)

        # Show sample transformation
        if old_indices:
            logger.info("\nSample document transformation:")
            sample = self._get_sample_document(old_indices[0])
            if sample:
                logger.info("\nOLD SCHEMA:")
                logger.info(json.dumps(sample, indent=2, default=str)[:500] + "...")

                transformed = self.transform_document(sample)
                logger.info("\nNEW SCHEMA:")
                logger.info(json.dumps(transformed, indent=2, default=str)[:500] + "...")

        return total_docs, new_index_names

    def _get_sample_document(self, index: str) -> Dict[str, Any]:
        """Get a sample document from index."""
        try:
            result = self.es.search(index=index, size=1)
            if result['hits']['hits']:
                return result['hits']['hits'][0]['_source']
        except Exception as e:
            logger.error(f"Failed to get sample document: {e}")
        return {}

    def execute_migration(self, batch_size: int = 500) -> Tuple[int, int]:
        """
        Execute the migration.

        Args:
            batch_size: Number of documents to process per batch

        Returns:
            Tuple of (migrated_count, error_count)
        """
        logger.info("=" * 80)
        logger.info("EXECUTING MIGRATION")
        logger.info("=" * 80)

        old_indices = self.get_old_indices()
        if not old_indices:
            logger.warning("No old indices found to migrate")
            return 0, 0

        migrated_count = 0
        error_count = 0

        for old_index in old_indices:
            new_index = self.generate_new_index_name(old_index)

            logger.info(f"\nMigrating: {old_index} → {new_index}")

            # Check if new index already exists
            if self.es.indices.exists(index=new_index):
                logger.warning(f"  Index {new_index} already exists, skipping...")
                continue

            # Create new index with same settings and mappings
            try:
                old_settings = self.es.indices.get(index=old_index)
                old_mapping = old_settings[old_index]['mappings']
                old_settings_config = old_settings[old_index]['settings']

                # Create new index
                self.es.indices.create(
                    index=new_index,
                    body={
                        'settings': {
                            'number_of_shards': old_settings_config.get('index', {}).get('number_of_shards', 1),
                            'number_of_replicas': old_settings_config.get('index', {}).get('number_of_replicas', 1),
                        },
                        'mappings': old_mapping
                    }
                )
                logger.info(f"  Created index: {new_index}")

            except Exception as e:
                logger.error(f"  Failed to create index {new_index}: {e}")
                error_count += 1
                continue

            # Migrate documents using bulk API
            try:
                def doc_generator():
                    """Generate transformed documents for bulk indexing."""
                    # Scroll through old index
                    scroll_resp = self.es.search(
                        index=old_index,
                        scroll='5m',
                        size=batch_size,
                        body={'query': {'match_all': {}}}
                    )

                    scroll_id = scroll_resp['_scroll_id']
                    hits = scroll_resp['hits']['hits']

                    while hits:
                        for hit in hits:
                            transformed = self.transform_document(hit['_source'])
                            yield {
                                '_index': new_index,
                                '_id': hit['_id'],
                                '_source': transformed
                            }

                        # Get next batch
                        scroll_resp = self.es.scroll(scroll_id=scroll_id, scroll='5m')
                        scroll_id = scroll_resp['_scroll_id']
                        hits = scroll_resp['hits']['hits']

                    # Clear scroll
                    self.es.clear_scroll(scroll_id=scroll_id)

                # Bulk index
                success, errors = helpers.bulk(
                    self.es,
                    doc_generator(),
                    chunk_size=batch_size,
                    raise_on_error=False,
                    stats_only=False
                )

                migrated_count += success
                error_count += len(errors) if errors else 0

                logger.info(f"  Migrated {success} documents")
                if errors:
                    logger.warning(f"  Errors: {len(errors)}")
                    for error in errors[:5]:  # Show first 5 errors
                        logger.warning(f"    {error}")

            except Exception as e:
                logger.error(f"  Failed to migrate documents: {e}", exc_info=True)
                error_count += 1

        logger.info("=" * 80)
        logger.info(f"Migration complete: {migrated_count} documents migrated, {error_count} errors")
        logger.info("=" * 80)

        return migrated_count, error_count

    def verify_migration(self) -> bool:
        """
        Verify migration completed successfully.

        Returns:
            True if verification passed, False otherwise
        """
        logger.info("=" * 80)
        logger.info("VERIFYING MIGRATION")
        logger.info("=" * 80)

        old_count = self.count_documents(self.OLD_INDEX_PATTERN)
        new_count = self.count_documents(self.NEW_INDEX_PATTERN)

        logger.info(f"Old indices ({self.OLD_INDEX_PATTERN}): {old_count} documents")
        logger.info(f"New indices ({self.NEW_INDEX_PATTERN}): {new_count} documents")

        if old_count == new_count:
            logger.info("✅ Document counts match!")
            success = True
        else:
            logger.warning(f"⚠️  Document count mismatch: {old_count} vs {new_count}")
            success = False

        # Sample verification
        new_indices = self.get_new_indices()
        if new_indices:
            logger.info("\nSample document from new index:")
            sample = self._get_sample_document(new_indices[0])
            if sample:
                # Verify required fields
                required_fields = ['type', 'project', 'sample_entries', 'total_failures']
                missing_fields = [f for f in required_fields if f not in sample]

                if not missing_fields:
                    logger.info("✅ All required fields present")
                    logger.info(f"  type: {sample.get('type')}")
                    logger.info(f"  project: {sample.get('project')}")
                    logger.info(f"  sample_entries: {len(sample.get('sample_entries', []))} entries")
                    logger.info(f"  total_failures: {sample.get('total_failures')}")
                else:
                    logger.warning(f"⚠️  Missing fields: {missing_fields}")
                    success = False

        logger.info("=" * 80)
        return success


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Migrate Docker failure signatures to new schema"
    )
    parser.add_argument(
        '--mode',
        choices=['dry-run', 'execute', 'verify'],
        default='dry-run',
        help='Migration mode (default: dry-run)'
    )
    parser.add_argument(
        '--es-hosts',
        default='http://localhost:9200',
        help='Elasticsearch hosts (comma-separated)'
    )
    parser.add_argument(
        '--batch-size',
        type=int,
        default=500,
        help='Batch size for bulk operations (default: 500)'
    )

    # Legacy flags for backward compatibility
    parser.add_argument('--dry-run', action='store_true', help='Run in dry-run mode')
    parser.add_argument('--execute', action='store_true', help='Execute migration')
    parser.add_argument('--verify', action='store_true', help='Verify migration')

    args = parser.parse_args()

    # Handle legacy flags
    if args.dry_run:
        args.mode = 'dry-run'
    elif args.execute:
        args.mode = 'execute'
    elif args.verify:
        args.mode = 'verify'

    # Parse ES hosts
    es_hosts = [h.strip() for h in args.es_hosts.split(',')]

    # Initialize migrator
    migrator = DockerFailureMigration(es_hosts=es_hosts)

    # Execute based on mode
    if args.mode == 'dry-run':
        total_docs, new_indices = migrator.dry_run()
        logger.info("\nTo execute migration, run:")
        logger.info("  python scripts/migrate_docker_failures.py --execute")

    elif args.mode == 'execute':
        # Confirm before executing
        old_count = migrator.count_documents(migrator.OLD_INDEX_PATTERN)
        logger.info(f"\n⚠️  About to migrate {old_count} documents")
        logger.info("This will create new indices with transformed data.")
        logger.info("Old indices will be preserved for backup.\n")

        response = input("Continue? [y/N]: ")
        if response.lower() != 'y':
            logger.info("Migration cancelled")
            return

        migrated, errors = migrator.execute_migration(batch_size=args.batch_size)

        logger.info("\nTo verify migration, run:")
        logger.info("  python scripts/migrate_docker_failures.py --verify")

    elif args.mode == 'verify':
        success = migrator.verify_migration()
        if success:
            logger.info("\n✅ Migration verification passed!")
            logger.info("\nOld indices are preserved for 30 days as backup.")
            logger.info("To delete old indices after verification:")
            logger.info("  # Wait 30 days, then run:")
            logger.info("  # curl -X DELETE 'http://localhost:9200/medic-failure-signatures-*'")
        else:
            logger.warning("\n⚠️  Migration verification failed!")
            logger.warning("Review the errors above and re-run migration if needed.")
            sys.exit(1)


if __name__ == "__main__":
    main()

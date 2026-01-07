#!/usr/bin/env python3
"""
Signature Consolidation Script

Re-fingerprints all existing failure signatures with the new normalizers
and consolidates duplicates that now have the same fingerprint.

This addresses the signature explosion where 1,945 variants of "failed for X times"
were stored as separate signatures instead of being grouped together.

Usage:
    python scripts/consolidate_signatures.py [--dry-run] [--verbose]

Options:
    --dry-run   Show what would be done without making changes
    --verbose   Show detailed progress and statistics
"""

import argparse
import asyncio
import json
import logging
import sys
from collections import defaultdict
from datetime import datetime
from typing import Dict, List, Any, Optional, Set

from elasticsearch import Elasticsearch
from services.medic.docker.fingerprint_engine import FingerprintEngine
from services.medic.normalizers import get_default_normalizers

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class SignatureConsolidator:
    """Consolidates duplicate failure signatures using new normalizers."""

    def __init__(
        self,
        es_client: Elasticsearch,
        index_pattern: str = "medic-failure-signatures-*",
        dry_run: bool = False,
        verbose: bool = False
    ):
        self.es_client = es_client
        self.index_pattern = index_pattern
        self.dry_run = dry_run
        self.verbose = verbose
        self.fingerprint_engine = FingerprintEngine()

        # Statistics
        self.stats = {
            "total_signatures": 0,
            "unique_fingerprints": 0,
            "consolidated_signatures": 0,
            "deleted_signatures": 0,
            "errors": 0,
            "start_time": datetime.utcnow(),
        }

    def log_verbose(self, message: str):
        """Log message only if verbose mode is enabled."""
        if self.verbose:
            logger.info(message)

    async def fetch_all_signatures(self) -> List[Dict[str, Any]]:
        """
        Fetch all existing signatures from Elasticsearch.

        Returns:
            List of signature documents with metadata
        """
        logger.info(f"Fetching all signatures from {self.index_pattern}...")

        signatures = []

        try:
            # Use scroll API for large result sets
            response = self.es_client.search(
                index=self.index_pattern,
                body={
                    "query": {"match_all": {}},
                    "size": 1000,
                    "sort": ["_doc"]
                },
                scroll="5m"
            )

            scroll_id = response.get("_scroll_id")
            hits = response["hits"]["hits"]
            signatures.extend(hits)

            # Continue scrolling
            while len(hits) > 0:
                response = self.es_client.scroll(
                    scroll_id=scroll_id,
                    scroll="5m"
                )
                scroll_id = response.get("_scroll_id")
                hits = response["hits"]["hits"]
                signatures.extend(hits)

            # Clear scroll
            if scroll_id:
                self.es_client.clear_scroll(scroll_id=scroll_id)

            self.stats["total_signatures"] = len(signatures)
            logger.info(f"Fetched {len(signatures)} signatures")

            return signatures

        except Exception as e:
            logger.error(f"Failed to fetch signatures: {e}", exc_info=True)
            self.stats["errors"] += 1
            return []

    def re_fingerprint(self, signature: Dict[str, Any]) -> Optional[str]:
        """
        Re-calculate fingerprint using new normalizers.

        Args:
            signature: Original signature document

        Returns:
            New fingerprint ID or None on error
        """
        try:
            source = signature["_source"]

            # Extract the error pattern from nested signature object
            sig_obj = source.get("signature", {})
            error_pattern = sig_obj.get("error_pattern", "")

            if not error_pattern:
                # Fallback to sample entry if available
                samples = source.get("sample_entries", [])
                if samples and len(samples) > 0:
                    error_pattern = samples[0].get("raw_message", "")

                if not error_pattern:
                    self.log_verbose(f"Signature {signature['_id']} has no error_pattern")
                    return None

            # Generate new fingerprint using current normalizers
            container_name = sig_obj.get("container_pattern", "unknown")

            fingerprint = self.fingerprint_engine.generate(
                container_name=container_name,
                log_entry={
                    "message": error_pattern,
                    "level": source.get("severity", "ERROR"),
                    "timestamp": source.get("first_seen", ""),
                }
            )

            return fingerprint.fingerprint_id

        except Exception as e:
            logger.error(f"Failed to re-fingerprint signature {signature.get('_id')}: {e}")
            self.stats["errors"] += 1
            return None

    def group_by_fingerprint(
        self,
        signatures: List[Dict[str, Any]]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        Group signatures by their new fingerprint.

        Args:
            signatures: List of signature documents

        Returns:
            Dict mapping new fingerprint ID to list of old signatures
        """
        logger.info("Re-fingerprinting and grouping signatures...")

        fingerprint_groups = defaultdict(list)

        for sig in signatures:
            new_fingerprint = self.re_fingerprint(sig)
            if new_fingerprint:
                fingerprint_groups[new_fingerprint].append(sig)
            else:
                self.stats["errors"] += 1

        self.stats["unique_fingerprints"] = len(fingerprint_groups)

        logger.info(f"Found {len(fingerprint_groups)} unique fingerprints")
        logger.info(f"Reduction: {self.stats['total_signatures']} → {self.stats['unique_fingerprints']} " +
                   f"({100 - (self.stats['unique_fingerprints'] / self.stats['total_signatures'] * 100):.1f}% reduction)")

        return dict(fingerprint_groups)

    def merge_signatures(
        self,
        fingerprint: str,
        signatures: List[Dict[str, Any]]
    ) -> Dict[str, Any]:
        """
        Merge multiple signatures with the same fingerprint into one.

        Args:
            fingerprint: The new fingerprint ID
            signatures: List of signatures to merge

        Returns:
            Consolidated signature document
        """
        if len(signatures) == 1:
            # No merging needed, but update fingerprint ID
            source = signatures[0]["_source"].copy()
            source["fingerprint_id"] = fingerprint
            return source

        # Merge all signatures
        first = signatures[0]["_source"]
        first_sig_obj = first.get("signature", {})

        # Find earliest first_seen
        first_seen = min(
            sig["_source"].get("first_seen", "9999-99-99T99:99:99Z")
            for sig in signatures
        )

        # Find latest last_seen
        last_seen = max(
            sig["_source"].get("last_seen", "0000-00-00T00:00:00Z")
            for sig in signatures
        )

        # Sum occurrence counts and total failures
        total_occurrences = sum(
            sig["_source"].get("occurrence_count", 1)
            for sig in signatures
        )

        total_failures = sum(
            sig["_source"].get("total_failures", 1)
            for sig in signatures
        )

        # Collect all unique sample entries (limit to 10 most recent)
        all_samples = []
        for sig in signatures:
            samples = sig["_source"].get("sample_entries", [])
            all_samples.extend(samples)

        # Sort by timestamp and take 10 most recent
        all_samples.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
        sample_entries = all_samples[:10]

        # Use the most common error_type
        error_types = [
            sig["_source"].get("signature", {}).get("error_type", "Unknown")
            for sig in signatures
        ]
        most_common_error_type = max(set(error_types), key=error_types.count)

        # Build consolidated signature with correct schema
        consolidated = {
            "type": first.get("type", "docker"),
            "project": first.get("project", "orchestrator"),
            "fingerprint_id": fingerprint,
            "created_at": first.get("created_at"),
            "updated_at": datetime.utcnow().isoformat() + "Z",
            "first_seen": first_seen,
            "last_seen": last_seen,
            "signature": {
                "container_pattern": first_sig_obj.get("container_pattern", "unknown"),
                "error_type": most_common_error_type,
                "error_pattern": first_sig_obj.get("error_pattern", ""),
                "stack_signature": first_sig_obj.get("stack_signature", []),
                "normalized_message": first_sig_obj.get("normalized_message", ""),
            },
            "occurrence_count": total_occurrences,
            "total_failures": total_failures,
            "occurrences_last_hour": first.get("occurrences_last_hour", 0),
            "occurrences_last_day": total_occurrences,
            "severity": first.get("severity", "ERROR"),
            "impact_score": first.get("impact_score", 1.0),
            "status": first.get("status", "new"),
            "investigation_status": first.get("investigation_status", "not_started"),
            "investigation_metadata": first.get("investigation_metadata", {}),
            "version": first.get("version", 1),
            "sample_entries": sample_entries,
            "consolidated_from": len(signatures),  # Track how many signatures were merged
            "consolidated_at": datetime.utcnow().isoformat() + "Z",
        }

        return consolidated

    async def write_consolidated_signature(
        self,
        fingerprint: str,
        signature: Dict[str, Any]
    ) -> bool:
        """
        Write consolidated signature to Elasticsearch.

        Args:
            fingerprint: Fingerprint ID (used as document ID)
            signature: Consolidated signature document

        Returns:
            True if successful, False otherwise
        """
        try:
            if self.dry_run:
                self.log_verbose(f"[DRY RUN] Would write consolidated signature: {fingerprint}")
                return True

            # Use current date for index name
            today = datetime.utcnow().strftime("%Y.%m.%d")
            index_name = f"medic-failure-signatures-{today}"

            self.es_client.index(
                index=index_name,
                id=fingerprint,
                document=signature
            )

            self.log_verbose(f"Wrote consolidated signature: {fingerprint}")
            return True

        except Exception as e:
            logger.error(f"Failed to write consolidated signature {fingerprint}: {e}")
            self.stats["errors"] += 1
            return False

    async def delete_old_signatures(
        self,
        signatures: List[Dict[str, Any]]
    ) -> int:
        """
        Delete old signature documents.

        Args:
            signatures: List of signatures to delete

        Returns:
            Number of signatures deleted
        """
        deleted = 0

        for sig in signatures:
            try:
                if self.dry_run:
                    self.log_verbose(f"[DRY RUN] Would delete signature: {sig['_id']} from {sig['_index']}")
                    deleted += 1
                    continue

                self.es_client.delete(
                    index=sig["_index"],
                    id=sig["_id"]
                )
                deleted += 1

            except Exception as e:
                logger.error(f"Failed to delete signature {sig.get('_id')}: {e}")
                self.stats["errors"] += 1

        return deleted

    async def consolidate(self) -> Dict[str, Any]:
        """
        Main consolidation process.

        Returns:
            Statistics about the consolidation
        """
        logger.info("=" * 60)
        logger.info("Starting signature consolidation...")
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Index pattern: {self.index_pattern}")
        logger.info("=" * 60)

        # Step 1: Fetch all signatures
        signatures = await self.fetch_all_signatures()
        if not signatures:
            logger.error("No signatures found or fetch failed")
            return self.stats

        # Step 2: Group by new fingerprint
        fingerprint_groups = self.group_by_fingerprint(signatures)

        if not fingerprint_groups:
            logger.error("Failed to group signatures")
            return self.stats

        # Step 3: Process each group
        logger.info("Consolidating signatures...")

        for fingerprint, sig_group in fingerprint_groups.items():
            if len(sig_group) == 1:
                # No consolidation needed, but update fingerprint ID
                original_fp = sig_group[0]["_source"]["fingerprint_id"]
                if original_fp != fingerprint:
                    # Fingerprint changed, need to update
                    consolidated = self.merge_signatures(fingerprint, sig_group)
                    await self.write_consolidated_signature(fingerprint, consolidated)
                    await self.delete_old_signatures(sig_group)
                    self.stats["consolidated_signatures"] += 1
                    self.stats["deleted_signatures"] += 1
                # else: Fingerprint unchanged, skip
            else:
                # Merge multiple signatures
                self.log_verbose(f"Merging {len(sig_group)} signatures into {fingerprint}")

                consolidated = self.merge_signatures(fingerprint, sig_group)

                # Write consolidated signature
                success = await self.write_consolidated_signature(fingerprint, consolidated)

                if success:
                    # Delete old signatures
                    deleted = await self.delete_old_signatures(sig_group)
                    self.stats["consolidated_signatures"] += 1
                    self.stats["deleted_signatures"] += deleted

        # Step 4: Refresh indices
        if not self.dry_run:
            logger.info("Refreshing indices...")
            self.es_client.indices.refresh(index=self.index_pattern)

        # Calculate final statistics
        self.stats["end_time"] = datetime.utcnow()
        self.stats["duration_seconds"] = (
            self.stats["end_time"] - self.stats["start_time"]
        ).total_seconds()

        # Print summary
        self.print_summary()

        return self.stats

    def print_summary(self):
        """Print consolidation summary."""
        logger.info("=" * 60)
        logger.info("CONSOLIDATION SUMMARY")
        logger.info("=" * 60)
        logger.info(f"Mode: {'DRY RUN' if self.dry_run else 'LIVE'}")
        logger.info(f"Total signatures processed: {self.stats['total_signatures']}")
        logger.info(f"Unique fingerprints (after): {self.stats['unique_fingerprints']}")
        logger.info(f"Reduction: {self.stats['total_signatures'] - self.stats['unique_fingerprints']} " +
                   f"({100 - (self.stats['unique_fingerprints'] / max(self.stats['total_signatures'], 1) * 100):.1f}%)")
        logger.info(f"Consolidated signatures: {self.stats['consolidated_signatures']}")
        logger.info(f"Deleted old signatures: {self.stats['deleted_signatures']}")
        logger.info(f"Errors: {self.stats['errors']}")
        logger.info(f"Duration: {self.stats['duration_seconds']:.2f} seconds")
        logger.info("=" * 60)

        if self.dry_run:
            logger.info("This was a DRY RUN - no changes were made")
            logger.info("Run without --dry-run to apply changes")


async def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Consolidate duplicate failure signatures"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without making changes"
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Show detailed progress"
    )
    parser.add_argument(
        "--es-host",
        default="elasticsearch:9200",
        help="Elasticsearch host (default: elasticsearch:9200)"
    )
    parser.add_argument(
        "--index-pattern",
        default="medic-failure-signatures-*",
        help="Index pattern to consolidate (default: medic-failure-signatures-*)"
    )

    args = parser.parse_args()

    # Set log level
    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Connect to Elasticsearch
    try:
        es_client = Elasticsearch([f"http://{args.es_host}"])

        # Test connection
        if not es_client.ping():
            logger.error(f"Failed to connect to Elasticsearch at {args.es_host}")
            return 1

        logger.info(f"Connected to Elasticsearch at {args.es_host}")

    except Exception as e:
        logger.error(f"Failed to connect to Elasticsearch: {e}")
        return 1

    # Run consolidation
    consolidator = SignatureConsolidator(
        es_client=es_client,
        index_pattern=args.index_pattern,
        dry_run=args.dry_run,
        verbose=args.verbose
    )

    try:
        stats = await consolidator.consolidate()

        # Return error code if there were errors
        if stats["errors"] > 0:
            logger.warning(f"Consolidation completed with {stats['errors']} errors")
            return 1

        logger.info("Consolidation completed successfully!")
        return 0

    except KeyboardInterrupt:
        logger.info("Consolidation interrupted by user")
        return 130
    except Exception as e:
        logger.error(f"Consolidation failed: {e}", exc_info=True)
        return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

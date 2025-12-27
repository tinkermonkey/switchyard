"""
Base Failure Signature Store

Abstract base class for failure signature storage in Elasticsearch.
Provides common CRUD operations and deduplication logic.

Subclasses must implement:
- _get_mapping(): Return Elasticsearch mapping for the signature type
- _create_signature_doc(): Create signature document from fingerprint
- _map_sample_entry(): Map raw entry to sample format
"""

import logging
import time
from abc import ABC, abstractmethod
from datetime import datetime, timedelta
from typing import Optional, Dict, Any, List, Tuple
from elasticsearch import Elasticsearch

from monitoring.timestamp_utils import utc_now, utc_isoformat
from monitoring.observability import get_observability_manager, EventType
from services.medic.shared import (
    setup_index_template,
    async_index,
    update_by_query,
    search_signatures,
    get_signature_by_id,
    delete_by_query,
    calculate_severity,
    calculate_status,
    calculate_impact_score,
    extract_tags,
    create_sample_entry,
    add_sample,
)

logger = logging.getLogger(__name__)


class BaseFailureSignatureStore(ABC):
    """
    Abstract base class for failure signature storage.

    Provides common Elasticsearch operations for both Docker and Claude systems.
    Subclasses customize document structure and sample entry format.
    """

    # Constants
    MAX_SAMPLE_ENTRIES = 20
    TRENDING_THRESHOLD = 2.0

    def __init__(self, es_client: Elasticsearch, index_prefix: str, ilm_policy_name: Optional[str] = None, ilm_policy_body: Optional[Dict] = None):
        """
        Initialize failure signature store.

        Args:
            es_client: Elasticsearch client
            index_prefix: Index name prefix (e.g., "medic-docker-failures")
            ilm_policy_name: Optional ILM policy name
            ilm_policy_body: Optional ILM policy configuration
        """
        self.es = es_client
        self.INDEX_PREFIX = index_prefix
        self.ilm_policy_name = ilm_policy_name
        self.ilm_policy_body = ilm_policy_body
        self._setup_elasticsearch()

    @abstractmethod
    def _get_mapping(self) -> Dict[str, Any]:
        """
        Return Elasticsearch mapping for this signature type.

        Returns:
            Mapping dict with 'mappings' key
        """
        pass

    @abstractmethod
    def _get_template_name(self) -> str:
        """
        Return index template name.

        Returns:
            Template name string
        """
        pass

    @abstractmethod
    def _create_signature_doc(self, fingerprint: Any, entry_data: dict, metadata: dict) -> Dict[str, Any]:
        """
        Create signature document from fingerprint and entry data.

        Args:
            fingerprint: Fingerprint object (ErrorFingerprint or ClaudeFailureFingerprint)
            entry_data: Raw entry data (log entry or cluster)
            metadata: Additional metadata (container_info, session_info, etc.)

        Returns:
            Signature document dict
        """
        pass

    @abstractmethod
    def _map_sample_entry(self, entry_data: dict, metadata: dict) -> Dict[str, Any]:
        """
        Map raw entry to sample format for storage.

        Args:
            entry_data: Raw entry data (log entry or cluster)
            metadata: Additional metadata

        Returns:
            Sample entry dict
        """
        pass

    @abstractmethod
    def _get_fingerprint_id(self, fingerprint: Any) -> str:
        """
        Get fingerprint ID from fingerprint object.

        Args:
            fingerprint: Fingerprint object

        Returns:
            Fingerprint ID string
        """
        pass

    def _setup_elasticsearch(self):
        """Setup Elasticsearch index template and ILM policy."""
        mapping = self._get_mapping()
        template_name = self._get_template_name()
        index_patterns = [f"{self.INDEX_PREFIX}-*"]

        success = setup_index_template(
            self.es,
            template_name,
            index_patterns,
            mapping,
            self.ilm_policy_name,
            self.ilm_policy_body
        )

        if success:
            logger.info(f"Elasticsearch setup complete for {template_name}")
        else:
            logger.warning(f"Elasticsearch setup failed for {template_name}, continuing anyway")

    def _get_index_name(self) -> str:
        """Get current index name (daily indices)."""
        today = utc_now().strftime("%Y.%m.%d")
        return f"{self.INDEX_PREFIX}-{today}"

    async def record_occurrence(self, fingerprint: Any, entry_data: dict, metadata: dict):
        """
        Record a new occurrence of a failure signature.

        Args:
            fingerprint: Fingerprint object
            entry_data: Raw entry data (log entry or cluster)
            metadata: Additional metadata
        """
        fingerprint_id = self._get_fingerprint_id(fingerprint)

        # Check if signature exists
        existing = await self._get_signature(fingerprint_id)

        if existing:
            # Update existing signature
            await self._update_occurrence(existing, entry_data, metadata)
        else:
            # Create new signature
            await self._create_signature(fingerprint, entry_data, metadata)

    async def _get_signature(self, fingerprint_id: str) -> Optional[Dict[str, Any]]:
        """Get signature by fingerprint ID."""
        return get_signature_by_id(self.es, f"{self.INDEX_PREFIX}-*", fingerprint_id)

    async def _create_signature(self, fingerprint: Any, entry_data: dict, metadata: dict):
        """Create a new failure signature."""
        fingerprint_id = self._get_fingerprint_id(fingerprint)

        # Create signature document (subclass-specific)
        doc = self._create_signature_doc(fingerprint, entry_data, metadata)

        # Write to Elasticsearch
        index_name = self._get_index_name()
        try:
            await async_index(
                self.es,
                index=index_name,
                id=fingerprint_id,
                document=doc
            )
            logger.info(f"Created new failure signature: {fingerprint_id[:16]}...")

            # Emit observability event
            self._emit_signature_created_event(fingerprint_id, doc)

        except Exception as e:
            logger.error(f"Failed to create signature in Elasticsearch: {e}")

    async def _update_occurrence(self, existing: dict, entry_data: dict, metadata: dict):
        """Update an existing failure signature with new occurrence."""
        fingerprint_id = existing["fingerprint_id"]
        now = utc_isoformat()

        # Calculate time-windowed occurrence counts
        occurrences_last_hour = await self._count_occurrences_since(existing, hours=1)
        occurrences_last_day = await self._count_occurrences_since(existing, hours=24)

        # Determine if trending
        old_rate = existing.get("occurrences_last_hour", 0)
        is_trending = occurrences_last_hour > old_rate * self.TRENDING_THRESHOLD

        # Update status
        new_status = calculate_status(
            existing["status"],
            existing["occurrence_count"] + 1,
            is_trending
        )

        # Create new sample entry
        sample_entry = self._map_sample_entry(entry_data, metadata)

        # Build update script
        script = {
            "source": """
                ctx._source.updated_at = params.updated_at;
                ctx._source.last_seen = params.last_seen;
                ctx._source.occurrence_count += 1;
                ctx._source.occurrences_last_hour = params.occurrences_last_hour;
                ctx._source.occurrences_last_day = params.occurrences_last_day;
                ctx._source.status = params.status;
                if (ctx._source.sample_entries.size() < params.max_samples) {
                    ctx._source.sample_entries.add(params.sample);
                } else {
                    ctx._source.sample_entries[ctx._source.sample_entries.size() - 1] = params.sample;
                }
            """,
            "params": {
                "updated_at": now,
                "last_seen": now,
                "occurrences_last_hour": occurrences_last_hour,
                "occurrences_last_day": occurrences_last_day,
                "status": new_status,
                "sample": sample_entry,
                "max_samples": self.MAX_SAMPLE_ENTRIES,
            },
        }

        result = update_by_query(
            self.es,
            f"{self.INDEX_PREFIX}-*",
            script,
            {"term": {"fingerprint_id": fingerprint_id}}
        )

        if result and result.get("updated", 0) > 0:
            logger.info(f"Updated signature {fingerprint_id[:16]}... (count: {existing['occurrence_count'] + 1})")

    async def _count_occurrences_since(self, signature: dict, hours: int) -> int:
        """
        Count occurrences within time window.

        Note: This is an approximation based on sample entries.
        For accurate counts, would need to query actual occurrence data.
        """
        # For now, return the stored counts
        if hours == 1:
            return signature.get("occurrences_last_hour", 0) + 1
        elif hours == 24:
            return signature.get("occurrences_last_day", 0) + 1
        else:
            return signature.get("occurrence_count", 0) + 1

    def _emit_signature_created_event(self, fingerprint_id: str, doc: dict):
        """Emit observability event for signature creation."""
        try:
            get_observability_manager().emit(
                EventType.MEDIC_SIGNATURE_CREATED,
                agent="medic",
                task_id=f"medic-{fingerprint_id[:8]}",
                project=doc.get("project", "unknown"),
                data={
                    "fingerprint_id": fingerprint_id,
                    "type": doc.get("type"),
                    "severity": doc.get("severity"),
                    "status": doc.get("status"),
                },
            )
        except Exception as e:
            logger.warning(f"Failed to emit signature created event: {e}")

    async def update_status(self, fingerprint_id: str, new_status: str, reason: Optional[str] = None):
        """
        Update the status of a failure signature.

        Args:
            fingerprint_id: Fingerprint ID
            new_status: New status (new, recurring, trending, ignored, resolved)
            reason: Optional reason for status change
        """
        now = utc_isoformat()

        script = {
            "source": "ctx._source.status = params.status; ctx._source.updated_at = params.updated_at;",
            "params": {
                "status": new_status,
                "updated_at": now,
            },
        }

        result = update_by_query(
            self.es,
            f"{self.INDEX_PREFIX}-*",
            script,
            {"term": {"fingerprint_id": fingerprint_id}}
        )

        if result and result.get("updated", 0) > 0:
            logger.info(f"Updated status for {fingerprint_id[:16]}... to {new_status}")
            return True
        else:
            logger.warning(f"Signature {fingerprint_id} not found for status update")
            return False

    def update_investigation_status(
        self,
        fingerprint_id: str,
        investigation_status: str,
        max_retries: int = 3
    ) -> bool:
        """
        Update the investigation status of a failure signature with retry logic.

        DEPRECATED: Use update_investigation_status_es_first instead for ES-first architecture.

        Args:
            fingerprint_id: Fingerprint ID
            investigation_status: Investigation status (not_started, queued, in_progress, etc.)
            max_retries: Maximum number of retry attempts (default: 3)

        Returns:
            True if update succeeded, False otherwise
        """
        now = utc_isoformat()

        script = {
            "source": "ctx._source.investigation_status = params.investigation_status; ctx._source.updated_at = params.updated_at;",
            "params": {
                "investigation_status": investigation_status,
                "updated_at": now,
            },
        }

        # Retry with exponential backoff: 0.5s, 1s, 2s
        delays = [0.5, 1.0, 2.0]

        for attempt in range(max_retries):
            try:
                result = update_by_query(
                    self.es,
                    f"{self.INDEX_PREFIX}-*",
                    script,
                    {"term": {"fingerprint_id": fingerprint_id}},
                    refresh=True,
                    conflicts="proceed"
                )

                if result and result.get("updated", 0) > 0:
                    if attempt > 0:
                        logger.info(
                            f"Updated investigation status for {fingerprint_id[:16]}... "
                            f"to {investigation_status} (succeeded on attempt {attempt + 1})"
                        )
                    else:
                        logger.info(
                            f"Updated investigation status for {fingerprint_id[:16]}... "
                            f"to {investigation_status}"
                        )
                    return True
                else:
                    # Document not found - retry may help if it's an indexing delay
                    if attempt < max_retries - 1:
                        logger.debug(
                            f"Signature {fingerprint_id} not found on attempt {attempt + 1}, "
                            f"retrying in {delays[attempt]}s..."
                        )
                        time.sleep(delays[attempt])
                    else:
                        logger.warning(
                            f"Signature {fingerprint_id} not found after {max_retries} attempts "
                            f"for investigation status update"
                        )
                        return False

            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"Failed to update investigation status for {fingerprint_id[:16]}... "
                        f"on attempt {attempt + 1}: {e}. Retrying in {delays[attempt]}s..."
                    )
                    time.sleep(delays[attempt])
                else:
                    logger.error(
                        f"Failed to update investigation status for {fingerprint_id[:16]}... "
                        f"after {max_retries} attempts: {e}"
                    )
                    return False

        return False

    def update_investigation_status_es_first(
        self,
        fingerprint_id: str,
        status: str,
        metadata: Optional[Dict] = None,
        expected_current: Optional[List[str]] = None,
        max_retries: int = 3
    ) -> bool:
        """
        Update investigation status in ES with retry and optimistic locking (ES-FIRST).

        This is a BLOCKING operation - caller waits until ES persisted.
        Use this for the ES-first architecture where ES is the source of truth.

        Args:
            fingerprint_id: Investigation fingerprint
            status: New investigation_status value
            metadata: Optional investigation_metadata fields to update (dict with nested keys like "container_name", "started_at", etc.)
            expected_current: List of valid current statuses for optimistic locking (None = no check)
            max_retries: Number of retry attempts (default: 3)

        Returns:
            True if updated successfully, False if failed after retries or optimistic lock conflict

        Example:
            success = store.update_investigation_status_es_first(
                fp_id,
                status="in_progress",
                metadata={"container_name": "inv-12345", "started_at": "2025-01-01T00:00:00Z"},
                expected_current=["queued"]
            )
        """
        now = utc_isoformat()

        # Build Painless script for ES update
        script_source = """
            if (params.expected_statuses != null &&
                !params.expected_statuses.contains(ctx._source.investigation_status)) {
                ctx.op = 'none';  // Abort - optimistic lock failed
            } else {
                ctx._source.investigation_status = params.new_status;
                ctx._source.updated_at = params.updated_at;
                if (params.metadata != null) {
                    if (ctx._source.investigation_metadata == null) {
                        ctx._source.investigation_metadata = [:];
                    }
                    for (entry in params.metadata.entrySet()) {
                        ctx._source.investigation_metadata[entry.getKey()] = entry.getValue();
                    }
                }
            }
        """

        script = {
            "source": script_source,
            "params": {
                "new_status": status,
                "updated_at": now,
                "expected_statuses": expected_current,
                "metadata": metadata
            }
        }

        # Exponential backoff: 0.5s, 1s, 2s
        delays = [0.5, 1.0, 2.0]

        for attempt in range(max_retries):
            try:
                result = update_by_query(
                    self.es,
                    f"{self.INDEX_PREFIX}-*",
                    script,
                    {"term": {"fingerprint_id": fingerprint_id}},
                    refresh=True,  # Block until searchable (wait_for)
                    conflicts="proceed"
                )

                if result and result.get("updated", 0) > 0:
                    logger.info(
                        f"ES-first update: {fingerprint_id[:16]}... -> {status}" +
                        (f" (metadata: {list(metadata.keys())})" if metadata else "")
                    )
                    return True
                elif result and result.get("updated", 0) == 0:
                    # Could be optimistic lock failure or document not found
                    logger.warning(
                        f"ES-first update rejected for {fingerprint_id[:16]}... "
                        f"(optimistic lock failed or not found)"
                    )
                    return False
                else:
                    # Retry
                    if attempt < max_retries - 1:
                        logger.debug(
                            f"ES-first update attempt {attempt + 1} failed for {fingerprint_id[:16]}..., "
                            f"retrying in {delays[attempt]}s"
                        )
                        time.sleep(delays[attempt])
                    else:
                        logger.error(
                            f"ES-first update failed after {max_retries} attempts for {fingerprint_id[:16]}..."
                        )
                        return False

            except Exception as e:
                if attempt < max_retries - 1:
                    logger.warning(
                        f"ES-first update error on attempt {attempt + 1} for {fingerprint_id[:16]}...: {e}. "
                        f"Retrying in {delays[attempt]}s..."
                    )
                    time.sleep(delays[attempt])
                else:
                    logger.error(
                        f"ES-first update failed after {max_retries} attempts for {fingerprint_id[:16]}...: {e}"
                    )
                    return False

        return False

    def cleanup_stale_signatures(self, days: int = 7) -> Tuple[int, List[str]]:
        """
        Delete signatures that haven't been seen in the specified number of days.

        Args:
            days: Number of days of inactivity before deletion (default: 7)

        Returns:
            Tuple of (number of signatures deleted, list of deleted fingerprint IDs)
        """
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        cutoff_iso = cutoff_date.isoformat() + 'Z'

        try:
            # First, get the list of signatures to be deleted
            query = {
                "query": {
                    "range": {
                        "last_seen": {
                            "lt": cutoff_iso
                        }
                    }
                },
                "_source": ["fingerprint_id"],
                "size": 10000
            }

            search_result = self.es.search(
                index=f"{self.INDEX_PREFIX}-*",
                body=query
            )

            fingerprint_ids = [hit['_source']['fingerprint_id'] for hit in search_result['hits']['hits']]

            if not fingerprint_ids:
                logger.info(f"No stale {self.INDEX_PREFIX} signatures to clean up")
                return 0, []

            # Delete using delete_by_query
            delete_result = delete_by_query(
                self.es,
                f"{self.INDEX_PREFIX}-*",
                query["query"],
                refresh=True
            )

            deleted_count = delete_result.get('deleted', 0) if delete_result else 0

            logger.info(f"Cleaned up {deleted_count} stale {self.INDEX_PREFIX} signatures older than {days} days")
            return deleted_count, fingerprint_ids

        except Exception as e:
            logger.error(f"Failed to cleanup stale signatures: {e}", exc_info=True)
            return 0, []

    def get_signature(self, fingerprint_id: str) -> Optional[Dict[str, Any]]:
        """
        Get a signature by fingerprint ID (sync version).

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Signature document or None
        """
        return get_signature_by_id(self.es, f"{self.INDEX_PREFIX}-*", fingerprint_id)

    def get_investigation_status(self, fingerprint_id: str) -> Optional[str]:
        """
        Get investigation status from Elasticsearch (single source of truth).

        Args:
            fingerprint_id: Fingerprint ID

        Returns:
            Investigation status or "not_started" if signature doesn't exist
        """
        sig = self.get_signature(fingerprint_id)
        if not sig:
            return "not_started"
        return sig.get('investigation_status', 'not_started')

    def get_unresolved_signatures(self, max_count: int = 100) -> List[Dict[str, Any]]:
        """
        Get unresolved failure signatures.

        Args:
            max_count: Maximum number of signatures to return

        Returns:
            List of signature documents
        """
        query = {
            "bool": {
                "must_not": [
                    {"term": {"status": "resolved"}},
                    {"term": {"status": "ignored"}}
                ]
            }
        }

        result = search_signatures(self.es, f"{self.INDEX_PREFIX}-*", query, size=max_count)

        if result and result.get("hits", {}).get("hits"):
            return [hit["_source"] for hit in result["hits"]["hits"]]

        return []

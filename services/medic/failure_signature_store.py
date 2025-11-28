"""
Failure Signature Store - Elasticsearch Backend

Manages failure signatures in Elasticsearch with deduplication,
aggregation, and status tracking.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional
from elasticsearch import Elasticsearch, NotFoundError

from monitoring.timestamp_utils import utc_now, utc_isoformat
from monitoring.observability import get_observability_manager, EventType
from .fingerprint_engine import ErrorFingerprint

logger = logging.getLogger(__name__)


# Elasticsearch Index Mapping
MEDIC_FAILURE_SIGNATURES_MAPPING = {
    "mappings": {
        "properties": {
            "fingerprint_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "first_seen": {"type": "date"},
            "last_seen": {"type": "date"},
            "signature": {
                "properties": {
                    "container_pattern": {"type": "keyword"},
                    "error_type": {"type": "keyword"},
                    "error_pattern": {
                        "type": "text",
                        "fields": {"keyword": {"type": "keyword"}},
                    },
                    "stack_signature": {"type": "keyword"},
                    "normalized_message": {"type": "text"},
                }
            },
            "occurrence_count": {"type": "integer"},
            "occurrences_last_hour": {"type": "integer"},
            "occurrences_last_day": {"type": "integer"},
            "severity": {"type": "keyword"},
            "impact_score": {"type": "float"},
            "status": {"type": "keyword"},
            "investigation_status": {"type": "keyword"},
            "sample_log_entries": {
                "type": "nested",
                "properties": {
                    "timestamp": {"type": "date"},
                    "container_id": {"type": "keyword"},
                    "container_name": {"type": "keyword"},
                    "raw_message": {"type": "text"},
                    "context": {"type": "object", "enabled": True},
                },
            },
            "tags": {"type": "keyword"},
            "related_signatures": {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "index.lifecycle.name": "medic-ilm-policy",
    },
}

# ILM Policy (30-day retention)
MEDIC_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot": {"min_age": "0ms", "actions": {"set_priority": {"priority": 100}}},
            "warm": {
                "min_age": "7d",
                "actions": {"set_priority": {"priority": 50}},
            },
            "delete": {"min_age": "30d", "actions": {"delete": {}}},
        }
    }
}


class FailureSignatureStore:
    """
    Manages failure signatures in Elasticsearch.
    Handles deduplication, aggregation, and status tracking.
    """

    MAX_SAMPLE_ENTRIES = 20  # Max sample log entries to store per signature
    TRENDING_THRESHOLD = 2.0  # Multiplier for trending detection

    def __init__(self, es_client: Elasticsearch):
        self.es = es_client
        self._setup_elasticsearch()

    def _setup_elasticsearch(self):
        """Setup Elasticsearch ILM policy and index template"""
        try:
            # Create ILM policy
            if not self.es.ilm.get_lifecycle(name="medic-ilm-policy", ignore=[404]):
                self.es.ilm.put_lifecycle(
                    name="medic-ilm-policy", body=MEDIC_ILM_POLICY
                )
                logger.info("Created Medic ILM policy")

            # Create index template
            template_body = {
                "index_patterns": ["medic-failure-signatures-*"],
                "template": MEDIC_FAILURE_SIGNATURES_MAPPING,
            }

            self.es.indices.put_index_template(
                name="medic-failure-signatures", body=template_body
            )
            logger.info("Created Medic failure signatures index template")

        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch for Medic: {e}")
            # Continue anyway - indexes will be created on first write

    async def record_occurrence(
        self, fingerprint: ErrorFingerprint, log_entry: dict, container_info: dict
    ):
        """
        Record a new occurrence of a failure signature.

        Args:
            fingerprint: ErrorFingerprint object
            log_entry: Original log entry dict
            container_info: Container metadata (id, name, etc.)
        """
        fingerprint_id = fingerprint.fingerprint_id

        # Check if signature exists
        existing = await self._get_signature(fingerprint_id)

        if existing:
            # Update existing signature
            await self._update_occurrence(existing, log_entry, container_info)
        else:
            # Create new signature
            await self._create_signature(fingerprint, log_entry, container_info)

    async def _create_signature(
        self, fingerprint: ErrorFingerprint, log_entry: dict, container_info: dict
    ):
        """Create a new failure signature"""
        now = utc_isoformat()

        # Calculate severity from log level
        severity = self._calculate_severity(log_entry)

        # Extract tags from log entry
        tags = self._extract_tags(log_entry, fingerprint)

        # Create sample entry
        sample_entry = self._create_sample(log_entry, container_info)

        doc = {
            "fingerprint_id": fingerprint.fingerprint_id,
            "created_at": now,
            "updated_at": now,
            "first_seen": now,
            "last_seen": now,
            "signature": {
                "container_pattern": fingerprint.container_pattern,
                "error_type": fingerprint.error_type,
                "error_pattern": fingerprint.error_pattern,
                "stack_signature": fingerprint.stack_signature,
                "normalized_message": fingerprint.normalized_message,
            },
            "occurrence_count": 1,
            "occurrences_last_hour": 1,
            "occurrences_last_day": 1,
            "severity": severity,
            "impact_score": 0.0,
            "status": "new",
            "investigation_status": "not_started",
            "sample_log_entries": [sample_entry],
            "tags": tags,
            "related_signatures": [],
        }

        # Write to Elasticsearch
        index_name = self._get_index_name()
        try:
            await self._async_index(
                index=index_name, id=fingerprint.fingerprint_id, document=doc
            )
            logger.info(f"Created new failure signature: {fingerprint.fingerprint_id[:16]}...")

            # Emit observability event
            get_observability_manager().emit(
                EventType.MEDIC_SIGNATURE_CREATED,
                agent="medic",
                task_id=f"medic-{fingerprint.fingerprint_id[:8]}",
                project="orchestrator",
                data={
                    "fingerprint_id": fingerprint.fingerprint_id,
                    "error_type": fingerprint.error_type,
                    "severity": severity,
                    "container_pattern": fingerprint.container_pattern,
                },
            )

        except Exception as e:
            logger.error(f"Failed to create signature in Elasticsearch: {e}")

    async def _update_occurrence(
        self, existing: dict, log_entry: dict, container_info: dict
    ):
        """Update an existing failure signature with new occurrence"""
        fingerprint_id = existing["fingerprint_id"]
        now = utc_isoformat()

        # Calculate time-windowed occurrence counts
        occurrences_last_hour = await self._count_occurrences_since(
            existing, hours=1
        )
        occurrences_last_day = await self._count_occurrences_since(
            existing, hours=24
        )

        # Determine if trending (occurrence rate increasing)
        old_rate = existing.get("occurrences_last_hour", 0)
        is_trending = occurrences_last_hour > old_rate * self.TRENDING_THRESHOLD

        # Update status based on occurrence count
        new_status = self._calculate_status(
            existing["status"], existing["occurrence_count"] + 1, is_trending
        )

        # Create new sample entry
        sample_entry = self._create_sample(log_entry, container_info)

        # Build update script
        script = {
            "source": """
                ctx._source.updated_at = params.updated_at;
                ctx._source.last_seen = params.last_seen;
                ctx._source.occurrence_count += 1;
                ctx._source.occurrences_last_hour = params.occurrences_last_hour;
                ctx._source.occurrences_last_day = params.occurrences_last_day;
                ctx._source.status = params.status;

                // Add new sample (limit to MAX_SAMPLE_ENTRIES)
                if (ctx._source.sample_log_entries.size() >= params.max_samples) {
                    ctx._source.sample_log_entries.remove(0);
                }
                ctx._source.sample_log_entries.add(params.sample_entry);
            """,
            "params": {
                "updated_at": now,
                "last_seen": now,
                "occurrences_last_hour": occurrences_last_hour + 1,
                "occurrences_last_day": occurrences_last_day + 1,
                "status": new_status,
                "sample_entry": sample_entry,
                "max_samples": self.MAX_SAMPLE_ENTRIES,
            },
        }

        # Update in Elasticsearch
        try:
            index_pattern = "medic-failure-signatures-*"
            self.es.update_by_query(
                index=index_pattern,
                body={
                    "script": script,
                    "query": {"term": {"fingerprint_id": fingerprint_id}},
                },
            )

            # Emit update event
            get_observability_manager().emit(
                EventType.MEDIC_SIGNATURE_UPDATED,
                agent="medic",
                task_id=f"medic-{fingerprint_id[:8]}",
                project="orchestrator",
                data={
                    "fingerprint_id": fingerprint_id,
                    "occurrence_count": existing["occurrence_count"] + 1,
                    "status": new_status,
                    "is_trending": is_trending,
                },
            )

            # Emit trending event if status changed to trending
            if is_trending and existing["status"] != "trending":
                get_observability_manager().emit(
                    EventType.MEDIC_SIGNATURE_TRENDING,
                    agent="medic",
                    task_id=f"medic-{fingerprint_id[:8]}",
                    project="orchestrator",
                    data={
                        "fingerprint_id": fingerprint_id,
                        "occurrence_count": existing["occurrence_count"] + 1,
                        "occurrences_last_hour": occurrences_last_hour + 1,
                    },
                )

        except Exception as e:
            logger.error(f"Failed to update signature in Elasticsearch: {e}")

    async def _get_signature(self, fingerprint_id: str) -> Optional[dict]:
        """Get signature by fingerprint ID"""
        try:
            result = self.es.search(
                index="medic-failure-signatures-*",
                body={"query": {"term": {"fingerprint_id": fingerprint_id}}},
            )

            if result["hits"]["total"]["value"] > 0:
                return result["hits"]["hits"][0]["_source"]
            return None

        except Exception as e:
            logger.error(f"Failed to get signature {fingerprint_id}: {e}")
            return None

    async def _count_occurrences_since(
        self, signature: dict, hours: int
    ) -> int:
        """Count occurrences in the last N hours"""
        # Count sample log entries within time window
        since = utc_now() - timedelta(hours=hours)
        count = 0

        for entry in signature.get("sample_log_entries", []):
            entry_time = datetime.fromisoformat(
                entry["timestamp"].replace("Z", "+00:00")
            )
            if entry_time >= since:
                count += 1

        return count

    def _calculate_severity(self, log_entry: dict) -> str:
        """Calculate severity from log level"""
        level = log_entry.get("level", "").upper()

        severity_map = {
            "CRITICAL": "CRITICAL",
            "FATAL": "CRITICAL",
            "ERROR": "ERROR",
            "WARNING": "WARNING",
            "WARN": "WARNING",
        }

        return severity_map.get(level, "ERROR")

    def _calculate_status(
        self, current_status: str, occurrence_count: int, is_trending: bool
    ) -> str:
        """Calculate status based on occurrence count and trends"""
        # Don't change manually set statuses
        if current_status in ["ignored", "resolved"]:
            return current_status

        if is_trending:
            return "trending"
        elif occurrence_count >= 2:
            return "recurring"
        else:
            return "new"

    def _extract_tags(self, log_entry: dict, fingerprint: ErrorFingerprint) -> list[str]:
        """Extract tags from log entry and fingerprint"""
        tags = []

        # Add container tag
        tags.append(fingerprint.container_pattern)

        # Add error type tag
        if fingerprint.error_type:
            tags.append(fingerprint.error_type)

        # Extract context-based tags
        message = log_entry.get("message", "").lower()
        if "agent" in message:
            tags.append("agent_execution")
        if "task" in message:
            tags.append("task_processing")
        if "pipeline" in message:
            tags.append("pipeline")
        if "github" in message:
            tags.append("github")
        if "docker" in message:
            tags.append("docker")

        return list(set(tags))  # Deduplicate

    def _create_sample(self, log_entry: dict, container_info: dict) -> dict:
        """Create a sample log entry for storage"""
        return {
            "timestamp": log_entry.get("timestamp", utc_isoformat()),
            "container_id": container_info.get("id", "unknown"),
            "container_name": container_info.get("name", "unknown"),
            "raw_message": log_entry.get("message", ""),
            "context": {
                "level": log_entry.get("level", ""),
                "logger": log_entry.get("name", ""),
                **log_entry.get("context", {}),
            },
        }

    def _get_index_name(self) -> str:
        """Get current index name (daily indices)"""
        today = utc_now().strftime("%Y.%m.%d")
        return f"medic-failure-signatures-{today}"

    async def _async_index(self, index: str, id: str, document: dict):
        """Async wrapper for ES index operation"""
        # Note: Using sync ES client but wrapping in async for future asyncio integration
        self.es.index(index=index, id=id, document=document)

    async def update_status(self, fingerprint_id: str, new_status: str, reason: Optional[str] = None):
        """Update the status of a failure signature (e.g., mark as ignored)"""
        now = utc_isoformat()

        script = {
            "source": "ctx._source.status = params.status; ctx._source.updated_at = params.updated_at;",
            "params": {
                "status": new_status,
                "updated_at": now,
            },
        }

        try:
            result = self.es.update_by_query(
                index="medic-failure-signatures-*",
                body={
                    "script": script,
                    "query": {"term": {"fingerprint_id": fingerprint_id}},
                },
            )

            if result["updated"] > 0:
                logger.info(f"Updated signature {fingerprint_id[:16]}... status to {new_status}")
                return True
            else:
                logger.warning(f"Signature {fingerprint_id} not found for status update")
                return False

        except Exception as e:
            logger.error(f"Failed to update signature status: {e}")
            return False

    async def update_investigation_status(self, fingerprint_id: str, investigation_status: str):
        """
        Update investigation status for a signature.

        Args:
            fingerprint_id: Failure signature ID
            investigation_status: One of: not_started, queued, in_progress, completed, failed, ignored
        """
        now = utc_isoformat()

        script = {
            "source": "ctx._source.investigation_status = params.investigation_status; ctx._source.updated_at = params.updated_at;",
            "params": {
                "investigation_status": investigation_status,
                "updated_at": now,
            },
        }

        try:
            result = self.es.update_by_query(
                index="medic-failure-signatures-*",
                body={
                    "script": script,
                    "query": {"term": {"fingerprint_id": fingerprint_id}},
                },
            )

            if result["updated"] > 0:
                logger.info(f"Updated investigation status for {fingerprint_id[:16]}... to {investigation_status}")
                return True
            else:
                logger.warning(f"Signature {fingerprint_id} not found for investigation status update")
                return False

        except Exception as e:
            logger.error(f"Failed to update investigation status: {e}")
            return False

    async def check_auto_trigger_conditions(self):
        """
        Check for signatures that should auto-trigger investigation.

        Auto-trigger thresholds:
        - CRITICAL: 3 occurrences
        - ERROR: 10 total OR 5 in last hour
        - WARNING: 50 total OR 20 in last hour

        Returns:
            List of fingerprint IDs that should be investigated
        """
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"investigation_status": "not_started"}},
                        {
                            "bool": {
                                "should": [
                                    # CRITICAL: 3 occurrences
                                    {
                                        "bool": {
                                            "must": [
                                                {"term": {"severity": "CRITICAL"}},
                                                {"range": {"occurrence_count": {"gte": 3}}},
                                            ]
                                        }
                                    },
                                    # ERROR: 10 total
                                    {
                                        "bool": {
                                            "must": [
                                                {"term": {"severity": "ERROR"}},
                                                {"range": {"occurrence_count": {"gte": 10}}},
                                            ]
                                        }
                                    },
                                    # ERROR: 5 in last hour
                                    {
                                        "bool": {
                                            "must": [
                                                {"term": {"severity": "ERROR"}},
                                                {"range": {"occurrences_last_hour": {"gte": 5}}},
                                            ]
                                        }
                                    },
                                    # WARNING: 50 total
                                    {
                                        "bool": {
                                            "must": [
                                                {"term": {"severity": "WARNING"}},
                                                {"range": {"occurrence_count": {"gte": 50}}},
                                            ]
                                        }
                                    },
                                    # WARNING: 20 in last hour
                                    {
                                        "bool": {
                                            "must": [
                                                {"term": {"severity": "WARNING"}},
                                                {"range": {"occurrences_last_hour": {"gte": 20}}},
                                            ]
                                        }
                                    },
                                ]
                            }
                        },
                    ]
                }
            },
            "size": 100,  # Max 100 auto-triggers per check
            "_source": ["fingerprint_id", "severity", "occurrence_count", "occurrences_last_hour"],
        }

        try:
            result = self.es.search(index="medic-failure-signatures-*", body=query)
            signatures = result["hits"]["hits"]

            triggered = []
            for hit in signatures:
                fingerprint_id = hit["_source"]["fingerprint_id"]
                triggered.append(fingerprint_id)

                # Update investigation status to queued
                await self.update_investigation_status(fingerprint_id, "queued")

            if triggered:
                logger.info(f"Auto-triggered {len(triggered)} investigations")

            return triggered

        except Exception as e:
            logger.error(f"Failed to check auto-trigger conditions: {e}", exc_info=True)
            return []

"""
Docker Failure Signature Store

Elasticsearch-backed storage for Docker container log failure signatures.
"""

import logging
from typing import Dict, Any
from elasticsearch import Elasticsearch

from services.medic.base import BaseFailureSignatureStore
from services.medic.shared import (
    calculate_severity,
    extract_tags,
    create_sample_entry,
)
from .fingerprint_engine import ErrorFingerprint
from monitoring.timestamp_utils import utc_isoformat

logger = logging.getLogger(__name__)


# Elasticsearch Index Mapping for Docker failures
DOCKER_FAILURE_MAPPING = {
    "mappings": {
        "properties": {
            "type": {"type": "keyword"},  # "docker"
            "project": {"type": "keyword"},  # "orchestrator"
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
            "total_failures": {"type": "integer"},  # Same as occurrence_count for Docker
            "occurrences_last_hour": {"type": "integer"},
            "occurrences_last_day": {"type": "integer"},
            "severity": {"type": "keyword"},
            "impact_score": {"type": "float"},
            "status": {"type": "keyword"},
            "investigation_status": {"type": "keyword"},
            "investigation_metadata": {
                "properties": {
                    "container_name": {"type": "keyword"},
                    "started_at": {"type": "date"},
                    "last_heartbeat": {"type": "date"},
                    "completed_at": {"type": "date"},
                    "result": {"type": "keyword"},
                    "retry_count": {"type": "integer"},
                    "error_message": {"type": "text"},
                }
            },
            "version": {"type": "long"},
            "sample_entries": {
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
        "index.lifecycle.name": "medic-docker-ilm-policy",
    },
}

# ILM Policy (30-day retention)
DOCKER_ILM_POLICY = {
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


class DockerFailureSignatureStore(BaseFailureSignatureStore):
    """
    Docker-specific failure signature storage.

    Stores failure signatures from Docker container logs in Elasticsearch.
    Uses unified schema with type="docker" and project="orchestrator".
    """

    def __init__(self, es_client: Elasticsearch):
        """
        Initialize Docker failure signature store.

        Args:
            es_client: Elasticsearch client
        """
        super().__init__(
            es_client=es_client,
            index_prefix="medic-docker-failures",
            ilm_policy_name="medic-docker-ilm-policy",
            ilm_policy_body=DOCKER_ILM_POLICY,
        )
        logger.info("DockerFailureSignatureStore initialized")

    def _get_mapping(self) -> Dict[str, Any]:
        """Return Elasticsearch mapping for Docker failures."""
        return DOCKER_FAILURE_MAPPING

    def _get_template_name(self) -> str:
        """Return index template name."""
        return "medic-docker-failures"

    def _create_signature_doc(
        self, fingerprint: Any, entry_data: dict, metadata: dict
    ) -> Dict[str, Any]:
        """
        Create signature document from ErrorFingerprint.

        Args:
            fingerprint: ErrorFingerprint object
            entry_data: Log entry dict
            metadata: Container info (id, name, etc.)

        Returns:
            Signature document dict
        """
        now = utc_isoformat()

        # Calculate severity from log level
        severity = calculate_severity(entry_data)

        # Extract tags
        tags = extract_tags(
            message=entry_data.get("message", ""),
            fingerprint=fingerprint,
            additional_tags=[fingerprint.container_pattern]
        )

        # Create sample entry
        sample_entry = create_sample_entry(
            entry_data=entry_data,
            metadata=metadata,
            entry_type="docker"
        )

        # Calculate initial impact score
        impact_score = self._calculate_initial_impact_score(severity)

        doc = {
            "type": "docker",
            "project": "orchestrator",
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
            "total_failures": 1,  # Same as occurrence_count for Docker
            "occurrences_last_hour": 1,
            "occurrences_last_day": 1,
            "severity": severity,
            "impact_score": impact_score,
            "status": "new",
            "investigation_status": "not_started",
            "investigation_metadata": {
                "container_name": None,
                "started_at": None,
                "last_heartbeat": None,
                "completed_at": None,
                "result": None,
                "retry_count": 0,
                "error_message": None,
            },
            "version": 1,
            "sample_entries": [sample_entry],
            "tags": tags,
            "related_signatures": [],
        }

        return doc

    def _map_sample_entry(self, entry_data: dict, metadata: dict) -> Dict[str, Any]:
        """
        Map log entry to sample format for storage.

        Args:
            entry_data: Log entry dict
            metadata: Container info

        Returns:
            Sample entry dict
        """
        return create_sample_entry(
            entry_data=entry_data,
            metadata=metadata,
            entry_type="docker"
        )

    def _get_fingerprint_id(self, fingerprint: Any) -> str:
        """
        Get fingerprint ID from ErrorFingerprint object.

        Args:
            fingerprint: ErrorFingerprint object

        Returns:
            Fingerprint ID string
        """
        return fingerprint.fingerprint_id

    def _calculate_initial_impact_score(self, severity: str) -> float:
        """
        Calculate initial impact score based on severity.

        Args:
            severity: Severity level (CRITICAL, ERROR, WARNING)

        Returns:
            Impact score (0-100)
        """
        severity_weights = {
            "CRITICAL": 10.0,
            "ERROR": 5.0,
            "WARNING": 1.0,
        }
        return severity_weights.get(severity, 5.0)

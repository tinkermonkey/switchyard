"""
Claude Failure Signature Store

Elasticsearch-backed storage for Claude Code tool execution failure signatures.
"""

import logging
from typing import Dict, Any
from elasticsearch import Elasticsearch

from services.medic.base import BaseFailureSignatureStore
from services.medic.shared import calculate_impact_score, extract_tags
from monitoring.timestamp_utils import utc_isoformat

logger = logging.getLogger(__name__)


# Elasticsearch Index Mapping for Claude failures
CLAUDE_FAILURE_MAPPING = {
    "mappings": {
        "properties": {
            "type": {"type": "keyword"},  # "claude"
            "project": {"type": "keyword"},  # Project name
            "fingerprint_id": {"type": "keyword"},
            "created_at": {"type": "date"},
            "updated_at": {"type": "date"},
            "first_seen": {"type": "date"},
            "last_seen": {"type": "date"},
            "signature": {
                "properties": {
                    "tool_name": {"type": "keyword"},
                    "error_type": {"type": "keyword"},
                    "error_pattern": {"type": "text"},
                    "context_signature": {"type": "keyword"},
                    "cluster_size_avg": {"type": "float"}
                }
            },
            "cluster_count": {"type": "integer"},  # Number of failure clusters
            "total_failures": {"type": "integer"},  # Sum of failures across all clusters
            "occurrences_last_hour": {"type": "integer"},  # Clusters in last hour
            "occurrences_last_day": {"type": "integer"},  # Clusters in last day
            "severity": {"type": "keyword"},
            "impact_score": {"type": "float"},
            "status": {"type": "keyword"},
            "investigation_status": {"type": "keyword"},
            "sample_entries": {
                "type": "nested",
                "properties": {
                    "cluster_id": {"type": "keyword"},
                    "timestamp": {"type": "date"},
                    "session_id": {"type": "keyword"},
                    "task_id": {"type": "keyword"},
                    "failure_count": {"type": "integer"},
                    "duration_seconds": {"type": "float"},
                    "tools_attempted": {"type": "keyword"},
                    "primary_error": {"type": "text"},
                },
            },
            "tags": {"type": "keyword"},
            "related_signatures": {"type": "keyword"},
        }
    },
    "settings": {
        "number_of_shards": 1,
        "number_of_replicas": 0,
        "index.lifecycle.name": "medic-claude-ilm-policy",
    },
}

# ILM Policy (30-day retention)
CLAUDE_ILM_POLICY = {
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


class ClaudeFailureSignatureStore(BaseFailureSignatureStore):
    """
    Claude-specific failure signature storage.

    Stores failure signatures from Claude Code tool execution failures in Elasticsearch.
    Uses unified schema with type="claude" and project-scoped organization.
    """

    def __init__(self, es_client: Elasticsearch):
        """
        Initialize Claude failure signature store.

        Args:
            es_client: Elasticsearch client
        """
        super().__init__(
            es_client=es_client,
            index_prefix="medic-claude-failures",
            ilm_policy_name="medic-claude-ilm-policy",
            ilm_policy_body=CLAUDE_ILM_POLICY,
        )
        logger.info("ClaudeFailureSignatureStore initialized")

    def _get_mapping(self) -> Dict[str, Any]:
        """Return Elasticsearch mapping for Claude failures."""
        return CLAUDE_FAILURE_MAPPING

    def _get_template_name(self) -> str:
        """Return index template name."""
        return "medic-claude-failures"

    def _create_signature_doc(
        self, fingerprint: Any, entry_data: dict, metadata: dict
    ) -> Dict[str, Any]:
        """
        Create signature document from ClaudeFailureFingerprint.

        Args:
            fingerprint: ClaudeFailureFingerprint object
            entry_data: FailureCluster dict
            metadata: Additional metadata (project, etc.)

        Returns:
            Signature document dict
        """
        now = utc_isoformat()
        cluster = entry_data  # For Claude, entry_data is actually a FailureCluster dict
        project = metadata.get("project", fingerprint.project)

        # Extract tags
        tags = self._generate_tags(fingerprint, cluster)

        # Create sample cluster entry
        sample_entry = self._cluster_to_sample(cluster)

        # Calculate initial impact score
        impact_score = calculate_impact_score(
            total_failures=cluster.get("failure_count", 1),
            total_occurrences=1,
            severity="ERROR"
        )

        doc = {
            "type": "claude",
            "project": project,
            "fingerprint_id": fingerprint.fingerprint_id,
            "created_at": now,
            "updated_at": now,
            "first_seen": cluster.get("first_failure", {}).get("timestamp", now),
            "last_seen": cluster.get("last_failure", {}).get("timestamp", now),
            "signature": {
                "tool_name": fingerprint.tool_name,
                "error_type": fingerprint.error_type,
                "error_pattern": fingerprint.error_pattern,
                "context_signature": fingerprint.context_signature,
                "cluster_size_avg": cluster.get("failure_count", 1)
            },
            "cluster_count": 1,
            "total_failures": cluster.get("failure_count", 1),
            "occurrences_last_hour": 1,
            "occurrences_last_day": 1,
            "severity": "ERROR",  # All tool failures are ERROR
            "impact_score": impact_score,
            "status": "new",
            "investigation_status": "not_started",
            "sample_entries": [sample_entry],
            "tags": tags,
            "related_signatures": [],
        }

        return doc

    def _map_sample_entry(self, entry_data: dict, metadata: dict) -> Dict[str, Any]:
        """
        Map cluster to sample format for storage.

        Args:
            entry_data: FailureCluster dict
            metadata: Additional metadata

        Returns:
            Sample entry dict
        """
        cluster = entry_data
        return self._cluster_to_sample(cluster)

    def _get_fingerprint_id(self, fingerprint: Any) -> str:
        """
        Get fingerprint ID from ClaudeFailureFingerprint object.

        Args:
            fingerprint: ClaudeFailureFingerprint object

        Returns:
            Fingerprint ID string
        """
        return fingerprint.fingerprint_id

    def _cluster_to_sample(self, cluster: dict) -> Dict[str, Any]:
        """
        Convert cluster to sample entry.

        Args:
            cluster: FailureCluster dict

        Returns:
            Sample entry dict
        """
        last_failure = cluster.get("last_failure", {})
        primary_failure = cluster.get("primary_failure", last_failure)

        return {
            "cluster_id": cluster.get("cluster_id", "unknown"),
            "timestamp": last_failure.get("timestamp"),
            "session_id": cluster.get("session_id", "unknown"),
            "task_id": last_failure.get("call_event", {}).get("task_id", "unknown"),
            "failure_count": cluster.get("failure_count", 1),
            "duration_seconds": cluster.get("duration_seconds", 0.0),
            "tools_attempted": cluster.get("tools_attempted", []),
            "primary_error": primary_failure.get("error_message", "")[:200]
        }

    def _generate_tags(self, fingerprint: Any, cluster: dict) -> list:
        """
        Generate tags for Claude signature.

        Args:
            fingerprint: ClaudeFailureFingerprint object
            cluster: FailureCluster dict

        Returns:
            List of tags
        """
        tags = ["tool_execution", "claude_code"]

        # Add tool name
        tags.append(f"tool:{fingerprint.tool_name}")

        # Add error type
        if fingerprint.error_type and fingerprint.error_type != "unknown_error":
            tags.append(fingerprint.error_type)

        # Add project tag
        tags.append(f"project:{fingerprint.project}")

        # Extract context-based tags using shared utility
        context_tags = extract_tags(
            message=fingerprint.context_signature,
            fingerprint=fingerprint,
            additional_tags=[]
        )

        tags.extend(context_tags)

        return list(set(tags))  # Deduplicate

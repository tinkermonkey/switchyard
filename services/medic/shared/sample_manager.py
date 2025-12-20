"""
Sample entry management utilities for failure signatures.

Handles creation and management of sample entries (log entries, clusters, etc.).
"""

import logging
from typing import List, Dict, Any
from monitoring.timestamp_utils import utc_isoformat

logger = logging.getLogger(__name__)


def create_sample_entry(
    entry_data: dict,
    metadata: dict,
    entry_type: str = "docker"
) -> Dict[str, Any]:
    """
    Create a sample entry for storage in failure signature.

    Args:
        entry_data: Raw entry data (log entry or cluster)
        metadata: Additional metadata (container_info, session_info, etc.)
        entry_type: Type of entry ("docker" or "claude")

    Returns:
        Formatted sample entry dict
    """
    if entry_type == "docker":
        return {
            "timestamp": entry_data.get("timestamp", utc_isoformat()),
            "container_id": metadata.get("id", "unknown"),
            "container_name": metadata.get("name", "unknown"),
            "raw_message": entry_data.get("message", ""),
            "context": {
                "level": entry_data.get("level", ""),
                "logger": entry_data.get("name", ""),
                **entry_data.get("context", {}),
            },
        }
    elif entry_type == "claude":
        return {
            "timestamp": entry_data.get("timestamp", utc_isoformat()),
            "cluster_id": entry_data.get("cluster_id", ""),
            "session_id": metadata.get("session_id", ""),
            "task_id": metadata.get("task_id", ""),
            "failure_count": entry_data.get("failure_count", 1),
            "duration_seconds": entry_data.get("duration_seconds", 0),
            "primary_error": entry_data.get("primary_error", ""),
            "tools_attempted": entry_data.get("tools_attempted", []),
        }
    else:
        logger.warning(f"Unknown entry_type: {entry_type}, using generic format")
        return {
            "timestamp": entry_data.get("timestamp", utc_isoformat()),
            "data": entry_data,
            "metadata": metadata,
        }


def trim_samples(samples: List[dict], max_count: int = 20) -> List[dict]:
    """
    Trim sample entries to maximum count, keeping most recent.

    Args:
        samples: List of sample entry dicts
        max_count: Maximum number of samples to keep

    Returns:
        Trimmed list of sample entries
    """
    if len(samples) <= max_count:
        return samples

    # Sort by timestamp (most recent first) and take top max_count
    sorted_samples = sorted(
        samples,
        key=lambda x: x.get("timestamp", ""),
        reverse=True
    )

    return sorted_samples[:max_count]


def add_sample(
    existing_samples: List[dict],
    new_sample: dict,
    max_count: int = 20
) -> List[dict]:
    """
    Add a new sample entry and trim to maximum count.

    Args:
        existing_samples: List of existing sample entries
        new_sample: New sample entry to add
        max_count: Maximum number of samples to keep

    Returns:
        Updated list of sample entries
    """
    updated_samples = existing_samples + [new_sample]
    return trim_samples(updated_samples, max_count)

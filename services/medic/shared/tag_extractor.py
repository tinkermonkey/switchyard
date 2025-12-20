"""
Tag extraction utilities for failure signatures.

Extracts relevant tags from log entries and fingerprints for categorization.
"""

import logging
from typing import List, Any

logger = logging.getLogger(__name__)


def extract_tags(message: str, fingerprint: Any, additional_tags: List[str] = None) -> List[str]:
    """
    Extract tags from message and fingerprint for categorization.

    Args:
        message: Log message or error message
        fingerprint: Fingerprint object (Docker or Claude)
        additional_tags: Optional list of additional tags to include

    Returns:
        List of unique tag strings
    """
    tags = []

    # Add tags from fingerprint attributes
    if hasattr(fingerprint, 'container_pattern') and fingerprint.container_pattern:
        tags.append(fingerprint.container_pattern)

    if hasattr(fingerprint, 'error_type') and fingerprint.error_type:
        tags.append(fingerprint.error_type)

    if hasattr(fingerprint, 'tool_name') and fingerprint.tool_name:
        tags.append(f"tool:{fingerprint.tool_name}")

    if hasattr(fingerprint, 'project') and fingerprint.project:
        tags.append(f"project:{fingerprint.project}")

    # Extract context-based tags from message
    message_lower = message.lower()

    context_keywords = {
        "agent": "agent_execution",
        "task": "task_processing",
        "pipeline": "pipeline",
        "github": "github",
        "docker": "docker",
        "elasticsearch": "elasticsearch",
        "redis": "redis",
        "timeout": "timeout",
        "permission": "permission",
        "network": "network",
        "disk": "disk_space",
        "memory": "memory",
    }

    for keyword, tag in context_keywords.items():
        if keyword in message_lower:
            tags.append(tag)

    # Add any additional tags provided
    if additional_tags:
        tags.extend(additional_tags)

    # Deduplicate and return
    return list(set(tags))

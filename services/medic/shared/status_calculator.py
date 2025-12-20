"""
Status and severity calculation utilities for failure signatures.

Provides consistent status and severity calculations across Docker and Claude systems.
"""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def calculate_severity(log_entry: dict) -> str:
    """
    Calculate severity from log level.

    Args:
        log_entry: Log entry dict with 'level' field

    Returns:
        Severity string: CRITICAL, ERROR, or WARNING
    """
    level = log_entry.get("level", "").upper()

    severity_map = {
        "CRITICAL": "CRITICAL",
        "FATAL": "CRITICAL",
        "ERROR": "ERROR",
        "WARNING": "WARNING",
        "WARN": "WARNING",
    }

    return severity_map.get(level, "ERROR")


def calculate_status(
    current_status: str,
    occurrence_count: int,
    is_trending: bool
) -> str:
    """
    Calculate signature status based on occurrence count and trends.

    Args:
        current_status: Current status string
        occurrence_count: Total number of occurrences
        is_trending: Whether the failure rate is increasing

    Returns:
        Status string: new, recurring, trending, ignored, or resolved
    """
    # Don't change manually set statuses
    if current_status in ["ignored", "resolved"]:
        return current_status

    if is_trending:
        return "trending"
    elif occurrence_count >= 2:
        return "recurring"
    else:
        return "new"


def calculate_impact_score(
    total_failures: int,
    total_occurrences: int,
    severity: str = "ERROR"
) -> float:
    """
    Calculate impact score for a failure signature.

    Args:
        total_failures: Total number of individual failures
        total_occurrences: Total number of occurrences (Docker) or clusters (Claude)
        severity: Severity level (CRITICAL, ERROR, WARNING)

    Returns:
        Impact score as float between 0.0 and 100.0
    """
    # Base score from failures and occurrences
    base_score = (total_failures * 0.7) + (total_occurrences * 0.3)

    # Severity multiplier
    severity_multiplier = {
        "CRITICAL": 2.0,
        "ERROR": 1.0,
        "WARNING": 0.5
    }.get(severity, 1.0)

    # Calculate final score (capped at 100)
    score = min(base_score * severity_multiplier, 100.0)

    return round(score, 2)

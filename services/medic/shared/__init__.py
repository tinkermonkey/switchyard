"""
Shared utilities for Medic service.

This package contains common utilities used by both Docker and Claude failure tracking systems.
"""

from .status_calculator import calculate_severity, calculate_status, calculate_impact_score
from .tag_extractor import extract_tags
from .sample_manager import create_sample_entry, trim_samples, add_sample
from .elasticsearch_utils import (
    setup_index_template,
    async_index,
    update_by_query,
    search_signatures,
    get_signature_by_id,
    delete_by_query,
)
from .redis_utils import acquire_lock, release_lock, get_investigation_status

__all__ = [
    'calculate_severity',
    'calculate_status',
    'calculate_impact_score',
    'extract_tags',
    'create_sample_entry',
    'trim_samples',
    'add_sample',
    'setup_index_template',
    'async_index',
    'update_by_query',
    'search_signatures',
    'get_signature_by_id',
    'delete_by_query',
    'acquire_lock',
    'release_lock',
    'get_investigation_status',
]

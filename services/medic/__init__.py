"""
Medic: Self-Healing Monitoring System for Clauditoreum

The Medic component monitors Docker container logs for errors and warnings,
creates unique fingerprints for deduplication, tracks failure patterns in
Elasticsearch, and investigates root causes.

Phase 1: Visibility & Detection (No Auto-Fixing)
- Docker log monitoring
- Error fingerprinting and deduplication
- Elasticsearch-backed failure signature storage
- REST API for querying signatures and statistics
"""

__version__ = "1.0.0"

from .fingerprint_engine import FingerprintEngine
from .failure_signature_store import FailureSignatureStore
from .normalizers import (
    TimestampNormalizer,
    UUIDNormalizer,
    PathNormalizer,
    IssueNumberNormalizer,
    ContainerIDNormalizer,
)

__all__ = [
    "FingerprintEngine",
    "FailureSignatureStore",
    "TimestampNormalizer",
    "UUIDNormalizer",
    "PathNormalizer",
    "IssueNumberNormalizer",
    "ContainerIDNormalizer",
]

"""
Medic: Self-Healing Monitoring System for Clauditoreum

The Medic component monitors failures from both Docker containers and Claude Code
tool executions. It creates unique fingerprints for deduplication, tracks failure
patterns in Elasticsearch, and investigates root causes.

Features:
- Docker log monitoring with failure fingerprinting
- Claude Code tool execution failure tracking
- Elasticsearch-backed signature storage
- Automated investigation orchestration
- REST API for querying signatures and statistics
"""

__version__ = "2.0.0"

# Import from submodules
from .docker import (
    FingerprintEngine,
    DockerFailureSignatureStore,
    DockerLogMonitor,
    DockerInvestigationOrchestrator,
    DockerInvestigationQueue,
    DockerReportManager,
    DockerInvestigationAgentRunner,
)

from .claude import (
    ClaudeFailureSignatureStore,
    ClaudeFailureMonitor,
    ClaudeInvestigationOrchestrator,
    ClaudeInvestigationQueue,
    ClaudeReportManager,
    ClaudeInvestigationAgentRunner,
    ClaudeFingerprintEngine,
    ClaudeFailureFingerprint,
    FailureClusteringEngine,
    FailureCluster,
    ClaudeSignatureCurator,
    ClaudeAdvisorOrchestrator,
    ClaudeAdvisorAgentRunner,
)

from .normalizers import (
    TimestampNormalizer,
    UUIDNormalizer,
    PathNormalizer,
    IssueNumberNormalizer,
    ContainerIDNormalizer,
)

__all__ = [
    # Docker components
    "FingerprintEngine",
    "DockerFailureSignatureStore",
    "DockerLogMonitor",
    "DockerInvestigationOrchestrator",
    "DockerInvestigationQueue",
    "DockerReportManager",
    "DockerInvestigationAgentRunner",
    # Claude components
    "ClaudeFailureSignatureStore",
    "ClaudeFailureMonitor",
    "ClaudeInvestigationOrchestrator",
    "ClaudeInvestigationQueue",
    "ClaudeReportManager",
    "ClaudeInvestigationAgentRunner",
    "ClaudeFingerprintEngine",
    "ClaudeFailureFingerprint",
    "FailureClusteringEngine",
    "FailureCluster",
    "ClaudeSignatureCurator",
    "ClaudeAdvisorOrchestrator",
    "ClaudeAdvisorAgentRunner",
    # Shared normalizers
    "TimestampNormalizer",
    "UUIDNormalizer",
    "PathNormalizer",
    "IssueNumberNormalizer",
    "ContainerIDNormalizer",
]

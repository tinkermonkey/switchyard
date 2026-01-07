"""
Message Normalizers for Error Fingerprinting

These normalizers strip variable components from error messages to enable
effective deduplication and fingerprinting.
"""

import re
from abc import ABC, abstractmethod
from typing import Pattern


class BaseNormalizer(ABC):
    """Base class for message normalizers"""

    @abstractmethod
    def normalize(self, message: str) -> str:
        """Normalize a message by removing/replacing variable components"""
        pass


class TimestampNormalizer(BaseNormalizer):
    """
    Strips timestamps in various formats.

    Examples:
    - "2025-11-28 12:45:23" -> "{timestamp}"
    - "2025-11-28T12:45:23.123456Z" -> "{timestamp}"
    - "1732800000" (unix timestamp) -> "{timestamp}"

    NOTE: This should NOT normalize timestamps in structured log prefixes that have already
    been parsed. It's for normalizing timestamps that appear in error messages themselves.
    """

    PATTERNS = [
        # ISO 8601 timestamps with timezone
        re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})'),
        # Date-time patterns like "2025-11-29 02:15:00 UTC"
        re.compile(r'\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}(?:\s+[A-Z]{3,4})?'),
        # Unix timestamps (10-13 digits) - must be word boundary
        re.compile(r'\b\d{10,13}\b'),
    ]

    def normalize(self, message: str) -> str:
        for pattern in self.PATTERNS:
            message = pattern.sub('{timestamp}', message)
        return message


class DurationNormalizer(BaseNormalizer):
    """
    Normalizes time durations in error messages.

    Examples:
    - "missed by 0:00:02.515269" -> "missed by {duration}"
    - "timeout after 30.123 seconds" -> "timeout after {duration} seconds"
    - "took 1:23:45.678" -> "took {duration}"
    """

    PATTERNS = [
        # Duration with hours:minutes:seconds.microseconds
        re.compile(r'\d+:\d{2}:\d{2}(?:\.\d+)?'),
        # Duration with minutes:seconds.microseconds
        re.compile(r'\d+:\d{2}(?:\.\d+)?(?=\s|$|\))'),
        # Decimal seconds (e.g., "30.123")
        re.compile(r'\b\d+\.\d{3,}(?=\s*(?:second|sec|ms|s|$))'),
    ]

    def normalize(self, message: str) -> str:
        for pattern in self.PATTERNS:
            message = pattern.sub('{duration}', message)
        return message


class UUIDNormalizer(BaseNormalizer):
    """
    Strips UUIDs and UUID-like identifiers.

    Examples:
    - "task_ba_550e8400-e29b-41d4-a716-446655440000" -> "task_ba_{uuid}"
    - "agent-execution-123e4567-e89b-12d3-a456-426614174000" -> "agent-execution-{uuid}"
    """

    PATTERN = re.compile(
        r'[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'
    )

    def normalize(self, message: str) -> str:
        return self.PATTERN.sub('{uuid}', message)


class PathNormalizer(BaseNormalizer):
    """
    Normalizes file paths, especially workspace-relative paths.

    Examples:
    - "/workspace/what_am_i_watching/src/main.py" -> "/workspace/{project}/src/main.py"
    - "/tmp/tmpAbc123/file.txt" -> "/tmp/{tmp}/file.txt"
    - "/home/user/workspace/project-name/file" -> "/home/user/workspace/{project}/file"
    """

    PATTERNS = [
        # Workspace paths
        (re.compile(r'/workspace/([^/]+)/'), r'/workspace/{project}/'),
        # Temp directories
        (re.compile(r'/tmp/[a-zA-Z0-9_-]+/'), r'/tmp/{tmp}/'),
        # Home directory project paths
        (re.compile(r'/home/[^/]+/workspace/([^/]+)/'), r'/home/{user}/workspace/{project}/'),
    ]

    def normalize(self, message: str) -> str:
        for pattern, replacement in self.PATTERNS:
            message = pattern.sub(replacement, message)
        return message


class IssueNumberNormalizer(BaseNormalizer):
    """
    Normalizes issue numbers, task IDs, and similar identifiers.

    Examples:
    - "issue #123" -> "issue #{issue}"
    - "task_ba_1234567890" -> "task_{task_id}"
    - "pipeline_run_1732800000" -> "pipeline_run_{pipeline_id}"
    - "container orchestrator-1" -> "container orchestrator-{instance}"
    """

    PATTERNS = [
        # GitHub issue numbers
        (re.compile(r'#\d+'), '#{issue}'),
        # Task IDs with UUIDs (must come before UUID normalizer)
        (re.compile(r'task_[a-z_]+_[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}'), 'task_{task_id}'),
        # Task IDs with timestamps
        (re.compile(r'task_[a-z_]+_\d+'), 'task_{task_id}'),
        # Pipeline run IDs
        (re.compile(r'pipeline_run_\d+'), 'pipeline_run_{pipeline_id}'),
        # Pipeline IDs
        (re.compile(r'pipeline_\d+'), 'pipeline_{pipeline_id}'),
        # Agent execution IDs
        (re.compile(r'agent_execution_\d+'), 'agent_execution_{execution_id}'),
        # Container instance numbers - IMPROVED: Only match after whitespace/start and before whitespace/end
        # Avoid matching in contexts like "2025-11-29" where it would incorrectly match "29"
        (re.compile(r'(?<=\s)(\w+)-(\d+)(?=\s|$)'), r'\1-{instance}'),
    ]

    def normalize(self, message: str) -> str:
        for pattern, replacement in self.PATTERNS:
            message = pattern.sub(replacement, message)
        return message


class ContainerIDNormalizer(BaseNormalizer):
    """
    Strips Docker container IDs and hashes.

    Examples:
    - "container abc123def456" -> "container {container_id}"
    - "sha256:abc123..." -> "sha256:{hash}"
    """

    PATTERNS = [
        # Docker container IDs (12-64 hex chars)
        re.compile(r'\b[0-9a-fA-F]{12,64}\b'),
        # SHA256 hashes
        re.compile(r'sha256:[0-9a-fA-F]+'),
        # Generic hashes (8+ hex chars)
        re.compile(r'\b[0-9a-fA-F]{8,}\b'),
    ]

    def normalize(self, message: str) -> str:
        for pattern in self.PATTERNS:
            message = pattern.sub('{hash}', message)
        return message


class LineNumberNormalizer(BaseNormalizer):
    """
    Preserves line numbers in stack traces but normalizes them when embedded in text.

    Examples:
    - "at file.py:123" -> "at file.py:{line}" (in regular text)
    - "file.py:123" -> "file.py:{line}" (in stack traces, preserve for signature)
    """

    # Only normalize line numbers in verbose messages, not in stack traces
    PATTERN = re.compile(r'line\s+\d+')

    def normalize(self, message: str) -> str:
        return self.PATTERN.sub('line {line}', message)


class JSONBlobNormalizer(BaseNormalizer):
    """
    Normalizes JSON blobs and large data structures.

    Examples:
    - '{"key": "value", ...}' -> '{json}'
    - Large JSON in error messages
    """

    PATTERN = re.compile(r'\{["\']?\w+["\']?\s*:\s*[^}]{20,}\}')

    def normalize(self, message: str) -> str:
        # Only normalize if JSON blob is large (>20 chars of content)
        return self.PATTERN.sub('{json}', message)


class CounterNormalizer(BaseNormalizer):
    """
    Normalize counters and numeric metrics in error messages.

    This addresses the signature explosion caused by incrementing counters
    in repeated error messages (e.g., "failed for 1000 times", "failed for 1001 times").

    Examples:
    - "failed for 1000 times" -> "failed for {count} times"
    - "succeeded 5 times" -> "succeeded {count} times"
    - "1000 times in a row" -> "{count} times in a row"
    - "timeout 30" -> "timeout {value}"
    - "processed 500 items" -> "processed {count} items"
    """

    PATTERNS = [
        # "failed for 1000 times" → "failed for {count} times"
        (re.compile(r'\bfailed\s+for\s+\d+\s+times?\b'), 'failed for {count} times'),
        # "succeeded 5 times" → "succeeded {count} times"
        (re.compile(r'\b(succeeded|failed|tried|attempted)\s+\d+\s+times?\b'), r'\1 {count} times'),
        # "1000 times in a row" → "{count} times in a row"
        (re.compile(r'\b\d+\s+times?\s+in\s+a\s+row\b'), '{count} times in a row'),
        # "timeout 30" → "timeout {value}"
        (re.compile(r'\b(timeout|delay|wait)\s+\d+\b'), r'\1 {value}'),
        # Standalone metrics: "processed 500 items" → "processed {count} items"
        (re.compile(r'\b\d+\s+(items?|records?|rows?|entries?)\b'), r'{count} \1'),
    ]

    def normalize(self, message: str) -> str:
        for pattern, replacement in self.PATTERNS:
            message = pattern.sub(replacement, message)
        return message


class AttemptNormalizer(BaseNormalizer):
    """
    Normalize retry and attempt counters.

    Addresses signature explosion from retry loops where attempt numbers vary
    (e.g., "attempt 0 of 3", "attempt 1 of 3", "attempt 2 of 3").

    Examples:
    - "attempt 0 of 3" -> "attempt {n} of {total}"
    - "try 2" -> "try {n}"
    - "(attempt 1)" -> "(attempt {n})"
    - "on retry #5" -> "on retry #{n}"
    """

    PATTERNS = [
        # "attempt 0 of 3" → "attempt {n} of {total}"
        (re.compile(r'\battempt\s+\d+\s+of\s+\d+\b'), 'attempt {n} of {total}'),
        # "try 2" → "try {n}"
        (re.compile(r'\b(try|attempt|retry)\s+\d+\b'), r'\1 {n}'),
        # "(attempt 1)" → "(attempt {n})"
        (re.compile(r'\(\s*(try|attempt|retry)\s+\d+\s*\)'), r'(\1 {n})'),
        # "on retry #5" → "on retry #{n}"
        (re.compile(r'\bon\s+(try|attempt|retry)\s+#?\d+\b'), r'on \1 {n}'),
    ]

    def normalize(self, message: str) -> str:
        for pattern, replacement in self.PATTERNS:
            message = pattern.sub(replacement, message)
        return message


class StatusCodeNormalizer(BaseNormalizer):
    """
    Normalize HTTP status codes and error codes.

    Prevents different HTTP status codes from creating separate signatures
    when the underlying error type is the same.

    Examples:
    - "status 503" -> "status {code}"
    - "code 404" -> "code {code}"
    - "HTTP 500" -> "HTTP {code}"
    - "exit code 1" -> "exit code {code}"
    """

    PATTERNS = [
        # "status 503" → "status {code}"
        (re.compile(r'\bstatus\s+\d{3}\b'), 'status {code}'),
        # "code 404" → "code {code}"
        (re.compile(r'\b(code|error)\s+\d{3,4}\b'), r'\1 {code}'),
        # "HTTP 500" → "HTTP {code}"
        (re.compile(r'\bHTTP\s+\d{3}\b'), 'HTTP {code}'),
        # "exit code 1" → "exit code {code}"
        (re.compile(r'\bexit\s+code\s+\d+\b'), 'exit code {code}'),
    ]

    def normalize(self, message: str) -> str:
        for pattern, replacement in self.PATTERNS:
            message = pattern.sub(replacement, message)
        return message


class RatioNormalizer(BaseNormalizer):
    """
    Normalize numeric ratios and fractions.

    Prevents different progress values from creating separate signatures
    (e.g., "0 of 3", "1 of 3", "2 of 3" should be one signature).

    Examples:
    - "0 of 3" -> "{n} of {total}"
    - "5/10 items" -> "{n}/{total} items"
    - "processed 50%" -> "processed {percentage}%"
    """

    PATTERNS = [
        # "0 of 3" → "{n} of {total}"
        (re.compile(r'\b\d+\s+of\s+\d+\b'), '{n} of {total}'),
        # "5/10 items" → "{n}/{total} items"
        (re.compile(r'\b\d+/\d+'), '{n}/{total}'),
        # "processed 50%" → "processed {percentage}%"
        (re.compile(r'\b\d+%'), '{percentage}%'),
    ]

    def normalize(self, message: str) -> str:
        for pattern, replacement in self.PATTERNS:
            message = pattern.sub(replacement, message)
        return message


def get_default_normalizers() -> list[BaseNormalizer]:
    """
    Get the default set of normalizers in order of application.

    Order matters!
    - TimestampNormalizer must come first to normalize dates before IssueNumberNormalizer
    - DurationNormalizer should come early to normalize durations
    - CounterNormalizer, AttemptNormalizer, StatusCodeNormalizer, RatioNormalizer should come
      before generic ID normalization to handle specific numeric patterns
    - IssueNumberNormalizer must come before UUIDNormalizer to catch task_ba_<UUID> patterns
    """
    return [
        TimestampNormalizer(),    # First to normalize dates/times
        DurationNormalizer(),     # Early to normalize time durations
        CounterNormalizer(),      # Normalize numeric counters and metrics
        AttemptNormalizer(),      # Normalize retry/attempt counters
        StatusCodeNormalizer(),   # Normalize HTTP and error status codes
        RatioNormalizer(),        # Normalize ratios and percentages
        IssueNumberNormalizer(),  # Before UUID to catch task_ba_UUID patterns
        UUIDNormalizer(),
        PathNormalizer(),
        ContainerIDNormalizer(),
        LineNumberNormalizer(),
        JSONBlobNormalizer(),
    ]

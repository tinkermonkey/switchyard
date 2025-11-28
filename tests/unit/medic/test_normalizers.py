"""
Unit tests for Medic normalizers
"""

import pytest
from services.medic.normalizers import (
    TimestampNormalizer,
    UUIDNormalizer,
    PathNormalizer,
    IssueNumberNormalizer,
    ContainerIDNormalizer,
    LineNumberNormalizer,
    JSONBlobNormalizer,
    get_default_normalizers,
)


class TestTimestampNormalizer:
    """Test timestamp normalization"""

    def setup_method(self):
        self.normalizer = TimestampNormalizer()

    def test_normalize_iso8601_timestamp(self):
        message = "Error occurred at 2025-11-28T12:45:23.123456Z"
        result = self.normalizer.normalize(message)
        assert result == "Error occurred at {timestamp}"

    def test_normalize_standard_timestamp(self):
        message = "2025-11-28 12:45:23 - ERROR - Something failed"
        result = self.normalizer.normalize(message)
        assert result == "{timestamp} - ERROR - Something failed"

    def test_normalize_unix_timestamp(self):
        message = "Timestamp: 1732800000 - Error"
        result = self.normalizer.normalize(message)
        assert result == "Timestamp: {timestamp} - Error"

    def test_normalize_multiple_timestamps(self):
        message = "Started at 2025-11-28 12:00:00 and failed at 2025-11-28 12:45:23"
        result = self.normalizer.normalize(message)
        assert result == "Started at {timestamp} and failed at {timestamp}"

    def test_preserve_non_timestamp_numbers(self):
        message = "Error code 123 occurred"
        result = self.normalizer.normalize(message)
        assert result == "Error code 123 occurred"


class TestUUIDNormalizer:
    """Test UUID normalization"""

    def setup_method(self):
        self.normalizer = UUIDNormalizer()

    def test_normalize_uuid(self):
        message = "Task task_ba_550e8400-e29b-41d4-a716-446655440000 failed"
        result = self.normalizer.normalize(message)
        assert result == "Task task_ba_{uuid} failed"

    def test_normalize_multiple_uuids(self):
        message = "Agent 123e4567-e89b-12d3-a456-426614174000 executing task 550e8400-e29b-41d4-a716-446655440000"
        result = self.normalizer.normalize(message)
        assert result == "Agent {uuid} executing task {uuid}"

    def test_preserve_non_uuid_hex(self):
        message = "Container abc123 running"
        result = self.normalizer.normalize(message)
        assert result == "Container abc123 running"


class TestPathNormalizer:
    """Test path normalization"""

    def setup_method(self):
        self.normalizer = PathNormalizer()

    def test_normalize_workspace_path(self):
        message = "Error in /workspace/what_am_i_watching/src/main.py"
        result = self.normalizer.normalize(message)
        assert result == "Error in /workspace/{project}/src/main.py"

    def test_normalize_temp_directory(self):
        message = "Created temp file /tmp/tmpAbc123/file.txt"
        result = self.normalizer.normalize(message)
        assert result == "Created temp file /tmp/{tmp}/file.txt"

    def test_normalize_home_workspace_path(self):
        message = "File at /home/user/workspace/my-project/src/file.py"
        result = self.normalizer.normalize(message)
        assert result == "File at /home/{user}/workspace/{project}/src/file.py"

    def test_preserve_non_workspace_paths(self):
        message = "Loading /usr/local/lib/python3.11/site.py"
        result = self.normalizer.normalize(message)
        assert result == "Loading /usr/local/lib/python3.11/site.py"


class TestIssueNumberNormalizer:
    """Test issue number and ID normalization"""

    def setup_method(self):
        self.normalizer = IssueNumberNormalizer()

    def test_normalize_github_issue(self):
        message = "Processing issue #123"
        result = self.normalizer.normalize(message)
        assert result == "Processing issue #{issue}"

    def test_normalize_task_id(self):
        message = "Task task_ba_1234567890 started"
        result = self.normalizer.normalize(message)
        assert result == "Task task_{task_id} started"

    def test_normalize_pipeline_run_id(self):
        message = "Pipeline run pipeline_run_1732800000 completed"
        result = self.normalizer.normalize(message)
        assert result == "Pipeline run pipeline_run_{pipeline_id} completed"

    def test_normalize_container_instance(self):
        message = "Container orchestrator-1 stopped"
        result = self.normalizer.normalize(message)
        assert result == "Container orchestrator-{instance} stopped"

    def test_normalize_multiple_ids(self):
        message = "Issue #42 in pipeline_1234 and task_ba_9876"
        result = self.normalizer.normalize(message)
        assert result == "Issue #{issue} in pipeline_{pipeline_id} and task_{task_id}"


class TestContainerIDNormalizer:
    """Test container ID and hash normalization"""

    def setup_method(self):
        self.normalizer = ContainerIDNormalizer()

    def test_normalize_docker_container_id(self):
        message = "Container abc123def456 is running"
        result = self.normalizer.normalize(message)
        assert result == "Container {hash} is running"

    def test_normalize_sha256_hash(self):
        message = "Image sha256:abc123def456789 pulled"
        result = self.normalizer.normalize(message)
        assert result == "Image sha256:{hash} pulled"

    def test_normalize_long_hex_hash(self):
        message = "Hash: abcdef1234567890abcdef1234567890"
        result = self.normalizer.normalize(message)
        assert result == "Hash: {hash}"

    def test_preserve_short_hex(self):
        message = "Error code abc occurred"
        result = self.normalizer.normalize(message)
        # Short hex (< 8 chars) should be preserved
        assert "abc" in result


class TestLineNumberNormalizer:
    """Test line number normalization"""

    def setup_method(self):
        self.normalizer = LineNumberNormalizer()

    def test_normalize_line_reference(self):
        message = "Error at line 123"
        result = self.normalizer.normalize(message)
        assert result == "Error at line {line}"

    def test_preserve_colon_line_numbers(self):
        # These are preserved for stack trace signatures
        message = "at file.py:123"
        result = self.normalizer.normalize(message)
        assert result == "at file.py:123"


class TestJSONBlobNormalizer:
    """Test JSON blob normalization"""

    def setup_method(self):
        self.normalizer = JSONBlobNormalizer()

    def test_normalize_large_json(self):
        message = 'Error with data: {"key": "value", "other": "data", "more": "stuff"}'
        result = self.normalizer.normalize(message)
        assert result == "Error with data: {json}"

    def test_preserve_small_json(self):
        message = 'Error: {"key": "val"}'
        result = self.normalizer.normalize(message)
        # Small JSON (< 20 chars content) is preserved
        assert '{"key": "val"}' in result


class TestNormalizerChain:
    """Test applying multiple normalizers in sequence"""

    def test_get_default_normalizers(self):
        normalizers = get_default_normalizers()
        assert len(normalizers) == 7
        assert isinstance(normalizers[0], TimestampNormalizer)
        assert isinstance(normalizers[1], IssueNumberNormalizer)  # Changed order
        assert isinstance(normalizers[2], UUIDNormalizer)

    def test_chain_normalization(self):
        """Test that normalizers work together correctly"""
        normalizers = get_default_normalizers()

        message = "2025-11-28 12:45:23 - task_ba_550e8400-e29b-41d4-a716-446655440000 failed at /workspace/my-project/src/main.py:123 in container orchestrator-1"

        # Apply all normalizers
        for normalizer in normalizers:
            message = normalizer.normalize(message)

        # Verify all variable parts are normalized
        assert "{timestamp}" in message
        # Note: task_ba_UUID gets fully normalized to task_{task_id}
        assert "task_{task_id}" in message
        assert "{project}" in message
        assert "{instance}" in message

    def test_normalization_preserves_error_content(self):
        """Ensure normalization doesn't remove important error information"""
        normalizers = get_default_normalizers()

        message = "KeyError: 'issue_number' in task context at main.py:42"

        for normalizer in normalizers:
            message = normalizer.normalize(message)

        # Core error information should be preserved
        assert "KeyError" in message
        assert "'issue_number'" in message
        assert "task context" in message
        assert "main.py" in message


class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    def test_empty_string(self):
        normalizers = get_default_normalizers()
        message = ""

        for normalizer in normalizers:
            message = normalizer.normalize(message)

        assert message == ""

    def test_no_matches(self):
        normalizers = get_default_normalizers()
        message = "Simple error message with no variable parts"

        for normalizer in normalizers:
            message = normalizer.normalize(message)

        assert message == "Simple error message with no variable parts"

    def test_special_characters(self):
        normalizers = get_default_normalizers()
        message = "Error: <KeyError> [CRITICAL] (failure)"

        for normalizer in normalizers:
            message = normalizer.normalize(message)

        # Special characters should be preserved
        assert "<KeyError>" in message or "KeyError" in message
        assert "CRITICAL" in message

    def test_unicode_characters(self):
        normalizers = get_default_normalizers()
        message = "Error: 文字化け occurred at 2025-11-28"

        for normalizer in normalizers:
            message = normalizer.normalize(message)

        assert "文字化け" in message
        assert "{timestamp}" in message

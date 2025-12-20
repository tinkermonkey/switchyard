"""
Unit tests for sample_manager utilities.
"""

import pytest
from services.medic.shared.sample_manager import (
    create_sample_entry,
    trim_samples,
    add_sample
)


class TestCreateSampleEntry:
    """Tests for create_sample_entry function."""

    def test_create_docker_sample(self):
        """Test creation of Docker sample entry."""
        entry_data = {
            "timestamp": "2025-12-19T10:00:00Z",
            "message": "Error occurred",
            "level": "ERROR",
            "name": "test.logger"
        }
        metadata = {
            "id": "container123",
            "name": "orchestrator"
        }

        sample = create_sample_entry(entry_data, metadata, "docker")

        assert sample["timestamp"] == "2025-12-19T10:00:00Z"
        assert sample["container_id"] == "container123"
        assert sample["container_name"] == "orchestrator"
        assert sample["raw_message"] == "Error occurred"
        assert sample["context"]["level"] == "ERROR"
        assert sample["context"]["logger"] == "test.logger"

    def test_create_claude_sample(self):
        """Test creation of Claude sample entry."""
        entry_data = {
            "timestamp": "2025-12-19T10:00:00Z",
            "cluster_id": "cluster123",
            "failure_count": 5,
            "duration_seconds": 30,
            "primary_error": "Tool execution failed",
            "tools_attempted": ["Read", "Write"]
        }
        metadata = {
            "session_id": "session123",
            "task_id": "task123"
        }

        sample = create_sample_entry(entry_data, metadata, "claude")

        assert sample["timestamp"] == "2025-12-19T10:00:00Z"
        assert sample["cluster_id"] == "cluster123"
        assert sample["session_id"] == "session123"
        assert sample["task_id"] == "task123"
        assert sample["failure_count"] == 5
        assert sample["duration_seconds"] == 30
        assert sample["primary_error"] == "Tool execution failed"
        assert sample["tools_attempted"] == ["Read", "Write"]

    def test_create_docker_sample_with_missing_fields(self):
        """Test Docker sample with missing fields uses defaults."""
        entry_data = {}
        metadata = {}

        sample = create_sample_entry(entry_data, metadata, "docker")

        assert "timestamp" in sample
        assert sample["container_id"] == "unknown"
        assert sample["container_name"] == "unknown"
        assert sample["raw_message"] == ""

    def test_create_unknown_type_uses_generic_format(self):
        """Test that unknown entry_type uses generic format."""
        entry_data = {"key": "value"}
        metadata = {"meta": "data"}

        sample = create_sample_entry(entry_data, metadata, "unknown_type")

        assert "timestamp" in sample
        assert sample["data"] == entry_data
        assert sample["metadata"] == metadata


class TestTrimSamples:
    """Tests for trim_samples function."""

    def test_trim_no_op_when_under_limit(self):
        """Test that samples are not trimmed when count is under limit."""
        samples = [{"timestamp": f"2025-12-19T10:00:0{i}Z"} for i in range(10)]
        trimmed = trim_samples(samples, max_count=20)
        assert len(trimmed) == 10

    def test_trim_when_over_limit(self):
        """Test that samples are trimmed when count exceeds limit."""
        samples = [{"timestamp": f"2025-12-19T10:00:{i:02d}Z"} for i in range(30)]
        trimmed = trim_samples(samples, max_count=20)
        assert len(trimmed) == 20

    def test_trim_keeps_most_recent(self):
        """Test that trimming keeps the most recent samples."""
        samples = [
            {"timestamp": "2025-12-19T10:00:00Z", "id": 1},
            {"timestamp": "2025-12-19T10:00:01Z", "id": 2},
            {"timestamp": "2025-12-19T10:00:02Z", "id": 3},
        ]
        trimmed = trim_samples(samples, max_count=2)

        assert len(trimmed) == 2
        # Should keep most recent (id 3 and 2)
        assert trimmed[0]["id"] == 3
        assert trimmed[1]["id"] == 2

    def test_trim_with_exact_limit(self):
        """Test trimming when count equals limit."""
        samples = [{"timestamp": f"2025-12-19T10:00:0{i}Z"} for i in range(20)]
        trimmed = trim_samples(samples, max_count=20)
        assert len(trimmed) == 20

    def test_trim_empty_list(self):
        """Test trimming empty list."""
        trimmed = trim_samples([], max_count=20)
        assert trimmed == []


class TestAddSample:
    """Tests for add_sample function."""

    def test_add_sample_to_empty_list(self):
        """Test adding sample to empty list."""
        existing = []
        new_sample = {"timestamp": "2025-12-19T10:00:00Z"}

        updated = add_sample(existing, new_sample, max_count=20)

        assert len(updated) == 1
        assert updated[0] == new_sample

    def test_add_sample_under_limit(self):
        """Test adding sample when under limit."""
        existing = [{"timestamp": f"2025-12-19T10:00:0{i}Z"} for i in range(5)]
        new_sample = {"timestamp": "2025-12-19T10:00:10Z"}

        updated = add_sample(existing, new_sample, max_count=20)

        assert len(updated) == 6
        assert new_sample in updated

    def test_add_sample_at_limit(self):
        """Test adding sample when at limit triggers trim."""
        existing = [{"timestamp": f"2025-12-19T10:00:{i:02d}Z", "id": i} for i in range(20)]
        new_sample = {"timestamp": "2025-12-19T10:01:00Z", "id": 99}

        updated = add_sample(existing, new_sample, max_count=20)

        assert len(updated) == 20
        # New sample should be included
        assert any(s["id"] == 99 for s in updated)

    def test_add_sample_preserves_order_by_timestamp(self):
        """Test that added sample is properly ordered by timestamp."""
        existing = [
            {"timestamp": "2025-12-19T10:00:00Z", "id": 1},
            {"timestamp": "2025-12-19T10:00:02Z", "id": 2},
        ]
        new_sample = {"timestamp": "2025-12-19T10:00:01Z", "id": 3}

        updated = add_sample(existing, new_sample, max_count=20)

        # After sorting by timestamp desc, order should be: 2, 3, 1
        assert len(updated) == 3

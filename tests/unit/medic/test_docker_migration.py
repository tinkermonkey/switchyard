"""
Unit tests for Docker failure signature migration.

Tests the document transformation logic without requiring Elasticsearch.
"""

import pytest
import sys
from pathlib import Path

# Add scripts directory to path
sys.path.append(str(Path(__file__).parent.parent.parent.parent / "scripts"))

from migrate_docker_failures import DockerFailureMigration


class TestDockerFailureMigration:
    """Test Docker failure signature migration transformations."""

    def setup_method(self):
        """Set up test fixtures."""
        # Create migrator without ES connection
        self.migrator = DockerFailureMigration.__new__(DockerFailureMigration)

    def test_transform_document_adds_type_and_project(self):
        """Test that transformation adds type and project fields."""
        old_doc = {
            "fingerprint_id": "test123",
            "signature": {"error_type": "ValueError"},
            "occurrence_count": 5,
        }

        new_doc = self.migrator.transform_document(old_doc)

        assert new_doc["type"] == "docker"
        assert new_doc["project"] == "orchestrator"

    def test_transform_document_renames_sample_log_entries(self):
        """Test that sample_log_entries is renamed to sample_entries."""
        old_doc = {
            "fingerprint_id": "test123",
            "sample_log_entries": [
                {"timestamp": "2025-01-01T00:00:00Z", "message": "Error 1"},
                {"timestamp": "2025-01-01T00:01:00Z", "message": "Error 2"},
            ],
            "occurrence_count": 2,
        }

        new_doc = self.migrator.transform_document(old_doc)

        assert "sample_log_entries" not in new_doc
        assert "sample_entries" in new_doc
        assert len(new_doc["sample_entries"]) == 2
        assert new_doc["sample_entries"][0]["message"] == "Error 1"

    def test_transform_document_adds_total_failures(self):
        """Test that total_failures is set equal to occurrence_count."""
        old_doc = {
            "fingerprint_id": "test123",
            "occurrence_count": 42,
        }

        new_doc = self.migrator.transform_document(old_doc)

        assert new_doc["total_failures"] == 42
        assert new_doc["occurrence_count"] == 42  # Original field preserved

    def test_transform_document_preserves_other_fields(self):
        """Test that other fields are preserved during transformation."""
        old_doc = {
            "fingerprint_id": "test123",
            "signature": {
                "error_type": "ConnectionError",
                "error_pattern": "Failed to connect",
                "container_pattern": "orchestrator",
            },
            "occurrence_count": 10,
            "status": "new",
            "severity": "ERROR",
            "tags": ["networking", "connection"],
            "first_seen": "2025-01-01T00:00:00Z",
            "last_seen": "2025-01-15T12:00:00Z",
            "investigation_status": "not_started",
        }

        new_doc = self.migrator.transform_document(old_doc)

        # Check all original fields are preserved
        assert new_doc["fingerprint_id"] == "test123"
        assert new_doc["signature"]["error_type"] == "ConnectionError"
        assert new_doc["occurrence_count"] == 10
        assert new_doc["status"] == "new"
        assert new_doc["severity"] == "ERROR"
        assert new_doc["tags"] == ["networking", "connection"]
        assert new_doc["investigation_status"] == "not_started"

    def test_transform_document_handles_missing_sample_entries(self):
        """Test transformation when sample_log_entries is missing."""
        old_doc = {
            "fingerprint_id": "test123",
            "occurrence_count": 1,
        }

        new_doc = self.migrator.transform_document(old_doc)

        # Should not have sample_entries if original didn't have sample_log_entries
        assert "sample_entries" not in new_doc
        assert "sample_log_entries" not in new_doc

    def test_generate_new_index_name_with_date_suffix(self):
        """Test new index name generation with date suffix."""
        old_index = "medic-failure-signatures-2025.01.15"

        new_index = self.migrator.generate_new_index_name(old_index)

        assert new_index == "medic-docker-failures-2025.01.15"

    def test_generate_new_index_name_with_timestamp_suffix(self):
        """Test new index name generation with timestamp suffix."""
        old_index = "medic-failure-signatures-20250115-120000"

        new_index = self.migrator.generate_new_index_name(old_index)

        assert new_index == "medic-docker-failures-20250115-120000"

    def test_generate_new_index_name_without_date(self):
        """Test new index name generation without date suffix."""
        old_index = "medic-failure-signatures"

        new_index = self.migrator.generate_new_index_name(old_index)

        # Should generate timestamp-based name
        assert new_index.startswith("medic-docker-failures-")

    def test_transform_preserves_nested_structures(self):
        """Test that nested structures in signature are preserved."""
        old_doc = {
            "fingerprint_id": "test123",
            "signature": {
                "error_type": "ValueError",
                "error_pattern": "invalid value",
                "container_pattern": "orchestrator",
                "metadata": {
                    "stack_trace": ["line1", "line2", "line3"],
                    "context": {"user_id": "123", "request_id": "abc"},
                },
            },
            "occurrence_count": 5,
        }

        new_doc = self.migrator.transform_document(old_doc)

        # Check nested structure is preserved
        assert new_doc["signature"]["metadata"]["stack_trace"] == ["line1", "line2", "line3"]
        assert new_doc["signature"]["metadata"]["context"]["user_id"] == "123"

    def test_transform_with_complex_sample_entries(self):
        """Test transformation with complex sample_log_entries."""
        old_doc = {
            "fingerprint_id": "test123",
            "sample_log_entries": [
                {
                    "timestamp": "2025-01-01T00:00:00Z",
                    "level": "ERROR",
                    "message": "Connection timeout",
                    "container_id": "abc123",
                    "container_name": "orchestrator",
                    "normalized_message": "Connection timeout",
                    "metadata": {"attempt": 1, "timeout_ms": 5000},
                }
            ],
            "occurrence_count": 1,
        }

        new_doc = self.migrator.transform_document(old_doc)

        assert len(new_doc["sample_entries"]) == 1
        sample = new_doc["sample_entries"][0]
        assert sample["level"] == "ERROR"
        assert sample["metadata"]["attempt"] == 1
        assert sample["metadata"]["timeout_ms"] == 5000

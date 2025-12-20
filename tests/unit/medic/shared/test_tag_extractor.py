"""
Unit tests for tag_extractor utilities.
"""

import pytest
from services.medic.shared.tag_extractor import extract_tags


class MockFingerprint:
    """Mock fingerprint object for testing."""

    def __init__(self, **kwargs):
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestExtractTags:
    """Tests for extract_tags function."""

    def test_extract_container_pattern_tag(self):
        """Test extraction of container_pattern tag from Docker fingerprint."""
        fingerprint = MockFingerprint(container_pattern="orchestrator")
        tags = extract_tags("test message", fingerprint)
        assert "orchestrator" in tags

    def test_extract_error_type_tag(self):
        """Test extraction of error_type tag."""
        fingerprint = MockFingerprint(error_type="AttributeError")
        tags = extract_tags("test message", fingerprint)
        assert "AttributeError" in tags

    def test_extract_tool_name_tag(self):
        """Test extraction of tool_name tag from Claude fingerprint."""
        fingerprint = MockFingerprint(tool_name="Read")
        tags = extract_tags("test message", fingerprint)
        assert "tool:Read" in tags

    def test_extract_project_tag(self):
        """Test extraction of project tag from Claude fingerprint."""
        fingerprint = MockFingerprint(project="context-studio")
        tags = extract_tags("test message", fingerprint)
        assert "project:context-studio" in tags

    def test_extract_context_tag_agent(self):
        """Test extraction of agent context tag from message."""
        fingerprint = MockFingerprint()
        tags = extract_tags("agent execution failed", fingerprint)
        assert "agent_execution" in tags

    def test_extract_context_tag_task(self):
        """Test extraction of task context tag from message."""
        fingerprint = MockFingerprint()
        tags = extract_tags("task processing error", fingerprint)
        assert "task_processing" in tags

    def test_extract_context_tag_pipeline(self):
        """Test extraction of pipeline context tag from message."""
        fingerprint = MockFingerprint()
        tags = extract_tags("pipeline stage failed", fingerprint)
        assert "pipeline" in tags

    def test_extract_multiple_context_tags(self):
        """Test extraction of multiple context tags from message."""
        fingerprint = MockFingerprint()
        tags = extract_tags("github pipeline task failed", fingerprint)
        assert "github" in tags
        assert "pipeline" in tags
        assert "task_processing" in tags

    def test_extract_timeout_tag(self):
        """Test extraction of timeout tag."""
        fingerprint = MockFingerprint()
        tags = extract_tags("operation timeout", fingerprint)
        assert "timeout" in tags

    def test_extract_permission_tag(self):
        """Test extraction of permission tag."""
        fingerprint = MockFingerprint()
        tags = extract_tags("permission denied", fingerprint)
        assert "permission" in tags

    def test_case_insensitive_keyword_matching(self):
        """Test that keyword matching is case insensitive."""
        fingerprint = MockFingerprint()
        tags = extract_tags("DOCKER container failed", fingerprint)
        assert "docker" in tags

    def test_additional_tags_included(self):
        """Test that additional tags are included."""
        fingerprint = MockFingerprint()
        additional = ["custom_tag", "test_tag"]
        tags = extract_tags("test message", fingerprint, additional_tags=additional)
        assert "custom_tag" in tags
        assert "test_tag" in tags

    def test_tags_deduplicated(self):
        """Test that duplicate tags are removed."""
        fingerprint = MockFingerprint(error_type="docker")
        tags = extract_tags("docker container failed", fingerprint)
        # "docker" appears in error_type and message, should only appear once
        assert tags.count("docker") == 1

    def test_empty_fingerprint_and_message(self):
        """Test with empty fingerprint and message."""
        fingerprint = MockFingerprint()
        tags = extract_tags("", fingerprint)
        assert isinstance(tags, list)
        # May be empty or contain default tags

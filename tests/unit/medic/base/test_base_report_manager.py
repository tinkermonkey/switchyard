"""
Unit tests for BaseReportManager.

Tests the concrete report management methods with mock file system.
"""

import pytest
import json
import tempfile
import shutil
from pathlib import Path
from datetime import datetime

from services.medic.base.base_report_manager import BaseReportManager


# Concrete implementation for testing
class TestReportManager(BaseReportManager):
    """Concrete implementation of BaseReportManager for testing."""

    def write_context(self, fingerprint_id: str, **kwargs) -> str:
        """Test implementation of write_context."""
        report_dir = self.ensure_report_dir(fingerprint_id)
        context_file = report_dir / "context.json"

        context = {
            "fingerprint_id": fingerprint_id,
            **kwargs,
        }

        with open(context_file, "w") as f:
            json.dump(context, f, indent=2)

        return str(context_file)


@pytest.fixture
def temp_dir():
    """Create a temporary directory for tests."""
    tmpdir = tempfile.mkdtemp()
    yield tmpdir
    shutil.rmtree(tmpdir)


@pytest.fixture
def docker_report_manager(temp_dir):
    """Create a Docker report manager."""
    return TestReportManager(base_dir=temp_dir, subdirectory="")


@pytest.fixture
def claude_report_manager(temp_dir):
    """Create a Claude report manager."""
    return TestReportManager(base_dir=temp_dir, subdirectory="claude")


class TestInitialization:
    """Tests for report manager initialization."""

    def test_initialization_docker(self, temp_dir):
        """Test initialization for Docker (no subdirectory)."""
        manager = TestReportManager(base_dir=temp_dir, subdirectory="")

        assert manager.base_dir == Path(temp_dir)
        assert manager.base_dir.exists()

    def test_initialization_claude(self, temp_dir):
        """Test initialization for Claude (with subdirectory)."""
        manager = TestReportManager(base_dir=temp_dir, subdirectory="claude")

        assert manager.base_dir == Path(temp_dir) / "claude"
        assert manager.base_dir.exists()


class TestDirectoryManagement:
    """Tests for directory management."""

    def test_get_report_dir(self, docker_report_manager):
        """Test getting report directory."""
        report_dir = docker_report_manager.get_report_dir("fp123")

        assert report_dir.name == "fp123"
        assert report_dir.parent == docker_report_manager.base_dir

    def test_ensure_report_dir_creates(self, docker_report_manager):
        """Test that ensure_report_dir creates directory."""
        report_dir = docker_report_manager.ensure_report_dir("fp123")

        assert report_dir.exists()
        assert report_dir.is_dir()

    def test_ensure_report_dir_idempotent(self, docker_report_manager):
        """Test that ensure_report_dir is idempotent."""
        report_dir1 = docker_report_manager.ensure_report_dir("fp123")
        report_dir2 = docker_report_manager.ensure_report_dir("fp123")

        assert report_dir1 == report_dir2
        assert report_dir1.exists()


class TestContextManagement:
    """Tests for context file management."""

    def test_write_context(self, docker_report_manager):
        """Test writing context file."""
        context_path = docker_report_manager.write_context(
            "fp123",
            test_data="value"
        )

        assert Path(context_path).exists()

        with open(context_path, "r") as f:
            data = json.load(f)
            assert data["fingerprint_id"] == "fp123"
            assert data["test_data"] == "value"

    def test_read_context_exists(self, docker_report_manager):
        """Test reading existing context file."""
        docker_report_manager.write_context("fp123", test_data="value")

        context = docker_report_manager.read_context("fp123")

        assert context is not None
        assert context["fingerprint_id"] == "fp123"
        assert context["test_data"] == "value"

    def test_read_context_not_exists(self, docker_report_manager):
        """Test reading non-existent context file."""
        context = docker_report_manager.read_context("fp123")

        assert context is None


class TestInvestigationLog:
    """Tests for investigation log management."""

    def test_get_investigation_log_path(self, docker_report_manager):
        """Test getting investigation log path."""
        log_path = docker_report_manager.get_investigation_log_path("fp123")

        assert "fp123" in log_path
        assert log_path.endswith("investigation_log.txt")
        # Should create directory
        assert Path(log_path).parent.exists()

    def test_count_log_lines_empty(self, docker_report_manager):
        """Test counting log lines when file doesn't exist."""
        count = docker_report_manager.count_log_lines("fp123")

        assert count == 0

    def test_count_log_lines(self, docker_report_manager):
        """Test counting log lines."""
        log_path = docker_report_manager.get_investigation_log_path("fp123")

        with open(log_path, "w") as f:
            f.write("line 1\n")
            f.write("line 2\n")
            f.write("line 3\n")

        count = docker_report_manager.count_log_lines("fp123")

        assert count == 3


class TestReportReading:
    """Tests for reading report files."""

    def test_read_diagnosis_exists(self, docker_report_manager):
        """Test reading existing diagnosis file."""
        report_dir = docker_report_manager.ensure_report_dir("fp123")
        diagnosis_file = report_dir / "diagnosis.md"

        with open(diagnosis_file, "w") as f:
            f.write("# Diagnosis\n\nThis is the diagnosis.")

        content = docker_report_manager.read_diagnosis("fp123")

        assert content is not None
        assert "Diagnosis" in content

    def test_read_diagnosis_not_exists(self, docker_report_manager):
        """Test reading non-existent diagnosis file."""
        content = docker_report_manager.read_diagnosis("fp123")

        assert content is None

    def test_read_fix_plan_exists(self, docker_report_manager):
        """Test reading existing fix plan file."""
        report_dir = docker_report_manager.ensure_report_dir("fp123")
        fix_plan_file = report_dir / "fix_plan.md"

        with open(fix_plan_file, "w") as f:
            f.write("# Fix Plan\n\nThis is the fix plan.")

        content = docker_report_manager.read_fix_plan("fp123")

        assert content is not None
        assert "Fix Plan" in content

    def test_read_fix_plan_not_exists(self, docker_report_manager):
        """Test reading non-existent fix plan file."""
        content = docker_report_manager.read_fix_plan("fp123")

        assert content is None

    def test_read_ignored_exists(self, docker_report_manager):
        """Test reading existing ignored file."""
        report_dir = docker_report_manager.ensure_report_dir("fp123")
        ignored_file = report_dir / "ignored.md"

        with open(ignored_file, "w") as f:
            f.write("# Ignored\n\nThis issue is ignored.")

        content = docker_report_manager.read_ignored("fp123")

        assert content is not None
        assert "Ignored" in content

    def test_read_ignored_not_exists(self, docker_report_manager):
        """Test reading non-existent ignored file."""
        content = docker_report_manager.read_ignored("fp123")

        assert content is None


class TestReportStatus:
    """Tests for report status."""

    def test_get_report_status_no_reports(self, docker_report_manager):
        """Test getting status when no reports exist."""
        status = docker_report_manager.get_report_status("fp123")

        assert status["has_context"] is False
        assert status["has_diagnosis"] is False
        assert status["has_fix_plan"] is False
        assert status["has_ignored"] is False
        assert status["has_investigation_log"] is False

    def test_get_report_status_with_reports(self, docker_report_manager):
        """Test getting status with some reports."""
        docker_report_manager.write_context("fp123", test_data="value")

        report_dir = docker_report_manager.ensure_report_dir("fp123")
        (report_dir / "diagnosis.md").write_text("diagnosis")
        (report_dir / "fix_plan.md").write_text("fix plan")

        status = docker_report_manager.get_report_status("fp123")

        assert status["has_context"] is True
        assert status["has_diagnosis"] is True
        assert status["has_fix_plan"] is True
        assert status["has_ignored"] is False
        assert status["has_investigation_log"] is False

    def test_get_report_status_includes_metadata(self, docker_report_manager):
        """Test that status includes file metadata."""
        docker_report_manager.write_context("fp123", test_data="value")

        status = docker_report_manager.get_report_status("fp123")

        assert "has_context_size" in status
        assert "has_context_modified" in status
        assert status["has_context_size"] > 0


class TestListInvestigations:
    """Tests for listing investigations."""

    def test_list_all_investigations_empty(self, docker_report_manager):
        """Test listing when no investigations exist."""
        investigations = docker_report_manager.list_all_investigations()

        assert investigations == []

    def test_list_all_investigations(self, docker_report_manager):
        """Test listing investigations."""
        docker_report_manager.write_context("sha256:fp1", data="1")
        docker_report_manager.write_context("sha256:fp2", data="2")
        docker_report_manager.write_context("sha256:fp3", data="3")

        # Create a non-fingerprint directory (should be ignored)
        (docker_report_manager.base_dir / "other").mkdir()

        investigations = docker_report_manager.list_all_investigations()

        assert len(investigations) == 3
        assert "sha256:fp1" in investigations
        assert "sha256:fp2" in investigations
        assert "sha256:fp3" in investigations
        assert "other" not in investigations

    def test_list_all_investigations_sorted(self, docker_report_manager):
        """Test that investigations are sorted."""
        docker_report_manager.write_context("sha256:fp3", data="3")
        docker_report_manager.write_context("sha256:fp1", data="1")
        docker_report_manager.write_context("sha256:fp2", data="2")

        investigations = docker_report_manager.list_all_investigations()

        assert investigations == sorted(investigations)

    def test_get_all_investigations_summary(self, docker_report_manager):
        """Test getting summary of all investigations."""
        docker_report_manager.write_context("sha256:fp1", data="1")
        docker_report_manager.write_context("sha256:fp2", data="2")

        summary = docker_report_manager.get_all_investigations_summary()

        assert len(summary) == 2
        assert all("fingerprint_id" in item for item in summary)
        assert all("has_context" in item for item in summary)


class TestAttachments:
    """Tests for attachments directory."""

    def test_get_attachments_dir(self, docker_report_manager):
        """Test getting attachments directory."""
        attachments_dir = docker_report_manager.get_attachments_dir("fp123")

        assert attachments_dir.exists()
        assert attachments_dir.is_dir()
        assert attachments_dir.name == "attachments"
        assert attachments_dir.parent.name == "fp123"


class TestCleanup:
    """Tests for cleanup methods."""

    def test_cleanup_investigation_exists(self, docker_report_manager):
        """Test cleaning up existing investigation."""
        docker_report_manager.write_context("fp123", data="test")

        result = docker_report_manager.cleanup_investigation("fp123")

        assert result is True
        assert not docker_report_manager.get_report_dir("fp123").exists()

    def test_cleanup_investigation_not_exists(self, docker_report_manager):
        """Test cleaning up non-existent investigation."""
        result = docker_report_manager.cleanup_investigation("fp123")

        assert result is False

    def test_cleanup_old_investigations(self, docker_report_manager):
        """Test cleaning up old investigations."""
        # Create an investigation
        docker_report_manager.write_context("sha256:fp1", data="1")

        # Modify the directory's mtime to be old
        report_dir = docker_report_manager.get_report_dir("sha256:fp1")
        import os
        old_time = datetime(2020, 1, 1).timestamp()
        os.utime(report_dir, (old_time, old_time))

        count = docker_report_manager.cleanup_old_investigations(days=30)

        assert count == 1
        assert not report_dir.exists()

    def test_cleanup_investigation_reports_bulk(self, docker_report_manager):
        """Test bulk cleanup of investigation reports."""
        docker_report_manager.write_context("sha256:fp1", data="1")
        docker_report_manager.write_context("sha256:fp2", data="2")
        docker_report_manager.write_context("sha256:fp3", data="3")

        count = docker_report_manager.cleanup_investigation_reports(
            ["sha256:fp1", "sha256:fp2"]
        )

        assert count == 2
        assert not docker_report_manager.get_report_dir("sha256:fp1").exists()
        assert not docker_report_manager.get_report_dir("sha256:fp2").exists()
        assert docker_report_manager.get_report_dir("sha256:fp3").exists()

    def test_cleanup_investigation_reports_empty_list(self, docker_report_manager):
        """Test bulk cleanup with empty list."""
        count = docker_report_manager.cleanup_investigation_reports([])

        assert count == 0


class TestSubdirectoryIsolation:
    """Tests that Docker and Claude managers are isolated."""

    def test_different_base_dirs(self, temp_dir):
        """Test that Docker and Claude use different base directories."""
        docker_manager = TestReportManager(base_dir=temp_dir, subdirectory="")
        claude_manager = TestReportManager(base_dir=temp_dir, subdirectory="claude")

        assert docker_manager.base_dir != claude_manager.base_dir
        assert claude_manager.base_dir == Path(temp_dir) / "claude"

    def test_isolated_operations(self, temp_dir):
        """Test that operations on different managers are isolated."""
        docker_manager = TestReportManager(base_dir=temp_dir, subdirectory="")
        claude_manager = TestReportManager(base_dir=temp_dir, subdirectory="claude")

        # Write to Docker
        docker_manager.write_context("fp123", data="docker")

        # Write to Claude
        claude_manager.write_context("fp123", data="claude")

        # Should be isolated
        docker_context = docker_manager.read_context("fp123")
        claude_context = claude_manager.read_context("fp123")

        assert docker_context["data"] == "docker"
        assert claude_context["data"] == "claude"

        # Listing should be isolated
        assert len(docker_manager.list_all_investigations()) == 1
        assert len(claude_manager.list_all_investigations()) == 1

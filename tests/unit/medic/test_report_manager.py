"""
Unit tests for Medic Report Manager
"""

import pytest
import tempfile
import shutil
from pathlib import Path
import json

from services.medic.docker import DockerDockerReportManager


@pytest.fixture
def temp_medic_dir():
    """Create temporary directory for medic reports"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def report_manager(temp_medic_dir):
    """Create report manager with temp directory"""
    return DockerReportManager(temp_medic_dir)


@pytest.fixture
def sample_fingerprint_id():
    """Sample fingerprint ID"""
    return "sha256:abc123def456"


@pytest.fixture
def sample_signature_data():
    """Sample signature data"""
    return {
        "fingerprint_id": "sha256:abc123def456",
        "signature": {
            "error_type": "KeyError",
            "error_pattern": "KeyError: '{key}'",
            "container_pattern": "orchestrator",
        },
        "severity": "ERROR",
        "occurrence_count": 15,
    }


@pytest.fixture
def sample_logs():
    """Sample log entries"""
    return [
        {"timestamp": "2025-11-28T12:00:00Z", "message": "Error 1"},
        {"timestamp": "2025-11-28T12:01:00Z", "message": "Error 2"},
    ]


class TestDockerReportManagerInit:
    """Test report manager initialization"""

    def test_init_creates_base_dir(self, temp_medic_dir):
        """Test that initialization creates base directory"""
        medic_dir = Path(temp_medic_dir) / "subdir"
        assert not medic_dir.exists()

        manager = DockerReportManager(str(medic_dir))
        assert medic_dir.exists()

    def test_init_with_existing_dir(self, temp_medic_dir):
        """Test initialization with existing directory"""
        manager = DockerReportManager(temp_medic_dir)
        assert Path(temp_medic_dir).exists()


class TestReportDirectories:
    """Test report directory management"""

    def test_get_report_dir(self, report_manager, sample_fingerprint_id):
        """Test getting report directory path"""
        report_dir = report_manager.get_report_dir(sample_fingerprint_id)
        assert sample_fingerprint_id in str(report_dir)
        assert isinstance(report_dir, Path)

    def test_ensure_report_dir_creates_directory(
        self, report_manager, sample_fingerprint_id
    ):
        """Test that ensure_report_dir creates directory"""
        report_dir = report_manager.ensure_report_dir(sample_fingerprint_id)
        assert report_dir.exists()
        assert report_dir.is_dir()

    def test_ensure_report_dir_idempotent(self, report_manager, sample_fingerprint_id):
        """Test that calling ensure_report_dir twice is safe"""
        dir1 = report_manager.ensure_report_dir(sample_fingerprint_id)
        dir2 = report_manager.ensure_report_dir(sample_fingerprint_id)
        assert dir1 == dir2
        assert dir1.exists()


class TestContextManagement:
    """Test context file management"""

    def test_write_context(
        self, report_manager, sample_fingerprint_id, sample_signature_data, sample_logs
    ):
        """Test writing context file"""
        context_file = report_manager.write_context(
            sample_fingerprint_id, sample_signature_data, sample_logs
        )

        assert Path(context_file).exists()
        assert "context.json" in context_file

        # Verify content
        with open(context_file, "r") as f:
            data = json.load(f)

        assert data["fingerprint_id"] == sample_fingerprint_id
        assert data["signature"] == sample_signature_data
        assert len(data["sample_logs"]) == 2
        assert "created_at" in data

    def test_read_context(
        self, report_manager, sample_fingerprint_id, sample_signature_data, sample_logs
    ):
        """Test reading context file"""
        # Write first
        report_manager.write_context(
            sample_fingerprint_id, sample_signature_data, sample_logs
        )

        # Read back
        context = report_manager.read_context(sample_fingerprint_id)

        assert context is not None
        assert context["fingerprint_id"] == sample_fingerprint_id
        # Signature data is stored whole, so error_type is nested
        assert context["signature"]["signature"]["error_type"] == "KeyError"

    def test_read_context_not_found(self, report_manager):
        """Test reading non-existent context"""
        context = report_manager.read_context("sha256:nonexistent")
        assert context is None


class TestReportReading:
    """Test reading report files"""

    def test_read_diagnosis(self, report_manager, sample_fingerprint_id):
        """Test reading diagnosis file"""
        # Create diagnosis file
        report_dir = report_manager.ensure_report_dir(sample_fingerprint_id)
        diagnosis_file = report_dir / "diagnosis.md"
        diagnosis_content = "# Diagnosis\n\nRoot cause identified."

        with open(diagnosis_file, "w") as f:
            f.write(diagnosis_content)

        # Read it
        content = report_manager.read_diagnosis(sample_fingerprint_id)
        assert content == diagnosis_content

    def test_read_diagnosis_not_found(self, report_manager):
        """Test reading non-existent diagnosis"""
        content = report_manager.read_diagnosis("sha256:nonexistent")
        assert content is None

    def test_read_fix_plan(self, report_manager, sample_fingerprint_id):
        """Test reading fix plan file"""
        report_dir = report_manager.ensure_report_dir(sample_fingerprint_id)
        fix_plan_file = report_dir / "fix_plan.md"
        fix_plan_content = "# Fix Plan\n\n1. Step 1\n2. Step 2"

        with open(fix_plan_file, "w") as f:
            f.write(fix_plan_content)

        content = report_manager.read_fix_plan(sample_fingerprint_id)
        assert content == fix_plan_content

    def test_read_ignored(self, report_manager, sample_fingerprint_id):
        """Test reading ignored report"""
        report_dir = report_manager.ensure_report_dir(sample_fingerprint_id)
        ignored_file = report_dir / "ignored.md"
        ignored_content = "# Ignored\n\nNot actionable."

        with open(ignored_file, "w") as f:
            f.write(ignored_content)

        content = report_manager.read_ignored(sample_fingerprint_id)
        assert content == ignored_content


class TestInvestigationLog:
    """Test investigation log management"""

    def test_get_investigation_log_path(self, report_manager, sample_fingerprint_id):
        """Test getting investigation log path"""
        log_path = report_manager.get_investigation_log_path(sample_fingerprint_id)
        assert "investigation_log.txt" in log_path
        assert sample_fingerprint_id in log_path

    def test_count_log_lines_empty(self, report_manager, sample_fingerprint_id):
        """Test counting lines in non-existent log"""
        count = report_manager.count_log_lines(sample_fingerprint_id)
        assert count == 0

    def test_count_log_lines(self, report_manager, sample_fingerprint_id):
        """Test counting lines in log file"""
        # Create log file with lines
        log_path = report_manager.get_investigation_log_path(sample_fingerprint_id)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)

        with open(log_path, "w") as f:
            f.write("Line 1\n")
            f.write("Line 2\n")
            f.write("Line 3\n")

        count = report_manager.count_log_lines(sample_fingerprint_id)
        assert count == 3


class TestReportStatus:
    """Test report status checking"""

    def test_get_report_status_empty(self, report_manager, sample_fingerprint_id):
        """Test status when no reports exist"""
        status = report_manager.get_report_status(sample_fingerprint_id)

        assert status["has_context"] is False
        assert status["has_diagnosis"] is False
        assert status["has_fix_plan"] is False
        assert status["has_ignored"] is False
        assert status["has_investigation_log"] is False

    def test_get_report_status_with_files(self, report_manager, sample_fingerprint_id):
        """Test status when reports exist"""
        report_dir = report_manager.ensure_report_dir(sample_fingerprint_id)

        # Create some files
        (report_dir / "context.json").write_text("{}")
        (report_dir / "diagnosis.md").write_text("# Diagnosis")

        status = report_manager.get_report_status(sample_fingerprint_id)

        assert status["has_context"] is True
        assert status["has_diagnosis"] is True
        assert status["has_fix_plan"] is False
        assert status["has_ignored"] is False

        # Check metadata
        assert "has_context_size" in status
        assert "has_diagnosis_size" in status
        assert status["has_context_size"] > 0


class TestInvestigationListing:
    """Test listing investigations"""

    def test_list_all_investigations_empty(self, report_manager):
        """Test listing when no investigations exist"""
        investigations = report_manager.list_all_investigations()
        assert investigations == []

    def test_list_all_investigations(self, report_manager):
        """Test listing multiple investigations"""
        fp1 = "sha256:abc123"
        fp2 = "sha256:def456"

        report_manager.ensure_report_dir(fp1)
        report_manager.ensure_report_dir(fp2)

        investigations = report_manager.list_all_investigations()
        assert len(investigations) == 2
        assert fp1 in investigations
        assert fp2 in investigations

    def test_get_all_investigations_summary(self, report_manager):
        """Test getting investigation summary"""
        fp1 = "sha256:abc123"

        # Create investigation with some reports
        report_dir = report_manager.ensure_report_dir(fp1)
        (report_dir / "diagnosis.md").write_text("# Diagnosis")
        (report_dir / "fix_plan.md").write_text("# Fix Plan")

        summaries = report_manager.get_all_investigations_summary()

        assert len(summaries) == 1
        summary = summaries[0]

        assert summary["fingerprint_id"] == fp1
        assert summary["has_diagnosis"] is True
        assert summary["has_fix_plan"] is True
        assert summary["has_ignored"] is False


class TestAttachments:
    """Test attachments directory"""

    def test_get_attachments_dir(self, report_manager, sample_fingerprint_id):
        """Test getting attachments directory"""
        attachments_dir = report_manager.get_attachments_dir(sample_fingerprint_id)

        assert attachments_dir.exists()
        assert attachments_dir.is_dir()
        assert "attachments" in str(attachments_dir)

    def test_get_attachments_dir_creates_if_missing(
        self, report_manager, sample_fingerprint_id
    ):
        """Test that attachments dir is created if it doesn't exist"""
        report_dir = report_manager.get_report_dir(sample_fingerprint_id)
        attachments_dir = report_dir / "attachments"

        assert not attachments_dir.exists()

        result = report_manager.get_attachments_dir(sample_fingerprint_id)

        assert result.exists()
        assert result == attachments_dir


class TestCleanup:
    """Test investigation cleanup"""

    def test_cleanup_investigation(self, report_manager, sample_fingerprint_id):
        """Test cleaning up investigation directory"""
        # Create investigation with files
        report_dir = report_manager.ensure_report_dir(sample_fingerprint_id)
        (report_dir / "diagnosis.md").write_text("# Diagnosis")
        (report_dir / "fix_plan.md").write_text("# Fix Plan")

        assert report_dir.exists()

        # Cleanup
        result = report_manager.cleanup_investigation(sample_fingerprint_id)

        assert result is True
        assert not report_dir.exists()

    def test_cleanup_nonexistent(self, report_manager):
        """Test cleanup of non-existent investigation"""
        result = report_manager.cleanup_investigation("sha256:nonexistent")
        assert result is False


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_count_log_lines_with_error(self, report_manager, sample_fingerprint_id):
        """Test counting lines when file is corrupted"""
        # Create directory but not file
        report_manager.ensure_report_dir(sample_fingerprint_id)

        # Should return 0 without crashing
        count = report_manager.count_log_lines(sample_fingerprint_id)
        assert count == 0

    def test_write_context_with_special_characters(
        self, report_manager, sample_fingerprint_id
    ):
        """Test writing context with special characters"""
        signature_data = {
            "message": "Error with 'quotes' and \"double quotes\" and unicode: 文字"
        }
        sample_logs = [{"message": "Special chars: \n\t\r"}]

        context_file = report_manager.write_context(
            sample_fingerprint_id, signature_data, sample_logs
        )

        # Should write successfully
        assert Path(context_file).exists()

        # Should be readable
        context = report_manager.read_context(sample_fingerprint_id)
        assert context is not None

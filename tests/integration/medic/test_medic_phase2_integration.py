"""
Integration tests for Medic Phase 2: Investigation Agent

Tests the complete investigation workflow from trigger to completion.
"""

import pytest
import tempfile
import shutil
import json
import time
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone

from services.medic.docker import DockerDockerReportManager
from services.medic.docker import DockerDockerInvestigationQueue
from services.medic.investigation_agent_runner import InvestigationAgentRunner

from services.medic.docker import DockerDockerInvestigationOrchestrator


@pytest.fixture
def temp_workspace():
    """Create temporary workspace"""
    temp_dir = tempfile.mkdtemp()
    workspace = Path(temp_dir) / "clauditoreum"
    workspace.mkdir()

    # Create investigator instructions
    medic_dir = workspace / "services" / "medic"
    medic_dir.mkdir(parents=True)
    instructions_file = medic_dir / "investigator_instructions.md"
    instructions_file.write_text("# Test Instructions")

    yield str(workspace)
    shutil.rmtree(temp_dir)


@pytest.fixture
def temp_medic_dir():
    """Create temporary medic directory"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def mock_redis():
    """Mock Redis client for testing"""
    redis_mock = Mock()
    redis_data = {}  # Simulate Redis data store

    def mock_set(key, value, **kwargs):
        """Set with support for NX (not exists) flag"""
        nx = kwargs.get('nx', False)
        if nx and key in redis_data:
            # NX flag: only set if key doesn't exist
            return False
        redis_data[key] = value
        return True

    def mock_get(key):
        value = redis_data.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            return value.encode('utf-8')
        return value

    def mock_delete(*keys):
        """Delete one or more keys"""
        count = 0
        for key in keys:
            if key in redis_data:
                del redis_data[key]
                count += 1
        return count

    def mock_blpop(key, timeout):
        # Simulate empty queue for tests
        return None

    def mock_sadd(key, value):
        if key not in redis_data:
            redis_data[key] = set()
        redis_data[key].add(value)
        return 1

    def mock_srem(key, value):
        if key in redis_data and isinstance(redis_data[key], set):
            redis_data[key].discard(value)
        return 1

    def mock_smembers(key):
        if key not in redis_data:
            return set()
        return redis_data[key]

    redis_mock.set = mock_set
    redis_mock.get = mock_get
    redis_mock.delete = mock_delete
    redis_mock.blpop = mock_blpop
    redis_mock.lpush = Mock(return_value=1)
    redis_mock.rpush = Mock(return_value=1)
    redis_mock.llen = Mock(return_value=0)
    redis_mock.sadd = mock_sadd
    redis_mock.srem = mock_srem
    redis_mock.smembers = mock_smembers

    return redis_mock


@pytest.fixture
def report_manager(temp_medic_dir):
    """Create report manager"""
    return DockerReportManager(temp_medic_dir)


@pytest.fixture
def investigation_queue(mock_redis):
    """Create investigation queue"""
    return DockerInvestigationQueue(mock_redis)


@pytest.fixture
def agent_runner(temp_workspace):
    """Create agent runner"""
    return InvestigationAgentRunner(temp_workspace)


@pytest.fixture
def sample_fingerprint_id():
    """Sample fingerprint ID"""
    return "sha256:integration_test_123"


@pytest.fixture
def sample_signature_data():
    """Sample signature data"""
    return {
        "fingerprint_id": "sha256:integration_test_123",
        "signature": {
            "error_type": "KeyError",
            "error_pattern": "KeyError: 'database_url'",
            "container_pattern": "orchestrator",
        },
        "severity": "ERROR",
        "occurrence_count": 15,
        "first_seen": datetime.now(timezone.utc).isoformat(),
        "last_seen": datetime.now(timezone.utc).isoformat(),
    }


@pytest.fixture
def sample_logs():
    """Sample log entries"""
    return [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "container": "orchestrator",
            "message": "KeyError: 'database_url'",
            "level": "ERROR",
        },
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "container": "orchestrator",
            "message": "Failed to initialize database connection",
            "level": "ERROR",
        },
    ]


class TestReportWorkflow:
    """Test complete report workflow"""

    def test_context_to_reports_workflow(
        self, report_manager, sample_fingerprint_id, sample_signature_data, sample_logs
    ):
        """Test complete workflow from context creation to report reading"""
        # Write context
        context_file = report_manager.write_context(
            sample_fingerprint_id, sample_signature_data, sample_logs
        )
        assert Path(context_file).exists()

        # Read context back
        context = report_manager.read_context(sample_fingerprint_id)
        assert context is not None
        assert context["fingerprint_id"] == sample_fingerprint_id

        # Simulate agent creating reports
        report_dir = report_manager.get_report_dir(sample_fingerprint_id)

        # Create diagnosis
        diagnosis_file = report_dir / "diagnosis.md"
        diagnosis_content = """# Root Cause Diagnosis

## Error Pattern
KeyError: 'database_url'

## Root Cause
Missing DATABASE_URL environment variable in orchestrator service.
"""
        diagnosis_file.write_text(diagnosis_content)

        # Create fix plan
        fix_plan_file = report_dir / "fix_plan.md"
        fix_plan_content = """# Fix Plan

## Steps
1. Add DATABASE_URL to docker-compose.yml
2. Update .env.example with DATABASE_URL
3. Document in README.md
"""
        fix_plan_file.write_text(fix_plan_content)

        # Read reports
        diagnosis = report_manager.read_diagnosis(sample_fingerprint_id)
        assert diagnosis == diagnosis_content

        fix_plan = report_manager.read_fix_plan(sample_fingerprint_id)
        assert fix_plan == fix_plan_content

        # Check status
        status = report_manager.get_report_status(sample_fingerprint_id)
        assert status["has_context"] is True
        assert status["has_diagnosis"] is True
        assert status["has_fix_plan"] is True
        assert status["has_ignored"] is False


class TestQueueWorkflow:
    """Test queue workflow"""

    def test_enqueue_process_complete_workflow(
        self, investigation_queue, sample_fingerprint_id
    ):
        """Test complete queue workflow"""
        # Enqueue
        result = investigation_queue.enqueue(sample_fingerprint_id)
        assert result is True

        # Check status
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_QUEUED

        # Acquire lock
        locked = investigation_queue.acquire_lock(sample_fingerprint_id)
        assert locked is True

        # Mark started
        investigation_queue.mark_started(sample_fingerprint_id)
        investigation_queue.set_pid(sample_fingerprint_id, 12345)

        # Update progress
        investigation_queue.update_heartbeat(sample_fingerprint_id)
        investigation_queue.set_output_lines(sample_fingerprint_id, 100)

        # Get info
        info = investigation_queue.get_investigation_info(sample_fingerprint_id)
        assert info["status"] == DockerInvestigationQueue.STATUS_IN_PROGRESS
        assert info["pid"] == 12345
        assert info["output_lines"] == 100

        # Mark completed
        investigation_queue.mark_completed(
            sample_fingerprint_id, DockerInvestigationQueue.RESULT_SUCCESS
        )

        # Verify completion
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_COMPLETED


class TestRecoveryWorkflow:
    """Test recovery workflow"""

    def test_recovery_with_completed_reports(
        self, investigation_queue, agent_runner, report_manager, sample_fingerprint_id, sample_signature_data, sample_logs
    ):
        """Test recovery when investigation completed but process died"""
        # Setup: investigation was running
        investigation_queue.enqueue(sample_fingerprint_id)
        investigation_queue.acquire_lock(sample_fingerprint_id)
        investigation_queue.mark_started(sample_fingerprint_id)
        investigation_queue.set_pid(sample_fingerprint_id, 99999)  # Non-existent PID

        # Create completed reports
        report_manager.write_context(sample_fingerprint_id, sample_signature_data, sample_logs)
        report_dir = report_manager.get_report_dir(sample_fingerprint_id)
        (report_dir / "diagnosis.md").write_text("# Diagnosis")
        (report_dir / "fix_plan.md").write_text("# Fix Plan")

        # Create recovery manager
        recovery = InvestigationRecovery(investigation_queue, agent_runner, report_manager)

        # Recover
        result = recovery.recover_investigation(sample_fingerprint_id)

        # Should mark as completed
        assert result == "completed"
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_COMPLETED


class TestEndToEndWorkflow:
    """Test end-to-end investigation workflow (mocked)"""

    @pytest.mark.asyncio
    async def test_complete_investigation_workflow(
        self, investigation_queue, agent_runner, report_manager,
        sample_fingerprint_id, sample_signature_data, sample_logs
    ):
        """Test complete workflow from enqueue to completion (without orchestrator)"""
        # 1. Enqueue investigation
        result = investigation_queue.enqueue(sample_fingerprint_id, priority="high")
        assert result is True

        # 2. Verify queued
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_QUEUED

        # 3. Acquire lock and start
        locked = investigation_queue.acquire_lock(sample_fingerprint_id)
        assert locked is True

        # Write context
        context_file = report_manager.write_context(
            sample_fingerprint_id, sample_signature_data, sample_logs
        )
        assert Path(context_file).exists()

        # Mark started
        investigation_queue.mark_started(sample_fingerprint_id)
        investigation_queue.set_pid(sample_fingerprint_id, 12345)

        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_IN_PROGRESS

        # 4. Simulate agent creating reports
        report_dir = report_manager.get_report_dir(sample_fingerprint_id)
        (report_dir / "diagnosis.md").write_text("# Root Cause\n\nFailure analysis")
        (report_dir / "fix_plan.md").write_text("# Fix Steps\n\n1. Fix it")

        # Create investigation log
        log_file = Path(report_manager.get_investigation_log_path(sample_fingerprint_id))
        log_file.parent.mkdir(parents=True, exist_ok=True)
        log_file.write_text("Investigation log line 1\nLine 2\nLine 3\n")

        # Update heartbeat
        investigation_queue.update_heartbeat(sample_fingerprint_id)
        investigation_queue.set_output_lines(sample_fingerprint_id, 3)

        # 5. Mark completed
        investigation_queue.mark_completed(
            sample_fingerprint_id, DockerInvestigationQueue.RESULT_SUCCESS
        )

        # 6. Verify completion
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_COMPLETED

        # 7. Verify reports exist
        assert report_manager.read_diagnosis(sample_fingerprint_id) is not None
        assert report_manager.read_fix_plan(sample_fingerprint_id) is not None

        # 8. Verify investigation info
        info = investigation_queue.get_investigation_info(sample_fingerprint_id)
        assert info["pid"] == 12345
        assert info["output_lines"] == 3


class TestMultipleInvestigations:
    """Test handling multiple concurrent investigations"""

    def test_multiple_enqueued_investigations(
        self, investigation_queue
    ):
        """Test enqueueing multiple investigations"""
        fingerprints = [f"sha256:test{i}" for i in range(5)]

        # Enqueue all
        for fp_id in fingerprints:
            result = investigation_queue.enqueue(fp_id)
            assert result is True

        # All should be queued
        for fp_id in fingerprints:
            status = investigation_queue.get_status(fp_id)
            assert status == DockerInvestigationQueue.STATUS_QUEUED

    def test_concurrent_lock_acquisition(
        self, investigation_queue
    ):
        """Test that only one process can acquire lock at a time"""
        fingerprint_ids = [f"sha256:concurrent_{i}" for i in range(3)]

        # Each should be able to acquire its own lock
        for fp_id in fingerprint_ids:
            locked = investigation_queue.acquire_lock(fp_id)
            assert locked is True

        # Second attempt to lock same ID should fail
        for fp_id in fingerprint_ids:
            locked = investigation_queue.acquire_lock(fp_id)
            assert locked is False


class TestFailureScenarios:
    """Test various failure scenarios"""

    def test_investigation_marked_failed(
        self, investigation_queue, sample_fingerprint_id
    ):
        """Test marking investigation as failed"""
        # Start investigation
        investigation_queue.enqueue(sample_fingerprint_id)
        investigation_queue.acquire_lock(sample_fingerprint_id)
        investigation_queue.mark_started(sample_fingerprint_id)

        # Mark as failed
        investigation_queue.mark_completed(
            sample_fingerprint_id,
            DockerInvestigationQueue.RESULT_FAILED,
            error_message="Process crashed"
        )

        # Verify status - RESULT_FAILED sets STATUS_FAILED
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_FAILED

        info = investigation_queue.get_investigation_info(sample_fingerprint_id)
        assert info.get("result") == DockerInvestigationQueue.RESULT_FAILED

    def test_lock_contention(self, investigation_queue, sample_fingerprint_id):
        """Test lock contention between processes"""
        # First process acquires lock
        locked1 = investigation_queue.acquire_lock(sample_fingerprint_id)
        assert locked1 is True

        # Second process tries to acquire same lock
        locked2 = investigation_queue.acquire_lock(sample_fingerprint_id)
        assert locked2 is False

        # First process releases
        investigation_queue.release_lock(sample_fingerprint_id)

        # Second process can now acquire
        locked3 = investigation_queue.acquire_lock(sample_fingerprint_id)
        assert locked3 is True


class TestCleanup:
    """Test cleanup operations"""

    def test_cleanup_old_investigations(
        self, investigation_queue, report_manager, sample_fingerprint_id, sample_signature_data, sample_logs
    ):
        """Test cleaning up old investigations"""
        # Create investigation with reports
        report_manager.write_context(sample_fingerprint_id, sample_signature_data, sample_logs)
        report_dir = report_manager.get_report_dir(sample_fingerprint_id)
        (report_dir / "diagnosis.md").write_text("# Diagnosis")

        # Mark as completed
        investigation_queue.enqueue(sample_fingerprint_id)
        investigation_queue.mark_completed(sample_fingerprint_id, DockerInvestigationQueue.RESULT_SUCCESS)

        # Cleanup
        investigation_queue.cleanup_investigation(sample_fingerprint_id)
        report_manager.cleanup_investigation(sample_fingerprint_id)

        # Verify cleanup
        assert not report_dir.exists()
        status = investigation_queue.get_status(sample_fingerprint_id)
        assert status is None  # No Redis data left

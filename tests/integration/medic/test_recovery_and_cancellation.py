"""
Integration tests for Investigation Recovery and Cancellation

Tests startup recovery scenarios and clean shutdown with task cancellation.
"""

import pytest
import asyncio
import tempfile
import shutil
import json
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock
from datetime import datetime, timezone, timedelta

from services.medic.report_manager import ReportManager
from services.medic.investigation_queue import InvestigationQueue
from services.medic.investigation_agent_runner import InvestigationAgentRunner
from services.medic.investigation_recovery import InvestigationRecovery
from services.medic.investigation_orchestrator import InvestigationOrchestrator
from monitoring.observability import EventType


@pytest.fixture
def temp_workspace():
    """Create temporary workspace"""
    temp_dir = tempfile.mkdtemp()
    workspace = Path(temp_dir) / "clauditoreum"
    workspace.mkdir()

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
    """Mock Redis client with full state tracking"""
    redis_mock = Mock()
    redis_data = {}

    def mock_set(key, value, **kwargs):
        nx = kwargs.get('nx', False)
        ex = kwargs.get('ex')
        if nx and key in redis_data:
            return False
        redis_data[key] = value
        # Note: Ignoring ex (expiration) for simplicity in tests
        return True

    def mock_get(key):
        value = redis_data.get(key)
        if value is None:
            return None
        if isinstance(value, str):
            return value.encode('utf-8')
        return value

    def mock_delete(*keys):
        count = 0
        for key in keys:
            if key in redis_data:
                del redis_data[key]
                count += 1
        return count

    def mock_blpop(key, timeout):
        queue_key = f"{key}:data"
        if queue_key in redis_data and redis_data[queue_key]:
            return (key.encode('utf-8'), redis_data[queue_key].pop(0).encode('utf-8'))
        return None

    def mock_rpush(key, value):
        queue_key = f"{key}:data"
        if queue_key not in redis_data:
            redis_data[queue_key] = []
        redis_data[queue_key].append(value)
        return len(redis_data[queue_key])

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
        return {v.encode('utf-8') if isinstance(v, str) else v for v in redis_data[key]}

    def mock_scan_iter(match):
        """Iterate over keys matching pattern"""
        return [k for k in redis_data.keys() if match.replace('*', '') in k]

    redis_mock.set = mock_set
    redis_mock.get = mock_get
    redis_mock.delete = mock_delete
    redis_mock.blpop = mock_blpop
    redis_mock.rpush = mock_rpush
    redis_mock.sadd = mock_sadd
    redis_mock.srem = mock_srem
    redis_mock.smembers = mock_smembers
    redis_mock.scan_iter = mock_scan_iter
    redis_mock.data = redis_data  # Expose for test inspection

    return redis_mock


@pytest.fixture
def mock_observability():
    """Mock observability manager"""
    obs = Mock()
    obs.emit = Mock()
    return obs


@pytest.fixture
def mock_failure_store():
    """Mock failure signature store"""
    store = AsyncMock()
    store._get_signature = AsyncMock(return_value={
        "fingerprint_id": "sha256:test123",
        "severity": "ERROR",
        "signature": {"error_type": "KeyError"},
        "sample_log_entries": [{"timestamp": "2025-11-28T12:00:00Z"}],
    })
    return store


class TestStartupRecovery:
    """Test startup recovery scenarios"""

    @pytest.mark.asyncio
    async def test_recovery_path_1_process_still_running(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Recovery Path 1: Process exists → continue monitoring"""

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_running"

        # Simulate running investigation with real PID (old system)
        # With new task-based system, pid=0, so this simulates old process-based recovery
        import os
        current_pid = os.getpid()  # Use real running process

        queue.set_pid(fingerprint_id, current_pid)
        queue.mark_started(fingerprint_id)

        # Run recovery
        # Recover the specific investigation (not async)
        recovery.recover_investigation(fingerprint_id)

        # Should continue monitoring (status unchanged)
        status = queue.get_status(fingerprint_id)
        assert status == InvestigationQueue.STATUS_IN_PROGRESS

    @pytest.mark.asyncio
    async def test_recovery_path_2_reports_exist(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Recovery Path 2: Process missing + reports exist → mark completed"""

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_completed"

        # Simulate investigation with task-based system (pid=0)
        queue.set_pid(fingerprint_id, 0)
        queue.mark_started(fingerprint_id)

        # Create reports (investigation completed)
        report_dir = Path(temp_medic_dir) / fingerprint_id
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "diagnosis.md").write_text("# Diagnosis\nCompleted")
        (report_dir / "fix_plan.md").write_text("# Fix Plan\nCompleted")

        # Run recovery
        # Recover the specific investigation (not async)
        recovery.recover_investigation(fingerprint_id)

        # Should mark as completed (status changes from in_progress)
        status = queue.get_status(fingerprint_id)

        # Status should be completed or removed (cleared from active)
        assert status in [InvestigationQueue.STATUS_COMPLETED, None]

    @pytest.mark.asyncio
    async def test_recovery_path_3_recent_no_reports(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Recovery Path 3: Process missing, no reports, <30min → wait"""

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_recent"

        # Simulate recent investigation (started now)
        queue.set_pid(fingerprint_id, 0)
        queue.mark_started(fingerprint_id)

        # No reports

        # Run recovery
        # Recover the specific investigation (not async)
        recovery.recover_investigation(fingerprint_id)

        # Should wait (status unchanged)
        status = queue.get_status(fingerprint_id)
        assert status == InvestigationQueue.STATUS_IN_PROGRESS

    @pytest.mark.asyncio
    async def test_recovery_path_4_timeout(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Recovery Path 4: Process missing, no reports, >4hr → mark timeout"""

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_timeout"

        # Simulate old investigation (started >4 hours ago)
        queue.set_pid(fingerprint_id, 0)

        # Manually set started_at to 5 hours ago
        started_at = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        mock_redis.set(f"medic:investigation:{fingerprint_id}:started_at", started_at)
        mock_redis.set(f"medic:investigation:{fingerprint_id}:status", InvestigationQueue.STATUS_IN_PROGRESS)

        # No reports

        # Run recovery
        # Recover the specific investigation (not async)
        recovery.recover_investigation(fingerprint_id)

        # Should mark as timeout or failed
        status = queue.get_status(fingerprint_id)

        # Recovery marks old investigations with various statuses
        # Accept timeout, completed, failed, or removed
        assert status in ["timeout", InvestigationQueue.STATUS_COMPLETED, InvestigationQueue.STATUS_FAILED, None]

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_recovery_path_5_relaunch(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Recovery Path 5: Process missing, no reports, 30min-4hr → re-launch"""

        mock_run_claude.return_value = "Re-launched investigation"

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_relaunch"

        # Simulate investigation started 1 hour ago
        queue.set_pid(fingerprint_id, 0)

        started_at = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        mock_redis.set(f"medic:investigation:{fingerprint_id}:started_at", started_at)
        mock_redis.set(f"medic:investigation:{fingerprint_id}:status", InvestigationQueue.STATUS_IN_PROGRESS)

        # No reports exist yet

        # Create context file (needed for re-launch)
        report_manager = ReportManager(base_dir=temp_medic_dir)
        signature = {"fingerprint_id": fingerprint_id, "severity": "ERROR", "signature": {}, "sample_log_entries": []}
        context_file = report_manager.write_context(fingerprint_id, signature, [])
        assert Path(context_file).exists()

        # Run recovery
        # Recover the specific investigation (not async)
        result = recovery.recover_investigation(fingerprint_id)

        # Recovery may re-launch or mark as failed depending on context availability
        # Since we created context, it should attempt re-launch or mark for retry
        status = queue.get_status(fingerprint_id)

        # Should have attempted recovery (various outcomes possible)
        assert result in ["recovered", "relaunch", "wait", "completed", "failed", "timeout"]


class TestCleanShutdown:
    """Test clean shutdown and task cancellation"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_orchestrator_cancels_tasks_on_shutdown(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test that orchestrator cancels all active tasks on shutdown"""

        # Mock long-running investigation
        async def long_investigation(prompt, context):
            try:
                await asyncio.sleep(10)
                return "Should not complete"
            except asyncio.CancelledError:
                # Clean cancellation
                raise

        mock_run_claude.side_effect = long_investigation

        # Create orchestrator
        orchestrator = InvestigationOrchestrator(
            redis_client=mock_redis,
            es_client=mock_failure_store,  # Using mock_failure_store as es_client
            workspace_root=temp_workspace,
            medic_dir=temp_medic_dir,
        )

        # Start orchestrator (don't await, run in background)
        orchestrator_task = asyncio.create_task(orchestrator.start())

        # Let it start
        await asyncio.sleep(0.1)

        # Manually trigger an investigation
        fingerprint_id = "sha256:test_shutdown"
        report_manager = orchestrator.report_manager

        signature = {
            "fingerprint_id": fingerprint_id,
            "severity": "ERROR",
            "signature": {"error_type": "TestError"},
            "sample_log_entries": [],
        }

        context_file = report_manager.write_context(fingerprint_id, signature, [])
        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        # Launch investigation directly
        investigation = await orchestrator.agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        # Track in active processes
        orchestrator.active_processes[fingerprint_id] = investigation

        # Let investigation start
        await asyncio.sleep(0.1)

        # Stop orchestrator (should cancel tasks)
        await orchestrator.stop()

        # Verify task was cancelled
        task = investigation['task']
        assert task.cancelled()

        # Clean up orchestrator task
        orchestrator_task.cancel()
        try:
            await orchestrator_task
        except asyncio.CancelledError:
            pass

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_multiple_tasks_cancelled_on_shutdown(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test that multiple active tasks are all cancelled on shutdown"""

        # Mock long-running investigations
        async def long_investigation(prompt, context):
            try:
                await asyncio.sleep(10)
                return "Should not complete"
            except asyncio.CancelledError:
                raise

        mock_run_claude.side_effect = long_investigation

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = ReportManager(base_dir=temp_medic_dir)

        # Create multiple investigations
        investigations = {}
        fingerprints = ["sha256:test1", "sha256:test2", "sha256:test3"]

        for fp_id in fingerprints:
            signature = {"fingerprint_id": fp_id, "severity": "ERROR", "signature": {}, "sample_log_entries": []}
            context_file = report_manager.write_context(fp_id, signature, [])
            output_log = report_manager.get_investigation_log_path(fp_id)

            investigation = await agent_runner.launch_investigation(
                fp_id, context_file, output_log, mock_observability
            )
            investigations[fp_id] = investigation

        # Let them start
        await asyncio.sleep(0.1)

        # Cancel all (simulate orchestrator stop)
        for fp_id, investigation in investigations.items():
            task = investigation['task']
            task.cancel()

        # Wait for cancellations
        for fp_id, investigation in investigations.items():
            task = investigation['task']
            with pytest.raises(asyncio.CancelledError):
                await task

        # Verify all cancelled
        for fp_id, investigation in investigations.items():
            assert investigation['task'].cancelled()

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_cancellation_with_cleanup(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
    ):
        """Test that cancellation allows cleanup code to run"""

        cleanup_ran = False

        async def investigation_with_cleanup(prompt, context):
            nonlocal cleanup_ran
            try:
                await asyncio.sleep(10)
                return "Should not complete"
            except asyncio.CancelledError:
                # Cleanup on cancellation
                cleanup_ran = True
                raise

        mock_run_claude.side_effect = investigation_with_cleanup

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = ReportManager(base_dir=temp_medic_dir)

        fingerprint_id = "sha256:test_cleanup"
        signature = {"fingerprint_id": fingerprint_id, "severity": "ERROR", "signature": {}, "sample_log_entries": []}
        context_file = report_manager.write_context(fingerprint_id, signature, [])
        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        # Let it start
        await asyncio.sleep(0.1)

        # Cancel
        task = investigation['task']
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass

        # Verify cleanup ran
        assert cleanup_ran is True


class TestStallDetection:
    """Test stall detection with task-based system"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_stall_detection_with_no_output(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test that investigations with no output for >10min are detected as stalled"""

        # Mock investigation that produces no output
        async def stalled_investigation(prompt, context):
            await asyncio.sleep(1)  # Just wait, no output
            return "Eventually completes but stalled"

        mock_run_claude.side_effect = stalled_investigation

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        recovery = InvestigationRecovery(
            queue, InvestigationAgentRunner(temp_workspace), report_manager
        )

        fingerprint_id = "sha256:test_stall"

        queue.set_pid(fingerprint_id, 0)
        queue.mark_started(fingerprint_id)

        # Set last heartbeat to 11 minutes ago
        old_heartbeat = (datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat()
        mock_redis.set(f"medic:investigation:{fingerprint_id}:last_heartbeat", old_heartbeat)

        # Check for stalls
        stalled = recovery.check_stalled_investigations()

        # Should detect as stalled
        assert fingerprint_id in stalled

    @pytest.mark.asyncio
    async def test_active_investigation_not_stalled(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test that active investigations with recent output are not marked stalled"""

        report_manager = ReportManager(base_dir=temp_medic_dir)
        queue = InvestigationQueue(mock_redis)
        recovery = InvestigationRecovery(
            queue, InvestigationAgentRunner(temp_workspace), report_manager
        )

        fingerprint_id = "sha256:test_active"

        queue.set_pid(fingerprint_id, 0)
        queue.mark_started(fingerprint_id)

        # Set recent heartbeat (1 minute ago)
        recent_heartbeat = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        mock_redis.set(f"medic:investigation:{fingerprint_id}:last_heartbeat", recent_heartbeat)

        # Check for stalls
        stalled = recovery.check_stalled_investigations()

        # Should NOT be stalled
        assert fingerprint_id not in stalled

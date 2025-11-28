"""
Unit tests for Medic Investigation Orchestrator
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime, timezone
import asyncio

from services.medic.investigation_orchestrator import InvestigationOrchestrator
from services.medic.investigation_queue import InvestigationQueue


@pytest.fixture
def mock_redis():
    """Mock Redis client"""
    return Mock()


@pytest.fixture
def mock_es():
    """Mock Elasticsearch client"""
    return Mock()


@pytest.fixture
def orchestrator(mock_redis, mock_es, tmp_path):
    """Create orchestrator with mocks"""
    with patch('services.medic.investigation_orchestrator.get_observability_manager') as mock_obs:
        mock_obs.return_value = Mock()
        return InvestigationOrchestrator(
            redis_client=mock_redis,
            es_client=mock_es,
            workspace_root=str(tmp_path / "workspace"),
            medic_dir=str(tmp_path / "medic"),
        )


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
        },
        "severity": "ERROR",
        "occurrence_count": 15,
    }


class TestOrchestratorInit:
    """Test orchestrator initialization"""

    def test_init(self, mock_redis, mock_es, tmp_path):
        """Test orchestrator initialization"""
        with patch('services.medic.investigation_orchestrator.get_observability_manager') as mock_obs:
            mock_obs.return_value = Mock()
            orchestrator = InvestigationOrchestrator(
                redis_client=mock_redis,
                es_client=mock_es,
                workspace_root=str(tmp_path / "workspace"),
                medic_dir=str(tmp_path / "medic"),
            )

            assert orchestrator.queue is not None
            assert orchestrator.agent_runner is not None
            assert orchestrator.report_manager is not None
            assert orchestrator.recovery is not None
            assert orchestrator.failure_store is not None
            assert orchestrator.observability is not None
            assert orchestrator.running is False
            assert orchestrator.active_processes == {}


class TestStartupRecovery:
    """Test startup recovery"""

    @pytest.mark.asyncio
    async def test_startup_recovery_called(self, orchestrator):
        """Test that startup recovery is called on start"""
        with patch.object(orchestrator.recovery, 'recover_all') as mock_recover:
            mock_recover.return_value = {"recovered": 0, "completed": 0}
            with patch.object(orchestrator.agent_runner, 'get_claude_version') as mock_version:
                mock_version.return_value = "Claude Code CLI v1.0.0"

                # Start and immediately stop
                orchestrator.running = False

                with patch.object(orchestrator, '_queue_processor', new_callable=AsyncMock):
                    with patch.object(orchestrator, '_heartbeat_monitor', new_callable=AsyncMock):
                        with patch.object(orchestrator, '_auto_trigger_checker', new_callable=AsyncMock):
                            try:
                                await asyncio.wait_for(orchestrator.start(), timeout=0.1)
                            except asyncio.TimeoutError:
                                pass

        mock_recover.assert_called_once()


class TestQueueProcessor:
    """Test queue processing"""

    @pytest.mark.asyncio
    async def test_queue_processor_empty(self, orchestrator):
        """Test processing empty queue"""
        with patch.object(orchestrator.queue, 'get_all_active') as mock_get_active:
            mock_get_active.return_value = []  # Empty list
            with patch.object(orchestrator.queue, 'dequeue') as mock_dequeue:
                mock_dequeue.return_value = None
                orchestrator.running = True

                # Run for short time
                task = asyncio.create_task(orchestrator._queue_processor())
                await asyncio.sleep(0.1)
                orchestrator.running = False

                try:
                    await asyncio.wait_for(task, timeout=1.0)
                except asyncio.TimeoutError:
                    task.cancel()

                # Should have attempted dequeue
                assert mock_dequeue.called

    @pytest.mark.asyncio
    async def test_start_investigation_success(
        self, orchestrator, sample_fingerprint_id, sample_signature_data
    ):
        """Test successful investigation start"""
        # Mock all dependencies
        with patch.object(orchestrator.queue, 'acquire_lock') as mock_lock:
            mock_lock.return_value = True
            with patch.object(orchestrator.failure_store, '_get_signature', new_callable=AsyncMock) as mock_get:
                mock_get.return_value = sample_signature_data
                with patch.object(orchestrator.report_manager, 'write_context') as mock_write:
                    mock_write.return_value = "/medic/test/context.json"
                    with patch.object(orchestrator.report_manager, 'get_investigation_log_path') as mock_log:
                        mock_log.return_value = "/medic/test/log.txt"
                        with patch.object(orchestrator.agent_runner, 'launch_investigation') as mock_launch:
                            mock_process = Mock()
                            mock_process.pid = 12345
                            mock_launch.return_value = mock_process

                            await orchestrator._start_investigation(sample_fingerprint_id)

        # Verify workflow
        mock_lock.assert_called_with(sample_fingerprint_id)
        mock_get.assert_called_once()
        mock_write.assert_called_once()
        mock_launch.assert_called_once()
        assert sample_fingerprint_id in orchestrator.active_processes

    @pytest.mark.asyncio
    async def test_start_investigation_lock_failed(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test when lock acquisition fails"""
        with patch.object(orchestrator.queue, 'acquire_lock') as mock_lock:
            mock_lock.return_value = False

            await orchestrator._start_investigation(sample_fingerprint_id)

            # Should not proceed
            assert sample_fingerprint_id not in orchestrator.active_processes

    @pytest.mark.asyncio
    async def test_start_investigation_launch_failed(
        self, orchestrator, sample_fingerprint_id, sample_signature_data
    ):
        """Test when process launch fails"""
        with patch.object(orchestrator.queue, 'acquire_lock') as mock_lock:
            mock_lock.return_value = True
            with patch.object(orchestrator.failure_store, '_get_signature', new_callable=AsyncMock) as mock_get:
                mock_get.return_value = sample_signature_data
                with patch.object(orchestrator.report_manager, 'write_context') as mock_write:
                    mock_write.return_value = "/medic/test/context.json"
                    with patch.object(orchestrator.report_manager, 'get_investigation_log_path') as mock_log:
                        mock_log.return_value = "/medic/test/log.txt"
                        with patch.object(orchestrator.agent_runner, 'launch_investigation') as mock_launch:
                            mock_launch.return_value = None
                            with patch.object(orchestrator.queue, 'mark_completed') as mock_complete:
                                await orchestrator._start_investigation(sample_fingerprint_id)

                                # Should mark as failed
                                mock_complete.assert_called_once()


class TestHeartbeatMonitor:
    """Test heartbeat monitoring"""

    @pytest.mark.asyncio
    async def test_heartbeat_monitor_updates(self, orchestrator, sample_fingerprint_id):
        """Test heartbeat monitoring updates"""
        mock_process = Mock()
        mock_process.poll.return_value = None  # Still running

        orchestrator.active_processes[sample_fingerprint_id] = mock_process

        with patch.object(orchestrator.report_manager, 'count_log_lines') as mock_count:
            mock_count.return_value = 100
            with patch.object(orchestrator.queue, 'get_output_lines') as mock_get:
                mock_get.return_value = 50
                with patch.object(orchestrator.queue, 'set_output_lines') as mock_set:
                    with patch.object(orchestrator.queue, 'update_heartbeat') as mock_heartbeat:
                        await orchestrator._check_investigation_progress(sample_fingerprint_id)

                        # Should have updated heartbeat
                        mock_heartbeat.assert_called_once()
                        mock_set.assert_called_with(sample_fingerprint_id, 100)

    @pytest.mark.asyncio
    async def test_heartbeat_monitor_process_completed(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test heartbeat detects completed process"""
        mock_process = Mock()
        mock_process.poll.return_value = 0  # Exited successfully

        orchestrator.active_processes[sample_fingerprint_id] = mock_process

        with patch.object(orchestrator, '_handle_investigation_completion', new_callable=AsyncMock) as mock_handle:
            await orchestrator._check_investigation_progress(sample_fingerprint_id)

            # Should call completion handler
            mock_handle.assert_called_once_with(sample_fingerprint_id, 0)


class TestInvestigationCompletion:
    """Test investigation completion handling"""

    @pytest.mark.asyncio
    async def test_handle_completion_with_diagnosis(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test completing investigation with diagnosis"""
        orchestrator.active_processes[sample_fingerprint_id] = Mock()

        with patch.object(orchestrator.report_manager, 'get_report_status') as mock_status:
            mock_status.return_value = {
                "has_diagnosis": True,
                "has_fix_plan": True,
                "has_ignored": False,
            }
            with patch.object(orchestrator.queue, 'mark_completed') as mock_complete:
                with patch.object(orchestrator.failure_store, 'update_investigation_status', new_callable=AsyncMock):
                    await orchestrator._handle_investigation_completion(sample_fingerprint_id, 0)

                    # Should mark as success
                    call_args = mock_complete.call_args[0]
                    assert call_args[0] == sample_fingerprint_id
                    assert call_args[1] == InvestigationQueue.RESULT_SUCCESS

        # Should remove from active
        assert sample_fingerprint_id not in orchestrator.active_processes

    @pytest.mark.asyncio
    async def test_handle_completion_ignored(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test completing investigation that was ignored"""
        orchestrator.active_processes[sample_fingerprint_id] = Mock()

        with patch.object(orchestrator.report_manager, 'get_report_status') as mock_status:
            mock_status.return_value = {
                "has_diagnosis": False,
                "has_fix_plan": False,
                "has_ignored": True,
            }
            with patch.object(orchestrator.queue, 'mark_completed') as mock_complete:
                with patch.object(orchestrator.failure_store, 'update_investigation_status', new_callable=AsyncMock):
                    await orchestrator._handle_investigation_completion(sample_fingerprint_id, 0)

                    # Should mark as ignored
                    call_args = mock_complete.call_args[0]
                    assert call_args[1] == InvestigationQueue.RESULT_IGNORED

    @pytest.mark.asyncio
    async def test_handle_completion_failed(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test completing investigation that failed"""
        orchestrator.active_processes[sample_fingerprint_id] = Mock()

        with patch.object(orchestrator.report_manager, 'get_report_status') as mock_status:
            mock_status.return_value = {
                "has_diagnosis": False,
                "has_fix_plan": False,
                "has_ignored": False,
            }
            with patch.object(orchestrator.queue, 'mark_completed') as mock_complete:
                with patch.object(orchestrator.failure_store, 'update_investigation_status', new_callable=AsyncMock):
                    await orchestrator._handle_investigation_completion(sample_fingerprint_id, 1)

                    # Should mark as failed
                    call_args = mock_complete.call_args[0]
                    assert call_args[1] == InvestigationQueue.RESULT_FAILED


class TestAutoTrigger:
    """Test auto-trigger functionality"""

    @pytest.mark.asyncio
    async def test_auto_trigger_enqueues(self, orchestrator):
        """Test auto-trigger enqueues investigations"""
        fingerprint_id = "sha256:auto123"

        with patch.object(orchestrator.failure_store, 'check_auto_trigger_conditions', new_callable=AsyncMock) as mock_check:
            mock_check.return_value = [fingerprint_id]
            with patch.object(orchestrator.queue, 'enqueue') as mock_enqueue:
                # Call the sleep-wrapped portion directly by mocking sleep
                with patch('asyncio.sleep', new_callable=AsyncMock):
                    orchestrator.running = True

                    # Create task and let it run one iteration
                    task = asyncio.create_task(orchestrator._auto_trigger_checker())
                    await asyncio.sleep(0.1)
                    orchestrator.running = False

                    try:
                        await asyncio.wait_for(task, timeout=1.0)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        task.cancel()

                    # Should have enqueued
                    if mock_check.called:
                        mock_enqueue.assert_called_with(fingerprint_id, priority="high")


class TestShutdown:
    """Test graceful shutdown"""

    @pytest.mark.asyncio
    async def test_stop(self, orchestrator, sample_fingerprint_id):
        """Test stopping orchestrator"""
        # Add active process
        mock_process = Mock()
        mock_process.pid = 12345
        orchestrator.active_processes[sample_fingerprint_id] = mock_process
        orchestrator.running = True

        with patch.object(orchestrator.agent_runner, 'terminate_process') as mock_terminate:
            await orchestrator.stop()

            assert orchestrator.running is False
            mock_terminate.assert_called_with(12345)


class TestEdgeCases:
    """Test edge cases and error handling"""

    @pytest.mark.asyncio
    async def test_start_investigation_exception_handling(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test exception handling during investigation start"""
        with patch.object(orchestrator.queue, 'acquire_lock') as mock_lock:
            mock_lock.side_effect = Exception("Lock error")
            with patch.object(orchestrator.queue, 'mark_completed') as mock_complete:
                # Should not crash
                await orchestrator._start_investigation(sample_fingerprint_id)

                # Should mark as failed
                mock_complete.assert_called_once()

    @pytest.mark.asyncio
    async def test_check_progress_exception_handling(
        self, orchestrator, sample_fingerprint_id
    ):
        """Test exception handling in progress check"""
        mock_process = Mock()
        mock_process.poll.side_effect = Exception("Process error")

        orchestrator.active_processes[sample_fingerprint_id] = mock_process

        # Should not crash
        await orchestrator._check_investigation_progress(sample_fingerprint_id)

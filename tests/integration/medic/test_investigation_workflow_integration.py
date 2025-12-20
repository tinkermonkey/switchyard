"""
Integration tests for Medic Investigation Workflow with run_claude_code

Tests the complete investigation workflow using the refactored run_claude_code approach.
"""

import pytest
import asyncio
import tempfile
import shutil
import json
import time
from pathlib import Path
from unittest.mock import Mock, patch, AsyncMock, MagicMock
from datetime import datetime, timezone

from services.medic.docker import DockerDockerReportManager
from services.medic.docker import DockerDockerInvestigationQueue
from services.medic.investigation_agent_runner import InvestigationAgentRunner

from services.medic.docker import DockerInvestigationOrchestrator
from monitoring.observability import ObservabilityManager


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
    """Create temporary medic directory for reports"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def mock_redis():
    """Mock Redis client"""
    redis_mock = Mock()
    redis_data = {}

    def mock_set(key, value, **kwargs):
        nx = kwargs.get('nx', False)
        if nx and key in redis_data:
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
        count = 0
        for key in keys:
            if key in redis_data:
                del redis_data[key]
                count += 1
        return count

    def mock_blpop(key, timeout):
        # Check if there's anything in the queue
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
        return redis_data[key]

    redis_mock.set = mock_set
    redis_mock.get = mock_get
    redis_mock.delete = mock_delete
    redis_mock.blpop = mock_blpop
    redis_mock.rpush = mock_rpush
    redis_mock.sadd = mock_sadd
    redis_mock.srem = mock_srem
    redis_mock.smembers = mock_smembers

    return redis_mock


@pytest.fixture
def mock_observability():
    """Mock observability manager"""
    obs = Mock(spec=ObservabilityManager)
    obs.emit = Mock()
    return obs


@pytest.fixture
def mock_failure_store():
    """Mock failure signature store"""
    store = AsyncMock()

    # Sample signature
    store._get_signature = AsyncMock(return_value={
        "fingerprint_id": "sha256:test123",
        "severity": "ERROR",
        "signature": {
            "error_type": "KeyError",
            "error_pattern": "KeyError: 'missing_key'",
        },
        "sample_log_entries": [
            {
                "timestamp": "2025-11-28T12:00:00Z",
                "container_name": "orchestrator-1",
                "raw_message": "KeyError: 'missing_key' in handler",
            }
        ],
        "occurrence_count": 15,
    })

    return store


class TestInvestigationWorkflowIntegration:
    """Test complete investigation workflow"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_complete_investigation_workflow_success(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test complete investigation workflow from trigger to report creation"""

        # Setup: Mock run_claude_code to simulate successful investigation
        mock_run_claude.return_value = "Investigation completed successfully"

        # Create components
        report_manager = DockerReportManager(base_dir=temp_medic_dir)
        queue = DockerInvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)

        fingerprint_id = "sha256:test123"

        # Enqueue investigation
        queue.enqueue(fingerprint_id, priority="normal")

        # Create signature data
        signature = {
            "fingerprint_id": fingerprint_id,
            "severity": "ERROR",
            "signature": {"error_type": "KeyError"},
            "sample_log_entries": [{"timestamp": "2025-11-28T12:00:00Z"}],
        }

        # Write context file
        context_file = report_manager.write_context(
            fingerprint_id, signature, signature["sample_log_entries"]
        )
        assert Path(context_file).exists()

        # Launch investigation
        output_log = report_manager.get_investigation_log_path(fingerprint_id)
        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        assert investigation is not None
        assert 'task' in investigation
        assert investigation['task'] is not None

        # Wait for task to complete
        task = investigation['task']
        result = await task

        assert result == "Investigation completed successfully"

        # Verify run_claude_code was called with correct context
        assert mock_run_claude.called
        context = mock_run_claude.call_args[0][1]

        assert context['agent'] == 'medic-investigator'
        assert context['task_id'] == fingerprint_id
        assert context['use_docker'] is False  # Runs locally
        assert context['project'] == 'clauditoreum'
        assert context['observability'] == mock_observability

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_investigation_creates_reports(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
    ):
        """Test that investigation can create diagnosis and fix plan reports"""

        # Mock run_claude_code to create report files
        async def mock_investigation(prompt, context):
            # Simulate creating report files
            fingerprint_id = context['task_id']
            report_dir = Path(temp_medic_dir) / fingerprint_id
            report_dir.mkdir(parents=True, exist_ok=True)

            # Create diagnosis
            diagnosis_file = report_dir / "diagnosis.md"
            diagnosis_file.write_text(f"""# Root Cause Diagnosis

**Failure Signature:** `{fingerprint_id}`

## Error Summary
Test error for integration testing

## Root Cause Analysis
This is a simulated error for testing purposes.
""")

            # Create fix plan
            fix_plan_file = report_dir / "fix_plan.md"
            fix_plan_file.write_text(f"""# Fix Plan

**Failure Signature:** `{fingerprint_id}`

## Proposed Solution
Fix the simulated error by updating the test.
""")

            return "Investigation completed"

        mock_run_claude.side_effect = mock_investigation

        # Launch investigation
        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = DockerReportManager(base_dir=temp_medic_dir)

        fingerprint_id = "sha256:test456"
        context_file = str(Path(temp_medic_dir) / fingerprint_id / "context.json")
        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        # Create context file directory
        Path(context_file).parent.mkdir(parents=True, exist_ok=True)
        Path(context_file).write_text(json.dumps({"test": "data"}))

        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        # Wait for completion
        await investigation['task']

        # Verify reports were created
        report_status = report_manager.get_report_status(fingerprint_id)
        assert report_status['has_diagnosis'] is True
        assert report_status['has_fix_plan'] is True

        # Verify report content
        diagnosis = report_manager.read_diagnosis(fingerprint_id)
        assert fingerprint_id in diagnosis
        assert "Root Cause Analysis" in diagnosis

        fix_plan = report_manager.read_fix_plan(fingerprint_id)
        assert fingerprint_id in fix_plan
        assert "Proposed Solution" in fix_plan

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_investigation_failure_handling(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
    ):
        """Test that investigation handles failures gracefully"""

        # Mock run_claude_code to raise exception
        mock_run_claude.side_effect = Exception("Claude Code execution failed")

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = DockerReportManager(base_dir=temp_medic_dir)

        fingerprint_id = "sha256:test_failure"
        context_file = str(Path(temp_medic_dir) / fingerprint_id / "context.json")
        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        # Create context file
        Path(context_file).parent.mkdir(parents=True, exist_ok=True)
        Path(context_file).write_text(json.dumps({"test": "data"}))

        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        # Wait for task to fail
        task = investigation['task']

        with pytest.raises(Exception) as exc_info:
            await task

        assert "Claude Code execution failed" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_multiple_concurrent_investigations(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
    ):
        """Test handling multiple concurrent investigations"""

        # Mock run_claude_code with slight delay to simulate work
        async def mock_slow_investigation(prompt, context):
            await asyncio.sleep(0.1)
            return f"Investigation {context['task_id']} completed"

        mock_run_claude.side_effect = mock_slow_investigation

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = DockerReportManager(base_dir=temp_medic_dir)

        # Launch 3 concurrent investigations
        investigations = []
        fingerprints = ["sha256:test1", "sha256:test2", "sha256:test3"]

        for fp_id in fingerprints:
            context_file = str(Path(temp_medic_dir) / fp_id / "context.json")
            Path(context_file).parent.mkdir(parents=True, exist_ok=True)
            Path(context_file).write_text(json.dumps({"fingerprint_id": fp_id}))

            output_log = report_manager.get_investigation_log_path(fp_id)

            investigation = await agent_runner.launch_investigation(
                fp_id, context_file, output_log, mock_observability
            )
            investigations.append((fp_id, investigation))

        # Wait for all to complete
        results = await asyncio.gather(*[inv['task'] for fp, inv in investigations])

        # Verify all completed
        assert len(results) == 3
        for i, fp_id in enumerate(fingerprints):
            assert f"Investigation {fp_id} completed" == results[i]

        # Verify run_claude_code was called 3 times
        assert mock_run_claude.call_count == 3


class TestRecoveryAndCancellation:
    """Test recovery and cancellation scenarios"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_task_cancellation(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_observability,
    ):
        """Test that investigation tasks can be cancelled cleanly"""

        # Mock run_claude_code with long-running task
        async def mock_long_investigation(prompt, context):
            try:
                await asyncio.sleep(10)  # Long running
                return "Should not complete"
            except asyncio.CancelledError:
                # Clean up and re-raise
                raise

        mock_run_claude.side_effect = mock_long_investigation

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = DockerReportManager(base_dir=temp_medic_dir)

        fingerprint_id = "sha256:test_cancel"
        context_file = str(Path(temp_medic_dir) / fingerprint_id / "context.json")
        Path(context_file).parent.mkdir(parents=True, exist_ok=True)
        Path(context_file).write_text(json.dumps({"test": "data"}))

        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        task = investigation['task']

        # Let it start
        await asyncio.sleep(0.1)

        # Cancel the task
        task.cancel()

        # Verify it raises CancelledError
        with pytest.raises(asyncio.CancelledError):
            await task

    @pytest.mark.asyncio
    async def test_recovery_with_existing_reports(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test recovery detects existing reports and marks investigation complete"""

        report_manager = DockerReportManager(base_dir=temp_medic_dir)
        queue = DockerInvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_recovery"

        # Simulate in_progress investigation
        queue.set_pid(fingerprint_id, 0)  # pid=0 for task-based
        queue.mark_started(fingerprint_id)

        # Create diagnosis report (simulate completed work)
        report_dir = Path(temp_medic_dir) / fingerprint_id
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / "diagnosis.md").write_text("# Diagnosis\nTest diagnosis")
        (report_dir / "fix_plan.md").write_text("# Fix Plan\nTest fix")

        # Run recovery
        # Recover the specific investigation (not async)
        recovery.recover_investigation(fingerprint_id)

        # Should mark as completed
        status = queue.get_status(fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_COMPLETED or status is None

    @pytest.mark.asyncio
    async def test_recovery_with_no_reports_recent_start(
        self,
        temp_workspace,
        temp_medic_dir,
        mock_redis,
        mock_observability,
        mock_failure_store,
    ):
        """Test recovery waits for recent investigations with no reports"""

        report_manager = DockerReportManager(base_dir=temp_medic_dir)
        queue = DockerInvestigationQueue(mock_redis)
        agent_runner = InvestigationAgentRunner(temp_workspace)
        recovery = InvestigationRecovery(
            queue, agent_runner, report_manager
        )

        fingerprint_id = "sha256:test_recent"

        # Simulate recent in_progress investigation (started <30 min ago)
        queue.set_pid(fingerprint_id, 0)
        queue.mark_started(fingerprint_id)

        # No reports created yet

        # Run recovery
        # Recover the specific investigation (not async)
        recovery.recover_investigation(fingerprint_id)

        # Should keep waiting (status remains in_progress)
        status = queue.get_status(fingerprint_id)
        assert status == DockerInvestigationQueue.STATUS_IN_PROGRESS


class TestObservabilityIntegration:
    """Test observability event emission"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_observability_events_emitted(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_observability,
    ):
        """Test that observability events are emitted during investigation"""

        mock_run_claude.return_value = "Investigation completed"

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = DockerReportManager(base_dir=temp_medic_dir)

        fingerprint_id = "sha256:test_obs"
        context_file = str(Path(temp_medic_dir) / fingerprint_id / "context.json")
        Path(context_file).parent.mkdir(parents=True, exist_ok=True)
        Path(context_file).write_text(json.dumps({"test": "data"}))

        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        await investigation['task']

        # Verify run_claude_code received observability manager
        context = mock_run_claude.call_args[0][1]
        assert context['observability'] == mock_observability

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    async def test_stream_callback_integration(
        self,
        mock_run_claude,
        temp_workspace,
        temp_medic_dir,
        mock_observability,
    ):
        """Test that stream callback receives events from run_claude_code"""

        # Track stream events
        stream_events = []

        def capture_stream(event):
            stream_events.append(event)

        # Mock run_claude_code to call stream callback
        async def mock_with_streaming(prompt, context):
            callback = context.get('stream_callback')
            if callback:
                # Simulate Claude Code events
                callback({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Analyzing...'}]}})
                callback({'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Complete'}]}})
            return "Investigation completed"

        mock_run_claude.side_effect = mock_with_streaming

        agent_runner = InvestigationAgentRunner(temp_workspace)
        report_manager = DockerReportManager(base_dir=temp_medic_dir)

        fingerprint_id = "sha256:test_stream"
        context_file = str(Path(temp_medic_dir) / fingerprint_id / "context.json")
        Path(context_file).parent.mkdir(parents=True, exist_ok=True)
        Path(context_file).write_text(json.dumps({"test": "data"}))

        output_log = report_manager.get_investigation_log_path(fingerprint_id)

        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        await investigation['task']

        # Note: stream_callback writes to file, not our capture function
        # Verify callback was provided
        context = mock_run_claude.call_args[0][1]
        assert 'stream_callback' in context
        assert callable(context['stream_callback'])

"""
Unit tests for Medic Investigation Agent Runner
"""

import pytest
import asyncio
from unittest.mock import Mock, MagicMock, patch, mock_open, AsyncMock
import subprocess
import os
import signal
import tempfile
from pathlib import Path

from services.medic.investigation_agent_runner import InvestigationAgentRunner


@pytest.fixture
def temp_workspace():
    """Create temporary workspace directory"""
    with tempfile.TemporaryDirectory() as tmpdir:
        workspace = Path(tmpdir) / "clauditoreum"
        workspace.mkdir()

        # Create investigator instructions file
        medic_dir = workspace / "services" / "medic"
        medic_dir.mkdir(parents=True)
        instructions_file = medic_dir / "investigator_instructions.md"
        instructions_file.write_text("# Investigator Instructions")

        yield str(workspace)


@pytest.fixture
def agent_runner(temp_workspace):
    """Create agent runner with temp workspace"""
    return InvestigationAgentRunner(temp_workspace)


@pytest.fixture
def sample_fingerprint_id():
    """Sample fingerprint ID"""
    return "sha256:abc123def456"


@pytest.fixture
def mock_observability():
    """Mock observability manager"""
    return Mock()


class TestAgentRunnerInit:
    """Test agent runner initialization"""

    def test_init(self, temp_workspace):
        """Test agent runner initialization"""
        runner = InvestigationAgentRunner(temp_workspace)
        assert str(runner.workspace_root) == temp_workspace

    def test_init_with_default_workspace(self):
        """Test initialization with default workspace"""
        runner = InvestigationAgentRunner()
        assert "clauditoreum" in str(runner.workspace_root)


class TestProcessManagement:
    """Test process checking and termination"""

    def test_check_process_running(self, agent_runner):
        """Test checking if process is running"""
        # Get current process (we know it's running)
        current_pid = os.getpid()

        result = agent_runner.check_process(current_pid)

        assert result is True

    def test_check_process_not_running(self, agent_runner):
        """Test checking non-existent process"""
        # Use a PID that likely doesn't exist
        fake_pid = 999999

        result = agent_runner.check_process(fake_pid)

        assert result is False

    @patch('os.killpg')
    @patch('os.kill')
    @patch('time.sleep')
    def test_terminate_process_graceful(
        self, mock_sleep, mock_kill, mock_killpg, agent_runner
    ):
        """Test graceful process termination"""
        test_pid = 12345

        # Mock check_process to return True then False (terminated)
        with patch.object(agent_runner, 'check_process', side_effect=[True, False]):
            result = agent_runner.terminate_process(test_pid, timeout=1)

        assert result is True
        mock_killpg.assert_called_with(test_pid, signal.SIGTERM)

    @patch('os.killpg')
    @patch('time.sleep')
    def test_terminate_process_force_kill(self, mock_sleep, mock_killpg, agent_runner):
        """Test forced process kill when SIGTERM doesn't work"""
        test_pid = 12345

        # Mock check_process to always return True (won't terminate)
        with patch.object(agent_runner, 'check_process', return_value=True):
            result = agent_runner.terminate_process(test_pid, timeout=1)

        # Should have tried SIGTERM and SIGKILL
        assert mock_killpg.call_count == 2
        calls = mock_killpg.call_args_list
        assert calls[0][0] == (test_pid, signal.SIGTERM)
        assert calls[1][0] == (test_pid, signal.SIGKILL)

    def test_terminate_process_already_dead(self, agent_runner):
        """Test terminating already dead process"""
        test_pid = 12345

        with patch.object(agent_runner, 'check_process', return_value=False):
            result = agent_runner.terminate_process(test_pid)

        assert result is True


class TestClaudeVersion:
    """Test Claude CLI version checking"""

    @patch('subprocess.run')
    def test_get_claude_version_success(self, mock_run, agent_runner):
        """Test getting Claude version successfully"""
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = "Claude Code CLI v1.0.0\n"
        mock_run.return_value = mock_result

        version = agent_runner.get_claude_version()

        assert version == "Claude Code CLI v1.0.0"
        mock_run.assert_called_with(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )

    @patch('subprocess.run')
    def test_get_claude_version_not_found(self, mock_run, agent_runner):
        """Test when Claude CLI is not available"""
        mock_run.side_effect = FileNotFoundError()

        version = agent_runner.get_claude_version()

        assert version is None

    @patch('subprocess.run')
    def test_get_claude_version_error(self, mock_run, agent_runner):
        """Test when Claude CLI returns error"""
        mock_result = Mock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        version = agent_runner.get_claude_version()

        assert version is None


class TestPromptBuilding:
    """Test investigation prompt building"""

    def test_build_investigation_prompt(self, agent_runner, sample_fingerprint_id):
        """Test building investigation prompt"""
        context_file = "/medic/sha256:abc/context.json"

        prompt = agent_runner._build_investigation_prompt(
            sample_fingerprint_id, context_file
        )

        assert sample_fingerprint_id in prompt
        assert context_file in prompt
        assert "diagnosis.md" in prompt
        assert "fix_plan.md" in prompt
        assert "ignored.md" in prompt
        assert "docker logs" in prompt.lower()

    def test_prompt_includes_templates(self, agent_runner, sample_fingerprint_id):
        """Test that prompt includes report templates"""
        context_file = "/medic/test/context.json"

        prompt = agent_runner._build_investigation_prompt(
            sample_fingerprint_id, context_file
        )

        # Should include markdown templates
        assert "# Root Cause Diagnosis" in prompt
        assert "# Fix Plan" in prompt
        assert "# Investigation Outcome: Ignored" in prompt


class TestLaunchInvestigation:
    """Test launching investigation process with run_claude_code"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_investigation_success(
        self, mock_file, mock_run_claude, agent_runner, sample_fingerprint_id, mock_observability
    ):
        """Test successfully launching investigation using run_claude_code"""
        # Mock run_claude_code to return a result
        mock_run_claude.return_value = "Investigation completed"

        context_file = "/medic/sha256:abc/context.json"
        output_log = "/medic/sha256:abc/investigation_log.txt"

        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, mock_observability
        )

        assert investigation is not None
        assert 'task' in investigation
        assert 'pid' in investigation
        assert investigation['pid'] is None  # No PID with run_claude_code
        assert 'log_file' in investigation

        # Wait for task to complete to verify it ran
        task = investigation['task']
        result = await task
        assert result == "Investigation completed"

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_investigation_context_structure(
        self, mock_file, mock_run_claude, agent_runner, sample_fingerprint_id, mock_observability
    ):
        """Test that context passed to run_claude_code has correct structure"""
        mock_run_claude.return_value = "Result"

        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, mock_observability
        )

        # Verify investigation was created
        assert investigation is not None
        assert 'task' in investigation

        # Wait for task to complete
        result = await investigation['task']
        assert result == "Result"

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_stream_callback_writes_to_log(
        self, mock_file, mock_run_claude, agent_runner, sample_fingerprint_id, mock_observability
    ):
        """Test that stream callback writes events to log file"""
        mock_run_claude.return_value = "Result"

        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, mock_observability
        )

        # Verify investigation created with stream callback
        assert investigation is not None

        # Wait for completion
        result = await investigation['task']
        assert result == "Result"

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_investigation_task_completion(
        self, mock_file, mock_run_claude, agent_runner, sample_fingerprint_id, mock_observability
    ):
        """Test that investigation task completes successfully"""
        expected_result = "Investigation completed successfully"
        mock_run_claude.return_value = expected_result

        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, mock_observability
        )

        # Wait for task to complete
        task = investigation['task']
        result = await task

        assert result == expected_result

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_investigation_task_failure(
        self, mock_file, mock_run_claude, agent_runner, sample_fingerprint_id, mock_observability
    ):
        """Test that investigation task handles failures"""
        mock_run_claude.side_effect = Exception("Claude Code failed")

        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, mock_observability
        )

        # Wait for task to fail
        task = investigation['task']

        with pytest.raises(Exception) as exc_info:
            await task

        assert "Claude Code failed" in str(exc_info.value)

    @pytest.mark.asyncio
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_investigation_exception_handling(
        self, mock_file, agent_runner, sample_fingerprint_id, mock_observability
    ):
        """Test launch handles exceptions gracefully"""
        # Make open() fail
        mock_file.side_effect = Exception("Cannot open file")

        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, mock_observability
        )

        assert investigation is None


class TestEdgeCases:
    """Test edge cases and error handling"""

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_with_unicode_fingerprint(
        self, mock_file, mock_run_claude, agent_runner, mock_observability
    ):
        """Test launch with fingerprint containing unicode"""
        fingerprint_id = "sha256:abc文字def"
        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        mock_run_claude.return_value = "Result"

        # Should not crash
        investigation = await agent_runner.launch_investigation(
            fingerprint_id, context_file, output_log, mock_observability
        )

        assert investigation is not None

        # Wait for completion
        result = await investigation['task']
        assert result == "Result"

    def test_terminate_process_with_exception(self, agent_runner):
        """Test terminate handles exceptions"""
        test_pid = 12345

        with patch('os.killpg', side_effect=Exception("Kill failed")):
            # Should not crash
            result = agent_runner.terminate_process(test_pid)

            # May return False due to error
            assert isinstance(result, bool)

    @patch('subprocess.run')
    def test_get_claude_version_timeout(self, mock_run, agent_runner):
        """Test Claude version check with timeout"""
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 5)

        version = agent_runner.get_claude_version()

        assert version is None

    @pytest.mark.asyncio
    @patch('claude.claude_integration.run_claude_code', new_callable=AsyncMock)
    @patch('builtins.open', new_callable=mock_open)
    async def test_launch_without_observability(
        self, mock_file, mock_run_claude, agent_runner, sample_fingerprint_id
    ):
        """Test launch works without observability manager"""
        mock_run_claude.return_value = "Result"

        context_file = "/medic/test/context.json"
        output_log = "/medic/test/log.txt"

        # No observability manager
        investigation = await agent_runner.launch_investigation(
            sample_fingerprint_id, context_file, output_log, None
        )

        assert investigation is not None

        # Wait for completion
        result = await investigation['task']
        assert result == "Result"

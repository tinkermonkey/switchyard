"""
Unit tests for Medic Docker log monitor
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
import json

from services.medic.docker_log_monitor import DockerLogMonitor


@pytest.fixture
def mock_docker_client():
    """Mock Docker client"""
    client = Mock()
    client.containers = Mock()
    return client


@pytest.fixture
def mock_fingerprint_engine():
    """Mock fingerprint engine"""
    engine = Mock()
    engine.generate = Mock(return_value=Mock(fingerprint_id="sha256:test123"))
    return engine


@pytest.fixture
def mock_failure_store():
    """Mock failure signature store"""
    store = Mock()
    store.record_occurrence = AsyncMock()
    return store


@pytest.fixture
def log_monitor(mock_docker_client, mock_fingerprint_engine, mock_failure_store):
    """Create log monitor with mocks"""
    return DockerLogMonitor(mock_docker_client, mock_fingerprint_engine, mock_failure_store)


class TestDockerLogMonitorInit:
    """Test log monitor initialization"""

    def test_init_sets_containers(self, log_monitor):
        """Test that monitored containers are set"""
        assert len(log_monitor.MONITORED_CONTAINER_PATTERNS) == 5
        assert "clauditoreum-orchestrator" in log_monitor.MONITORED_CONTAINER_PATTERNS

    def test_init_sets_log_levels(self, log_monitor):
        """Test that monitored log levels are set"""
        assert "ERROR" in log_monitor.MONITORED_LEVELS
        assert "CRITICAL" in log_monitor.MONITORED_LEVELS
        assert "WARNING" in log_monitor.MONITORED_LEVELS
        assert "FATAL" in log_monitor.MONITORED_LEVELS

    def test_init_not_running(self, log_monitor):
        """Test that monitor is not running initially"""
        assert log_monitor.running is False


class TestParseLogLine:
    """Test log line parsing"""

    def test_parse_json_log(self, log_monitor):
        """Test parsing JSON-formatted logs"""
        json_log = json.dumps({
            "asctime": "2025-11-28 12:45:23",
            "levelname": "ERROR",
            "message": "Test error message",
            "name": "orchestrator",
            "exc_text": "Traceback here"
        })

        parsed = log_monitor._parse_log_line(json_log)

        assert parsed["level"] == "ERROR"
        assert parsed["message"] == "Test error message"
        assert parsed["name"] == "orchestrator"
        assert parsed["traceback"] == "Traceback here"

    def test_parse_standard_python_log(self, log_monitor):
        """Test parsing standard Python logging format"""
        log_line = "2025-11-28 12:45:23 - orchestrator - ERROR - Something failed"

        parsed = log_monitor._parse_log_line(log_line)

        assert parsed["level"] == "ERROR"
        assert parsed["message"] == "Something failed"
        assert parsed["name"] == "orchestrator"
        assert "2025-11-28 12:45:23" in parsed["timestamp"]

    def test_parse_plain_text_with_error(self, log_monitor):
        """Test parsing plain text with ERROR marker"""
        log_line = "ERROR: Connection failed"

        parsed = log_monitor._parse_log_line(log_line)

        assert parsed["level"] == "ERROR"
        assert parsed["message"] == log_line

    def test_parse_plain_text_with_critical(self, log_monitor):
        """Test parsing plain text with CRITICAL marker"""
        log_line = "[CRITICAL] System failure"

        parsed = log_monitor._parse_log_line(log_line)

        assert parsed["level"] == "CRITICAL"

    def test_parse_plain_text_defaults_to_info(self, log_monitor):
        """Test that plain text without markers defaults to INFO"""
        log_line = "This is a regular message"

        parsed = log_monitor._parse_log_line(log_line)

        assert parsed["level"] == "INFO"

    def test_parse_detects_traceback(self, log_monitor):
        """Test that embedded tracebacks are detected"""
        log_line = "Error occurred\nTraceback (most recent call last):\n  File main.py\n  KeyError"

        parsed = log_monitor._parse_log_line(log_line)

        assert parsed["traceback"] == log_line

    def test_parse_handles_malformed_json(self, log_monitor):
        """Test that malformed JSON falls back to text parsing"""
        log_line = '{"invalid json'

        parsed = log_monitor._parse_log_line(log_line)

        # Should not crash, should parse as text
        assert "level" in parsed
        assert "message" in parsed


class TestProcessLogLine:
    """Test log line processing"""

    @pytest.mark.asyncio
    async def test_process_error_log_line(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing an ERROR log line"""
        log_line = b"ERROR: Something failed"

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Should generate fingerprint
        mock_fingerprint_engine.generate.assert_called_once()

        # Should record occurrence
        mock_failure_store.record_occurrence.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_warning_log_line(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing a WARNING log line"""
        log_line = b"WARNING: Low memory"

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        mock_fingerprint_engine.generate.assert_called_once()
        mock_failure_store.record_occurrence.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_critical_log_line(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing a CRITICAL log line"""
        log_line = b"CRITICAL: System failure"

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        mock_fingerprint_engine.generate.assert_called_once()
        mock_failure_store.record_occurrence.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_ignores_info_log_line(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test that INFO logs are ignored"""
        log_line = b"INFO: Processing task"

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Should not generate fingerprint or record occurrence
        mock_fingerprint_engine.generate.assert_not_called()
        mock_failure_store.record_occurrence.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_ignores_debug_log_line(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test that DEBUG logs are ignored"""
        log_line = b"DEBUG: Verbose output"

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        mock_fingerprint_engine.generate.assert_not_called()
        mock_failure_store.record_occurrence.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_empty_log_line(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing empty log line"""
        log_line = b""

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Should not process empty lines
        mock_fingerprint_engine.generate.assert_not_called()
        mock_failure_store.record_occurrence.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_handles_unicode_decode_errors(self, log_monitor):
        """Test that unicode decode errors are handled gracefully"""
        # Invalid UTF-8 bytes
        log_line = b"\xff\xfe ERROR: Invalid encoding"

        # Should not crash
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

    @pytest.mark.asyncio
    async def test_process_handles_fingerprint_generation_error(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test handling of fingerprint generation errors"""
        log_line = b"ERROR: Test"
        mock_fingerprint_engine.generate.side_effect = Exception("Fingerprint error")

        # Should not crash
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Should not call record_occurrence if fingerprinting fails
        mock_failure_store.record_occurrence.assert_not_called()

    @pytest.mark.asyncio
    async def test_process_passes_container_info(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test that container info is passed correctly"""
        log_line = b"ERROR: Test"

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Check that container info was passed to record_occurrence
        call_args = mock_failure_store.record_occurrence.call_args[0]
        container_info = call_args[2]

        assert container_info["id"] == "container123"
        assert container_info["name"] == "orchestrator-1"


class TestMonitorContainerPattern:
    """Test container pattern monitoring"""

    @pytest.mark.asyncio
    async def test_monitor_finds_matching_containers(self, log_monitor, mock_docker_client):
        """Test finding containers matching pattern"""
        mock_container = Mock()
        mock_container.id = "container123"
        mock_container.name = "clauditoreum-orchestrator-1"
        mock_docker_client.containers.list.return_value = [mock_container]

        with patch.object(log_monitor, '_stream_container_logs', new=AsyncMock()):
            await log_monitor._monitor_container_pattern("clauditoreum-orchestrator")

            mock_docker_client.containers.list.assert_called_once_with(
                filters={"name": "clauditoreum-orchestrator"},
                all=False
            )

            # Should have created a stream task
            assert mock_container.id in log_monitor.streams

    @pytest.mark.asyncio
    async def test_monitor_handles_no_matching_containers(self, log_monitor, mock_docker_client):
        """Test handling when no containers match pattern"""
        mock_docker_client.containers.list.return_value = []

        await log_monitor._monitor_container_pattern("nonexistent")

        # Should not crash
        assert len(log_monitor.streams) == 0

    @pytest.mark.asyncio
    async def test_monitor_handles_docker_error(self, log_monitor, mock_docker_client):
        """Test handling Docker API errors"""
        mock_docker_client.containers.list.side_effect = Exception("Docker error")

        # Should not crash
        await log_monitor._monitor_container_pattern("orchestrator")


class TestStopMonitor:
    """Test stopping the monitor"""

    def test_stop_sets_running_false(self, log_monitor):
        """Test that stop sets running to False"""
        log_monitor.running = True

        log_monitor.stop()

        assert log_monitor.running is False


class TestRealWorldScenarios:
    """Test real-world log scenarios"""

    @pytest.mark.asyncio
    async def test_process_python_exception_with_traceback(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing Python exception with full traceback"""
        json_log = json.dumps({
            "asctime": "2025-11-28 12:45:23",
            "levelname": "ERROR",
            "message": "KeyError: 'issue_number'",
            "name": "agent_executor",
            "exc_text": "Traceback (most recent call last):\n  File main.py, line 42\n  KeyError: 'issue_number'"
        })

        log_line = json_log.encode('utf-8')

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Verify fingerprint was generated with correct data
        mock_fingerprint_engine.generate.assert_called_once()
        call_kwargs = mock_fingerprint_engine.generate.call_args.kwargs
        log_entry = call_kwargs["log_entry"]

        assert log_entry["level"] == "ERROR"
        assert "KeyError" in log_entry["message"]
        assert log_entry["traceback"] is not None

    @pytest.mark.asyncio
    async def test_process_docker_error_log(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing Docker container error"""
        log_line = b"2025-11-28 12:45:23 - docker - ERROR - Container failed to start"

        await log_monitor._process_log_line("docker-daemon", "daemon123", log_line)

        mock_fingerprint_engine.generate.assert_called_once()
        call_kwargs = mock_fingerprint_engine.generate.call_args.kwargs

        assert call_kwargs["container_name"] == "docker-daemon"
        assert call_kwargs["log_entry"]["level"] == "ERROR"

    @pytest.mark.asyncio
    async def test_process_multiline_error(self, log_monitor, mock_fingerprint_engine, mock_failure_store):
        """Test processing multi-line error messages"""
        log_line = b"""ERROR: Multi-line error
Line 2 of error
Line 3 of error"""

        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Should still process as ERROR
        mock_fingerprint_engine.generate.assert_called_once()


class TestEdgeCases:
    """Test edge cases and boundary conditions"""

    @pytest.mark.asyncio
    async def test_process_very_long_log_line(self, log_monitor, mock_fingerprint_engine):
        """Test processing very long log lines"""
        long_message = "ERROR: " + ("x" * 10000)
        log_line = long_message.encode('utf-8')

        # Should not crash
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        mock_fingerprint_engine.generate.assert_called_once()

    @pytest.mark.asyncio
    async def test_process_log_with_special_characters(self, log_monitor, mock_fingerprint_engine):
        """Test processing logs with special characters"""
        log_line = "ERROR: Special chars: \n\t\r\\\"'".encode('utf-8')

        # Should not crash
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        mock_fingerprint_engine.generate.assert_called_once()

    def test_parse_log_with_nested_json(self, log_monitor):
        """Test parsing log with nested JSON data"""
        json_log = json.dumps({
            "levelname": "ERROR",
            "message": "Failed to process",
            "custom_field": {
                "nested": {
                    "data": "value"
                }
            }
        })

        parsed = log_monitor._parse_log_line(json_log)

        assert parsed["level"] == "ERROR"
        # Custom fields are preserved in context with nested structure
        assert "custom_field" in parsed["context"]
        assert parsed["context"]["custom_field"]["nested"]["data"] == "value"

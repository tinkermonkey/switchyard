"""
Integration tests for Medic end-to-end flow
Tests the complete pipeline: Docker logs → parsing → fingerprinting → ES storage → API retrieval
"""

import pytest
import asyncio
import json
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime

from services.medic.docker_log_monitor import DockerLogMonitor
from services.medic.fingerprint_engine import FingerprintEngine
from services.medic.failure_signature_store import FailureSignatureStore


@pytest.fixture
def mock_es_client():
    """Mock Elasticsearch client with realistic responses"""
    client = Mock()
    client.ilm = Mock()
    client.indices = Mock()
    client.search = Mock()
    client.index = Mock()
    client.update_by_query = Mock()

    # Mock ILM policy check
    client.ilm.get_lifecycle.side_effect = Exception("Not found")
    client.ilm.put_lifecycle.return_value = {"acknowledged": True}

    # Mock index template
    client.indices.put_index_template.return_value = {"acknowledged": True}

    # Mock search - return empty initially
    client.search.return_value = {
        "hits": {
            "total": {"value": 0},
            "hits": []
        }
    }

    # Mock index operation
    client.index.return_value = {"_id": "test123", "result": "created"}

    # Mock update_by_query
    client.update_by_query.return_value = {"updated": 1}

    return client


@pytest.fixture
def mock_docker_client():
    """Mock Docker client with containers"""
    client = Mock()
    client.containers = Mock()
    return client


@pytest.fixture
def fingerprint_engine():
    """Create real fingerprint engine"""
    return FingerprintEngine()


@pytest.fixture
def failure_store(mock_es_client):
    """Create failure store with mocked ES"""
    return FailureSignatureStore(mock_es_client)


@pytest.fixture
def log_monitor(mock_docker_client, fingerprint_engine, failure_store):
    """Create log monitor with real fingerprinting and mocked ES"""
    return DockerLogMonitor(mock_docker_client, fingerprint_engine, failure_store)


class TestEndToEndFlow:
    """Test complete Medic flow from log to storage"""

    @pytest.mark.asyncio
    async def test_single_error_creates_signature(
        self, log_monitor, failure_store, mock_es_client
    ):
        """Test that a single error log creates a new signature"""
        # Arrange: Error log line
        log_line = b'{"asctime": "2025-11-28 12:45:23", "levelname": "ERROR", "message": "KeyError: \'issue_number\'", "name": "orchestrator"}'

        # Act: Process the log line
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Assert: Signature was created in ES
        mock_es_client.search.assert_called()
        mock_es_client.index.assert_called_once()

        # Verify the indexed document structure
        call_args = mock_es_client.index.call_args
        doc = call_args[1]["document"]

        assert doc["fingerprint_id"].startswith("sha256:")
        assert doc["signature"]["error_type"] == "KeyError"
        assert doc["occurrence_count"] == 1
        assert doc["status"] == "new"
        assert doc["severity"] == "ERROR"
        assert len(doc["sample_log_entries"]) == 1

    @pytest.mark.asyncio
    async def test_duplicate_error_updates_signature(
        self, log_monitor, failure_store, mock_es_client
    ):
        """Test that duplicate errors update the existing signature"""
        # Arrange: First error creates signature
        log_line1 = b"ERROR: Connection timeout to database"

        # Mock ES to return existing signature on second error
        existing_signature = {
            "_source": {
                "fingerprint_id": "sha256:abc123",
                "occurrence_count": 1,
                "status": "new",
                "sample_log_entries": [],
            }
        }

        # First call returns nothing, second returns existing
        mock_es_client.search.side_effect = [
            {"hits": {"total": {"value": 0}, "hits": []}},  # First error - new
            {"hits": {"total": {"value": 1}, "hits": [existing_signature]}},  # Second error - existing
        ]

        # Act: Process two identical errors
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line1)
        await log_monitor._process_log_line("orchestrator-1", "container456", log_line1)

        # Assert: First creates, second updates
        assert mock_es_client.index.call_count == 1  # Only one creation
        assert mock_es_client.update_by_query.call_count == 1  # One update

    @pytest.mark.asyncio
    async def test_different_errors_create_different_signatures(
        self, log_monitor, mock_es_client
    ):
        """Test that different error types create separate signatures"""
        # Arrange: Two different errors
        error1 = b"ERROR: KeyError: 'missing_key'"
        error2 = b"ERROR: ValueError: invalid input"

        # Mock ES to return no existing signatures
        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        # Act: Process both errors
        await log_monitor._process_log_line("orchestrator-1", "container123", error1)
        await log_monitor._process_log_line("orchestrator-1", "container123", error2)

        # Assert: Two signatures created
        assert mock_es_client.index.call_count == 2

        # Verify different fingerprints
        call1 = mock_es_client.index.call_args_list[0]
        call2 = mock_es_client.index.call_args_list[1]

        doc1 = call1[1]["document"]
        doc2 = call2[1]["document"]

        assert doc1["fingerprint_id"] != doc2["fingerprint_id"]
        assert doc1["signature"]["error_type"] == "KeyError"
        assert doc2["signature"]["error_type"] == "ValueError"

    @pytest.mark.asyncio
    async def test_same_error_different_times_same_fingerprint(
        self, fingerprint_engine
    ):
        """Test that same error at different times produces same fingerprint"""
        # Arrange: Same error with different timestamps
        log1 = {
            "level": "ERROR",
            "message": "KeyError: 'issue_number' at 2025-11-28 12:00:00",
            "timestamp": "2025-11-28T12:00:00Z",
        }
        log2 = {
            "level": "ERROR",
            "message": "KeyError: 'issue_number' at 2025-11-28 13:00:00",
            "timestamp": "2025-11-28T13:00:00Z",
        }

        # Act: Generate fingerprints
        fp1 = fingerprint_engine.generate("orchestrator-1", log1)
        fp2 = fingerprint_engine.generate("orchestrator-1", log2)

        # Assert: Same fingerprint ID
        assert fp1.fingerprint_id == fp2.fingerprint_id
        assert fp1.error_type == fp2.error_type == "KeyError"
        assert fp1.container_pattern == fp2.container_pattern == "orchestrator"

    @pytest.mark.asyncio
    async def test_multiple_log_formats_correctly_parsed(
        self, log_monitor, mock_es_client
    ):
        """Test that different log formats are all correctly processed"""
        # Arrange: Different log formats
        json_log = b'{"levelname": "ERROR", "message": "JSON format error"}'
        python_log = b"2025-11-28 12:45:23 - orchestrator - ERROR - Python format error"
        plain_log = b"ERROR: Plain text format error"

        # Mock ES to return no existing signatures
        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        # Act: Process all formats
        await log_monitor._process_log_line("orchestrator-1", "c1", json_log)
        await log_monitor._process_log_line("orchestrator-1", "c2", python_log)
        await log_monitor._process_log_line("orchestrator-1", "c3", plain_log)

        # Assert: All processed and stored
        assert mock_es_client.index.call_count == 3

        # Verify all have ERROR level
        for call in mock_es_client.index.call_args_list:
            doc = call[1]["document"]
            assert doc["severity"] == "ERROR"

    @pytest.mark.asyncio
    async def test_info_logs_are_ignored(self, log_monitor, mock_es_client):
        """Test that INFO logs don't create signatures"""
        # Arrange: INFO log
        info_log = b"INFO: This is just informational"

        # Act: Process INFO log
        await log_monitor._process_log_line("orchestrator-1", "container123", info_log)

        # Assert: Nothing stored
        mock_es_client.search.assert_not_called()
        mock_es_client.index.assert_not_called()

    @pytest.mark.asyncio
    async def test_warning_logs_are_processed(self, log_monitor, mock_es_client):
        """Test that WARNING logs create signatures"""
        # Arrange: WARNING log
        warning_log = b"WARNING: Resource usage is high"

        # Mock ES to return no existing
        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        # Act: Process WARNING log
        await log_monitor._process_log_line("orchestrator-1", "container123", warning_log)

        # Assert: Signature created
        mock_es_client.index.assert_called_once()
        doc = mock_es_client.index.call_args[1]["document"]
        assert doc["severity"] == "WARNING"

    @pytest.mark.asyncio
    async def test_critical_logs_are_processed(self, log_monitor, mock_es_client):
        """Test that CRITICAL logs create signatures"""
        # Arrange: CRITICAL log
        critical_log = b"CRITICAL: System failure imminent"

        # Mock ES to return no existing
        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        # Act: Process CRITICAL log
        await log_monitor._process_log_line("orchestrator-1", "container123", critical_log)

        # Assert: Signature created with CRITICAL severity
        mock_es_client.index.assert_called_once()
        doc = mock_es_client.index.call_args[1]["document"]
        assert doc["severity"] == "CRITICAL"


class TestContainerMonitoring:
    """Test Docker container monitoring"""

    @pytest.mark.asyncio
    async def test_monitor_finds_matching_containers(self, log_monitor, mock_docker_client):
        """Test that monitor finds and streams from matching containers"""
        # Arrange: Mock container
        mock_container = Mock()
        mock_container.id = "container123"
        mock_container.name = "clauditoreum-orchestrator-1"
        mock_container.logs.return_value = iter([])  # Empty log stream

        mock_docker_client.containers.list.return_value = [mock_container]

        # Act: Monitor container pattern
        with patch.object(log_monitor, '_stream_container_logs', new=AsyncMock()):
            await log_monitor._monitor_container_pattern("clauditoreum-orchestrator")

        # Assert: Container list called with correct filter
        mock_docker_client.containers.list.assert_called_once_with(
            filters={"name": "clauditoreum-orchestrator"},
            all=False
        )

        # Verify stream was created
        assert mock_container.id in log_monitor.streams


class TestRealWorldScenarios:
    """Test realistic failure scenarios"""

    @pytest.mark.asyncio
    async def test_python_exception_with_traceback(
        self, log_monitor, mock_es_client
    ):
        """Test processing a real Python exception with traceback"""
        # Arrange: Realistic Python exception log
        log_line = json.dumps({
            "asctime": "2025-11-28 12:45:23,123",
            "levelname": "ERROR",
            "message": "KeyError: 'issue_number'",
            "name": "agent_executor",
            "exc_text": """Traceback (most recent call last):
  File "/workspace/clauditoreum/services/agent_executor.py", line 242, in execute_agent
    issue_number = task_context["issue_number"]
KeyError: 'issue_number'"""
        }).encode('utf-8')

        # Mock ES
        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        # Act: Process the error
        await log_monitor._process_log_line("orchestrator-1", "container123", log_line)

        # Assert: Signature created with full context
        mock_es_client.index.assert_called_once()
        doc = mock_es_client.index.call_args[1]["document"]

        assert doc["signature"]["error_type"] == "KeyError"
        assert len(doc["signature"]["stack_signature"]) > 0
        assert "agent_executor.py:execute_agent:242" in doc["signature"]["stack_signature"]
        assert doc["severity"] == "ERROR"

    @pytest.mark.asyncio
    async def test_recurring_error_status_progression(
        self, log_monitor, failure_store, mock_es_client
    ):
        """Test that recurring errors progress through status states"""
        # Arrange: Same error occurring multiple times
        log_line = b"ERROR: Database connection failed"

        # Mock ES to simulate progression
        mock_es_client.search.side_effect = [
            # First occurrence - new
            {"hits": {"total": {"value": 0}, "hits": []}},
            # Second occurrence - existing, new status
            {"hits": {"total": {"value": 1}, "hits": [{
                "_source": {
                    "fingerprint_id": "sha256:abc123",
                    "occurrence_count": 1,
                    "status": "new",
                    "sample_log_entries": [],
                }
            }]}},
        ]

        # Act: Process same error twice
        await log_monitor._process_log_line("orchestrator-1", "c1", log_line)
        await log_monitor._process_log_line("orchestrator-1", "c2", log_line)

        # Assert: First creates with "new" status
        create_call = mock_es_client.index.call_args
        assert create_call[1]["document"]["status"] == "new"

        # Second updates to "recurring"
        update_call = mock_es_client.update_by_query.call_args
        script_params = update_call[1]["body"]["script"]["params"]
        assert script_params["status"] == "recurring"


class TestErrorHandling:
    """Test error handling and edge cases"""

    @pytest.mark.asyncio
    async def test_elasticsearch_error_handled_gracefully(
        self, log_monitor, mock_es_client
    ):
        """Test that ES errors don't crash the monitor"""
        # Arrange: ES throws error
        mock_es_client.search.side_effect = Exception("ES connection failed")

        log_line = b"ERROR: Test error"

        # Act: Process log (should not raise exception)
        try:
            await log_monitor._process_log_line("orchestrator-1", "c1", log_line)
            success = True
        except Exception:
            success = False

        # Assert: Error was handled gracefully
        assert success

    @pytest.mark.asyncio
    async def test_malformed_json_log_handled(self, log_monitor, mock_es_client):
        """Test that malformed JSON logs are handled"""
        # Arrange: Malformed JSON
        log_line = b'{"invalid json'

        mock_es_client.search.return_value = {
            "hits": {"total": {"value": 0}, "hits": []}
        }

        # Act: Process malformed log
        try:
            await log_monitor._process_log_line("orchestrator-1", "c1", log_line)
            success = True
        except Exception:
            success = False

        # Assert: Handled gracefully
        assert success

    @pytest.mark.asyncio
    async def test_unicode_decode_error_handled(self, log_monitor):
        """Test that invalid UTF-8 is handled"""
        # Arrange: Invalid UTF-8 bytes
        log_line = b"\xff\xfe ERROR: Invalid encoding"

        # Act: Process invalid encoding
        try:
            await log_monitor._process_log_line("orchestrator-1", "c1", log_line)
            success = True
        except Exception:
            success = False

        # Assert: Handled gracefully
        assert success

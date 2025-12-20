"""
Unit tests for Medic failure signature store
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime, timedelta, timezone

from services.medic.docker import DockerDockerFailureSignatureStore
from services.medic.fingerprint_engine import ErrorFingerprint


@pytest.fixture
def mock_es_client():
    """Mock Elasticsearch client"""
    client = Mock()
    client.ilm = Mock()
    client.indices = Mock()
    client.search = Mock()
    client.index = Mock()
    client.update_by_query = Mock()
    return client


@pytest.fixture
def failure_store(mock_es_client):
    """Create failure signature store with mocked ES"""
    with patch.object(DockerFailureSignatureStore, '_setup_elasticsearch'):
        store = DockerFailureSignatureStore(mock_es_client)
        return store


@pytest.fixture
def sample_fingerprint():
    """Sample error fingerprint"""
    return ErrorFingerprint(
        fingerprint_id="sha256:abc123",
        container_pattern="orchestrator",
        error_type="KeyError",
        error_pattern="KeyError: '{key}' in task context",
        stack_signature=["main.py:process:42"],
        normalized_message="'{key}' in task context",
        raw_data={
            "original_message": "KeyError: 'issue_number' in task context",
            "original_container": "orchestrator-1",
            "stack_trace": None,
            "log_entry": {},
        }
    )


@pytest.fixture
def sample_log_entry():
    """Sample log entry"""
    return {
        "level": "ERROR",
        "message": "KeyError: 'issue_number' in task context",
        "timestamp": "2025-11-28T12:45:23Z",
        "name": "orchestrator",
    }


@pytest.fixture
def sample_container_info():
    """Sample container info"""
    return {
        "id": "container123",
        "name": "orchestrator-1",
    }


class TestDockerFailureSignatureStoreSetup:
    """Test Elasticsearch setup"""

    def test_setup_creates_ilm_policy(self, mock_es_client):
        """Test that setup creates ILM policy"""
        mock_es_client.ilm.get_lifecycle.return_value = None

        store = DockerFailureSignatureStore(mock_es_client)

        mock_es_client.ilm.put_lifecycle.assert_called_once()
        call_args = mock_es_client.ilm.put_lifecycle.call_args
        assert call_args[1]["name"] == "medic-ilm-policy"

    def test_setup_creates_index_template(self, mock_es_client):
        """Test that setup creates index template"""
        store = DockerFailureSignatureStore(mock_es_client)

        mock_es_client.indices.put_index_template.assert_called_once()
        call_args = mock_es_client.indices.put_index_template.call_args
        assert call_args[1]["name"] == "medic-failure-signatures"


class TestRecordOccurrence:
    """Test recording failure occurrences"""

    @pytest.mark.asyncio
    async def test_record_occurrence_creates_new_signature(
        self, failure_store, sample_fingerprint, sample_log_entry, sample_container_info
    ):
        """Test that first occurrence creates new signature"""
        failure_store._get_signature = AsyncMock(return_value=None)
        failure_store._create_signature = AsyncMock()

        await failure_store.record_occurrence(
            sample_fingerprint, sample_log_entry, sample_container_info
        )

        failure_store._create_signature.assert_called_once()

    @pytest.mark.asyncio
    async def test_record_occurrence_updates_existing_signature(
        self, failure_store, sample_fingerprint, sample_log_entry, sample_container_info
    ):
        """Test that subsequent occurrences update existing signature"""
        existing_signature = {
            "fingerprint_id": "sha256:abc123",
            "occurrence_count": 5,
            "status": "recurring",
        }
        failure_store._get_signature = AsyncMock(return_value=existing_signature)
        failure_store._update_occurrence = AsyncMock()

        await failure_store.record_occurrence(
            sample_fingerprint, sample_log_entry, sample_container_info
        )

        failure_store._update_occurrence.assert_called_once()


class TestCreateSignature:
    """Test creating new signatures"""

    @pytest.mark.asyncio
    async def test_create_signature_sets_correct_fields(
        self, failure_store, sample_fingerprint, sample_log_entry, sample_container_info
    ):
        """Test that new signature has correct fields"""
        failure_store._async_index = AsyncMock()

        await failure_store._create_signature(
            sample_fingerprint, sample_log_entry, sample_container_info
        )

        # Verify index was called with correct data
        call_args = failure_store._async_index.call_args[1]
        doc = call_args["document"]

        assert doc["fingerprint_id"] == "sha256:abc123"
        assert doc["signature"]["error_type"] == "KeyError"
        assert doc["occurrence_count"] == 1
        assert doc["status"] == "new"
        assert doc["investigation_status"] == "not_started"

    @pytest.mark.asyncio
    async def test_create_signature_calculates_severity(
        self, failure_store, sample_fingerprint, sample_container_info
    ):
        """Test severity calculation"""
        failure_store._async_index = AsyncMock()

        # Test ERROR level
        log_entry = {"level": "ERROR", "message": "Test"}
        await failure_store._create_signature(sample_fingerprint, log_entry, sample_container_info)
        doc = failure_store._async_index.call_args[1]["document"]
        assert doc["severity"] == "ERROR"

        # Test CRITICAL level
        log_entry = {"level": "CRITICAL", "message": "Test"}
        await failure_store._create_signature(sample_fingerprint, log_entry, sample_container_info)
        doc = failure_store._async_index.call_args[1]["document"]
        assert doc["severity"] == "CRITICAL"

        # Test WARNING level
        log_entry = {"level": "WARNING", "message": "Test"}
        await failure_store._create_signature(sample_fingerprint, log_entry, sample_container_info)
        doc = failure_store._async_index.call_args[1]["document"]
        assert doc["severity"] == "WARNING"


class TestUpdateOccurrence:
    """Test updating existing signatures"""

    @pytest.mark.asyncio
    async def test_update_occurrence_increments_count(
        self, failure_store, sample_log_entry, sample_container_info
    ):
        """Test that update increments occurrence count"""
        existing = {
            "fingerprint_id": "sha256:abc123",
            "occurrence_count": 5,
            "status": "recurring",
            "sample_log_entries": [],
        }
        failure_store._count_occurrences_since = AsyncMock(return_value=2)
        failure_store.es.update_by_query = Mock()

        await failure_store._update_occurrence(existing, sample_log_entry, sample_container_info)

        # Verify update_by_query was called
        failure_store.es.update_by_query.assert_called_once()
        call_args = failure_store.es.update_by_query.call_args[1]
        script_params = call_args["body"]["script"]["params"]

        # Verify occurrence counts are updated
        assert script_params["occurrences_last_hour"] == 3  # 2 + 1
        assert script_params["occurrences_last_day"] == 3  # 2 + 1


class TestStatusCalculation:
    """Test status state machine"""

    def test_calculate_status_new_to_recurring(self, failure_store):
        """Test transition from new to recurring"""
        status = failure_store._calculate_status("new", occurrence_count=2, is_trending=False)
        assert status == "recurring"

    def test_calculate_status_recurring_to_trending(self, failure_store):
        """Test transition to trending"""
        status = failure_store._calculate_status("recurring", occurrence_count=10, is_trending=True)
        assert status == "trending"

    def test_calculate_status_preserves_ignored(self, failure_store):
        """Test that ignored status is preserved"""
        status = failure_store._calculate_status("ignored", occurrence_count=100, is_trending=True)
        assert status == "ignored"

    def test_calculate_status_preserves_resolved(self, failure_store):
        """Test that resolved status is preserved"""
        status = failure_store._calculate_status("resolved", occurrence_count=10, is_trending=False)
        assert status == "resolved"


class TestSeverityCalculation:
    """Test severity mapping"""

    def test_calculate_severity_critical(self, failure_store):
        """Test CRITICAL severity"""
        assert failure_store._calculate_severity({"level": "CRITICAL"}) == "CRITICAL"
        assert failure_store._calculate_severity({"level": "FATAL"}) == "CRITICAL"

    def test_calculate_severity_error(self, failure_store):
        """Test ERROR severity"""
        assert failure_store._calculate_severity({"level": "ERROR"}) == "ERROR"

    def test_calculate_severity_warning(self, failure_store):
        """Test WARNING severity"""
        assert failure_store._calculate_severity({"level": "WARNING"}) == "WARNING"
        assert failure_store._calculate_severity({"level": "WARN"}) == "WARNING"

    def test_calculate_severity_default(self, failure_store):
        """Test default severity for unknown levels"""
        assert failure_store._calculate_severity({"level": "INFO"}) == "ERROR"
        assert failure_store._calculate_severity({"level": ""}) == "ERROR"


class TestTagExtraction:
    """Test tag extraction from log entries"""

    def test_extract_tags_includes_container(self, failure_store, sample_log_entry, sample_fingerprint):
        """Test that container is included in tags"""
        tags = failure_store._extract_tags(sample_log_entry, sample_fingerprint)
        assert "orchestrator" in tags

    def test_extract_tags_includes_error_type(self, failure_store, sample_log_entry, sample_fingerprint):
        """Test that error type is included in tags"""
        tags = failure_store._extract_tags(sample_log_entry, sample_fingerprint)
        assert "KeyError" in tags

    def test_extract_tags_detects_context_based_tags(self, failure_store, sample_fingerprint):
        """Test context-based tag detection"""
        log_entry = {"message": "Agent task failed in pipeline"}
        tags = failure_store._extract_tags(log_entry, sample_fingerprint)

        assert "agent_execution" in tags
        assert "task_processing" in tags
        assert "pipeline" in tags

    def test_extract_tags_deduplicates(self, failure_store, sample_fingerprint):
        """Test that tags are deduplicated"""
        log_entry = {"message": "agent agent agent"}
        tags = failure_store._extract_tags(log_entry, sample_fingerprint)

        # Should only appear once
        assert tags.count("agent_execution") == 1


class TestCreateSample:
    """Test sample log entry creation"""

    def test_create_sample_includes_all_fields(self, failure_store, sample_log_entry, sample_container_info):
        """Test that sample includes all required fields"""
        sample = failure_store._create_sample(sample_log_entry, sample_container_info)

        assert "timestamp" in sample
        assert "container_id" in sample
        assert "container_name" in sample
        assert "raw_message" in sample
        assert "context" in sample

    def test_create_sample_preserves_raw_message(self, failure_store, sample_log_entry, sample_container_info):
        """Test that raw message is preserved"""
        sample = failure_store._create_sample(sample_log_entry, sample_container_info)
        assert sample["raw_message"] == sample_log_entry["message"]


class TestUpdateStatus:
    """Test manual status updates"""

    @pytest.mark.asyncio
    async def test_update_status_success(self, failure_store):
        """Test successful status update"""
        failure_store.es.update_by_query = Mock(return_value={"updated": 1})

        result = await failure_store.update_status("sha256:abc123", "ignored")

        assert result is True
        failure_store.es.update_by_query.assert_called_once()

    @pytest.mark.asyncio
    async def test_update_status_not_found(self, failure_store):
        """Test status update when signature not found"""
        failure_store.es.update_by_query = Mock(return_value={"updated": 0})

        result = await failure_store.update_status("sha256:notfound", "ignored")

        assert result is False


class TestGetIndexName:
    """Test index name generation"""

    def test_get_index_name_format(self, failure_store):
        """Test index name has correct format"""
        index_name = failure_store._get_index_name()

        assert index_name.startswith("medic-failure-signatures-")
        # Should end with YYYY.MM.DD
        assert len(index_name.split("-")[-1].split(".")) == 3


class TestCountOccurrencesSince:
    """Test time-windowed occurrence counting"""

    @pytest.mark.asyncio
    async def test_count_occurrences_since_filters_by_time(self, failure_store):
        """Test that occurrence counting filters by time window"""
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(hours=2)).isoformat().replace('+00:00', 'Z')
        recent_time = (now - timedelta(minutes=30)).isoformat().replace('+00:00', 'Z')

        signature = {
            "sample_log_entries": [
                {"timestamp": old_time},  # Outside 1 hour window
                {"timestamp": recent_time},  # Inside 1 hour window
                {"timestamp": now.isoformat().replace('+00:00', 'Z')},  # Inside window
            ]
        }

        count = await failure_store._count_occurrences_since(signature, hours=1)

        assert count == 2  # Only the 2 recent entries


class TestEdgeCases:
    """Test edge cases and error conditions"""

    @pytest.mark.asyncio
    async def test_get_signature_returns_none_when_not_found(self, failure_store):
        """Test getting non-existent signature"""
        failure_store.es.search = Mock(return_value={"hits": {"total": {"value": 0}, "hits": []}})

        result = await failure_store._get_signature("sha256:notfound")

        assert result is None

    @pytest.mark.asyncio
    async def test_create_signature_handles_es_error(self, failure_store, sample_fingerprint, sample_log_entry, sample_container_info):
        """Test that ES errors are handled gracefully"""
        failure_store._async_index = AsyncMock(side_effect=Exception("ES error"))

        # Should not raise exception
        await failure_store._create_signature(sample_fingerprint, sample_log_entry, sample_container_info)

    @pytest.mark.asyncio
    async def test_update_status_handles_es_error(self, failure_store):
        """Test that status update handles ES errors"""
        failure_store.es.update_by_query = Mock(side_effect=Exception("ES error"))

        result = await failure_store.update_status("sha256:abc123", "ignored")

        assert result is False

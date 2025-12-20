"""
Unit tests for redis_utils.
"""

import pytest
from unittest.mock import Mock, patch
from services.medic.shared.redis_utils import (
    acquire_lock,
    release_lock,
    get_investigation_status,
    set_investigation_status,
    get_queue_length,
    clear_keys_by_pattern
)


@pytest.fixture
def mock_redis_client():
    """Create a mock Redis client."""
    return Mock()


class TestAcquireLock:
    """Tests for acquire_lock function."""

    def test_acquire_lock_success(self, mock_redis_client):
        """Test successful lock acquisition."""
        mock_redis_client.set.return_value = True

        result = acquire_lock(mock_redis_client, "test:lock", ttl_seconds=60)

        assert result is True
        mock_redis_client.set.assert_called_once()
        # Verify nx=True and ex were passed
        call_args = mock_redis_client.set.call_args
        assert call_args[1]["nx"] is True
        assert call_args[1]["ex"] == 60

    def test_acquire_lock_timeout(self, mock_redis_client):
        """Test lock acquisition timeout."""
        mock_redis_client.set.return_value = False

        result = acquire_lock(
            mock_redis_client,
            "test:lock",
            ttl_seconds=60,
            timeout_seconds=0.3  # Short timeout but enough for multiple attempts
        )

        assert result is False
        # Should have tried multiple times (at least 2)
        assert mock_redis_client.set.call_count >= 2

    def test_acquire_lock_immediate_success(self, mock_redis_client):
        """Test immediate lock acquisition."""
        mock_redis_client.set.return_value = True

        result = acquire_lock(
            mock_redis_client,
            "test:lock",
            ttl_seconds=60,
            timeout_seconds=1
        )

        assert result is True
        # Should only call once if successful immediately
        assert mock_redis_client.set.call_count == 1


class TestReleaseLock:
    """Tests for release_lock function."""

    def test_release_lock_success(self, mock_redis_client):
        """Test successful lock release."""
        result = release_lock(mock_redis_client, "test:lock")

        assert result is True
        mock_redis_client.delete.assert_called_once_with("test:lock")

    def test_release_lock_exception_handling(self, mock_redis_client):
        """Test that exceptions are handled gracefully."""
        mock_redis_client.delete.side_effect = Exception("Redis error")

        result = release_lock(mock_redis_client, "test:lock")

        assert result is False


class TestGetInvestigationStatus:
    """Tests for get_investigation_status function."""

    def test_get_investigation_status_found(self, mock_redis_client):
        """Test getting status when it exists."""
        mock_redis_client.get.return_value = "in_progress"

        result = get_investigation_status(
            mock_redis_client,
            "medic:docker_investigation",
            "fp123"
        )

        assert result == "in_progress"
        mock_redis_client.get.assert_called_once_with(
            "medic:docker_investigation:fp123:status"
        )

    def test_get_investigation_status_not_found(self, mock_redis_client):
        """Test getting status when it doesn't exist."""
        mock_redis_client.get.return_value = None

        result = get_investigation_status(
            mock_redis_client,
            "medic:docker_investigation",
            "fp123"
        )

        assert result is None

    def test_get_investigation_status_exception_handling(self, mock_redis_client):
        """Test that exceptions are handled gracefully."""
        mock_redis_client.get.side_effect = Exception("Redis error")

        result = get_investigation_status(
            mock_redis_client,
            "medic:docker_investigation",
            "fp123"
        )

        assert result is None


class TestSetInvestigationStatus:
    """Tests for set_investigation_status function."""

    def test_set_investigation_status_without_ttl(self, mock_redis_client):
        """Test setting status without TTL."""
        result = set_investigation_status(
            mock_redis_client,
            "medic:docker_investigation",
            "fp123",
            "in_progress"
        )

        assert result is True
        mock_redis_client.set.assert_called_once_with(
            "medic:docker_investigation:fp123:status",
            "in_progress"
        )

    def test_set_investigation_status_with_ttl(self, mock_redis_client):
        """Test setting status with TTL."""
        result = set_investigation_status(
            mock_redis_client,
            "medic:docker_investigation",
            "fp123",
            "completed",
            ttl_seconds=3600
        )

        assert result is True
        mock_redis_client.setex.assert_called_once_with(
            "medic:docker_investigation:fp123:status",
            3600,
            "completed"
        )

    def test_set_investigation_status_exception_handling(self, mock_redis_client):
        """Test that exceptions are handled gracefully."""
        mock_redis_client.set.side_effect = Exception("Redis error")

        result = set_investigation_status(
            mock_redis_client,
            "medic:docker_investigation",
            "fp123",
            "failed"
        )

        assert result is False


class TestGetQueueLength:
    """Tests for get_queue_length function."""

    def test_get_queue_length_success(self, mock_redis_client):
        """Test getting queue length."""
        mock_redis_client.llen.return_value = 5

        result = get_queue_length(mock_redis_client, "medic:docker:queue")

        assert result == 5
        mock_redis_client.llen.assert_called_once_with("medic:docker:queue")

    def test_get_queue_length_empty_queue(self, mock_redis_client):
        """Test getting length of empty queue."""
        mock_redis_client.llen.return_value = 0

        result = get_queue_length(mock_redis_client, "medic:docker:queue")

        assert result == 0

    def test_get_queue_length_exception_handling(self, mock_redis_client):
        """Test that exceptions are handled gracefully."""
        mock_redis_client.llen.side_effect = Exception("Redis error")

        result = get_queue_length(mock_redis_client, "medic:docker:queue")

        assert result == 0


class TestClearKeysByPattern:
    """Tests for clear_keys_by_pattern function."""

    def test_clear_keys_by_pattern_success(self, mock_redis_client):
        """Test clearing keys matching pattern."""
        mock_redis_client.keys.return_value = ["key1", "key2", "key3"]
        mock_redis_client.delete.return_value = 3

        result = clear_keys_by_pattern(mock_redis_client, "medic:investigation:*")

        assert result == 3
        mock_redis_client.keys.assert_called_once_with("medic:investigation:*")
        mock_redis_client.delete.assert_called_once_with("key1", "key2", "key3")

    def test_clear_keys_by_pattern_no_matches(self, mock_redis_client):
        """Test clearing when no keys match pattern."""
        mock_redis_client.keys.return_value = []

        result = clear_keys_by_pattern(mock_redis_client, "medic:investigation:*")

        assert result == 0
        mock_redis_client.delete.assert_not_called()

    def test_clear_keys_by_pattern_exception_handling(self, mock_redis_client):
        """Test that exceptions are handled gracefully."""
        mock_redis_client.keys.side_effect = Exception("Redis error")

        result = clear_keys_by_pattern(mock_redis_client, "medic:investigation:*")

        assert result == 0

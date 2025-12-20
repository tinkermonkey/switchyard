"""
Unit tests for BaseInvestigationQueue.

Tests the concrete queue implementation with parameterized key prefixes.
"""

import pytest
from unittest.mock import Mock, MagicMock, call
from datetime import datetime, timezone

from services.medic.base.base_investigation_queue import BaseInvestigationQueue


@pytest.fixture
def mock_redis():
    """Create a mock Redis client."""
    return Mock()


@pytest.fixture
def docker_queue(mock_redis):
    """Create a Docker investigation queue."""
    return BaseInvestigationQueue(mock_redis, "medic:docker_investigation")


@pytest.fixture
def claude_queue(mock_redis):
    """Create a Claude investigation queue."""
    return BaseInvestigationQueue(mock_redis, "medic:claude_investigation")


class TestInitialization:
    """Tests for queue initialization."""

    def test_initialization_with_docker_prefix(self, mock_redis):
        """Test initialization with Docker prefix."""
        queue = BaseInvestigationQueue(mock_redis, "medic:docker_investigation")

        assert queue.redis == mock_redis
        assert queue.KEY_PREFIX == "medic:docker_investigation"
        assert queue.QUEUE_KEY == "medic:docker_investigation:queue"
        assert queue.ACTIVE_SET_KEY == "medic:docker_investigation:active"

    def test_initialization_with_claude_prefix(self, mock_redis):
        """Test initialization with Claude prefix."""
        queue = BaseInvestigationQueue(mock_redis, "medic:claude_investigation")

        assert queue.redis == mock_redis
        assert queue.KEY_PREFIX == "medic:claude_investigation"
        assert queue.QUEUE_KEY == "medic:claude_investigation:queue"
        assert queue.ACTIVE_SET_KEY == "medic:claude_investigation:active"


class TestEnqueue:
    """Tests for enqueue method."""

    def test_enqueue_new_investigation(self, docker_queue, mock_redis):
        """Test enqueueing a new investigation."""
        mock_redis.get.return_value = None  # No existing status

        result = docker_queue.enqueue("fp123", priority="normal")

        assert result is True
        mock_redis.set.assert_called_once_with(
            "medic:docker_investigation:fp123:status",
            docker_queue.STATUS_QUEUED
        )
        mock_redis.rpush.assert_called_once_with(
            "medic:docker_investigation:queue",
            "fp123"
        )

    def test_enqueue_high_priority(self, docker_queue, mock_redis):
        """Test enqueueing with high priority."""
        mock_redis.get.return_value = None

        result = docker_queue.enqueue("fp123", priority="high")

        assert result is True
        mock_redis.lpush.assert_called_once_with(
            "medic:docker_investigation:queue",
            "fp123"
        )

    def test_enqueue_already_queued(self, docker_queue, mock_redis):
        """Test enqueueing when already queued."""
        mock_redis.get.return_value = docker_queue.STATUS_QUEUED

        result = docker_queue.enqueue("fp123")

        assert result is False
        mock_redis.rpush.assert_not_called()
        mock_redis.lpush.assert_not_called()

    def test_enqueue_already_in_progress(self, docker_queue, mock_redis):
        """Test enqueueing when already in progress."""
        mock_redis.get.return_value = docker_queue.STATUS_IN_PROGRESS

        result = docker_queue.enqueue("fp123")

        assert result is False


class TestDequeue:
    """Tests for dequeue method."""

    def test_dequeue_success(self, docker_queue, mock_redis):
        """Test successful dequeue."""
        mock_redis.blpop.return_value = ("medic:docker_investigation:queue", "fp123")

        result = docker_queue.dequeue()

        assert result == "fp123"
        mock_redis.blpop.assert_called_once_with(
            "medic:docker_investigation:queue",
            timeout=5
        )

    def test_dequeue_empty_queue(self, docker_queue, mock_redis):
        """Test dequeue on empty queue."""
        mock_redis.blpop.return_value = None

        result = docker_queue.dequeue()

        assert result is None


class TestStatus:
    """Tests for status management."""

    def test_get_status(self, docker_queue, mock_redis):
        """Test getting status."""
        mock_redis.get.return_value = "in_progress"

        status = docker_queue.get_status("fp123")

        assert status == "in_progress"
        mock_redis.get.assert_called_once_with(
            "medic:docker_investigation:fp123:status"
        )

    def test_get_status_none(self, docker_queue, mock_redis):
        """Test getting status when it doesn't exist."""
        mock_redis.get.return_value = None

        status = docker_queue.get_status("fp123")

        assert status is None

    def test_update_status(self, docker_queue, mock_redis):
        """Test updating status."""
        docker_queue.update_status("fp123", "in_progress")

        mock_redis.set.assert_called_once_with(
            "medic:docker_investigation:fp123:status",
            "in_progress"
        )


class TestMarkStarted:
    """Tests for mark_started method."""

    def test_mark_started(self, docker_queue, mock_redis):
        """Test marking investigation as started."""
        docker_queue.mark_started("fp123", 12345)

        # Should set pid
        set_calls = mock_redis.set.call_args_list
        assert any("fp123:pid" in str(call) and "12345" in str(call) for call in set_calls)

        # Should set status to in_progress
        assert any("fp123:status" in str(call) and "in_progress" in str(call) for call in set_calls)

        # Should add to active set
        mock_redis.sadd.assert_called_once_with(
            "medic:docker_investigation:active",
            "fp123"
        )


class TestHeartbeat:
    """Tests for heartbeat updates."""

    def test_update_heartbeat(self, docker_queue, mock_redis):
        """Test updating heartbeat."""
        docker_queue.update_heartbeat("fp123", agent_output_lines=100)

        # Should update last_heartbeat
        set_calls = mock_redis.set.call_args_list
        assert any("fp123:last_heartbeat" in str(call) for call in set_calls)

        # Should update agent_output_lines
        assert any("fp123:agent_output_lines" in str(call) and "100" in str(call) for call in set_calls)

    def test_update_heartbeat_no_lines(self, docker_queue, mock_redis):
        """Test updating heartbeat without line count."""
        docker_queue.update_heartbeat("fp123")

        # Should only update last_heartbeat, not agent_output_lines
        set_calls = mock_redis.set.call_args_list
        assert len(set_calls) == 1  # Only heartbeat update


class TestMarkCompleted:
    """Tests for mark_completed method."""

    def test_mark_completed_success(self, docker_queue, mock_redis):
        """Test marking investigation as completed with success."""
        docker_queue.mark_completed("fp123", docker_queue.RESULT_SUCCESS)

        # Should set status to completed
        set_calls = mock_redis.set.call_args_list
        assert any("fp123:status" in str(call) and docker_queue.STATUS_COMPLETED in str(call)
                  for call in set_calls)

        # Should set result
        assert any("fp123:result" in str(call) and docker_queue.RESULT_SUCCESS in str(call)
                  for call in set_calls)

        # Should remove from active set
        mock_redis.srem.assert_called_once_with(
            "medic:docker_investigation:active",
            "fp123"
        )

    def test_mark_completed_ignored(self, docker_queue, mock_redis):
        """Test marking investigation as ignored."""
        docker_queue.mark_completed("fp123", docker_queue.RESULT_IGNORED)

        # Should set status to ignored
        set_calls = mock_redis.set.call_args_list
        assert any("fp123:status" in str(call) and docker_queue.STATUS_IGNORED in str(call)
                  for call in set_calls)

    def test_mark_completed_timeout(self, docker_queue, mock_redis):
        """Test marking investigation as timeout."""
        docker_queue.mark_completed("fp123", docker_queue.RESULT_TIMEOUT)

        # Should set status to timeout
        set_calls = mock_redis.set.call_args_list
        assert any("fp123:status" in str(call) and docker_queue.STATUS_TIMEOUT in str(call)
                  for call in set_calls)


class TestActiveManagement:
    """Tests for active investigation management."""

    def test_get_active_count(self, docker_queue, mock_redis):
        """Test getting active count."""
        mock_redis.scard.return_value = 5

        count = docker_queue.get_active_count()

        assert count == 5
        mock_redis.scard.assert_called_once_with("medic:docker_investigation:active")

    def test_get_all_active(self, docker_queue, mock_redis):
        """Test getting all active investigations."""
        mock_redis.smembers.return_value = {"fp1", "fp2", "fp3"}

        active = docker_queue.get_all_active()

        assert len(active) == 3
        assert "fp1" in active
        assert "fp2" in active
        assert "fp3" in active

    def test_get_all_active_empty(self, docker_queue, mock_redis):
        """Test getting all active when none exist."""
        mock_redis.smembers.return_value = None

        active = docker_queue.get_all_active()

        assert active == []


class TestQueueManagement:
    """Tests for queue management."""

    def test_get_queue_length(self, docker_queue, mock_redis):
        """Test getting queue length."""
        mock_redis.llen.return_value = 10

        length = docker_queue.get_queue_length()

        assert length == 10
        mock_redis.llen.assert_called_once_with("medic:docker_investigation:queue")


class TestInvestigationInfo:
    """Tests for get_investigation_info method."""

    def test_get_investigation_info(self, docker_queue, mock_redis):
        """Test getting full investigation info."""
        mock_redis.get.side_effect = lambda key: {
            "medic:docker_investigation:fp123:status": "in_progress",
            "medic:docker_investigation:fp123:pid": "12345",
            "medic:docker_investigation:fp123:started_at": "2025-01-01T00:00:00Z",
            "medic:docker_investigation:fp123:last_heartbeat": "2025-01-01T00:10:00Z",
            "medic:docker_investigation:fp123:agent_output_lines": "100",
            "medic:docker_investigation:fp123:result": None,
            "medic:docker_investigation:fp123:completed_at": None,
        }.get(key)

        info = docker_queue.get_investigation_info("fp123")

        assert info["fingerprint_id"] == "fp123"
        assert info["status"] == "in_progress"
        assert info["pid"] == "12345"
        assert info["started_at"] == "2025-01-01T00:00:00Z"
        assert info["last_heartbeat"] == "2025-01-01T00:10:00Z"
        assert info["agent_output_lines"] == "100"


class TestCleanup:
    """Tests for cleanup methods."""

    def test_cleanup_investigation(self, docker_queue, mock_redis):
        """Test cleaning up investigation keys."""
        docker_queue.cleanup_investigation("fp123")

        # Should delete all keys
        assert mock_redis.delete.call_count >= 7  # At least 7 keys

        # Should remove from active set
        mock_redis.srem.assert_called_once_with(
            "medic:docker_investigation:active",
            "fp123"
        )

    def test_cleanup_orphaned_keys(self, docker_queue, mock_redis):
        """Test cleaning up orphaned keys."""
        fingerprint_ids = ["fp1", "fp2", "fp3"]

        count = docker_queue.cleanup_orphaned_keys(fingerprint_ids)

        assert count == 3
        # Should call cleanup_investigation for each
        assert mock_redis.srem.call_count == 3


class TestKeyPrefixIsolation:
    """Tests that Docker and Claude queues are isolated."""

    def test_different_queue_keys(self, mock_redis):
        """Test that Docker and Claude queues use different keys."""
        docker_queue = BaseInvestigationQueue(mock_redis, "medic:docker_investigation")
        claude_queue = BaseInvestigationQueue(mock_redis, "medic:claude_investigation")

        assert docker_queue.QUEUE_KEY != claude_queue.QUEUE_KEY
        assert docker_queue.ACTIVE_SET_KEY != claude_queue.ACTIVE_SET_KEY

    def test_isolated_operations(self, mock_redis):
        """Test that operations on different queues are isolated."""
        docker_queue = BaseInvestigationQueue(mock_redis, "medic:docker_investigation")
        claude_queue = BaseInvestigationQueue(mock_redis, "medic:claude_investigation")

        # Enqueue on Docker queue
        docker_queue.enqueue("fp123")

        # Should use Docker queue key, not Claude
        calls = [str(call) for call in mock_redis.rpush.call_args_list]
        assert any("medic:docker_investigation:queue" in call for call in calls)
        assert not any("medic:claude_investigation:queue" in call for call in calls)

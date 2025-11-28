"""
Unit tests for Medic Investigation Queue
"""

import pytest
from unittest.mock import Mock, MagicMock
from datetime import datetime, timezone, timedelta
import time

from services.medic.investigation_queue import InvestigationQueue


@pytest.fixture
def mock_redis():
    """Mock Redis client"""
    redis_mock = Mock()
    redis_mock.set = Mock(return_value=True)
    redis_mock.get = Mock(return_value=None)
    redis_mock.delete = Mock(return_value=1)
    redis_mock.lpush = Mock(return_value=1)
    redis_mock.rpush = Mock(return_value=1)
    redis_mock.blpop = Mock(return_value=None)
    redis_mock.sadd = Mock(return_value=1)
    redis_mock.srem = Mock(return_value=1)
    redis_mock.smembers = Mock(return_value=set())
    redis_mock.llen = Mock(return_value=0)
    return redis_mock


@pytest.fixture
def queue(mock_redis):
    """Create investigation queue with mocked Redis"""
    return InvestigationQueue(mock_redis)


@pytest.fixture
def sample_fingerprint_id():
    """Sample fingerprint ID"""
    return "sha256:abc123def456"


class TestInvestigationQueueInit:
    """Test queue initialization"""

    def test_init(self, mock_redis):
        """Test queue initialization"""
        queue = InvestigationQueue(mock_redis)
        assert queue.redis == mock_redis


class TestEnqueueDequeue:
    """Test enqueue and dequeue operations"""

    def test_enqueue_new_investigation(self, queue, mock_redis, sample_fingerprint_id):
        """Test enqueuing new investigation"""
        # Mock: no existing status
        mock_redis.get.return_value = None

        result = queue.enqueue(sample_fingerprint_id, priority="normal")

        assert result is True
        mock_redis.set.assert_called()
        mock_redis.rpush.assert_called_with(
            "medic:investigation:queue", sample_fingerprint_id
        )

    def test_enqueue_with_high_priority(self, queue, mock_redis, sample_fingerprint_id):
        """Test enqueuing with high priority uses lpush"""
        mock_redis.get.return_value = None

        queue.enqueue(sample_fingerprint_id, priority="high")

        mock_redis.lpush.assert_called_with(
            "medic:investigation:queue", sample_fingerprint_id
        )

    def test_enqueue_already_queued(self, queue, mock_redis, sample_fingerprint_id):
        """Test enqueuing investigation that's already queued"""
        # Mock: already queued
        mock_redis.get.return_value = b"queued"

        result = queue.enqueue(sample_fingerprint_id)

        assert result is False
        # Should not add to queue again
        assert not mock_redis.rpush.called

    def test_enqueue_already_in_progress(self, queue, mock_redis, sample_fingerprint_id):
        """Test enqueuing investigation that's already running"""
        mock_redis.get.return_value = b"in_progress"

        result = queue.enqueue(sample_fingerprint_id)

        assert result is False

    def test_dequeue_empty_queue(self, queue, mock_redis):
        """Test dequeuing from empty queue"""
        mock_redis.blpop.return_value = None

        result = queue.dequeue()

        assert result is None
        mock_redis.blpop.assert_called_with("medic:investigation:queue", timeout=5)

    def test_dequeue_with_item(self, queue, mock_redis, sample_fingerprint_id):
        """Test dequeuing item from queue"""
        mock_redis.blpop.return_value = (
            b"medic:investigation:queue",
            sample_fingerprint_id.encode("utf-8"),
        )

        result = queue.dequeue()

        assert result == sample_fingerprint_id


class TestLocking:
    """Test distributed locking"""

    def test_acquire_lock_success(self, queue, mock_redis, sample_fingerprint_id):
        """Test successfully acquiring lock"""
        mock_redis.set.return_value = True

        result = queue.acquire_lock(sample_fingerprint_id)

        assert result is True
        # Check set called with NX and EX
        call_args = mock_redis.set.call_args
        assert call_args[1]["nx"] is True
        assert call_args[1]["ex"] == InvestigationQueue.LOCK_TTL

    def test_acquire_lock_already_locked(self, queue, mock_redis, sample_fingerprint_id):
        """Test acquiring lock when already locked"""
        mock_redis.set.return_value = False

        result = queue.acquire_lock(sample_fingerprint_id)

        assert result is False

    def test_release_lock(self, queue, mock_redis, sample_fingerprint_id):
        """Test releasing lock"""
        queue.release_lock(sample_fingerprint_id)

        lock_key = f"medic:investigation:{sample_fingerprint_id}:lock"
        mock_redis.delete.assert_called_with(lock_key)


class TestStatusManagement:
    """Test status tracking"""

    def test_update_status(self, queue, mock_redis, sample_fingerprint_id):
        """Test updating investigation status"""
        queue.update_status(sample_fingerprint_id, InvestigationQueue.STATUS_IN_PROGRESS)

        status_key = f"medic:investigation:{sample_fingerprint_id}:status"
        mock_redis.set.assert_called_with(status_key, InvestigationQueue.STATUS_IN_PROGRESS)

    def test_get_status_exists(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting status when it exists"""
        mock_redis.get.return_value = b"in_progress"

        status = queue.get_status(sample_fingerprint_id)

        assert status == "in_progress"

    def test_get_status_not_exists(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting status when it doesn't exist"""
        mock_redis.get.return_value = None

        status = queue.get_status(sample_fingerprint_id)

        assert status is None


class TestProcessTracking:
    """Test process ID tracking"""

    def test_set_pid(self, queue, mock_redis, sample_fingerprint_id):
        """Test setting process ID"""
        queue.set_pid(sample_fingerprint_id, 12345)

        pid_key = f"medic:investigation:{sample_fingerprint_id}:pid"
        mock_redis.set.assert_called_with(pid_key, "12345")
        mock_redis.sadd.assert_called_with("medic:investigation:active", sample_fingerprint_id)

    def test_get_pid_exists(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting PID when it exists"""
        mock_redis.get.return_value = b"12345"

        pid = queue.get_pid(sample_fingerprint_id)

        assert pid == 12345

    def test_get_pid_not_exists(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting PID when it doesn't exist"""
        mock_redis.get.return_value = None

        pid = queue.get_pid(sample_fingerprint_id)

        assert pid is None


class TestTimestampTracking:
    """Test timestamp management"""

    def test_mark_started(self, queue, mock_redis, sample_fingerprint_id):
        """Test marking investigation as started"""
        queue.mark_started(sample_fingerprint_id)

        # Should set started_at, last_heartbeat, and status
        assert mock_redis.set.call_count >= 3

    def test_get_started_at(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting start timestamp"""
        now = datetime.now(timezone.utc).isoformat()
        mock_redis.get.return_value = now.encode("utf-8")

        started_at = queue.get_started_at(sample_fingerprint_id)

        assert started_at is not None
        assert isinstance(started_at, datetime)

    def test_update_heartbeat(self, queue, mock_redis, sample_fingerprint_id):
        """Test updating heartbeat"""
        queue.update_heartbeat(sample_fingerprint_id)

        heartbeat_key = f"medic:investigation:{sample_fingerprint_id}:last_heartbeat"
        mock_redis.set.assert_called()

    def test_get_last_heartbeat(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting last heartbeat"""
        now = datetime.now(timezone.utc).isoformat()
        mock_redis.get.return_value = now.encode("utf-8")

        heartbeat = queue.get_last_heartbeat(sample_fingerprint_id)

        assert heartbeat is not None
        assert isinstance(heartbeat, datetime)


class TestOutputTracking:
    """Test output line tracking"""

    def test_set_output_lines(self, queue, mock_redis, sample_fingerprint_id):
        """Test setting output line count"""
        queue.set_output_lines(sample_fingerprint_id, 100)

        lines_key = f"medic:investigation:{sample_fingerprint_id}:agent_output_lines"
        mock_redis.set.assert_called_with(lines_key, "100")

    def test_get_output_lines_exists(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting output lines when they exist"""
        mock_redis.get.return_value = b"100"

        lines = queue.get_output_lines(sample_fingerprint_id)

        assert lines == 100

    def test_get_output_lines_not_exists(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting output lines when they don't exist"""
        mock_redis.get.return_value = None

        lines = queue.get_output_lines(sample_fingerprint_id)

        assert lines == 0


class TestCompletion:
    """Test investigation completion"""

    def test_mark_completed_success(self, queue, mock_redis, sample_fingerprint_id):
        """Test marking investigation as completed successfully"""
        queue.mark_completed(sample_fingerprint_id, InvestigationQueue.RESULT_SUCCESS)

        # Should set completed_at, result, status
        assert mock_redis.set.call_count >= 3

        # Should remove from active set
        mock_redis.srem.assert_called_with("medic:investigation:active", sample_fingerprint_id)

        # Should release lock
        mock_redis.delete.assert_called()

    def test_mark_completed_with_error(self, queue, mock_redis, sample_fingerprint_id):
        """Test marking investigation as completed with error"""
        queue.mark_completed(
            sample_fingerprint_id,
            InvestigationQueue.RESULT_FAILED,
            error_message="Something went wrong",
        )

        # Should also set error message
        assert mock_redis.set.call_count >= 4

    def test_mark_completed_sets_correct_status(self, queue, mock_redis, sample_fingerprint_id):
        """Test that completion sets correct status based on result"""
        queue.mark_completed(sample_fingerprint_id, InvestigationQueue.RESULT_SUCCESS)

        # Find the status set call
        calls = [call for call in mock_redis.set.call_args_list if ":status" in str(call)]
        assert len(calls) > 0


class TestInvestigationInfo:
    """Test getting investigation information"""

    def test_get_investigation_info(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting complete investigation info"""
        # Mock various get calls
        def mock_get_side_effect(key):
            if ":status" in key:
                return b"in_progress"
            elif ":pid" in key:
                return b"12345"
            elif ":started_at" in key:
                return datetime.now(timezone.utc).isoformat().encode("utf-8")
            elif ":agent_output_lines" in key:
                return b"50"
            return None

        mock_redis.get.side_effect = mock_get_side_effect

        info = queue.get_investigation_info(sample_fingerprint_id)

        assert info["fingerprint_id"] == sample_fingerprint_id
        assert info["status"] == "in_progress"
        assert info["pid"] == 12345
        assert info["started_at"] is not None
        assert info["output_lines"] == 50


class TestActiveManagement:
    """Test active investigation management"""

    def test_get_all_active(self, queue, mock_redis):
        """Test getting all active investigations"""
        mock_redis.smembers.return_value = {
            b"sha256:abc123",
            b"sha256:def456",
        }

        active = queue.get_all_active()

        assert len(active) == 2
        assert "sha256:abc123" in active
        assert "sha256:def456" in active

    def test_get_queue_length(self, queue, mock_redis):
        """Test getting queue length"""
        mock_redis.llen.return_value = 5

        length = queue.get_queue_length()

        assert length == 5
        mock_redis.llen.assert_called_with("medic:investigation:queue")


class TestStalledAndTimeout:
    """Test stalled and timeout detection"""

    def test_check_stalled_with_no_heartbeat(self, queue, mock_redis, sample_fingerprint_id):
        """Test stall detection when no heartbeat exists"""
        mock_redis.get.return_value = None

        is_stalled = queue.check_stalled(sample_fingerprint_id)

        assert is_stalled is True

    def test_check_stalled_recent_heartbeat(self, queue, mock_redis, sample_fingerprint_id):
        """Test stall detection with recent heartbeat"""
        recent = datetime.now(timezone.utc).isoformat()
        mock_redis.get.return_value = recent.encode("utf-8")

        is_stalled = queue.check_stalled(sample_fingerprint_id)

        assert is_stalled is False

    def test_check_stalled_old_heartbeat(self, queue, mock_redis, sample_fingerprint_id):
        """Test stall detection with old heartbeat"""
        old = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat()
        mock_redis.get.return_value = old.encode("utf-8")

        is_stalled = queue.check_stalled(sample_fingerprint_id)

        assert is_stalled is True

    def test_check_timeout_not_started(self, queue, mock_redis, sample_fingerprint_id):
        """Test timeout check when not started"""
        mock_redis.get.return_value = None

        is_timeout = queue.check_timeout(sample_fingerprint_id)

        assert is_timeout is False

    def test_check_timeout_within_limit(self, queue, mock_redis, sample_fingerprint_id):
        """Test timeout check within time limit"""
        recent = datetime.now(timezone.utc).isoformat()
        mock_redis.get.return_value = recent.encode("utf-8")

        is_timeout = queue.check_timeout(sample_fingerprint_id)

        assert is_timeout is False

    def test_check_timeout_exceeded(self, queue, mock_redis, sample_fingerprint_id):
        """Test timeout check when limit exceeded"""
        old = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        mock_redis.get.return_value = old.encode("utf-8")

        is_timeout = queue.check_timeout(sample_fingerprint_id)

        assert is_timeout is True


class TestCleanup:
    """Test investigation cleanup"""

    def test_cleanup_investigation(self, queue, mock_redis, sample_fingerprint_id):
        """Test cleaning up investigation data"""
        queue.cleanup_investigation(sample_fingerprint_id)

        # Should delete multiple keys
        assert mock_redis.delete.called

        # Should remove from active set
        mock_redis.srem.assert_called_with("medic:investigation:active", sample_fingerprint_id)


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_enqueue_with_invalid_priority(self, queue, mock_redis, sample_fingerprint_id):
        """Test enqueue still works with invalid priority (treats as normal)"""
        mock_redis.get.return_value = None

        # Should not crash
        result = queue.enqueue(sample_fingerprint_id, priority="invalid")

        # Should still enqueue (defaults to rpush)
        assert result is True

    def test_get_started_at_invalid_format(self, queue, mock_redis, sample_fingerprint_id):
        """Test getting started_at with invalid timestamp"""
        mock_redis.get.return_value = b"invalid-timestamp"

        # Should handle gracefully (may raise or return None)
        try:
            started_at = queue.get_started_at(sample_fingerprint_id)
        except Exception:
            pass  # Expected to possibly fail

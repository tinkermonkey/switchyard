"""
Unit tests for Medic Investigation Recovery
"""

import pytest
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timezone, timedelta

from services.medic.investigation_recovery import InvestigationRecovery
from services.medic.investigation_queue import InvestigationQueue


@pytest.fixture
def mock_queue():
    """Mock investigation queue"""
    queue = Mock(spec=InvestigationQueue)
    queue.get_all_active = Mock(return_value=[])
    queue.get_investigation_info = Mock(return_value={})
    queue.acquire_lock = Mock(return_value=True)
    queue.release_lock = Mock()
    queue.set_pid = Mock()
    queue.update_status = Mock()
    queue.update_heartbeat = Mock()
    queue.mark_completed = Mock()
    return queue


@pytest.fixture
def mock_agent_runner():
    """Mock agent runner"""
    runner = Mock()
    runner.check_process = Mock(return_value=False)
    runner.launch_investigation = Mock(return_value=None)
    runner.terminate_process = Mock(return_value=True)
    return runner


@pytest.fixture
def mock_report_manager():
    """Mock report manager"""
    manager = Mock()
    manager.get_report_status = Mock(return_value={
        "has_diagnosis": False,
        "has_fix_plan": False,
        "has_ignored": False,
    })
    manager.get_report_dir = Mock()
    return manager


@pytest.fixture
def recovery(mock_queue, mock_agent_runner, mock_report_manager):
    """Create recovery manager with mocks"""
    return InvestigationRecovery(mock_queue, mock_agent_runner, mock_report_manager)


@pytest.fixture
def sample_fingerprint_id():
    """Sample fingerprint ID"""
    return "sha256:abc123def456"


class TestRecoveryInit:
    """Test recovery initialization"""

    def test_init(self, mock_queue, mock_agent_runner, mock_report_manager):
        """Test recovery initialization"""
        recovery = InvestigationRecovery(mock_queue, mock_agent_runner, mock_report_manager)
        assert recovery.queue == mock_queue
        assert recovery.agent_runner == mock_agent_runner
        assert recovery.report_manager == mock_report_manager


class TestRecoverAll:
    """Test recovering all investigations"""

    def test_recover_all_empty(self, recovery, mock_queue):
        """Test recovery when no active investigations"""
        mock_queue.get_all_active.return_value = []

        stats = recovery.recover_all()

        assert stats["recovered"] == 0
        assert stats["completed"] == 0
        assert stats["failed"] == 0

    def test_recover_all_with_multiple(self, recovery, mock_queue):
        """Test recovery with multiple investigations"""
        mock_queue.get_all_active.return_value = [
            "sha256:abc123",
            "sha256:def456",
        ]

        # Mock each recovery to return different result
        with patch.object(recovery, 'recover_investigation', side_effect=["recovered", "completed"]):
            stats = recovery.recover_all()

        assert stats["recovered"] == 1
        assert stats["completed"] == 1


class TestRecoverInvestigation:
    """Test recovering individual investigation"""

    def test_recover_process_still_running(
        self, recovery, mock_queue, mock_agent_runner, sample_fingerprint_id
    ):
        """Test recovery when process is still running"""
        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": 12345,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        mock_agent_runner.check_process.return_value = True

        result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "recovered"
        # Should not modify anything
        mock_queue.mark_completed.assert_not_called()

    def test_recover_process_dead_with_reports(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test recovery when process dead but reports exist"""
        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": 12345,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        mock_agent_runner.check_process.return_value = False
        mock_report_manager.get_report_status.return_value = {
            "has_diagnosis": True,
            "has_fix_plan": True,
            "has_ignored": False,
        }

        result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "completed"
        mock_queue.mark_completed.assert_called_once()

    def test_recover_process_dead_ignored_report(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test recovery with ignored report"""
        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": 12345,
            "started_at": datetime.now(timezone.utc).isoformat(),
        }
        mock_agent_runner.check_process.return_value = False
        mock_report_manager.get_report_status.return_value = {
            "has_diagnosis": False,
            "has_fix_plan": False,
            "has_ignored": True,
        }

        result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "completed"
        # Should mark as ignored
        call_args = mock_queue.mark_completed.call_args[0]
        assert call_args[1] == InvestigationQueue.RESULT_IGNORED

    def test_recover_process_dead_no_reports_recently_started(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test recovery when recently started with no reports"""
        # Started 10 minutes ago (< 30min threshold)
        started_at = datetime.now(timezone.utc) - timedelta(minutes=10)

        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": 12345,
            "started_at": started_at.isoformat(),
        }
        mock_agent_runner.check_process.return_value = False
        mock_report_manager.get_report_status.return_value = {
            "has_diagnosis": False,
            "has_fix_plan": False,
            "has_ignored": False,
        }

        result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "waiting"
        # Should not mark as completed
        mock_queue.mark_completed.assert_not_called()

    def test_recover_process_dead_no_reports_timeout(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test recovery when timed out"""
        # Started 5 hours ago (> 4hr timeout)
        started_at = datetime.now(timezone.utc) - timedelta(hours=5)

        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": 12345,
            "started_at": started_at.isoformat(),
        }
        mock_agent_runner.check_process.return_value = False
        mock_report_manager.get_report_status.return_value = {
            "has_diagnosis": False,
            "has_fix_plan": False,
            "has_ignored": False,
        }

        result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "timeout"
        # Should mark as timeout
        call_args = mock_queue.mark_completed.call_args[0]
        assert call_args[1] == InvestigationQueue.RESULT_TIMEOUT

    def test_recover_process_dead_no_reports_relaunch_window(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test recovery when in relaunch window (30min - 4hr)"""
        # Started 2 hours ago (in relaunch window)
        started_at = datetime.now(timezone.utc) - timedelta(hours=2)

        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": 12345,
            "started_at": started_at.isoformat(),
        }
        mock_agent_runner.check_process.return_value = False
        mock_report_manager.get_report_status.return_value = {
            "has_diagnosis": False,
            "has_fix_plan": False,
            "has_ignored": False,
        }

        # Mock successful relaunch
        with patch.object(recovery, '_relaunch_investigation', return_value=True):
            result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "relaunched"

    def test_recover_no_start_time(
        self, recovery, mock_queue, mock_agent_runner, sample_fingerprint_id
    ):
        """Test recovery when no start time recorded"""
        mock_queue.get_investigation_info.return_value = {
            "status": "in_progress",
            "pid": None,
            "started_at": None,
        }

        result = recovery.recover_investigation(sample_fingerprint_id)

        assert result == "failed"
        mock_queue.mark_completed.assert_called_once()


class TestRelaunchInvestigation:
    """Test relaunching investigations"""

    def test_relaunch_success(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test successful relaunch"""
        # Mock successful launch
        mock_process = Mock()
        mock_process.pid = 67890
        mock_agent_runner.launch_investigation.return_value = mock_process

        # Mock context file exists
        context_file = Mock()
        context_file.exists.return_value = True
        mock_report_dir = Mock()
        mock_report_dir.__truediv__ = Mock(return_value=context_file)
        mock_report_manager.get_report_dir.return_value = mock_report_dir
        mock_report_manager.get_investigation_log_path.return_value = "/medic/test/log.txt"

        result = recovery._relaunch_investigation(sample_fingerprint_id)

        assert result is True
        mock_queue.set_pid.assert_called_with(sample_fingerprint_id, 67890)
        mock_queue.update_status.assert_called()

    def test_relaunch_lock_failed(
        self, recovery, mock_queue, sample_fingerprint_id
    ):
        """Test relaunch when lock cannot be acquired"""
        mock_queue.acquire_lock.return_value = False

        result = recovery._relaunch_investigation(sample_fingerprint_id)

        assert result is False
        mock_queue.release_lock.assert_not_called()

    def test_relaunch_context_missing(
        self, recovery, mock_queue, mock_report_manager, sample_fingerprint_id
    ):
        """Test relaunch when context file missing"""
        # Mock context file doesn't exist
        context_file = Mock()
        context_file.exists.return_value = False
        mock_report_dir = Mock()
        mock_report_dir.__truediv__ = Mock(return_value=context_file)
        mock_report_manager.get_report_dir.return_value = mock_report_dir

        result = recovery._relaunch_investigation(sample_fingerprint_id)

        assert result is False
        mock_queue.release_lock.assert_called_once()

    def test_relaunch_launch_failed(
        self, recovery, mock_queue, mock_agent_runner, mock_report_manager, sample_fingerprint_id
    ):
        """Test relaunch when process launch fails"""
        mock_agent_runner.launch_investigation.return_value = None

        # Mock context file exists
        context_file = Mock()
        context_file.exists.return_value = True
        mock_report_dir = Mock()
        mock_report_dir.__truediv__ = Mock(return_value=context_file)
        mock_report_manager.get_report_dir.return_value = mock_report_dir
        mock_report_manager.get_investigation_log_path.return_value = "/medic/test/log.txt"

        result = recovery._relaunch_investigation(sample_fingerprint_id)

        assert result is False


class TestCheckStalled:
    """Test stalled investigation detection"""

    def test_check_stalled_investigations(self, recovery, mock_queue):
        """Test checking for stalled investigations"""
        mock_queue.get_all_active.return_value = ["sha256:abc", "sha256:def"]
        mock_queue.check_stalled.side_effect = [True, False]

        stalled = recovery.check_stalled_investigations()

        assert len(stalled) == 1
        assert "sha256:abc" in stalled
        mock_queue.update_status.assert_called_once()


class TestCheckTimeouts:
    """Test timeout detection"""

    def test_check_timeouts(
        self, recovery, mock_queue, mock_agent_runner
    ):
        """Test checking for timed out investigations"""
        mock_queue.get_all_active.return_value = ["sha256:abc", "sha256:def"]
        mock_queue.check_timeout.side_effect = [True, False]
        mock_queue.get_pid.return_value = 12345
        mock_agent_runner.check_process.return_value = True

        timed_out = recovery.check_timeouts()

        assert len(timed_out) == 1
        assert "sha256:abc" in timed_out

        # Should kill process
        mock_agent_runner.terminate_process.assert_called_with(12345)

        # Should mark as completed
        mock_queue.mark_completed.assert_called_once()

    def test_check_timeouts_process_already_dead(
        self, recovery, mock_queue, mock_agent_runner
    ):
        """Test timeout when process already dead"""
        mock_queue.get_all_active.return_value = ["sha256:abc"]
        mock_queue.check_timeout.return_value = True
        mock_queue.get_pid.return_value = 12345
        mock_agent_runner.check_process.return_value = False

        timed_out = recovery.check_timeouts()

        # Should not try to kill
        mock_agent_runner.terminate_process.assert_not_called()

        # Should still mark as completed
        mock_queue.mark_completed.assert_called_once()


class TestCleanup:
    """Test cleanup of old investigations"""

    def test_cleanup_completed_investigations(
        self, recovery, mock_queue, mock_report_manager
    ):
        """Test cleaning up old completed investigations"""
        # Mock old completed investigation
        old_time = (datetime.now(timezone.utc) - timedelta(days=45)).isoformat()

        mock_report_manager.list_all_investigations.return_value = ["sha256:abc", "sha256:def"]
        mock_queue.get_investigation_info.side_effect = [
            {
                "status": InvestigationQueue.STATUS_COMPLETED,
                "completed_at": old_time,
            },
            {
                "status": InvestigationQueue.STATUS_IN_PROGRESS,
                "completed_at": None,
            },
        ]

        cleaned = recovery.cleanup_completed_investigations(retention_days=30)

        assert cleaned == 1
        mock_queue.cleanup_investigation.assert_called_once_with("sha256:abc")
        mock_report_manager.cleanup_investigation.assert_called_once_with("sha256:abc")

    def test_cleanup_skips_recent(
        self, recovery, mock_queue, mock_report_manager
    ):
        """Test cleanup skips recent investigations"""
        # Recent completion
        recent_time = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat()

        mock_report_manager.list_all_investigations.return_value = ["sha256:abc"]
        mock_queue.get_investigation_info.return_value = {
            "status": InvestigationQueue.STATUS_COMPLETED,
            "completed_at": recent_time,
        }

        cleaned = recovery.cleanup_completed_investigations(retention_days=30)

        assert cleaned == 0
        mock_queue.cleanup_investigation.assert_not_called()

    def test_cleanup_skips_in_progress(
        self, recovery, mock_queue, mock_report_manager
    ):
        """Test cleanup skips in-progress investigations"""
        mock_report_manager.list_all_investigations.return_value = ["sha256:abc"]
        mock_queue.get_investigation_info.return_value = {
            "status": InvestigationQueue.STATUS_IN_PROGRESS,
            "completed_at": None,
        }

        cleaned = recovery.cleanup_completed_investigations(retention_days=30)

        assert cleaned == 0


class TestEdgeCases:
    """Test edge cases and error handling"""

    def test_recover_with_exception(
        self, recovery, mock_queue, sample_fingerprint_id
    ):
        """Test recovery raises exception when Redis fails"""
        mock_queue.get_investigation_info.side_effect = Exception("Redis error")

        # Should raise the exception
        with pytest.raises(Exception, match="Redis error"):
            recovery.recover_investigation(sample_fingerprint_id)

    def test_relaunch_with_exception(
        self, recovery, mock_queue, sample_fingerprint_id
    ):
        """Test relaunch handles exceptions"""
        mock_queue.acquire_lock.side_effect = Exception("Lock error")

        result = recovery._relaunch_investigation(sample_fingerprint_id)

        assert result is False

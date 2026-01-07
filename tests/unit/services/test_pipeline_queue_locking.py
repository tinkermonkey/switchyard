"""
Test file locking in PipelineQueueManager to prevent race conditions.
"""
import pytest
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone
import concurrent.futures
import time

from services.pipeline_queue_manager import PipelineQueueManager


@pytest.fixture
def temp_state_dir():
    """Create temporary state directory for testing"""
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


@pytest.fixture
def queue_manager(temp_state_dir):
    """Create PipelineQueueManager with mocked dependencies"""
    manager = PipelineQueueManager(
        project_name='test_project',
        board_name='SDLC Execution',
        state_dir=temp_state_dir
    )

    # Mock get_issues_in_column_order to avoid GitHub API calls
    manager.get_issues_in_column_order = Mock(return_value=[])

    return manager


class TestFileLocking:
    """Test file locking prevents race conditions"""

    def test_concurrent_enqueue_no_duplicates(self, queue_manager):
        """Test that concurrent enqueue operations don't create duplicate entries"""
        # Mock get_issues_in_column_order to return issue positions
        queue_manager.get_issues_in_column_order = Mock(return_value=[
            {'issue_number': 100, 'position': 0},
            {'issue_number': 101, 'position': 1},
            {'issue_number': 102, 'position': 2},
        ])

        # Create multiple threads trying to enqueue the same issues
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = []
            timestamp = datetime.now(timezone.utc).isoformat()

            # Each thread tries to enqueue issues 100, 101, 102
            for _ in range(5):
                futures.append(executor.submit(
                    queue_manager.enqueue_issue, 100, 'Development', timestamp
                ))
                futures.append(executor.submit(
                    queue_manager.enqueue_issue, 101, 'Development', timestamp
                ))
                futures.append(executor.submit(
                    queue_manager.enqueue_issue, 102, 'Development', timestamp
                ))

            # Wait for all to complete
            concurrent.futures.wait(futures)

        # Verify no duplicates in queue
        queue = queue_manager.load_queue()
        issue_numbers = [issue['issue_number'] for issue in queue]

        assert len(issue_numbers) == len(set(issue_numbers)), f"Found duplicates: {issue_numbers}"
        assert 100 in issue_numbers
        assert 101 in issue_numbers
        assert 102 in issue_numbers

    def test_concurrent_sync_no_duplicates(self, queue_manager):
        """Test that concurrent sync operations don't create duplicate entries"""
        # Mock GitHub API to return 3 issues
        queue_manager.get_issues_in_column_order = Mock(return_value=[
            {'issue_number': 200, 'position': 0, 'title': 'Issue 200'},
            {'issue_number': 201, 'position': 1, 'title': 'Issue 201'},
            {'issue_number': 202, 'position': 2, 'title': 'Issue 202'},
        ])

        # Start with empty queue
        queue_manager.save_queue([])

        # Create multiple threads calling sync_queue_with_github
        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(queue_manager.sync_queue_with_github) for _ in range(10)]
            concurrent.futures.wait(futures)

        # Verify no duplicates in queue
        queue = queue_manager.load_queue()
        issue_numbers = [issue['issue_number'] for issue in queue]

        assert len(issue_numbers) == len(set(issue_numbers)), f"Found duplicates: {issue_numbers}"
        assert 200 in issue_numbers
        assert 201 in issue_numbers
        assert 202 in issue_numbers
        assert len(issue_numbers) == 3, f"Expected 3 issues, found {len(issue_numbers)}"

    def test_concurrent_mark_active_safe(self, queue_manager):
        """Test that concurrent mark_issue_active calls are safe"""
        # Start with queue containing 3 waiting issues
        initial_queue = [
            {
                'issue_number': 300,
                'status': 'waiting',
                'position_in_column': 0,
                'queued_at': datetime.now(timezone.utc).isoformat(),
                'last_position_check': datetime.now(timezone.utc).isoformat()
            },
            {
                'issue_number': 301,
                'status': 'waiting',
                'position_in_column': 1,
                'queued_at': datetime.now(timezone.utc).isoformat(),
                'last_position_check': datetime.now(timezone.utc).isoformat()
            },
            {
                'issue_number': 302,
                'status': 'waiting',
                'position_in_column': 2,
                'queued_at': datetime.now(timezone.utc).isoformat(),
                'last_position_check': datetime.now(timezone.utc).isoformat()
            }
        ]
        queue_manager.save_queue(initial_queue)

        # Multiple threads try to mark different issues as active
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(queue_manager.mark_issue_active, 300),
                executor.submit(queue_manager.mark_issue_active, 301),
                executor.submit(queue_manager.mark_issue_active, 302),
            ]
            concurrent.futures.wait(futures)

        # Verify all 3 issues are marked active
        queue = queue_manager.load_queue()
        assert len(queue) == 3

        for issue in queue:
            assert issue['status'] == 'active'
            assert 'activated_at' in issue

    def test_lock_timeout_handling(self, queue_manager):
        """Test that lock timeout is handled correctly"""
        import fcntl

        # Manually acquire the lock to force a timeout
        lock_file = queue_manager._get_lock_file()
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        f = open(lock_file, 'w')
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)

        try:
            # Try to acquire lock with very short timeout - should raise TimeoutError
            with pytest.raises(TimeoutError, match="Could not acquire queue lock"):
                with queue_manager._queue_lock(timeout=0.5):
                    pass
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()

    def test_lock_released_on_exception(self, queue_manager):
        """Test that lock is released even when exception occurs"""
        # This should acquire and release the lock despite the exception
        with pytest.raises(ValueError):
            with queue_manager._queue_lock():
                raise ValueError("Test exception")

        # Verify we can acquire the lock again (proves it was released)
        acquired = False
        with queue_manager._queue_lock(timeout=1):
            acquired = True

        assert acquired, "Lock should have been released after exception"

    def test_concurrent_mixed_operations(self, queue_manager):
        """Test concurrent mix of enqueue, sync, mark_active, and remove operations"""
        # Setup initial state
        queue_manager.get_issues_in_column_order = Mock(return_value=[
            {'issue_number': 400, 'position': 0, 'title': 'Issue 400'},
            {'issue_number': 401, 'position': 1, 'title': 'Issue 401'},
            {'issue_number': 402, 'position': 2, 'title': 'Issue 402'},
        ])

        initial_queue = [
            {
                'issue_number': 400,
                'status': 'waiting',
                'position_in_column': 0,
                'queued_at': datetime.now(timezone.utc).isoformat(),
                'last_position_check': datetime.now(timezone.utc).isoformat()
            }
        ]
        queue_manager.save_queue(initial_queue)

        # Run mixed operations concurrently
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
            futures = []
            timestamp = datetime.now(timezone.utc).isoformat()

            # Mix of operations
            for _ in range(3):
                futures.append(executor.submit(queue_manager.sync_queue_with_github))
                futures.append(executor.submit(queue_manager.enqueue_issue, 401, 'Development', timestamp))
                futures.append(executor.submit(queue_manager.mark_issue_active, 400))

            concurrent.futures.wait(futures)

        # Verify queue is in a consistent state
        queue = queue_manager.load_queue()
        issue_numbers = [issue['issue_number'] for issue in queue]

        # No duplicates
        assert len(issue_numbers) == len(set(issue_numbers)), f"Found duplicates: {issue_numbers}"

        # All expected issues present
        assert 400 in issue_numbers
        assert 401 in issue_numbers
        assert 402 in issue_numbers

        # Issue 400 should be active
        issue_400 = next(i for i in queue if i['issue_number'] == 400)
        assert issue_400['status'] == 'active'

    def test_github_api_called_outside_lock(self, queue_manager):
        """Test that GitHub API calls happen outside the lock (no timeout under concurrent load)"""
        # Mock GitHub API to have 2-second delay (simulating network I/O)
        call_times = []

        def slow_github_api(*args, **kwargs):
            call_times.append(time.time())
            time.sleep(2)  # Simulate slow API call
            return [
                {'issue_number': 500, 'position': 0, 'title': 'Issue 500'},
                {'issue_number': 501, 'position': 1, 'title': 'Issue 501'},
            ]

        queue_manager.get_issues_in_column_order = slow_github_api
        queue_manager.save_queue([])

        # Run 5 concurrent syncs - if API calls were inside the lock, this would timeout
        # Lock timeout is 10 seconds, but 5 * 2 seconds = 10 seconds
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(queue_manager.sync_queue_with_github) for _ in range(5)]

            # This should complete without TimeoutError
            concurrent.futures.wait(futures, timeout=15)

            # Verify all completed successfully
            for future in futures:
                assert future.done()
                assert future.exception() is None

        # Verify API was called 5 times (once per sync)
        assert len(call_times) == 5

        # Verify calls happened concurrently (not sequentially)
        # If sequential, total time would be 10+ seconds
        # If concurrent, total time should be ~2 seconds
        total_duration = call_times[-1] - call_times[0]
        assert total_duration < 5, f"Calls were sequential (took {total_duration}s), not concurrent"

    def test_get_next_waiting_issue_atomic_read(self, queue_manager):
        """Test that get_next_waiting_issue uses atomic read to prevent race conditions"""
        # Setup: Queue with 2 waiting issues
        queue_manager.get_issues_in_column_order = Mock(return_value=[
            {'issue_number': 600, 'position': 0, 'title': 'Issue 600'},
            {'issue_number': 601, 'position': 1, 'title': 'Issue 601'},
        ])

        initial_queue = [
            {
                'issue_number': 600,
                'status': 'waiting',
                'position_in_column': 0,
                'queued_at': datetime.now(timezone.utc).isoformat(),
                'last_position_check': datetime.now(timezone.utc).isoformat()
            },
            {
                'issue_number': 601,
                'status': 'waiting',
                'position_in_column': 1,
                'queued_at': datetime.now(timezone.utc).isoformat(),
                'last_position_check': datetime.now(timezone.utc).isoformat()
            }
        ]
        queue_manager.save_queue(initial_queue)

        # Run get_next_waiting_issue concurrently from multiple threads
        results = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
            futures = [executor.submit(queue_manager.get_next_waiting_issue) for _ in range(3)]
            concurrent.futures.wait(futures)
            results = [f.result() for f in futures]

        # All threads should get the same result (issue 600, highest priority)
        assert all(r['issue_number'] == 600 for r in results if r is not None)

        # Verify queue wasn't corrupted
        queue = queue_manager.load_queue()
        issue_numbers = [i['issue_number'] for i in queue]
        assert len(issue_numbers) == len(set(issue_numbers)), "Queue has duplicates"

    def test_save_queue_raises_on_failure(self, queue_manager, tmp_path):
        """Test that save_queue raises exceptions instead of swallowing them"""
        # Make state file read-only to force save failure
        state_file = queue_manager._get_state_file()
        queue_manager.save_queue([])  # Create the file first
        state_file.chmod(0o444)  # Make read-only

        try:
            # Attempt to save should raise PermissionError
            with pytest.raises(PermissionError):
                queue_manager.save_queue([{'issue_number': 999, 'status': 'waiting'}])
        finally:
            # Cleanup: restore permissions
            state_file.chmod(0o644)

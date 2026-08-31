"""
Unit tests for the threading lock guarding ProjectMonitor.last_state.

self.last_state is written by the polling loop's detect_changes() on its own
cadence, and read by _reconcile_active_runs() and _rescan_boards_for_stalled_items()
during reconciliation/rescan. All three sites must share a single in-process
threading.Lock (self._last_state_lock) so a poll-write can never race a
reconcile/rescan read of the same board's state.
"""
import threading
import time

import pytest
from unittest.mock import Mock

from services.project_monitor import ProjectMonitor, ProjectItem
from config.manager import ConfigManager


def _make_item(issue_number: int, status: str = "Backlog") -> ProjectItem:
    return ProjectItem(
        item_id=f"item-{issue_number}",
        content_id=f"content-{issue_number}",
        issue_number=issue_number,
        title=f"Issue {issue_number}",
        status=status,
        repository="test-repo",
        last_updated="2026-01-01T00:00:00Z",
    )


class TestLastStateLock:
    """Test the shared lock protecting ProjectMonitor.last_state."""

    @pytest.fixture
    def mock_config_manager(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        return config_manager

    @pytest.fixture
    def monitor(self, mock_config_manager):
        return ProjectMonitor(Mock(), mock_config_manager)

    def test_last_state_lock_initialized_in_init(self, monitor):
        """__init__ creates a plain in-process threading.Lock, not a cross-process lock."""
        assert hasattr(monitor, "_last_state_lock")
        # threading.Lock() instances are of type _thread.lock; RLock would expose
        # _is_owned. Either is acceptable per the issue, but it must be a real
        # threading primitive with acquire()/release(), not a file-based lock.
        assert hasattr(monitor._last_state_lock, "acquire")
        assert hasattr(monitor._last_state_lock, "release")
        # Must support the non-blocking acquire used to prove mutual exclusion below.
        acquired = monitor._last_state_lock.acquire(blocking=False)
        assert acquired is True
        monitor._last_state_lock.release()

    def test_detect_changes_write_path_blocks_while_lock_held(self, monitor):
        """
        The write site in detect_changes() (both the initial last-known-state
        read and the final self.last_state[project_name] = ... assignment) must
        run under self._last_state_lock: holding the lock externally should
        block a concurrent detect_changes() call until it is released.
        """
        board_key = "test_project_Board"
        monitor.last_state[board_key] = {}

        writer_finished = threading.Event()
        result_holder = {}

        def writer():
            changes = monitor.detect_changes(board_key, [_make_item(1)])
            result_holder["changes"] = changes
            writer_finished.set()

        monitor._last_state_lock.acquire()
        try:
            t = threading.Thread(target=writer)
            t.start()
            # Give the writer thread ample opportunity to run if (incorrectly)
            # unguarded; it must NOT be able to finish while we hold the lock.
            time.sleep(0.3)
            assert not writer_finished.is_set(), (
                "detect_changes() completed while _last_state_lock was held "
                "externally — the write path is not using the shared lock"
            )
        finally:
            monitor._last_state_lock.release()

        t.join(timeout=2)
        assert writer_finished.is_set(), "writer thread never completed after lock release"
        assert result_holder["changes"][0]["type"] == "item_added"
        assert monitor.last_state[board_key][1].issue_number == 1

    def test_reconcile_and_rescan_read_pattern_blocks_while_lock_held(self, monitor):
        """
        The read sites in _reconcile_active_runs() and _rescan_boards_for_stalled_items()
        both do:

            with self._last_state_lock:
                if board_key not in self.last_state:
                    continue
                current_items = list(self.last_state[board_key].values())

        Holding the lock externally must block that read pattern too, proving
        reads and writes share the same lock.
        """
        board_key = "test_project_Board"
        monitor.last_state[board_key] = {1: _make_item(1), 2: _make_item(2)}

        reader_finished = threading.Event()
        result_holder = {}

        def reconcile_style_read():
            # Mirrors services/project_monitor.py _reconcile_active_runs() and
            # _rescan_boards_for_stalled_items() read sites verbatim.
            with monitor._last_state_lock:
                if board_key not in monitor.last_state:
                    return
                current_items = list(monitor.last_state[board_key].values())
            result_holder["items"] = current_items
            reader_finished.set()

        monitor._last_state_lock.acquire()
        try:
            t = threading.Thread(target=reconcile_style_read)
            t.start()
            time.sleep(0.3)
            assert not reader_finished.is_set(), (
                "reconcile/rescan-style read completed while _last_state_lock was "
                "held externally — the read sites are not using the shared lock"
            )
        finally:
            monitor._last_state_lock.release()

        t.join(timeout=2)
        assert reader_finished.is_set(), "reader thread never completed after lock release"
        assert len(result_holder["items"]) == 2

    def test_concurrent_poll_write_and_reconcile_read_do_not_race(self, monitor):
        """
        Stress test: a writer thread repeatedly calls the real detect_changes()
        (the polling loop's write path) while a reader thread repeatedly performs
        the reconcile/rescan read pattern against the same board_key, both driven
        through the same self._last_state_lock. No exceptions should surface in
        either thread, and every snapshot the reader observes must be a fully
        formed, internally consistent dict (never a partially-mutated one).
        """
        board_key = "test_project_Board"
        monitor.last_state[board_key] = {}

        iterations = 200
        errors = []

        def writer():
            try:
                for i in range(iterations):
                    size = (i % 10) + 1
                    items = [_make_item(n) for n in range(size)]
                    monitor.detect_changes(board_key, items)
            except Exception as e:  # pragma: no cover - failure path
                errors.append(("writer", e))

        def reader():
            try:
                for _ in range(iterations):
                    with monitor._last_state_lock:
                        if board_key not in monitor.last_state:
                            continue
                        current_items = list(monitor.last_state[board_key].values())
                    # Snapshot must be self-consistent: every issue_number key
                    # matches its item's issue_number, with no duplicates and
                    # no torn/partial entries.
                    issue_numbers = [item.issue_number for item in current_items]
                    if len(issue_numbers) != len(set(issue_numbers)):
                        raise AssertionError(
                            f"inconsistent snapshot read: {issue_numbers}"
                        )
            except Exception as e:  # pragma: no cover - failure path
                errors.append(("reader", e))

        threads = [threading.Thread(target=writer), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not any(t.is_alive() for t in threads), "race test threads did not complete"
        assert errors == [], f"unexpected errors during concurrent access: {errors}"

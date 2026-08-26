"""
Tests for scripts/release_lock.py — the sole human recovery path for a pipeline
lock durably marked retained-due-to-failure (see PipelineLockManager.
mark_lock_failed / services.pipeline_run.PipelineRunManager.mark_failed).

A bug here either strands an issue forever (refuses to release when it
shouldn't) or force-releases an actively-running pipeline's lock out from
under it (releases when it shouldn't) — both are meaningful failure modes for
what is deliberately the ONLY supported way to clear a retained lock.
"""

import unittest
from unittest.mock import MagicMock, patch
from datetime import datetime, timezone

from scripts.release_lock import main


def _make_lock(locked_by_issue=123, retained_reason=None, lock_status='locked'):
    lock = MagicMock()
    lock.locked_by_issue = locked_by_issue
    lock.lock_status = lock_status
    lock.retained_reason = retained_reason
    lock.retained_at = datetime.now(timezone.utc).isoformat() if retained_reason else None
    return lock


class TestReleaseLockScript(unittest.TestCase):
    def _run(self, argv, get_lock_return, release_lock_return=True, reads_healthy=True):
        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock.return_value = get_lock_return
        mock_lock_manager.get_lock_fail_closed.return_value = (get_lock_return, reads_healthy)
        mock_lock_manager.release_lock.return_value = release_lock_return
        mock_queue = MagicMock()
        mock_cancellation = MagicMock()

        with patch('sys.argv', ['release_lock.py'] + argv), \
             patch('scripts.release_lock.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_queue_manager.get_pipeline_queue_manager', return_value=mock_queue), \
             patch('services.cancellation.get_cancellation_signal', return_value=mock_cancellation):
            try:
                main()
                exited_with = None
            except SystemExit as e:
                exited_with = e.code

        return mock_lock_manager, mock_queue, mock_cancellation, exited_with

    def test_no_lock_present_is_a_noop(self):
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123'],
            get_lock_return=None,
        )
        lock_manager.release_lock.assert_not_called()
        self.assertIsNone(exit_code)

    def test_fails_closed_when_lock_state_cannot_be_determined(self):
        """This is the deliberate recovery tool a human runs specifically
        during an outage — it must not fold "both Redis and YAML failed to
        read" into "nothing to do", which would actively mislead an operator
        exactly when they most need an honest "state unknown"."""
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123'],
            get_lock_return=None,
            reads_healthy=False,
        )
        lock_manager.release_lock.assert_not_called()
        self.assertEqual(exit_code, 1)

    def test_refuses_when_held_by_a_different_issue(self):
        lock = _make_lock(locked_by_issue=999, retained_reason='crashed')
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123'],
            get_lock_return=lock,
        )
        lock_manager.release_lock.assert_not_called()
        self.assertEqual(exit_code, 1)

    def test_refuses_non_retained_lock_without_force(self):
        """The core safety guarantee: releasing a lock that isn't marked
        retained-due-to-failure looks like it's actively in use — this must be
        refused by default, since releasing it would pull the lock out from
        under a running pipeline."""
        lock = _make_lock(locked_by_issue=123, retained_reason=None)
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123'],
            get_lock_return=lock,
        )
        lock_manager.release_lock.assert_not_called()
        self.assertEqual(exit_code, 1)

    def test_releases_non_retained_lock_with_force(self):
        lock = _make_lock(locked_by_issue=123, retained_reason=None)
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123', '--force'],
            get_lock_return=lock,
        )
        lock_manager.release_lock.assert_called_once_with('proj', 'board', 123, force=True)
        self.assertIsNone(exit_code)

    def test_releases_retained_lock_and_resets_queue_and_cancellation(self):
        lock = _make_lock(locked_by_issue=123, retained_reason='review cycle crashed')
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123'],
            get_lock_return=lock,
        )
        lock_manager.release_lock.assert_called_once_with('proj', 'board', 123, force=True)
        cancellation.clear.assert_called_once_with('proj', 123)
        queue.reset_issue_to_waiting.assert_called_once_with(123)
        self.assertIsNone(exit_code)

    def test_reports_failure_if_release_itself_fails(self):
        lock = _make_lock(locked_by_issue=123, retained_reason='crashed')
        lock_manager, queue, cancellation, exit_code = self._run(
            ['--project', 'proj', '--board', 'board', '--issue', '123'],
            get_lock_return=lock,
            release_lock_return=False,
        )
        self.assertEqual(exit_code, 1)
        queue.reset_issue_to_waiting.assert_not_called()


if __name__ == '__main__':
    unittest.main()

"""
Regression tests for #148's contention budget.

'lock_contention' is deliberately exempt from MAX_CONSECUTIVE_DISPATCH_FAILURES
— counting it there would mark the run failed and durably retain the board's
lock over a lock working exactly as designed. But an exemption with no counter
at all is its own silent failure: an orchestrator-side coroutine wedged while
holding project_checkout (its heartbeat thread refreshes the Redis TTL for the
life of the process) makes every dispatch wait the full timeout, record
lock_contention, get re-dispatched by the next 30s poll, and wait again —
forever, with nothing but a per-occurrence log line to show for it.

_escalate_sustained_lock_contention() is the visibility valve: it logs at ERROR,
emits a decision event, and posts exactly one issue comment per day — and
deliberately does NOT retain any lock or mark any run failed, because the
condition really does clear itself when the other holder finishes.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.project_monitor import ProjectMonitor, MAX_CONSECUTIVE_LOCK_CONTENTIONS

PROJECT = 'test-project'
BOARD = 'Development'
ISSUE = 8080


def _monitor(redis_set_result=True):
    monitor = object.__new__(ProjectMonitor)
    monitor.decision_events = MagicMock()
    monitor.task_queue = MagicMock()
    monitor.task_queue.redis_client.set.return_value = redis_set_result
    monitor.config_manager = MagicMock()
    monitor.pipeline_run_manager = MagicMock()
    return monitor


def _escalate(monitor, github):
    with patch('services.github_integration.GitHubIntegration', return_value=github):
        monitor._escalate_sustained_lock_contention(
            project_name=PROJECT,
            board_name=BOARD,
            repository='test-repo',
            issue_number=ISSUE,
            status='Code Review',
            agent='code_reviewer',
            contention_count=MAX_CONSECUTIVE_LOCK_CONTENTIONS,
            last_error="Could not acquire 'project_checkout' lock within 10900.0s: held by #99",
        )


def _github():
    github = MagicMock()
    github.post_comment = AsyncMock()
    return github


class TestEscalateSustainedLockContention:
    def test_no_lock_is_retained_and_no_run_is_marked_failed(self):
        """The whole point: escalation is visibility only. Retaining the board's
        lock here would reintroduce exactly what the exemption prevents."""
        monitor = _monitor()
        _escalate(monitor, _github())
        monitor.pipeline_run_manager.mark_failed.assert_not_called()
        monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()

    def test_a_decision_event_is_emitted(self):
        monitor = _monitor()
        _escalate(monitor, _github())
        monitor.decision_events.emit_error_decision.assert_called_once()
        kwargs = monitor.decision_events.emit_error_decision.call_args.kwargs
        assert kwargs['error_type'] == 'sustained_lock_contention'
        assert kwargs['context']['consecutive_lock_contentions'] == MAX_CONSECUTIVE_LOCK_CONTENTIONS

    def test_the_comment_names_the_lock_detail_that_was_recorded(self):
        monitor = _monitor()
        github = _github()
        _escalate(monitor, github)
        github.post_comment.assert_called_once()
        body = github.post_comment.call_args.args[1]
        assert 'project_checkout' in body
        assert 'held by #99' in body

    def test_the_comment_is_gated_so_a_30s_poll_cannot_spam_the_issue(self):
        monitor = _monitor(redis_set_result=None)  # SET NX found the key already set
        github = _github()
        _escalate(monitor, github)
        github.post_comment.assert_not_called()
        # The log line and the event are unconditional — only the comment is gated.
        monitor.decision_events.emit_error_decision.assert_called_once()

    def test_no_redis_skips_the_comment_rather_than_posting_every_poll(self):
        monitor = _monitor()
        monitor.task_queue.redis_client = None
        github = _github()
        _escalate(monitor, github)
        github.post_comment.assert_not_called()

    def test_a_github_failure_does_not_propagate(self):
        """Escalation is best-effort reporting on a dispatch that is about to
        proceed regardless — it must never become the exception that stops it."""
        monitor = _monitor()
        github = _github()
        github.post_comment.side_effect = RuntimeError("GitHub is down")
        _escalate(monitor, github)  # must not raise

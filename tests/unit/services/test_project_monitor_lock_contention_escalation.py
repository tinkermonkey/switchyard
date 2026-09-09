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

from services.project_monitor import (
    ProjectMonitor,
    MAX_CONSECUTIVE_LOCK_CONTENTIONS,
    classify_contention_dispatch,
)

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


class TestClassifyContentionDispatch:
    """
    The count/threshold decision behind the escalation, extracted to module level
    because the two ways it silently disappears are both one-identifier edits.
    """

    @staticmethod
    def _tracker(count):
        tracker = MagicMock()
        tracker.count_consecutive_lock_contentions.return_value = count
        # count_consecutive_failures() SKIPS 'lock_contention' entries by design,
        # so it returns 0 for a pure-contention history. If the classifier ever
        # calls it instead, the threshold is never reached and the escalation is
        # silently dead — this stub makes that swap fail loudly here.
        tracker.count_consecutive_failures.return_value = 0
        return tracker

    def _classify(self, tracker):
        return classify_contention_dispatch(
            tracker,
            project_name=PROJECT,
            issue_number=ISSUE,
            column='Code Review',
            agent='code_reviewer',
        )

    def test_below_the_threshold_does_not_escalate(self):
        count, should_escalate = self._classify(
            self._tracker(MAX_CONSECUTIVE_LOCK_CONTENTIONS - 1)
        )
        assert count == MAX_CONSECUTIVE_LOCK_CONTENTIONS - 1
        assert should_escalate is False

    def test_at_the_threshold_escalates(self):
        count, should_escalate = self._classify(
            self._tracker(MAX_CONSECUTIVE_LOCK_CONTENTIONS)
        )
        assert count == MAX_CONSECUTIVE_LOCK_CONTENTIONS
        assert should_escalate is True

    def test_above_the_threshold_keeps_escalating(self):
        _, should_escalate = self._classify(
            self._tracker(MAX_CONSECUTIVE_LOCK_CONTENTIONS + 5)
        )
        assert should_escalate is True

    def test_it_counts_contentions_not_failures(self):
        tracker = self._tracker(MAX_CONSECUTIVE_LOCK_CONTENTIONS)
        self._classify(tracker)
        tracker.count_consecutive_lock_contentions.assert_called_once()
        tracker.count_consecutive_failures.assert_not_called()

    def test_a_tracker_failure_degrades_to_no_escalation(self):
        """This runs on the dispatch path of an issue that is about to be
        dispatched either way — it must never become the exception that stops it."""
        tracker = MagicMock()
        tracker.count_consecutive_lock_contentions.side_effect = OSError("state volume full")
        assert self._classify(tracker) == (0, False)


class TestEscalationIsWiredIntoDispatch:
    """
    The wiring, not just the valve (#148). Every teardown path this work item
    added ends the run with retain_lock=False, so on the next poll the issue does
    NOT hold the board lock — and review, conversational and PR-review columns are
    not pipeline trigger columns at all. The check therefore has to run before any
    column-type or lock-state branching, or the counter climbs forever with
    nothing but per-occurrence INFO lines to show for it.
    """

    @staticmethod
    def _monitor_with_history(outcome, count, agent='senior_software_engineer'):
        monitor = _monitor()
        monitor._escalate_sustained_lock_contention = MagicMock()
        tracker = MagicMock()
        tracker.get_last_execution_for_column.return_value = {
            'outcome': outcome,
            'agent': agent,
            'error': "Could not acquire 'project_checkout' lock within 10900.0s",
        }
        tracker.count_consecutive_lock_contentions.return_value = count
        tracker.count_consecutive_failures.return_value = 0
        return monitor, tracker

    @staticmethod
    def _check(monitor, tracker, status='Code Review'):
        with patch('services.work_execution_state.work_execution_tracker', tracker):
            monitor._check_sustained_lock_contention(
                project_name=PROJECT,
                board_name=BOARD,
                repository='test-repo',
                issue_number=ISSUE,
                status=status,
            )

    def test_a_non_trigger_review_column_escalates(self):
        """'Code Review' is not in pipeline_trigger_columns — the branch this
        check used to live in was unreachable for it."""
        monitor, tracker = self._monitor_with_history(
            'lock_contention', MAX_CONSECUTIVE_LOCK_CONTENTIONS
        )
        self._check(monitor, tracker)
        monitor._escalate_sustained_lock_contention.assert_called_once()
        kwargs = monitor._escalate_sustained_lock_contention.call_args.kwargs
        assert kwargs['contention_count'] == MAX_CONSECUTIVE_LOCK_CONTENTIONS
        assert kwargs['status'] == 'Code Review'
        assert 'project_checkout' in kwargs['last_error']

    def test_below_the_threshold_only_logs(self):
        monitor, tracker = self._monitor_with_history(
            'lock_contention', MAX_CONSECUTIVE_LOCK_CONTENTIONS - 1
        )
        self._check(monitor, tracker)
        monitor._escalate_sustained_lock_contention.assert_not_called()

    def test_a_failure_history_never_reaches_the_contention_path(self):
        monitor, tracker = self._monitor_with_history(
            'failure', MAX_CONSECUTIVE_LOCK_CONTENTIONS
        )
        self._check(monitor, tracker)
        monitor._escalate_sustained_lock_contention.assert_not_called()
        tracker.count_consecutive_lock_contentions.assert_not_called()

    def test_no_prior_execution_is_a_no_op(self):
        monitor = _monitor()
        monitor._escalate_sustained_lock_contention = MagicMock()
        tracker = MagicMock()
        tracker.get_last_execution_for_column.return_value = None
        self._check(monitor, tracker)
        monitor._escalate_sustained_lock_contention.assert_not_called()

    def test_it_never_retains_a_lock_or_marks_a_run_failed(self):
        monitor, tracker = self._monitor_with_history(
            'lock_contention', MAX_CONSECUTIVE_LOCK_CONTENTIONS
        )
        self._check(monitor, tracker)
        monitor.pipeline_run_manager.mark_failed.assert_not_called()
        monitor.pipeline_run_manager.end_pipeline_run.assert_not_called()

    def test_a_state_read_failure_does_not_stop_the_dispatch(self):
        monitor = _monitor()
        monitor._escalate_sustained_lock_contention = MagicMock()
        tracker = MagicMock()
        tracker.get_last_execution_for_column.side_effect = OSError("state volume full")
        self._check(monitor, tracker)  # must not raise
        monitor._escalate_sustained_lock_contention.assert_not_called()

    def test_a_maker_agent_entry_in_a_review_column_is_found(self):
        """The lookup is by COLUMN, not by the column's configured agent: a review
        column's maker dispatch records under the maker's own name, and PR review
        under the synthetic 'pr_review_stage' wrapper. An agent-keyed lookup finds
        neither — which is most of what this escalation exists for."""
        monitor, tracker = self._monitor_with_history(
            'lock_contention', MAX_CONSECUTIVE_LOCK_CONTENTIONS,
            agent='senior_software_engineer',
        )
        self._check(monitor, tracker, status='Code Review')
        tracker.get_last_execution_for_column.assert_called_once()
        assert 'agent' not in tracker.get_last_execution_for_column.call_args.kwargs
        # The count itself stays agent-scoped, against whoever actually recorded it.
        assert tracker.count_consecutive_lock_contentions.call_args.kwargs['agent'] == (
            'senior_software_engineer'
        )
        assert monitor._escalate_sustained_lock_contention.call_args.kwargs['agent'] == (
            'senior_software_engineer'
        )

    def test_a_pr_review_stage_entry_is_found_too(self):
        monitor, tracker = self._monitor_with_history(
            'lock_contention', MAX_CONSECUTIVE_LOCK_CONTENTIONS, agent='pr_review_stage',
        )
        self._check(monitor, tracker, status='In Review')
        monitor._escalate_sustained_lock_contention.assert_called_once()
        assert monitor._escalate_sustained_lock_contention.call_args.kwargs['agent'] == (
            'pr_review_stage'
        )

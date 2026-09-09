"""
Regression tests for #148's failure-budget accounting.

'lock_contention' must be transparent to count_consecutive_failures() in BOTH
directions. Not counting it is the point of the outcome (three counted
contentions would trip project_monitor's MAX_CONSECUTIVE_DISPATCH_FAILURES and
mark_failed(), durably retaining the whole board's lock over contention). But
ENDING the run on it — which is what "count trailing 'failure', break on
anything else" did — lets contention erase real failure history: a genuinely
broken agent on a busy shared base clone that loses the lock race every third
dispatch produces failure, failure, lock_contention, failure, failure, ... and
never reaches a trailing count of 3, so the budget never fires and the broken
agent is re-dispatched forever.

count_consecutive_lock_contentions() is the bounded counter that keeps the
exemption from being unbounded silence — project_monitor escalates on it
without retaining any lock.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import patch

from services.work_execution_state import WorkExecutionStateTracker

PROJECT = 'test-project'
ISSUE = 777
COLUMN = 'Code Review'
AGENT = 'code_reviewer'


def _tracker(outcomes):
    """A tracker whose stored history for (COLUMN, AGENT) is `outcomes`, oldest
    first, plus one unrelated row that must never be counted."""
    tracker = WorkExecutionStateTracker()
    history = [{'column': 'Backlog', 'agent': 'business_analyst', 'outcome': 'failure'}]
    history += [
        {'column': COLUMN, 'agent': AGENT, 'outcome': outcome}
        for outcome in outcomes
    ]
    return tracker, {'execution_history': history}


def _failures(outcomes):
    tracker, state = _tracker(outcomes)
    with patch.object(tracker, 'load_state', return_value=state):
        return tracker.count_consecutive_failures(PROJECT, ISSUE, COLUMN, AGENT)


def _contentions(outcomes):
    tracker, state = _tracker(outcomes)
    with patch.object(tracker, 'load_state', return_value=state):
        return tracker.count_consecutive_lock_contentions(PROJECT, ISSUE, COLUMN, AGENT)


class TestCountConsecutiveFailures:
    def test_contention_does_not_count_as_a_failure(self):
        assert _failures(['lock_contention', 'lock_contention', 'lock_contention']) == 0

    def test_contention_does_not_reset_the_failure_run(self):
        # The exact interleaving that let a permanently broken agent escape the
        # budget: three real failures, one contention in the middle.
        assert _failures(['failure', 'failure', 'lock_contention', 'failure']) == 3

    def test_trailing_contention_is_skipped_not_treated_as_the_end(self):
        assert _failures(['failure', 'failure', 'failure', 'lock_contention']) == 3

    def test_success_still_ends_the_run(self):
        assert _failures(['failure', 'failure', 'success', 'failure']) == 1

    def test_frozen_still_ends_the_run(self):
        assert _failures(['failure', 'failure', 'frozen', 'failure']) == 1

    def test_plain_consecutive_failures_are_unchanged(self):
        assert _failures(['success', 'failure', 'failure']) == 2


class TestCountConsecutiveLockContentions:
    def test_counts_the_trailing_run(self):
        assert _contentions(['failure', 'lock_contention', 'lock_contention']) == 2

    def test_a_real_failure_ends_the_run(self):
        """A dispatch that got far enough to fail proves the lock was obtainable
        in between, so the contention was not continuous."""
        assert _contentions(['lock_contention', 'failure', 'lock_contention']) == 1

    def test_success_ends_the_run(self):
        assert _contentions(['lock_contention', 'lock_contention', 'success']) == 0

    def test_empty_history(self):
        assert _contentions([]) == 0


class TestShouldExecuteWorkAfterContention:
    def test_contention_is_redispatchable(self):
        """The counterpart of not counting it as a failure: the next poll has to
        actually be the retry."""
        tracker, state = _tracker(['lock_contention'])
        state['status_changes'] = []
        with patch.object(tracker, 'load_state', return_value=state):
            should_execute, reason = tracker.should_execute_work(
                issue_number=ISSUE,
                column=COLUMN,
                agent=AGENT,
                trigger_source='pipeline_progression',
                project_name=PROJECT,
            )
        assert should_execute is True
        assert 'lock_contention' in reason

"""
Unit tests for PipelineQueueManager.get_next_n_waiting_issues() and
get_queue_summary()'s 'active_issues' fix.

Phase 2 of the concurrency redesign (issue #57, parent #88, umbrella #34):
- get_next_waiting_issue() -> get_next_n_waiting_issues(n): the priority sort
  is unchanged, only the "take 1" truncation becomes "take up to n". n=1 must
  be byte-identical to the pre-#57 get_next_waiting_issue() implementation.
- get_queue_summary()'s 'active_issue' (singular, silently dropped all but
  the first active entry) -> 'active_issues' (plural, full list).

No live network/GitHub access - get_issues_in_column_order() is mocked so
sync_queue_with_github() is a no-op against the seeded queue state.
"""
import shutil
import tempfile
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

from services.pipeline_queue_manager import PipelineQueueManager


@pytest.fixture
def temp_state_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


@pytest.fixture
def queue_manager(temp_state_dir):
    manager = PipelineQueueManager(
        project_name='test_project',
        board_name='SDLC Execution',
        state_dir=temp_state_dir,
    )
    # Avoid any GitHub network call - sync_queue_with_github() calls
    # get_issues_in_column_order() first; returning [] makes it a no-op
    # against the queue state seeded directly via save_queue() below.
    manager.get_issues_in_column_order = Mock(return_value=[])
    # Avoid a real config_manager.get_project_config('test_project') lookup
    # (found in PR #138 review, /pr-review-toolkit:review-pr): sync_queue_with_github()
    # also calls _get_pipeline_trigger_column(), which loads this project's
    # real config -- 'test_project' has no config/projects/test_project.yaml
    # in a fresh checkout (that directory is gitignored), so this raised
    # ConfigurationError on any machine but one with that untracked fixture
    # file left over locally. Mirrors test_pipeline_queue_manager_batching.py's
    # existing fixture, which already avoids this the same way.
    manager._get_pipeline_trigger_column = Mock(return_value='Development')
    return manager


def _waiting_issue(issue_number, position):
    now = datetime.now(timezone.utc).isoformat()
    return {
        'issue_number': issue_number,
        'status': 'waiting',
        'position_in_column': position,
        'queued_at': now,
        'last_position_check': now,
    }


def _active_issue(issue_number, activated_at=None):
    now = activated_at or datetime.now(timezone.utc).isoformat()
    return {
        'issue_number': issue_number,
        'status': 'active',
        'position_in_column': 0,
        'queued_at': now,
        'activated_at': now,
    }


class TestGetNextNWaitingIssues:
    """get_next_n_waiting_issues(n) - the generalized "take up to n" API."""

    def _seed(self, queue_manager):
        queue_manager.save_queue([
            _waiting_issue(301, position=2),
            _waiting_issue(302, position=0),  # highest priority (topmost)
            _waiting_issue(303, position=1),
            _waiting_issue(304, position=3),
        ])

    def test_n_zero_returns_empty_list(self, queue_manager):
        self._seed(queue_manager)
        assert queue_manager.get_next_n_waiting_issues(0) == []

    def test_n_negative_returns_empty_list(self, queue_manager):
        self._seed(queue_manager)
        assert queue_manager.get_next_n_waiting_issues(-1) == []

    def test_n_one_returns_single_highest_priority_issue(self, queue_manager):
        self._seed(queue_manager)
        result = queue_manager.get_next_n_waiting_issues(1)
        assert [i['issue_number'] for i in result] == [302]

    def test_n_two_returns_top_two_in_priority_order(self, queue_manager):
        self._seed(queue_manager)
        result = queue_manager.get_next_n_waiting_issues(2)
        assert [i['issue_number'] for i in result] == [302, 303]

    def test_n_greater_than_available_returns_all_in_priority_order(self, queue_manager):
        self._seed(queue_manager)
        result = queue_manager.get_next_n_waiting_issues(100)
        assert [i['issue_number'] for i in result] == [302, 303, 301, 304]

    def test_no_waiting_issues_returns_empty_list(self, queue_manager):
        queue_manager.save_queue([_active_issue(999)])
        assert queue_manager.get_next_n_waiting_issues(3) == []

    def test_empty_queue_returns_empty_list(self, queue_manager):
        assert queue_manager.get_next_n_waiting_issues(3) == []

    def test_active_issues_excluded_from_candidates(self, queue_manager):
        queue_manager.save_queue([
            _active_issue(400),
            _waiting_issue(401, position=0),
        ])
        result = queue_manager.get_next_n_waiting_issues(5)
        assert [i['issue_number'] for i in result] == [401]


class TestGetNextWaitingIssueMatchesNEqualsOne:
    """Explicit byte-identical-at-n=1 check: get_next_waiting_issue() (the
    thin wrapper) must return exactly get_next_n_waiting_issues(1)'s single
    element, for both the "issue found" and "queue empty" cases."""

    def test_matches_when_issue_available(self, queue_manager):
        queue_manager.save_queue([
            _waiting_issue(501, position=1),
            _waiting_issue(502, position=0),
        ])

        via_wrapper = queue_manager.get_next_waiting_issue()
        via_n = queue_manager.get_next_n_waiting_issues(1)

        assert via_n == [via_wrapper]
        assert via_wrapper['issue_number'] == 502

    def test_matches_when_queue_empty(self, queue_manager):
        via_wrapper = queue_manager.get_next_waiting_issue()
        via_n = queue_manager.get_next_n_waiting_issues(1)

        assert via_wrapper is None
        assert via_n == []

    def test_forwards_prefetched_board_data(self, queue_manager):
        """get_next_waiting_issue() must still forward prefetched_board_data
        through to get_next_n_waiting_issues() unchanged (issue #100
        behavior, preserved through the #57 refactor)."""
        sentinel = {'id': 'board-1', 'items': {'nodes': []}}
        queue_manager.get_next_n_waiting_issues = Mock(return_value=[])

        queue_manager.get_next_waiting_issue(prefetched_board_data=sentinel)

        queue_manager.get_next_n_waiting_issues.assert_called_once_with(
            1, prefetched_board_data=sentinel
        )


class TestGetQueueSummaryActiveIssues:
    """get_queue_summary()'s 'active_issues' (plural, full list) fix -
    'active_issue' (singular) used to silently drop every entry past the
    first."""

    def test_single_active_issue_returned_in_list(self, queue_manager):
        queue_manager.save_queue([_active_issue(601)])

        summary = queue_manager.get_queue_summary()

        assert 'active_issue' not in summary
        assert [i['issue_number'] for i in summary['active_issues']] == [601]
        assert summary['active_count'] == 1

    def test_multiple_active_issues_all_returned(self, queue_manager):
        """The actual bug this issue fixes: mark_issue_active() being called
        more than once for the same board isn't prevented at the queue-
        manager level (the cap is enforced elsewhere, in the lock) - so this
        must not silently drop any of them."""
        queue_manager.save_queue([
            _active_issue(701),
            _active_issue(702),
            _waiting_issue(703, position=0),
        ])

        summary = queue_manager.get_queue_summary()

        assert sorted(i['issue_number'] for i in summary['active_issues']) == [701, 702]
        assert summary['active_count'] == 2
        assert summary['waiting_count'] == 1

    def test_no_active_issues_returns_empty_list(self, queue_manager):
        queue_manager.save_queue([_waiting_issue(801, position=0)])

        summary = queue_manager.get_queue_summary()

        assert summary['active_issues'] == []
        assert summary['active_count'] == 0

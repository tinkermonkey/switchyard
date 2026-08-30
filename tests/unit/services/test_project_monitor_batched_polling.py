"""
Unit tests for GitHub issue #94 (sub-issue of #36): wiring monitor_projects()'s
polling loop to batched board queries, plus per-board adaptive backoff.

Covers:
- _use_batched_board_queries defaults to False (env-var-gated; today's
  sequential-fetch + single global adaptive interval stays in effect
  unless USE_BATCHED_BOARD_QUERIES=true is set).
- _parse_board_items()/_split_valid_invalid_items()/_parse_and_validate_board_items():
  the single shared parsing/validation implementation behind BOTH
  get_project_items() (see test_project_monitor_status_validation.py) and
  the batched path's _resolve_batched_board_items() - not duplicated.
- _resolve_batched_board_items(): returns validated items with no extra
  query when a batched fetch is already clean, and delegates to
  get_project_items() for its full retry-on-invalid-status handling
  (without re-implementing that retry loop) when it isn't.
- _fetch_boards_batched(): one execute_batched_board_queries() call
  replaces N per-board get_project_items() calls; a board whose batched
  fetch failed still gets its data via a bounded single-board fallback
  that never blanks out unrelated boards.
- _process_board_poll_result(): the per-board change-detection/feedback/
  discussion/escalated-cycle processing shared, unchanged, by both the
  legacy and batched fetch paths.
- Per-board adaptive backoff (_get_or_seed_board_poll_state/_is_board_due/
  _record_board_poll_outcome/_next_cycle_sleep_seconds): a board with no
  changes decays toward the max interval independently of a different,
  simultaneously-active board.

No live network access - all GitHub API calls are mocked.
"""
import os
import time

import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import Mock, patch
from services.project_monitor import ProjectMonitor, ProjectItem
from config.manager import ConfigManager


def _node(issue_number, status, item_id=None):
    """Minimal raw GraphQL item node, shaped like get_project_items() expects."""
    return {
        'id': item_id or f'item-{issue_number}',
        'content': {
            'id': f'issue-{issue_number}',
            'number': issue_number,
            'title': f'Issue {issue_number}',
            'updatedAt': '2025-01-01T00:00:00Z',
            'repository': {'name': 'test-repo'},
        },
        'fieldValues': {'nodes': [{'field': {'name': 'Status'}, 'name': status}]},
    }


def _board_data(*nodes):
    """Minimal projectV2-shaped payload, matching what execute_batched_board_queries()
    and execute_board_query_cached() (once unwrapped) both hand back."""
    return {'id': 'PVT_1', 'title': 'Board', 'items': {'nodes': list(nodes)}}


@pytest.fixture
def monitor():
    """ProjectMonitor with a minimally-mocked ConfigManager - same pattern
    TestAdaptivePollInterval in test_github_api_caching.py already uses for
    this area of the code."""
    config_manager = Mock(spec=ConfigManager)
    config_manager.list_projects.return_value = []
    task_queue = Mock()
    return ProjectMonitor(task_queue, config_manager)


class TestBatchedFlagDefault:
    def test_defaults_off(self, monitor):
        """Without USE_BATCHED_BOARD_QUERIES set, today's sequential
        per-board fetch + single global adaptive interval must stay in
        effect (this is what the off-path 'byte-identical' guarantee
        depends on)."""
        assert monitor._use_batched_board_queries is False

    def test_enabled_via_env_var(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        with patch.dict(os.environ, {'USE_BATCHED_BOARD_QUERIES': 'true'}):
            flagged_monitor = ProjectMonitor(Mock(), config_manager)
        assert flagged_monitor._use_batched_board_queries is True

    def test_board_poll_state_starts_empty(self, monitor):
        assert monitor._board_poll_state == {}


class TestParseAndValidateSharedHelper:
    """_parse_and_validate_board_items() is the single shared implementation
    get_project_items() and _resolve_batched_board_items() both call -
    exercised directly here rather than duplicated per call site."""

    def test_parses_items_and_splits_valid_invalid(self, monitor):
        raw = _board_data(_node(1, 'In Progress'), _node(2, 'Bogus Status'), _node(3, 'No Status'))
        valid, invalid = monitor._parse_and_validate_board_items(
            'acme', 1, raw, {'In Progress', 'Done'}
        )
        assert [i.issue_number for i in valid] == [1]
        assert [i.issue_number for i in invalid] == [2]
        # "No Status" items are dropped silently from both lists.
        assert 3 not in [i.issue_number for i in valid] + [i.issue_number for i in invalid]

    def test_all_valid_returns_no_invalid_items(self, monitor):
        raw = _board_data(_node(1, 'Done'), _node(2, 'In Progress'))
        valid, invalid = monitor._parse_and_validate_board_items(
            'acme', 1, raw, {'In Progress', 'Done'}
        )
        assert len(valid) == 2
        assert invalid == []

    def test_parse_board_items_skips_draft_items_without_content(self, monitor):
        raw = _board_data({'id': 'draft-1', 'content': None, 'fieldValues': {'nodes': []}})
        items = monitor._parse_board_items(raw)
        assert items == []


class TestResolveBatchedBoardItems:
    """_resolve_batched_board_items() backs the batched path; it must reuse
    get_project_items()'s retry semantic via delegation rather than
    re-implementing a second retry loop."""

    def test_returns_valid_items_without_extra_query_when_already_clean(self, monitor):
        raw = _board_data(_node(1, 'In Progress'))
        with patch.object(monitor, '_get_valid_columns_for_board', return_value={'In Progress', 'Done'}), \
             patch.object(monitor, 'get_project_items') as mock_single:
            items = monitor._resolve_batched_board_items('acme', 1, raw)

        assert [i.issue_number for i in items] == [1]
        mock_single.assert_not_called()

    def test_delegates_to_get_project_items_on_invalid_status(self, monitor):
        """Preserves get_project_items()'s per-board retry-on-invalid-status
        semantic by delegating to it, instead of duplicating that retry
        loop in the batched path."""
        raw = _board_data(_node(1, 'Bogus Status'))
        with patch.object(monitor, '_get_valid_columns_for_board', return_value={'In Progress', 'Done'}), \
             patch.object(monitor, 'get_project_items', return_value=['delegated-result']) as mock_single:
            result = monitor._resolve_batched_board_items('acme', 1, raw)

        mock_single.assert_called_once_with('acme', 1)
        assert result == ['delegated-result']

    def test_invalidates_cache_before_delegating_on_invalid_status(self, monitor):
        """
        Regression test: execute_batched_board_queries() just wrote this
        board's (invalid) data into the shared board-query cache, so
        get_project_items()'s own first attempt would otherwise be a
        near-guaranteed cache hit on the same stale data - silently
        burning one of its 3 total retry attempts. The cache must be
        invalidated before delegating so get_project_items() gets a
        genuinely fresh fetch on its first attempt.
        """
        raw = _board_data(_node(1, 'Bogus Status'))
        call_order = []
        with patch.object(monitor, '_get_valid_columns_for_board', return_value={'In Progress', 'Done'}), \
             patch('services.github_owner_utils.invalidate_board_query_cache',
                    side_effect=lambda *a: call_order.append('invalidate')) as mock_invalidate, \
             patch.object(monitor, 'get_project_items',
                           side_effect=lambda *a: call_order.append('get_project_items') or ['delegated-result']):
            result = monitor._resolve_batched_board_items('acme', 1, raw)

        mock_invalidate.assert_called_once_with('acme', 1)
        assert call_order == ['invalidate', 'get_project_items']
        assert result == ['delegated-result']

    def test_returns_raw_unvalidated_when_workflow_lookup_fails(self, monitor):
        """Matches get_project_items()'s own no-valid-columns branch:
        return items unvalidated rather than dropping them, no retry."""
        raw = _board_data(_node(1, 'No Status'))
        with patch.object(monitor, '_get_valid_columns_for_board', return_value=set()), \
             patch.object(monitor, 'get_project_items') as mock_single:
            items = monitor._resolve_batched_board_items('acme', 1, raw)

        assert [i.issue_number for i in items] == [1]
        mock_single.assert_not_called()


class TestFetchBoardsBatched:
    """Part A acceptance: one execute_batched_board_queries() call replaces
    N sequential get_project_items() calls per cycle; a board whose
    batched fetch failed still gets its data via a bounded single-board
    fallback, not a whole-cycle failure."""

    @staticmethod
    def _board(name, owner, number):
        pipeline = Mock()
        pipeline.board_name = f"{name} Board"
        return {
            'project_name': name,
            'project_config': Mock(),
            'pipeline': pipeline,
            'owner': owner,
            'project_number': number,
            'board_key': (name, pipeline.board_name),
        }

    def test_one_batched_call_replaces_n_single_board_calls(self, monitor):
        boards = [self._board('proj-a', 'acme', 1), self._board('proj-b', 'acme', 2), self._board('proj-c', 'acme', 3)]
        results = {
            ('acme', 1): _board_data(_node(1, 'Done')),
            ('acme', 2): _board_data(_node(2, 'Done')),
            ('acme', 3): _board_data(_node(3, 'Done')),
        }

        with patch('services.github_owner_utils.execute_batched_board_queries', return_value=(results, {})) as mock_batched, \
             patch.object(monitor, 'get_project_items') as mock_single, \
             patch.object(monitor, '_get_valid_columns_for_board', return_value={'Done'}):
            fetched = monitor._fetch_boards_batched(boards)

        assert mock_batched.call_count == 1
        mock_single.assert_not_called()
        assert set(fetched.keys()) == {b['board_key'] for b in boards}
        for b in boards:
            assert [i.issue_number for i in fetched[b['board_key']]] == [b['project_number']]

    def test_bounded_fallback_for_boards_whose_batch_fetch_failed(self, monitor):
        boards = [self._board('proj-a', 'acme', 1), self._board('proj-b', 'acme', 2)]
        # proj-a's batched fetch failed; proj-b's succeeded within the same batch.
        results = {('acme', 2): _board_data(_node(2, 'Done'))}
        errors = {('acme', 1): 'boom'}
        fallback_items = [ProjectItem('i', 'c', 1, 'Fallback', 'Done', 'repo', '2025-01-01T00:00:00Z')]

        with patch('services.github_owner_utils.execute_batched_board_queries', return_value=(results, errors)) as mock_batched, \
             patch.object(monitor, 'get_project_items', return_value=fallback_items) as mock_single, \
             patch.object(monitor, '_get_valid_columns_for_board', return_value={'Done'}):
            fetched = monitor._fetch_boards_batched(boards)

        assert mock_batched.call_count == 1
        # Only the failed board falls back to a single-board fetch - the
        # unrelated succeeding board is never touched by the fallback.
        mock_single.assert_called_once_with('acme', 1)
        assert fetched[('proj-a', 'proj-a Board')] == fallback_items
        assert [i.issue_number for i in fetched[('proj-b', 'proj-b Board')]] == [2]

    def test_no_due_boards_skips_the_batched_call_entirely(self, monitor):
        with patch('services.github_owner_utils.execute_batched_board_queries') as mock_batched:
            fetched = monitor._fetch_boards_batched([])

        mock_batched.assert_not_called()
        assert fetched == {}

    def test_circuit_breaker_open_skips_batched_call_entirely(self, monitor):
        """Matches get_project_items()'s own breaker check: don't serve
        even a cache hit while the breaker is open, so the two
        feature-gated paths don't diverge in what 'breaker open' means."""
        boards = [self._board('proj-a', 'acme', 1)]
        mock_client = Mock()
        mock_client.breaker.is_open.return_value = True

        with patch('services.github_api_client.get_github_client', return_value=mock_client), \
             patch('services.github_owner_utils.execute_batched_board_queries') as mock_batched:
            fetched = monitor._fetch_boards_batched(boards)

        mock_batched.assert_not_called()
        assert fetched == {}

    def test_malformed_payload_for_one_board_falls_back_not_unhandled_exception(self, monitor):
        """
        Regression test: a malformed/unexpected payload for one board must
        not raise out of the loop and abort every other due board's fetch
        for the whole cycle - matches get_project_items()'s own
        (KeyError, TypeError) parsing guard, falling back to the
        single-board path for just the malformed board.
        """
        boards = [self._board('proj-a', 'acme', 1), self._board('proj-b', 'acme', 2)]
        results = {
            ('acme', 1): {'this': 'is missing the expected items/nodes shape'},
            ('acme', 2): _board_data(_node(2, 'Done')),
        }
        fallback_items = [ProjectItem('i', 'c', 1, 'Fallback', 'Done', 'repo', '2025-01-01T00:00:00Z')]

        with patch('services.github_owner_utils.execute_batched_board_queries', return_value=(results, {})), \
             patch.object(monitor, 'get_project_items', return_value=fallback_items) as mock_single, \
             patch.object(monitor, '_get_valid_columns_for_board', return_value={'Done'}):
            fetched = monitor._fetch_boards_batched(boards)  # must not raise

        mock_single.assert_called_once_with('acme', 1)
        assert fetched[('proj-a', 'proj-a Board')] == fallback_items
        assert [i.issue_number for i in fetched[('proj-b', 'proj-b Board')]] == [2]

    def test_board_missing_from_both_results_and_errors_falls_back_not_keyerror(self, monitor):
        """
        Regression test: build_batched_board_queries() can silently drop a
        board's chunk before ever querying it (e.g. an unresolvable owner
        type), leaving it absent from BOTH the results dict and the errors
        dict. That must still be treated as a failure and fall back to a
        single-board fetch, not raise an unhandled KeyError that would
        abort every other due board's processing this cycle.
        """
        boards = [self._board('proj-a', 'acme', 1), self._board('proj-b', 'acme', 2)]
        # proj-a is in neither results nor errors; proj-b succeeded.
        results = {('acme', 2): _board_data(_node(2, 'Done'))}
        errors = {}
        fallback_items = [ProjectItem('i', 'c', 1, 'Fallback', 'Done', 'repo', '2025-01-01T00:00:00Z')]

        with patch('services.github_owner_utils.execute_batched_board_queries', return_value=(results, errors)), \
             patch.object(monitor, 'get_project_items', return_value=fallback_items) as mock_single, \
             patch.object(monitor, '_get_valid_columns_for_board', return_value={'Done'}):
            fetched = monitor._fetch_boards_batched(boards)  # must not raise

        mock_single.assert_called_once_with('acme', 1)
        assert fetched[('proj-a', 'proj-a Board')] == fallback_items
        assert [i.issue_number for i in fetched[('proj-b', 'proj-b Board')]] == [2]


class TestProcessBoardPollResult:
    """Shared by both the legacy and batched fetch paths - exercised once
    here rather than duplicated per path."""

    def test_returns_true_and_processes_changes_when_detected(self, monitor):
        pipeline = Mock()
        pipeline.board_name = 'Board'
        pipeline.workspace = 'issues'
        project_config = Mock()
        items = [ProjectItem('i1', 'c1', 1, 'T', 'Done', 'repo', '2025-01-01T00:00:00Z')]

        with patch.object(monitor, 'detect_changes', return_value=[{'type': 'status_changed'}]) as mock_detect, \
             patch.object(monitor, 'process_board_changes') as mock_process, \
             patch.object(monitor, 'check_for_feedback') as mock_feedback, \
             patch.object(monitor, 'monitor_escalated_issue_cycles') as mock_escalated:
            had_changes = monitor._process_board_poll_result('proj', pipeline, project_config, items)

        assert had_changes is True
        mock_detect.assert_called_once_with('proj_Board', items)
        mock_process.assert_called_once()
        mock_feedback.assert_called_once()
        mock_escalated.assert_called_once_with('proj', 'Board')

    def test_returns_false_when_no_changes_detected(self, monitor):
        pipeline = Mock()
        pipeline.board_name = 'Board'
        pipeline.workspace = 'issues'
        project_config = Mock()
        items = [ProjectItem('i1', 'c1', 1, 'T', 'Done', 'repo', '2025-01-01T00:00:00Z')]

        with patch.object(monitor, 'detect_changes', return_value=[]), \
             patch.object(monitor, 'process_board_changes') as mock_process, \
             patch.object(monitor, 'check_for_feedback'), \
             patch.object(monitor, 'monitor_escalated_issue_cycles'):
            had_changes = monitor._process_board_poll_result('proj', pipeline, project_config, items)

        assert had_changes is False
        mock_process.assert_not_called()

    def test_returns_false_for_empty_items(self, monitor):
        pipeline = Mock()
        pipeline.board_name = 'Board'
        pipeline.workspace = 'issues'
        project_config = Mock()

        with patch.object(monitor, 'detect_changes') as mock_detect, \
             patch.object(monitor, 'monitor_escalated_issue_cycles'):
            had_changes = monitor._process_board_poll_result('proj', pipeline, project_config, [])

        assert had_changes is False
        mock_detect.assert_not_called()


class TestPerBoardAdaptiveBackoff:
    """Part B acceptance: a board with no changes decays toward the max
    interval independently of a different, simultaneously-active/noisy
    board."""

    def test_new_board_seeded_at_base_interval_and_immediately_due(self, monitor):
        board_key = ('proj', 'Board')
        state = monitor._get_or_seed_board_poll_state(board_key)

        assert state['interval'] == monitor._base_poll_interval
        assert state['idle_cycles'] == 0
        assert monitor._is_board_due(board_key, now=time.monotonic()) is True

    def test_active_board_stays_at_base_interval(self, monitor):
        board_key = ('proj', 'Active Board')
        now = 1000.0
        for _ in range(6):
            monitor._record_board_poll_outcome(board_key, had_changes=True, now=now)
            now += 1.0

        state = monitor._board_poll_state[board_key]
        assert state['interval'] == monitor._base_poll_interval
        assert state['idle_cycles'] == 0

    def test_idle_board_backs_off_past_threshold(self, monitor):
        board_key = ('proj', 'Idle Board')
        now = 1000.0
        for _ in range(monitor._idle_backoff_threshold + 3):
            monitor._record_board_poll_outcome(board_key, had_changes=False, now=now)
            now += 1.0

        state = monitor._board_poll_state[board_key]
        assert state['interval'] > monitor._base_poll_interval
        assert state['interval'] <= monitor._max_poll_interval

    def test_idle_board_interval_capped_at_max(self, monitor):
        board_key = ('proj', 'Idle Board')
        now = 1000.0
        for _ in range(50):
            monitor._record_board_poll_outcome(board_key, had_changes=False, now=now)
            now += 1.0

        assert monitor._board_poll_state[board_key]['interval'] == monitor._max_poll_interval

    def test_idle_board_resets_to_base_once_it_gets_a_change(self, monitor):
        board_key = ('proj', 'Board')
        now = 1000.0
        for _ in range(monitor._idle_backoff_threshold + 3):
            monitor._record_board_poll_outcome(board_key, had_changes=False, now=now)
            now += 1.0
        assert monitor._board_poll_state[board_key]['interval'] > monitor._base_poll_interval

        monitor._record_board_poll_outcome(board_key, had_changes=True, now=now)

        assert monitor._board_poll_state[board_key]['interval'] == monitor._base_poll_interval
        assert monitor._board_poll_state[board_key]['idle_cycles'] == 0

    def test_two_boards_diverge_independently(self, monitor):
        """One board always reports changes (noisy/active), the other never
        does (idle) - their tracked intervals must diverge over several
        simulated cycles, with the idle board's backoff having zero effect
        on the noisy board's interval."""
        noisy = ('proj', 'Noisy Board')
        idle = ('proj', 'Idle Board')
        now = 1000.0

        for _ in range(monitor._idle_backoff_threshold + 5):
            monitor._record_board_poll_outcome(noisy, had_changes=True, now=now)
            monitor._record_board_poll_outcome(idle, had_changes=False, now=now)
            now += 1.0

        noisy_state = monitor._board_poll_state[noisy]
        idle_state = monitor._board_poll_state[idle]

        assert noisy_state['interval'] == monitor._base_poll_interval
        assert idle_state['interval'] > monitor._base_poll_interval
        assert idle_state['interval'] > noisy_state['interval']

    def test_is_board_due_false_before_next_due_true_at_or_after(self, monitor):
        board_key = ('p', 'b')
        monitor._record_board_poll_outcome(board_key, had_changes=False, now=1000.0)
        interval = monitor._board_poll_state[board_key]['interval']

        assert monitor._is_board_due(board_key, now=1000.0 + interval - 0.001) is False
        assert monitor._is_board_due(board_key, now=1000.0 + interval) is True

    def test_next_cycle_sleep_is_minimum_across_tracked_boards(self, monitor):
        now = time.monotonic()
        monitor._board_poll_state[('p', 'fast')] = {'interval': 15, 'idle_cycles': 0, 'next_due': now + 5}
        monitor._board_poll_state[('p', 'slow')] = {'interval': 60, 'idle_cycles': 10, 'next_due': now + 55}

        sleep_seconds = monitor._next_cycle_sleep_seconds()

        # Close to the fast board's ~5s remaining (allowing for test timing slack)
        # and nowhere near the slow board's ~55s.
        assert 0 <= sleep_seconds <= 6

    def test_next_cycle_sleep_falls_back_to_base_interval_when_no_boards_tracked(self, monitor):
        assert monitor._board_poll_state == {}
        assert monitor._next_cycle_sleep_seconds() == monitor._base_poll_interval

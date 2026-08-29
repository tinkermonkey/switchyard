"""
Unit tests for GitHub issue #100 (sub-issue of #36): letting
PipelineQueueManager.get_issues_in_column_order() accept pre-fetched raw
board data instead of always calling execute_board_query_cached() itself,
and threading that data through sync_queue_with_github() /
get_next_waiting_issue().

Covers:
- get_issues_in_column_order(): with prefetched_board_data provided, the
  network fetch is skipped entirely and the result matches what the
  un-prefetched path (execute_board_query_cached()) produces for
  equivalent data - byte-for-byte identical item extraction/filtering.
- get_issues_in_column_order(): with prefetched_board_data=None (the
  default - every caller before #100), behavior is unchanged: it still
  calls execute_board_query_cached() itself.
- sync_queue_with_github() / get_next_waiting_issue(): forward their
  optional prefetched_board_data parameter down to
  get_issues_in_column_order() unchanged, and default to None (unchanged
  behavior) when not passed - covering the existing one-off callers
  (pipeline_progression.py, pipeline_run.py, observability_server.py,
  scripts/release_lock.py), none of which pass this new parameter.

No live network access - all GitHub API calls are mocked.
"""
import tempfile
import shutil
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from services.pipeline_queue_manager import PipelineQueueManager


def _node(issue_number, status, item_id=None, title=None, state='OPEN'):
    """Minimal raw GraphQL item node, matching what get_issues_in_column_order()
    iterates over (project_data['items']['nodes'])."""
    return {
        'id': item_id or f'item-{issue_number}',
        'fieldValues': {'nodes': [{'field': {'name': 'Status'}, 'name': status}]},
        'content': {
            '__typename': 'Issue',
            'number': issue_number,
            'state': state,
            'title': title or f'Issue {issue_number}',
        },
    }


def _project_data(*nodes):
    """The UNWRAPPED projectV2-level dict - i.e. exactly what
    execute_batched_board_queries()'s results dict holds per board (see
    services/github_owner_utils.py: parse_batched_projects_v2_response()
    returns results[board_key] = project_data, and
    execute_board_query_cached()'s own envelope unwraps to this same shape
    via data['user']['projectV2'] / data['organization']['projectV2'])."""
    return {'id': 'proj-1', 'title': 'Board', 'items': {'nodes': list(nodes)}}


def _envelope(project_data, owner_type='organization'):
    """The WRAPPED shape execute_board_query_cached() returns."""
    return {owner_type: {'projectV2': project_data}}


@pytest.fixture
def temp_state_dir():
    temp_dir = tempfile.mkdtemp()
    yield Path(temp_dir)
    shutil.rmtree(temp_dir)


@pytest.fixture
def queue_manager(temp_state_dir):
    return PipelineQueueManager(
        project_name='test_project',
        board_name='SDLC Execution',
        state_dir=temp_state_dir,
    )


class TestGetIssuesInColumnOrderPrefetch:
    """get_issues_in_column_order()'s new optional prefetched_board_data param."""

    def test_prefetched_data_skips_network_call(self, queue_manager):
        """When prefetched_board_data is provided, execute_board_query_cached()
        must never be called - the whole point of #100's batching."""
        project_data = _project_data(_node(101, 'Development'))

        with patch(
            'services.github_owner_utils.execute_board_query_cached'
        ) as mock_fetch:
            result = queue_manager.get_issues_in_column_order(
                'Development', prefetched_board_data=project_data
            )

        mock_fetch.assert_not_called()
        assert result == [
            {'issue_number': 101, 'position': 0, 'item_id': 'item-101', 'title': 'Issue 101'}
        ]

    def test_prefetched_result_matches_unprefetched_result(self, queue_manager):
        """The extraction/filtering logic is identical either way - only how
        the raw board data reaches it differs."""
        project_data = _project_data(
            _node(101, 'Development'),
            _node(102, 'Code Review'),  # different column - filtered out
            _node(103, 'Development', state='CLOSED'),  # closed - filtered out
            _node(104, 'Development'),
        )

        prefetched_result = queue_manager.get_issues_in_column_order(
            'Development', prefetched_board_data=project_data
        )

        with patch(
            'config.manager.config_manager'
        ) as mock_config_manager, patch(
            'config.state_manager.state_manager'
        ) as mock_state_manager, patch(
            'services.github_owner_utils.execute_board_query_cached',
            return_value=_envelope(project_data),
        ) as mock_fetch, patch(
            'services.github_owner_utils.get_owner_type', return_value='organization'
        ):
            project_config = Mock()
            project_config.github = {'org': 'test-org'}
            mock_config_manager.get_project_config.return_value = project_config

            board_state = Mock()
            board_state.project_number = 42
            project_state = Mock()
            project_state.boards = {'SDLC Execution': board_state}
            mock_state_manager.load_project_state.return_value = project_state

            fetched_result = queue_manager.get_issues_in_column_order('Development')

        mock_fetch.assert_called_once_with('test-org', 42)
        assert fetched_result == prefetched_result
        assert fetched_result == [
            {'issue_number': 101, 'position': 0, 'item_id': 'item-101', 'title': 'Issue 101'},
            {'issue_number': 104, 'position': 1, 'item_id': 'item-104', 'title': 'Issue 104'},
        ]

    def test_no_prefetched_data_preserves_existing_behavior(self, queue_manager):
        """prefetched_board_data defaults to None - every caller that predates
        #100 (and every one-off caller that still doesn't pass it) keeps
        fetching the board itself via execute_board_query_cached(), exactly
        as before #100."""
        project_data = _project_data(_node(200, 'Development'))

        with patch(
            'config.manager.config_manager'
        ) as mock_config_manager, patch(
            'config.state_manager.state_manager'
        ) as mock_state_manager, patch(
            'services.github_owner_utils.execute_board_query_cached',
            return_value=_envelope(project_data, owner_type='user'),
        ) as mock_fetch, patch(
            'services.github_owner_utils.get_owner_type', return_value='user'
        ):
            project_config = Mock()
            project_config.github = {'org': 'test-user'}
            mock_config_manager.get_project_config.return_value = project_config

            board_state = Mock()
            board_state.project_number = 7
            project_state = Mock()
            project_state.boards = {'SDLC Execution': board_state}
            mock_state_manager.load_project_state.return_value = project_state

            # Called with no second argument - matches every real caller today.
            result = queue_manager.get_issues_in_column_order('Development')

        mock_fetch.assert_called_once_with('test-user', 7)
        assert result == [
            {'issue_number': 200, 'position': 0, 'item_id': 'item-200', 'title': 'Issue 200'}
        ]


class TestPrefetchedDataForwarding:
    """sync_queue_with_github() / get_next_waiting_issue() forward the new
    parameter unchanged, and default it to None."""

    def test_sync_queue_with_github_forwards_prefetched_data(self, queue_manager):
        sentinel = {'items': {'nodes': []}}
        queue_manager._get_pipeline_trigger_column = Mock(return_value='Development')
        queue_manager.get_issues_in_column_order = Mock(return_value=[])

        queue_manager.sync_queue_with_github(prefetched_board_data=sentinel)

        queue_manager.get_issues_in_column_order.assert_called_once_with(
            'Development', prefetched_board_data=sentinel
        )

    def test_sync_queue_with_github_default_forwards_none(self, queue_manager):
        queue_manager._get_pipeline_trigger_column = Mock(return_value='Development')
        queue_manager.get_issues_in_column_order = Mock(return_value=[])

        # Existing call shape - no second argument.
        queue_manager.sync_queue_with_github()

        queue_manager.get_issues_in_column_order.assert_called_once_with(
            'Development', prefetched_board_data=None
        )

    def test_get_next_waiting_issue_forwards_prefetched_data(self, queue_manager):
        sentinel = {'items': {'nodes': []}}
        queue_manager.sync_queue_with_github = Mock()
        queue_manager._get_pipeline_trigger_column = Mock(return_value='Development')

        queue_manager.get_next_waiting_issue(prefetched_board_data=sentinel)

        queue_manager.sync_queue_with_github.assert_called_once_with(
            prefetched_board_data=sentinel
        )

    def test_get_next_waiting_issue_default_forwards_none(self, queue_manager):
        """Existing one-off callers (pipeline_progression.py, pipeline_run.py,
        observability_server.py, scripts/release_lock.py) all call this with
        no arguments - must keep behaving exactly as before #100."""
        queue_manager.sync_queue_with_github = Mock()
        queue_manager._get_pipeline_trigger_column = Mock(return_value='Development')

        queue_manager.get_next_waiting_issue()

        queue_manager.sync_queue_with_github.assert_called_once_with(
            prefetched_board_data=None
        )

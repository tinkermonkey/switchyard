"""
Unit tests for PipelineRunManager._resolve_issue_column_from_github().

Issue #147: the dispatch call sites resolve the next queued issue's column
through this helper, which used to build its own `gh api graphql` query with
items(first: 100) and no pageInfo follow-up. Boards accumulate items in
Done/Staged indefinitely, so any board past 100 items silently lost every
issue after the 100th -- the helper returned None, the caller read that as
"not on the board", and the issue was rolled straight back to 'waiting' on
every single dispatch attempt. That is the same data-fidelity bug
github_owner_utils already fixed for the batched board path (#96).

The helper also collapsed "the board query failed" and "the issue genuinely
isn't on the board" into one None, so an operator reading the log could not
tell a rate limit from a removed card.

No live GitHub/Redis/Elasticsearch access - everything is mocked.
"""
import unittest
from unittest.mock import MagicMock, Mock, patch

from services.pipeline_run import PipelineRunManager


def _board_envelope(item_numbers, status='Development', root='organization'):
    """A board query response in execute_board_query_cached()'s envelope shape."""
    return {
        root: {
            'projectV2': {
                'id': 'PVT_x',
                'title': 'dev',
                'items': {
                    'pageInfo': {'hasNextPage': False, 'endCursor': None},
                    'nodes': [
                        {
                            'id': f'item-{n}',
                            'content': {'__typename': 'Issue', 'number': n},
                            'fieldValues': {
                                'nodes': [
                                    {'name': status, 'field': {'name': 'Status'}}
                                ]
                            },
                        }
                        for n in item_numbers
                    ],
                },
            }
        }
    }


class TestResolveIssueColumnFromGitHub(unittest.TestCase):
    def setUp(self):
        self.mock_es = MagicMock()
        self.mock_redis = MagicMock()
        with patch('services.pipeline_run.Elasticsearch', return_value=self.mock_es), \
             patch('services.pipeline_run.redis.Redis', return_value=self.mock_redis):
            self.manager = PipelineRunManager()

        self.project_config = Mock()
        self.project_config.name = 'test-project'
        self.project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        self.pipeline_config = Mock()
        self.pipeline_config.board_name = 'dev'

        board_state = Mock()
        board_state.project_number = 7
        self.github_state = Mock()
        self.github_state.boards = {'dev': board_state}

    def _resolve(self, issue_number, board_data, mock_state=True):
        state_manager = Mock()
        state_manager.load_project_state.return_value = (
            self.github_state if mock_state else None
        )
        with patch('config.state_manager.state_manager', state_manager), \
             patch('services.github_owner_utils.execute_board_query_cached',
                   return_value=board_data) as mock_query:
            result = self.manager._resolve_issue_column_from_github(
                self.project_config, self.pipeline_config, issue_number
            )
        return result, mock_query

    def test_resolves_an_issue_past_the_first_hundred_board_items(self):
        """REGRESSION (#147/#96): a 140-item board with the target at position
        137. The hand-rolled items(first: 100) query never saw it and reported
        it as absent from the board -- so the dispatch site raised, rolled the
        issue back to 'waiting', and repeated that on every subsequent exit.
        Going through execute_board_query_cached() picks up the
        hasNextPage/endCursor walk, so the full board is searched."""
        numbers = list(range(400, 540))  # 140 items
        target = numbers[136]

        (column, reads_ok), _ = self._resolve(target, _board_envelope(numbers))

        self.assertEqual(column, 'Development')
        self.assertTrue(reads_ok)

    def test_goes_through_the_paginated_cached_board_query(self):
        """The paginated helper is the one that must be called - not a
        hand-built single-page GraphQL query."""
        (_column, _ok), mock_query = self._resolve(400, _board_envelope([400]))

        mock_query.assert_called_once_with('test-org', 7)

    def test_absent_issue_reads_healthy(self):
        """The board was read fine, the issue simply is not on it."""
        (column, reads_ok), _ = self._resolve(999, _board_envelope([400, 401]))

        self.assertIsNone(column)
        self.assertTrue(reads_ok)

    def test_failed_board_query_is_not_reported_as_absent(self):
        """REGRESSION (#147): execute_board_query_cached() returning None means
        the board could not be read (auth, rate limit, network). Collapsing that
        into the same None as "not on the board" made the dispatch sites blame
        the board for what was really a transient query failure."""
        (column, reads_ok), _ = self._resolve(400, None)

        self.assertIsNone(column)
        self.assertFalse(reads_ok)

    def test_missing_board_state_reads_unhealthy(self):
        """No GitHub state means nothing can be concluded about the column."""
        (column, reads_ok), _ = self._resolve(400, _board_envelope([400]), mock_state=False)

        self.assertIsNone(column)
        self.assertFalse(reads_ok)

    def test_issue_on_board_without_a_status_value_reads_healthy(self):
        """On the board but with no Status set: genuinely no column, and the
        read itself was fine."""
        data = _board_envelope([400])
        data['organization']['projectV2']['items']['nodes'][0]['fieldValues']['nodes'] = []

        (column, reads_ok), _ = self._resolve(400, data)

        self.assertIsNone(column)
        self.assertTrue(reads_ok)

    def test_user_owned_board_envelope_is_unwrapped(self):
        """Both owner types come back through the same helper."""
        (column, reads_ok), _ = self._resolve(
            400, _board_envelope([400], root='user')
        )

        self.assertEqual(column, 'Development')
        self.assertTrue(reads_ok)

    def test_thin_wrapper_still_returns_a_bare_column(self):
        """_get_issue_column_from_github() keeps its old single-value contract
        for the callers that don't need to tell the two None cases apart."""
        state_manager = Mock()
        state_manager.load_project_state.return_value = self.github_state
        with patch('config.state_manager.state_manager', state_manager), \
             patch('services.github_owner_utils.execute_board_query_cached',
                   return_value=_board_envelope([400])):
            column = self.manager._get_issue_column_from_github(
                self.project_config, self.pipeline_config, 400
            )

        self.assertEqual(column, 'Development')


if __name__ == '__main__':
    unittest.main()

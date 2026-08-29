"""
Tests for board-scoping in /active-pipeline-runs (switchyard issue #43).

Before this change, the endpoint's dedup set was keyed on (project, issue_number)
only, and get_recent_pipeline_run_id() had no board parameter. So if project X
had an active run for issue #5 on board A, and issue #5 also had a separate
failed/retained lock on board B, the (project, issue_number) dedup check in the
failed-run-from-locks loop would see (X, 5) already "seen" from board A's active
run and silently drop board B's failed entry entirely from the response — even
though they are two independent, simultaneously-valid entries.
"""

import sys
import os
from unittest.mock import MagicMock, patch

import pytest

if not os.path.isdir('/app'):
    # observability_server.py is importable outside Docker, but keep this
    # suite consistent with the rest of the repo's test-skip convention in
    # case CI later adds container-only setup here.
    pass

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

import services.observability_server as obs_server


@pytest.fixture
def client():
    obs_server.app.config['TESTING'] = True
    return obs_server.app.test_client()


def _lock(project, board, locked_by_issue, retained_reason=None, retained_at=None):
    lock = MagicMock()
    lock.project = project
    lock.board = board
    lock.locked_by_issue = locked_by_issue
    lock.retained_reason = retained_reason
    lock.retained_at = retained_at
    return lock


class TestTwoBoardsSameIssueBothAppear:
    """The core regression scenario from issue #43."""

    def test_active_run_on_board_a_and_failed_lock_on_board_b_both_appear(self, client):
        project = 'proj'
        issue_number = 99

        active_run_a = {
            'id': 'run-A',
            'project': project,
            'board': 'BoardA',
            'issue_number': issue_number,
            'issue_title': 'Issue on Board A',
            'issue_url': 'https://github.com/org/repo/issues/99',
            'started_at': '2026-08-28T10:00:00Z',
            'status': 'active',
        }

        es_search_result = {
            'hits': {
                'hits': [{'_source': dict(active_run_a)}]
            }
        }

        failed_lock_b = _lock(
            project, 'BoardB', issue_number,
            retained_reason='agent crashed repeatedly',
            retained_at='2026-08-28T09:00:00Z',
        )

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_status_for_issue.return_value = 'holding_lock'
        mock_lock_manager.get_lock_holder.return_value = issue_number
        mock_lock_manager.get_all_locks.return_value = [failed_lock_b]

        mock_run_manager = MagicMock()
        mock_run_manager.get_recent_pipeline_run_id.return_value = None

        with patch.object(obs_server.es_client, 'search', return_value=es_search_result), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=mock_run_manager):
            resp = client.get('/active-pipeline-runs')

        assert resp.status_code == 200
        data = resp.get_json()
        assert data['success'] is True
        assert data['count'] == 2

        by_board = {r['board']: r for r in data['runs']}
        assert set(by_board.keys()) == {'BoardA', 'BoardB'}

        # Neither entry dropped, and metadata is not cross-attributed.
        run_a = by_board['BoardA']
        run_b = by_board['BoardB']
        assert run_a['status'] == 'active'
        assert run_a['id'] == 'run-A'
        assert run_b['status'] == 'failed'
        assert run_b['reason'] == 'agent crashed repeatedly'
        assert run_a['issue_number'] == issue_number
        assert run_b['issue_number'] == issue_number
        # Board A's run id/reason must not have leaked onto Board B's entry.
        assert run_b.get('id') != run_a['id']
        assert 'reason' not in run_a or run_a.get('reason') != run_b['reason']

    def test_get_recent_pipeline_run_id_called_with_the_failed_locks_own_board(self, client):
        """The enrichment lookup for a failed lock must be scoped to that lock's
        own board, not left board-agnostic (which could attribute board A's
        recent run onto board B's failed entry)."""
        project = 'proj'
        issue_number = 5

        es_search_result = {'hits': {'hits': []}}
        failed_lock_b = _lock(project, 'BoardB', issue_number, retained_reason='boom')

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_all_locks.return_value = [failed_lock_b]

        mock_run_manager = MagicMock()
        mock_run_manager.get_recent_pipeline_run_id.return_value = None

        with patch.object(obs_server.es_client, 'search', return_value=es_search_result), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=mock_run_manager):
            resp = client.get('/active-pipeline-runs')

        assert resp.status_code == 200
        mock_run_manager.get_recent_pipeline_run_id.assert_called_once_with(
            project, issue_number, board='BoardB'
        )

    def test_same_board_duplicate_is_still_deduped(self, client):
        """Sanity check that the rename to seen_project_board_issue didn't lose
        the original same-board dedup behavior (a failed lock for a board that
        already has an active run entry is still skipped)."""
        project = 'proj'
        issue_number = 5

        active_run = {
            'id': 'run-1',
            'project': project,
            'board': 'BoardA',
            'issue_number': issue_number,
            'issue_title': 't',
            'issue_url': 'u',
            'started_at': '2026-08-28T10:00:00Z',
            'status': 'active',
        }
        es_search_result = {'hits': {'hits': [{'_source': dict(active_run)}]}}

        same_board_lock = _lock(project, 'BoardA', issue_number, retained_reason='should be skipped')

        mock_lock_manager = MagicMock()
        mock_lock_manager.get_lock_status_for_issue.return_value = 'holding_lock'
        mock_lock_manager.get_lock_holder.return_value = issue_number
        mock_lock_manager.get_all_locks.return_value = [same_board_lock]

        with patch.object(obs_server.es_client, 'search', return_value=es_search_result), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager):
            resp = client.get('/active-pipeline-runs')

        data = resp.get_json()
        assert data['count'] == 1
        assert data['runs'][0]['status'] == 'active'

"""
Operator-facing rendering of project-scoped resource locks (#140 item 29).

ProjectResourceLockManager namespaces a resource lock into
PipelineLockManager's (project, board) space as
board="__resource__<resource_name>", and mints a process-unique holder id in
place of the issue number (see project_checkout_lock.py, "Why every acquisition
gets its own unique holder id"). PipelineLockManager.get_all_locks() returns
those alongside real board locks, and the two consumers that render locks to a
human -- /active-pipeline-runs and scripts/list_failed_pipeline_runs.py -- read
PipelineLock.board and .locked_by_issue straight through.

So once mark_resource_failed() gets a real caller, an operator would be shown
`"board": "__resource__project_checkout"` with a `issue_number` that names no
issue, plus an issue_url and board_url built from it.

These pin the display helpers and both consumers' use of them. Resource locks
stay VISIBLE -- a retained one is a real operational condition -- but are
labelled for what they are.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

import services.observability_server as obs_server
from services.project_resource_lock_manager import (
    RESOURCE_BOARD_PREFIX,
    describe_lock_board,
    is_resource_board,
    resource_name_from_board,
)


class TestDisplayHelpers:

    def test_resource_board_is_recognised(self):
        assert is_resource_board(f"{RESOURCE_BOARD_PREFIX}project_checkout") is True

    def test_real_board_is_not(self):
        assert is_resource_board("SDLC Execution") is False

    def test_none_is_not(self):
        assert is_resource_board(None) is False

    def test_a_board_merely_containing_the_prefix_is_not(self):
        """The marker is a PREFIX -- _resource_board() only ever prepends it."""
        assert is_resource_board(f"Board {RESOURCE_BOARD_PREFIX}x") is False

    def test_resource_name_round_trips(self):
        assert resource_name_from_board(
            f"{RESOURCE_BOARD_PREFIX}dev_container_build"
        ) == 'dev_container_build'

    def test_resource_name_is_none_for_a_real_board(self):
        assert resource_name_from_board('SDLC Execution') is None

    def test_describe_translates_a_resource_board(self):
        assert describe_lock_board(
            f"{RESOURCE_BOARD_PREFIX}project_checkout"
        ) == 'resource:project_checkout'

    def test_describe_leaves_a_real_board_alone(self):
        assert describe_lock_board('SDLC Execution') == 'SDLC Execution'

    def test_describe_passes_none_through(self):
        assert describe_lock_board(None) is None


def _lock(project, board, locked_by_issue, retained_reason=None, retained_at=None):
    lock = MagicMock()
    lock.project = project
    lock.board = board
    lock.locked_by_issue = locked_by_issue
    lock.retained_reason = retained_reason
    lock.retained_at = retained_at
    return lock


@pytest.fixture
def client():
    obs_server.app.config['TESTING'] = True
    return obs_server.app.test_client()


def _get_runs(client, locks):
    mock_lock_manager = MagicMock()
    mock_lock_manager.get_all_locks.return_value = locks

    mock_run_manager = MagicMock()
    mock_run_manager.get_recent_pipeline_run_id.return_value = None

    with patch.object(obs_server.es_client, 'search', return_value={'hits': {'hits': []}}), \
         patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_lock_manager), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=mock_run_manager):
        resp = client.get('/active-pipeline-runs')

    assert resp.status_code == 200
    return resp.get_json()['runs']


class TestActivePipelineRunsEndpoint:

    def test_a_retained_resource_lock_is_not_rendered_with_its_internal_board(self, client):
        """THE regression: `__resource__project_checkout` must never reach an
        operator's screen."""
        runs = _get_runs(client, [
            _lock('proj', f'{RESOURCE_BOARD_PREFIX}project_checkout', 8123456789,
                  retained_reason='holder died mid-clone',
                  retained_at='2026-09-01T10:00:00Z'),
        ])

        assert len(runs) == 1
        assert runs[0]['board'] == 'resource:project_checkout'
        assert RESOURCE_BOARD_PREFIX not in str(runs[0])

    def test_the_minted_holder_id_is_not_presented_as_an_issue_number(self, client):
        runs = _get_runs(client, [
            _lock('proj', f'{RESOURCE_BOARD_PREFIX}project_checkout', 8123456789,
                  retained_reason='holder died mid-clone'),
        ])

        assert runs[0]['issue_number'] is None
        # Still discoverable -- an operator needs the holder id to release it.
        assert runs[0]['lock_holder_issue'] == 8123456789

    def test_no_issue_or_board_url_is_fabricated_for_a_resource_lock(self, client):
        runs = _get_runs(client, [
            _lock('proj', f'{RESOURCE_BOARD_PREFIX}dev_container_build', 42,
                  retained_reason='build blew up'),
        ])

        assert runs[0].get('issue_url') is None
        assert runs[0]['board_url'] is None

    def test_a_resource_lock_is_still_reported_not_hidden(self, client):
        """Hiding it is how a stuck project-wide lock goes unnoticed."""
        runs = _get_runs(client, [
            _lock('proj', f'{RESOURCE_BOARD_PREFIX}project_checkout', 7,
                  retained_reason='holder died mid-clone'),
        ])

        assert len(runs) == 1
        assert runs[0]['reason'] == 'holder died mid-clone'
        assert runs[0]['status'] == 'failed'

    def test_a_real_board_lock_is_unaffected(self, client):
        """Control: ordinary retained board locks render exactly as before."""
        runs = _get_runs(client, [
            _lock('proj', 'SDLC Execution', 99,
                  retained_reason='agent crashed repeatedly',
                  retained_at='2026-09-01T09:00:00Z'),
        ])

        assert runs[0]['board'] == 'SDLC Execution'
        assert runs[0]['issue_number'] == 99
        assert runs[0]['lock_holder_issue'] == 99

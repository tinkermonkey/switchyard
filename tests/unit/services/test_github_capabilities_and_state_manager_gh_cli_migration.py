"""
Tests for the final phase of the GitHub circuit breaker consolidation:
services/github_capabilities.py's check_capabilities() (`gh auth status`)
and config/state_manager.py's GitHubStateManager.refresh_board_field_ids()
(`gh project field-list`), both migrated onto GitHubAPIClient.gh_cli().

_probe_projects_v2_write() (also in github_capabilities.py) was already
covered by tests/unit/services/test_github_credential_routing.py's
TestProjectsV2Guard and is not duplicated here.
"""
from unittest.mock import MagicMock, patch

import pytest

from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture(autouse=True)
def reset_breaker():
    client = get_github_client()
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None
    yield
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None


def _mock_result(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestCheckCapabilitiesGhAuthStatus:
    def _make_capabilities(self):
        from services.github_capabilities import GitHubCapabilities
        return GitHubCapabilities()

    def test_authenticated_pat_reports_true(self):
        caps = self._make_capabilities()
        app = MagicMock(enabled=False)
        with patch('services.github_app.github_app', app), \
             patch('subprocess.run', return_value=_mock_result()), \
             patch.object(caps, '_probe_projects_v2_write', return_value=(True, 'ok')):
            status = caps.check_capabilities()
        assert status['capabilities']['pat_authentication'] is True

    def test_unauthenticated_reports_false(self):
        caps = self._make_capabilities()
        app = MagicMock(enabled=False)
        with patch('services.github_app.github_app', app), \
             patch('subprocess.run', return_value=_mock_result(returncode=1, stderr="not logged in")), \
             patch.object(caps, '_probe_projects_v2_write', return_value=(False, 'no creds')):
            status = caps.check_capabilities()
        assert status['capabilities']['pat_authentication'] is False
        assert any('no usable GitHub credential' in w for w in status['warnings'])

    def test_open_breaker_reports_unauthenticated_without_calling_subprocess(self):
        caps = self._make_capabilities()
        app = MagicMock(enabled=False)
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('services.github_app.github_app', app), \
             patch('subprocess.run') as mock_run, \
             patch.object(caps, '_probe_projects_v2_write', return_value=(False, 'no creds')):
            status = caps.check_capabilities()
        assert status['capabilities']['pat_authentication'] is False
        mock_run.assert_not_called()


class TestRefreshBoardFieldIds:
    @pytest.fixture
    def manager(self, tmp_path):
        from config.state_manager import GitHubStateManager
        return GitHubStateManager(state_root=str(tmp_path), config_manager=MagicMock())

    def _seed_state(self, manager):
        from config.state_manager import GitHubProjectState, GitHubBoard, GitHubColumn
        board = GitHubBoard(
            project_number=7, project_id='PROJECT_ID', node_id='PROJECT_ID',
            name='SDLC Execution',
            columns=[GitHubColumn(name='Backlog', id='OLD_ID', node_id='OLD_ID')],
        )
        state = GitHubProjectState(
            project_name='acme-project', org='acme', repo='widgets',
            boards={'SDLC Execution': board},
            labels_created=[], last_sync='', sync_hash='',
        )
        manager.save_project_state(state)
        return state

    def test_success_updates_field_and_column_ids(self, manager):
        self._seed_state(manager)
        result = _mock_result(stdout='{"fields": [{"name": "Status", '
                                      '"type": "ProjectV2SingleSelectField", "id": "FIELD_ID", '
                                      '"options": [{"name": "Backlog", "id": "NEW_OPTION_ID"}]}]}')
        with patch('subprocess.run', return_value=result):
            ok = manager.refresh_board_field_ids('acme-project', 'SDLC Execution')

        assert ok is True
        updated = manager.load_project_state('acme-project')
        board = updated.boards['SDLC Execution']
        assert board.status_field_id == 'FIELD_ID'
        assert board.columns[0].id == 'NEW_OPTION_ID'

    def test_gh_failure_returns_false(self, manager):
        self._seed_state(manager)
        result = _mock_result(returncode=1, stderr="HTTP 404: Not Found")
        with patch('subprocess.run', return_value=result):
            ok = manager.refresh_board_field_ids('acme-project', 'SDLC Execution')
        assert ok is False

    def test_malformed_json_on_exit_zero_returns_false_not_crash(self, manager):
        self._seed_state(manager)
        result = _mock_result(returncode=0, stdout='not json at all')
        with patch('subprocess.run', return_value=result):
            ok = manager.refresh_board_field_ids('acme-project', 'SDLC Execution')
        assert ok is False

    def test_open_breaker_returns_false_without_calling_subprocess(self, manager):
        self._seed_state(manager)
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None
        with patch('subprocess.run') as mock_run:
            ok = manager.refresh_board_field_ids('acme-project', 'SDLC Execution')
        assert ok is False
        mock_run.assert_not_called()

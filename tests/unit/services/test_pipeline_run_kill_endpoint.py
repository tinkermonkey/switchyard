import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../../..')))

import services.observability_server as obs_server


@pytest.fixture
def client():
    obs_server.app.config['TESTING'] = True
    return obs_server.app.test_client()


def _pipeline_run(run_id='run-1', project='proj', board='BoardA', issue_number=42, status='active'):
    run = MagicMock()
    run.id = run_id
    run.project = project
    run.board = board
    run.issue_number = issue_number
    run.status = status
    run.to_dict.return_value = {
        'id': run_id,
        'project': project,
        'board': board,
        'issue_number': issue_number,
        'status': status,
    }
    return run


class TestKillPipelineRun:

    def test_it_marks_failed_on_the_run_board_instead_of_releasing_the_lock(self, client):
        run = _pipeline_run(run_id='run-123', board='BoardB')
        manager = MagicMock()
        manager.get_pipeline_run_by_id.side_effect = [
            run,
            _pipeline_run(run_id='run-123', board='BoardB', status='failed'),
        ]
        manager.mark_failed.return_value = True

        signal = MagicMock()

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=manager), \
             patch('services.cancellation.get_cancellation_signal', return_value=signal), \
             patch('services.cancellation.cancel_issue_work') as cancel_issue_work:
            response = client.post('/pipeline-runs/run-123/kill')

        assert response.status_code == 200
        signal.cancel.assert_called_once_with('proj', 42, 'Pipeline run killed via Web UI')
        manager.mark_failed.assert_called_once_with(
            project='proj',
            board='BoardB',
            issue_number=42,
            reason='Killed by user via Web UI',
        )
        manager.end_pipeline_run.assert_not_called()
        cancel_issue_work.assert_called_once_with('proj', 42, 'Pipeline run killed via Web UI')

    def test_it_force_closes_the_specific_run_when_it_still_reads_active(self, client):
        run = _pipeline_run(run_id='run-123', board='BoardB', status='active')
        manager = MagicMock()
        manager.get_pipeline_run_by_id.side_effect = [run, run]
        manager.mark_failed.return_value = True

        signal = MagicMock()

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=manager), \
             patch('services.cancellation.get_cancellation_signal', return_value=signal), \
             patch('services.cancellation.cancel_issue_work') as cancel_issue_work:
            response = client.post('/pipeline-runs/run-123/kill')

        assert response.status_code == 200
        manager.mark_failed.assert_called_once_with(
            project='proj',
            board='BoardB',
            issue_number=42,
            reason='Killed by user via Web UI',
        )
        manager._end_run_in_elasticsearch.assert_called_once_with(
            run.to_dict.return_value,
            'Killed by user via Web UI (forced update)',
            outcome='failed',
        )
        cancel_issue_work.assert_called_once_with('proj', 42, 'Pipeline run killed via Web UI')

    def test_it_surfaces_retention_failure_after_cancelling_work(self, client):
        run = _pipeline_run(run_id='run-123', board='BoardB')
        manager = MagicMock()
        manager.get_pipeline_run_by_id.side_effect = [run, _pipeline_run(run_id='run-123', board='BoardB', status='failed')]
        manager.mark_failed.return_value = False

        signal = MagicMock()

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=manager), \
             patch('services.cancellation.get_cancellation_signal', return_value=signal), \
             patch('services.cancellation.cancel_issue_work') as cancel_issue_work:
            response = client.post('/pipeline-runs/run-123/kill')

        assert response.status_code == 500
        body = response.get_json()
        assert body['success'] is False
        assert 'lock could not be durably retained' in body['error'].lower()
        assert body['requires_manual_verification'] is True
        signal.cancel.assert_called_once_with('proj', 42, 'Pipeline run killed via Web UI')
        manager.mark_failed.assert_called_once_with(
            project='proj',
            board='BoardB',
            issue_number=42,
            reason='Killed by user via Web UI',
        )
        cancel_issue_work.assert_called_once_with('proj', 42, 'Pipeline run killed via Web UI')

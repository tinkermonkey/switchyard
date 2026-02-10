"""
Unit tests for WorkExecutionStateTracker._try_recover_result_from_redis().

Tests that the cleanup_stuck_in_progress_states() method recovers persisted
results from Redis before marking executions as failed.
"""

import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock


def _import_tracker_class(tmp_path):
    """Import WorkExecutionStateTracker with ORCHESTRATOR_ROOT set to tmp_path.

    The module-level singleton fires mkdir on import, so we must set the env
    var before the first import and force re-import if the module was already
    cached with a different ORCHESTRATOR_ROOT.
    """
    os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
    sys.modules.pop('services.work_execution_state', None)
    from services.work_execution_state import WorkExecutionStateTracker
    return WorkExecutionStateTracker


class TestTryRecoverResultFromRedis:
    """Tests for _try_recover_result_from_redis()."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def _make_execution(self):
        return {
            'column': 'In Development',
            'agent': 'software_engineer',
            'timestamp': '2025-01-01T00:00:00+00:00',
            'outcome': 'in_progress',
            'trigger_source': 'pipeline_progression',
        }

    def test_recovers_success_result(self, tmp_path):
        """When Redis has a result with exit_code 0, sets outcome to success."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'container_name': 'claude-agent-myproject-abc123',
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'abc123',
            'exit_code': 0,
            'output': 'All changes applied successfully.',
            'completed_at': '2025-01-01T00:05:00+00:00',
            'recovered': False,
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is True
        assert execution['outcome'] == 'success'
        assert 'error' not in execution
        mock_redis.delete.assert_called_once_with('agent_result:myproject:42:abc123')

    def test_recovers_failure_result(self, tmp_path):
        """When Redis has a result with non-zero exit_code, sets outcome to failure with error."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'container_name': 'claude-agent-myproject-abc123',
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'abc123',
            'exit_code': 1,
            'output': 'Error: tests failed\nAssertionError in test_foo.py',
            'completed_at': '2025-01-01T00:05:00+00:00',
            'recovered': False,
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is True
        assert execution['outcome'] == 'failure'
        assert 'exited with code 1' in execution['error']
        assert 'Result recovered from Redis' in execution['error']
        mock_redis.delete.assert_called_once_with('agent_result:myproject:42:abc123')

    def test_returns_false_when_no_redis_key(self, tmp_path):
        """When no Redis key exists for the project/issue, returns False."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter([])

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged
        mock_redis.delete.assert_not_called()

    def test_returns_false_when_redis_unavailable(self, tmp_path):
        """When Redis connection fails, returns False gracefully."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        with patch('redis.Redis', side_effect=ConnectionError("Connection refused")):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged

    def test_deletes_redis_key_after_recovery(self, tmp_path):
        """The Redis key is deleted after successful recovery to prevent reprocessing."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'xyz789',
            'exit_code': 0,
            'output': 'Done.',
            'completed_at': '2025-01-01T00:05:00+00:00',
            'recovered': False,
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:xyz789'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        mock_redis.delete.assert_called_once_with('agent_result:myproject:42:xyz789')

    def test_skips_result_for_different_agent(self, tmp_path):
        """When the Redis result is for a different agent, skips it."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'code_reviewer',  # different agent
            'task_id': 'abc123',
            'exit_code': 0,
            'output': 'Review complete.',
            'completed_at': '2025-01-01T00:05:00+00:00',
            'recovered': False,
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged

    def test_handles_malformed_json_gracefully(self, tmp_path):
        """When Redis contains invalid JSON, returns False without crashing."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = 'not valid json{'

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False

    def test_truncates_long_output_in_error(self, tmp_path):
        """When the agent output is long, the error field truncates to the tail."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        long_output = 'x' * 1000

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'abc123',
            'exit_code': 1,
            'output': long_output,
            'completed_at': '2025-01-01T00:05:00+00:00',
            'recovered': False,
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        # Error should contain the tail of the output (last 500 chars)
        assert len(execution['error']) < len(long_output) + 200
        assert execution['error'].endswith('x' * 500)

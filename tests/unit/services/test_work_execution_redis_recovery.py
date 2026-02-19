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

    def test_skips_result_with_missing_agent_field(self, tmp_path):
        """When the Redis result has no agent field, it is not matched (strict matching)."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            # no 'agent' key at all
            'task_id': 'abc123',
            'exit_code': 0,
            'output': 'Done.',
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

    def test_skips_result_with_no_exit_code(self, tmp_path):
        """When the Redis result has no exit_code, it is skipped (cannot determine outcome)."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'abc123',
            # no 'exit_code' key
            'output': 'Something happened.',
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

    def test_returns_true_even_if_delete_fails(self, tmp_path):
        """When delete() raises, the method still returns True with the correct outcome."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'abc123',
            'exit_code': 0,
            'output': 'All good.',
            'completed_at': '2025-01-01T00:05:00+00:00',
            'recovered': False,
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = json.dumps(result_data)
        mock_redis.delete.side_effect = ConnectionError("Redis went away")

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is True
        assert execution['outcome'] == 'success'

    def test_handles_null_output_in_result(self, tmp_path):
        """When the Redis result has output: null, it doesn't crash."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()

        result_data = {
            'project': 'myproject',
            'issue_number': 42,
            'agent': 'software_engineer',
            'task_id': 'abc123',
            'exit_code': 1,
            'output': None,  # null in JSON
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


class TestTaskIdBasedRecovery:
    """Tests for task_id-based exact Redis key lookup in recovery."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def _make_execution(self, task_id=None, timestamp='2025-01-01T12:00:00+00:00'):
        execution = {
            'column': 'In Development',
            'agent': 'software_engineer',
            'timestamp': timestamp,
            'outcome': 'in_progress',
            'trigger_source': 'pipeline_progression',
        }
        if task_id:
            execution['task_id'] = task_id
        return execution

    def test_exact_match_by_task_id(self, tmp_path):
        """When execution has task_id, uses exact key lookup and recovers result."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution(task_id='task_sw_eng_1234567890')

        result_data = {
            'agent': 'software_engineer',
            'exit_code': 0,
            'output': 'All changes applied.',
            'completed_at': '2025-01-01T12:05:00+00:00',
        }

        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is True
        assert execution['outcome'] == 'success'
        # Should use exact key, NOT scan_iter
        mock_redis.get.assert_called_once_with(
            'agent_result:myproject:42:task_sw_eng_1234567890'
        )
        mock_redis.scan_iter.assert_not_called()
        mock_redis.delete.assert_called_once_with(
            'agent_result:myproject:42:task_sw_eng_1234567890'
        )

    def test_no_result_for_task_id_returns_false(self, tmp_path):
        """When execution has task_id but no matching Redis key, returns False immediately."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution(task_id='task_sw_eng_1234567890')

        mock_redis = MagicMock()
        mock_redis.get.return_value = None  # No result at exact key

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged
        # Should NOT fall back to scan
        mock_redis.scan_iter.assert_not_called()

    def test_agent_mismatch_on_exact_key(self, tmp_path):
        """When exact key exists but agent doesn't match, returns False."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution(task_id='task_sw_eng_1234567890')

        result_data = {
            'agent': 'code_reviewer',  # Different agent
            'exit_code': 0,
            'output': 'Review done.',
            'completed_at': '2025-01-01T12:05:00+00:00',
        }

        mock_redis = MagicMock()
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged

    def test_exact_key_malformed_json(self, tmp_path):
        """When exact key contains malformed JSON, returns False without crashing."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution(task_id='task_sw_eng_1234567890')

        mock_redis = MagicMock()
        mock_redis.get.return_value = 'not valid json{'

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged
        mock_redis.scan_iter.assert_not_called()

    def test_fallback_rejects_unparseable_timestamps(self, tmp_path):
        """When timestamps are present but malformed, rejects result to be safe."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution(timestamp='not-a-timestamp')

        result_data = {
            'agent': 'software_engineer',
            'exit_code': 0,
            'output': 'Done.',
            'completed_at': 'also-not-a-timestamp',
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:abc123'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # rejected due to unparseable timestamps

    def test_fallback_rejects_stale_result(self, tmp_path):
        """Without task_id, falls back to scan and rejects results completed before execution started."""
        tracker = self._make_tracker(tmp_path)
        # Execution started at 13:08
        execution = self._make_execution(timestamp='2025-01-01T13:08:00+00:00')

        # Result completed at 11:30 (from earlier execution)
        result_data = {
            'agent': 'software_engineer',
            'exit_code': 0,
            'output': 'Old result from earlier run.',
            'completed_at': '2025-01-01T11:30:00+00:00',
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:old_task_123'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is False
        assert execution['outcome'] == 'in_progress'  # unchanged — stale result rejected

    def test_fallback_accepts_valid_result(self, tmp_path):
        """Without task_id, falls back to scan and accepts results completed after execution started."""
        tracker = self._make_tracker(tmp_path)
        # Execution started at 13:08
        execution = self._make_execution(timestamp='2025-01-01T13:08:00+00:00')

        # Result completed at 13:15 (after execution started)
        result_data = {
            'agent': 'software_engineer',
            'exit_code': 0,
            'output': 'Valid result.',
            'completed_at': '2025-01-01T13:15:00+00:00',
        }

        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent_result:myproject:42:new_task_456'])
        mock_redis.get.return_value = json.dumps(result_data)

        with patch('redis.Redis', return_value=mock_redis):
            recovered = tracker._try_recover_result_from_redis(
                'myproject', 42, 'software_engineer', 'In Development', execution
            )

        assert recovered is True
        assert execution['outcome'] == 'success'

    def test_fallback_accepts_result_without_timestamps(self, tmp_path):
        """Without task_id and without timestamps, accepts result for backward compatibility."""
        tracker = self._make_tracker(tmp_path)
        execution = self._make_execution()
        # Remove timestamp from execution
        del execution['timestamp']

        result_data = {
            'agent': 'software_engineer',
            'exit_code': 0,
            'output': 'Done.',
            # No completed_at field
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


class TestStampExecutionTaskId:
    """Tests for stamp_execution_task_id()."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def test_stamps_task_id_on_matching_execution(self, tmp_path):
        """Finds the correct in_progress execution and adds task_id field."""
        tracker = self._make_tracker(tmp_path)

        # Create an in_progress execution
        tracker.record_execution_start(
            issue_number=42,
            column='In Development',
            agent='software_engineer',
            trigger_source='pipeline_progression',
            project_name='myproject'
        )

        # Stamp task_id
        tracker.stamp_execution_task_id(
            'myproject', 42, 'software_engineer', 'In Development',
            'task_sw_eng_1234567890'
        )

        # Verify
        state = tracker.load_state('myproject', 42)
        last_exec = state['execution_history'][-1]
        assert last_exec['task_id'] == 'task_sw_eng_1234567890'
        assert last_exec['outcome'] == 'in_progress'
        assert last_exec['agent'] == 'software_engineer'

    def test_noop_when_no_matching_execution(self, tmp_path):
        """Does not crash or modify state when no matching execution exists."""
        tracker = self._make_tracker(tmp_path)

        # Create execution for different agent
        tracker.record_execution_start(
            issue_number=42,
            column='In Development',
            agent='code_reviewer',
            trigger_source='pipeline_progression',
            project_name='myproject'
        )

        # Try to stamp for non-matching agent — should not crash
        tracker.stamp_execution_task_id(
            'myproject', 42, 'software_engineer', 'In Development',
            'task_sw_eng_1234567890'
        )

        # Verify original execution is untouched
        state = tracker.load_state('myproject', 42)
        last_exec = state['execution_history'][-1]
        assert 'task_id' not in last_exec
        assert last_exec['agent'] == 'code_reviewer'


class TestRecordExecutionOutcome:
    """Tests for record_execution_outcome() phantom probe cleanup behavior."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def _add_in_progress(self, tracker, project, issue, agent, column, trigger_source='task_queue'):
        tracker.record_execution_start(
            issue_number=issue,
            column=column,
            agent=agent,
            trigger_source=trigger_source,
            project_name=project,
        )

    def test_single_entry_success(self, tmp_path):
        """Single in_progress entry: gets correct outcome, no error field when error=None."""
        tracker = self._make_tracker(tmp_path)
        self._add_in_progress(tracker, 'proj', 1, 'software_engineer', 'In Development')

        tracker.record_execution_outcome(1, 'In Development', 'software_engineer', 'success', 'proj')

        state = tracker.load_state('proj', 1)
        assert len(state['execution_history']) == 1
        assert state['execution_history'][0]['outcome'] == 'success'
        assert 'error' not in state['execution_history'][0]

    def test_single_entry_failure_with_error(self, tmp_path):
        """Single in_progress entry: error field is set when error is provided."""
        tracker = self._make_tracker(tmp_path)
        self._add_in_progress(tracker, 'proj', 2, 'software_engineer', 'In Development')

        tracker.record_execution_outcome(2, 'In Development', 'software_engineer', 'failure', 'proj',
                                         error='Container exited with code 1')

        state = tracker.load_state('proj', 2)
        assert state['execution_history'][0]['outcome'] == 'failure'
        assert state['execution_history'][0]['error'] == 'Container exited with code 1'

    def test_phantom_probe_is_cleaned_up(self, tmp_path):
        """Phantom probe (pre-enqueue, trigger_source='manual') is updated alongside the real entry."""
        tracker = self._make_tracker(tmp_path)
        # Probe entry created first (manual trigger_source, before enqueue)
        self._add_in_progress(tracker, 'proj', 3, 'software_engineer', 'In Development', trigger_source='manual')
        # Real entry created at dequeue time
        self._add_in_progress(tracker, 'proj', 3, 'software_engineer', 'In Development', trigger_source='task_queue')

        tracker.record_execution_outcome(3, 'In Development', 'software_engineer', 'success', 'proj')

        state = tracker.load_state('proj', 3)
        entries = state['execution_history']
        assert len(entries) == 2
        # Both entries should have outcome 'success', not 'in_progress'
        assert all(e['outcome'] == 'success' for e in entries)
        # The phantom (older, index 0) gets the superseded error message
        assert 'Superseded by a later execution' in entries[0]['error']
        # The primary (newer, index 1) has no error field (error=None was passed)
        assert 'error' not in entries[1]

    def test_multiple_phantoms_all_cleaned_up(self, tmp_path):
        """Multiple phantom entries are all updated when one real entry exists."""
        tracker = self._make_tracker(tmp_path)
        self._add_in_progress(tracker, 'proj', 4, 'software_engineer', 'In Development', trigger_source='manual')
        self._add_in_progress(tracker, 'proj', 4, 'software_engineer', 'In Development', trigger_source='manual')
        self._add_in_progress(tracker, 'proj', 4, 'software_engineer', 'In Development', trigger_source='task_queue')

        tracker.record_execution_outcome(4, 'In Development', 'software_engineer', 'success', 'proj')

        state = tracker.load_state('proj', 4)
        entries = state['execution_history']
        assert len(entries) == 3
        assert all(e['outcome'] == 'success' for e in entries)
        # Two phantom entries both get superseded message
        assert 'Superseded by a later execution' in entries[0]['error']
        assert 'Superseded by a later execution' in entries[1]['error']
        assert 'error' not in entries[2]

    def test_zero_in_progress_falls_through_to_fallback(self, tmp_path):
        """When no in_progress entry exists, the fallback path creates a new record."""
        tracker = self._make_tracker(tmp_path)
        # No in_progress entries — state file doesn't even exist yet

        tracker.record_execution_outcome(5, 'In Development', 'software_engineer', 'failure', 'proj',
                                         error='Restart after crash')

        state = tracker.load_state('proj', 5)
        assert len(state['execution_history']) == 1
        assert state['execution_history'][0]['outcome'] == 'failure'
        assert state['execution_history'][0]['trigger_source'] == 'unknown'

    def test_unrelated_entries_not_affected(self, tmp_path):
        """Entries for a different agent or column are not touched."""
        tracker = self._make_tracker(tmp_path)
        self._add_in_progress(tracker, 'proj', 6, 'code_reviewer', 'In Review')
        self._add_in_progress(tracker, 'proj', 6, 'software_engineer', 'In Development')

        tracker.record_execution_outcome(6, 'In Development', 'software_engineer', 'success', 'proj')

        state = tracker.load_state('proj', 6)
        by_agent = {e['agent']: e for e in state['execution_history']}
        assert by_agent['software_engineer']['outcome'] == 'success'
        assert by_agent['code_reviewer']['outcome'] == 'in_progress'

    def test_phantom_does_not_receive_caller_error(self, tmp_path):
        """Error from the real execution is not propagated to the phantom entry."""
        tracker = self._make_tracker(tmp_path)
        self._add_in_progress(tracker, 'proj', 7, 'software_engineer', 'In Development', trigger_source='manual')
        self._add_in_progress(tracker, 'proj', 7, 'software_engineer', 'In Development', trigger_source='task_queue')

        tracker.record_execution_outcome(7, 'In Development', 'software_engineer', 'failure', 'proj',
                                         error='Tests failed')

        state = tracker.load_state('proj', 7)
        entries = state['execution_history']
        # Primary (index 1) gets the real error
        assert entries[1]['error'] == 'Tests failed'
        # Phantom (index 0) gets the superseded message, not the real error
        assert 'Superseded by a later execution' in entries[0]['error']
        assert 'Tests failed' not in entries[0]['error']


class TestDevEnvironmentSetupContext:
    """Tests for dev_environment_setup task context flags."""

    def test_skip_workspace_prep_in_context(self):
        """Verify queue_dev_environment_setup() task includes skip_workspace_prep flag."""
        from services.dev_container_state import DevContainerStatus

        mock_queue_instance = MagicMock()

        with patch('task_queue.task_manager.TaskQueue', return_value=mock_queue_instance), \
             patch('services.dev_container_state.dev_container_state') as mock_state:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED

            import asyncio
            from agents.orchestrator_integration import queue_dev_environment_setup

            mock_logger = MagicMock()
            asyncio.run(queue_dev_environment_setup('myproject', mock_logger))

            # Get the task that was enqueued
            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            assert enqueued_task.context['skip_workspace_prep'] is True
            assert enqueued_task.context['use_docker'] is False
            assert enqueued_task.context['issue_number'] == 0

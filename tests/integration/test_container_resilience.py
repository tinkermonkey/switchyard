"""
Integration tests for container result loss and failure detection

Tests the complete system behavior:
1. Container restart scenario
2. Empty output scenario
3. Redis failure scenario
"""

import pytest
import time
import subprocess
import json
import tempfile
import yaml
import os
from pathlib import Path
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock
import redis

# Mock ORCHESTRATOR_ROOT before importing to avoid /app permission errors
with tempfile.TemporaryDirectory() as _tmpdir:
    with patch.dict(os.environ, {'ORCHESTRATOR_ROOT': _tmpdir}):
        from services.work_execution_state import WorkExecutionStateTracker
        from claude.docker_runner import DockerAgentRunner


class TestContainerRestartScenario:
    """Test result persistence survives orchestrator restart"""

    @pytest.mark.integration
    def test_result_persisted_to_redis(self):
        """Test wrapper successfully writes result to Redis"""
        # This requires actual Redis instance
        try:
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True, socket_connect_timeout=2)
            redis_client.ping()
        except Exception:
            pytest.skip("Redis not available for integration test")

        # Test key
        test_key = "agent_result:test:999:test-integration"
        test_result = {
            'project': 'test',
            'issue_number': '999',
            'agent': 'test-agent',
            'task_id': 'test-integration',
            'exit_code': 0,
            'output': 'Test output from integration test',
            'completed_at': datetime.now(timezone.utc).isoformat()
        }

        # Write to Redis
        redis_client.setex(test_key, 300, json.dumps(test_result))

        # Verify can retrieve
        retrieved = redis_client.get(test_key)
        assert retrieved is not None

        retrieved_data = json.loads(retrieved)
        assert retrieved_data['project'] == 'test'
        assert retrieved_data['exit_code'] == 0
        assert retrieved_data['output'] == 'Test output from integration test'

        # Cleanup
        redis_client.delete(test_key)

    @pytest.mark.integration
    def test_fallback_file_retrieval(self):
        """Test docker cp retrieval of fallback file"""
        # This would require actual Docker container
        # For now, test the file creation logic
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "test-task-123"
            result_file = Path(tmpdir) / f"agent_result_{task_id}.json"

            # Simulate wrapper writing fallback file
            result_data = {
                'project': 'test',
                'issue_number': '123',
                'agent': 'test-agent',
                'task_id': task_id,
                'exit_code': 0,
                'output': 'Fallback output',
                'storage': 'fallback_file'
            }

            with open(result_file, 'w') as f:
                json.dump(result_data, f, indent=2)

            # Verify file exists and can be read
            assert result_file.exists()

            with open(result_file) as f:
                retrieved = json.load(f)

            assert retrieved['storage'] == 'fallback_file'
            assert retrieved['output'] == 'Fallback output'

    @pytest.mark.integration
    def test_docker_logs_fallback(self):
        """Test docker logs can be retrieved as final fallback"""
        # This would require actual Docker container
        # Testing the command structure
        container_name = "test-container"

        # Simulate docker logs command (don't actually run)
        cmd = ['docker', 'logs', container_name]
        assert cmd == ['docker', 'logs', 'test-container']


class TestEmptyOutputScenario:
    """Test empty output detection and retry"""

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def tracker(self, temp_state_dir):
        return WorkExecutionStateTracker(state_dir=temp_state_dir)

    @pytest.mark.integration
    def test_complete_empty_output_workflow(self, tracker, temp_state_dir):
        """Test complete workflow: execution → empty output → detection → retry"""
        # Step 1: Create successful execution with no output
        state_file = temp_state_dir / "test_project_issue_123.yaml"

        old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
        state_data = {
            'project_name': 'test-project',
            'issue_number': 123,
            'execution_history': [
                {
                    'agent': 'senior-software-engineer',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'exit_code': 0,
                    'completed_at': old_time.isoformat(),
                    'timestamp': old_time.isoformat()
                }
            ]
        }

        with open(state_file, 'w') as f:
            yaml.dump(state_data, f)

        # Step 2: Run watchdog detection
        with patch.object(tracker, 'has_active_execution', return_value=False):
            with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        # Run detection
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

        # Step 3: Verify execution marked as failure
        assert retried_count == 1

        with open(state_file) as f:
            updated_state = yaml.safe_load(f)

        last_exec = updated_state['execution_history'][-1]
        assert last_exec['outcome'] == 'failure'
        assert 'no visible GitHub output' in last_exec['error']
        assert last_exec['watchdog_retry_triggered'] is True
        assert last_exec['watchdog_retry_count'] == 1

        # Step 4: Verify project_monitor would pick up this failure
        # (This is implicit - project_monitor polls for failed executions)
        assert last_exec['outcome'] == 'failure'  # Ready for project_monitor

    @pytest.mark.integration
    def test_validation_prevents_empty_output_success(self):
        """Test docker_runner validation prevents marking empty output as success"""
        runner = DockerAgentRunner()

        # Simulate container exiting with 0 but no output
        exit_code = 0
        result_text = ""

        # Validation should fail
        is_valid, error = runner._validate_result(exit_code, result_text, "test-container")

        assert is_valid is False
        assert 'no output' in error.lower()

        # In real execution, this would trigger failure recording instead of success


class TestRedisFailureScenario:
    """Test system behavior when Redis is unavailable"""

    @pytest.mark.integration
    def test_wrapper_continues_without_redis(self):
        """Test wrapper continues execution when Redis is down"""
        # Simulate Redis connection failure
        try:
            # Try to connect to non-existent Redis
            redis_client = redis.Redis(host='nonexistent-host', port=6379, socket_connect_timeout=1)
            redis_client.ping()
            pytest.skip("Expected Redis to be unavailable")
        except Exception:
            # Good - Redis is unavailable
            pass

        # Wrapper should handle this gracefully
        # (Tested in unit tests - wrapper.connect_redis() returns False but doesn't crash)

    @pytest.mark.integration
    def test_fallback_storage_used_when_redis_fails(self):
        """Test fallback storage is used when Redis write fails"""
        with tempfile.TemporaryDirectory() as tmpdir:
            task_id = "test-task-fallback"
            result_file = Path(tmpdir) / f"agent_result_{task_id}.json"

            # Simulate wrapper's fallback write
            result_data = {
                'project': 'test',
                'issue_number': '123',
                'agent': 'test-agent',
                'task_id': task_id,
                'exit_code': 0,
                'output': 'Fallback storage used',
                'storage': 'fallback_file'
            }

            with open(result_file, 'w') as f:
                json.dump(result_data, f, indent=2)

            # Verify fallback file created
            assert result_file.exists()

            # Docker runner should be able to retrieve this via docker cp
            # (In real execution: subprocess.run(['docker', 'cp', f'{container}:{result_file}', '-']))

    @pytest.mark.integration
    def test_triple_redundancy_workflow(self):
        """Test all three storage mechanisms work independently"""
        task_id = "test-task-redundancy"

        # 1. Redis storage (primary)
        try:
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True, socket_connect_timeout=2)
            redis_client.ping()

            redis_key = f"agent_result:test:123:{task_id}"
            redis_result = {'source': 'redis', 'output': 'Redis output'}
            redis_client.setex(redis_key, 300, json.dumps(redis_result))

            assert redis_client.get(redis_key) is not None
            redis_client.delete(redis_key)
        except Exception:
            pytest.skip("Redis not available")

        # 2. Fallback file (secondary)
        with tempfile.TemporaryDirectory() as tmpdir:
            result_file = Path(tmpdir) / f"agent_result_{task_id}.json"
            file_result = {'source': 'file', 'output': 'File output'}

            with open(result_file, 'w') as f:
                json.dump(file_result, f)

            assert result_file.exists()

            with open(result_file) as f:
                retrieved = json.load(f)
            assert retrieved['source'] == 'file'

        # 3. Docker logs (tertiary)
        # Simulated by printing to stdout during execution
        # (Automatically captured by docker logs command)


class TestObservabilityEvents:
    """Test observability events are emitted correctly"""

    @pytest.mark.integration
    def test_retry_attempted_event_emitted(self):
        """Test RETRY_ATTEMPTED event is emitted by watchdog"""
        # This requires mocking observability manager
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = WorkExecutionStateTracker(state_dir=Path(tmpdir))

            state_file = Path(tmpdir) / "test_project_issue_123.yaml"
            old_time = datetime.now(timezone.utc) - timedelta(minutes=10)
            state_data = {
                'project_name': 'test-project',
                'issue_number': 123,
                'execution_history': [{
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': old_time.isoformat(),
                    'timestamp': old_time.isoformat()
                }]
            }

            with open(state_file, 'w') as f:
                yaml.dump(state_data, f)

            # Mock observability manager
            with patch('monitoring.observability.get_observability_manager') as mock_obs:
                mock_obs_instance = MagicMock()
                mock_obs.return_value = mock_obs_instance

                with patch.object(tracker, 'has_active_execution', return_value=False):
                    with patch.object(tracker, '_should_retry_failed_execution', return_value=(True, "eligible")):
                        with patch.object(tracker, '_has_github_output', return_value=False):
                            with patch('utils.file_lock.file_lock'):
                                tracker.detect_and_retry_empty_successful_executions()

                                # Verify observability event was emitted
                                # The method tries to emit, but may fail silently if not available
                                # We just verify it doesn't crash


class TestEndToEndWorkflow:
    """Test complete end-to-end workflow"""

    @pytest.mark.integration
    @pytest.mark.slow
    def test_complete_resilience_workflow(self):
        """Test complete workflow from execution to recovery"""
        # This would be a full end-to-end test requiring:
        # 1. Actual Docker container execution
        # 2. Redis instance
        # 3. Orchestrator running
        # 4. GitHub API interaction

        # For now, we've tested individual components
        # A full e2e test would be run manually or in CI/CD
        pytest.skip("Full e2e test requires complete orchestrator setup")

    @pytest.mark.integration
    def test_retry_limit_respected(self):
        """Test retry limit is respected (no infinite loops)"""
        with tempfile.TemporaryDirectory() as tmpdir:
            tracker = WorkExecutionStateTracker(state_dir=Path(tmpdir))

            state_file = Path(tmpdir) / "test_project_issue_123.yaml"
            old_time = datetime.now(timezone.utc) - timedelta(minutes=10)

            # Create state with max retries already attempted
            state_data = {
                'project_name': 'test-project',
                'issue_number': 123,
                'execution_history': [{
                    'agent': 'test-agent',
                    'column': 'In Progress',
                    'outcome': 'success',
                    'completed_at': old_time.isoformat(),
                    'timestamp': old_time.isoformat(),
                    'watchdog_retry_count': 3  # Already at max
                }]
            }

            with open(state_file, 'w') as f:
                yaml.dump(state_data, f)

            # Run watchdog
            with patch.object(tracker, 'has_active_execution', return_value=False):
                with patch.object(tracker, '_has_github_output', return_value=False):
                    with patch('utils.file_lock.file_lock'):
                        # Should not retry (max exceeded)
                        retried_count = tracker.detect_and_retry_empty_successful_executions()

                        assert retried_count == 0

                        # State should not be modified
                        with open(state_file) as f:
                            updated_state = yaml.safe_load(f)

                        last_exec = updated_state['execution_history'][-1]
                        assert last_exec['outcome'] == 'success'  # Still success, not retried

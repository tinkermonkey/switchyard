"""
Unit tests for reliable Redis container tracking.

Tests:
- DockerAgentRunner._get_redis() lazy caching and failure fallback
- _register_active_container retry with backoff and container_id passthrough
- WorkExecutionStateTracker._repair_missing_redis_tracking from Docker labels
- _check_redis_tracking_for_agent uses scan_iter (not keys)
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call


class TestDockerAgentRunnerRedisClient:
    """Test _get_redis() lazy caching and failure handling."""

    def test_get_redis_caches_connection(self):
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()
        mock_redis_instance = MagicMock()
        mock_redis_instance.ping.return_value = True

        with patch('redis.Redis', return_value=mock_redis_instance) as mock_cls:
            client1 = runner._get_redis()
            client2 = runner._get_redis()

            assert client1 is client2
            # Only one Redis() call — second call returns cached
            mock_cls.assert_called_once()

    def test_get_redis_returns_none_on_failure(self):
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()

        with patch('redis.Redis', side_effect=ConnectionError("refused")):
            client = runner._get_redis()
            assert client is None

    def test_get_redis_retries_after_reset(self):
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()

        mock_redis_instance = MagicMock()
        mock_redis_instance.ping.return_value = True

        # First call fails, then we reset, second call succeeds
        with patch('redis.Redis', side_effect=ConnectionError("refused")):
            assert runner._get_redis() is None

        runner._redis = None  # Reset

        with patch('redis.Redis', return_value=mock_redis_instance):
            assert runner._get_redis() is mock_redis_instance


class TestRegisterActiveContainer:
    """Test _register_active_container retry logic and container_id passthrough."""

    def _make_runner_with_redis(self):
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        runner._redis = mock_redis
        return runner, mock_redis

    def test_first_attempt_success(self):
        runner, mock_redis = self._make_runner_with_redis()

        runner._register_active_container(
            'claude-agent-proj-123', 'code_agent', 'proj', 'task-1',
            {'context': {'issue_number': 42, 'pipeline_run_id': 'run-1'}},
            container_id='abc123def'
        )

        mock_redis.hset.assert_called_once()
        key = mock_redis.hset.call_args[0][0]
        mapping = mock_redis.hset.call_args[1]['mapping']

        assert key == 'agent:container:claude-agent-proj-123'
        assert mapping['container_id'] == 'abc123def'
        assert mapping['agent'] == 'code_agent'
        assert mapping['project'] == 'proj'
        assert mapping['issue_number'] == '42'
        assert mapping['pipeline_run_id'] == 'run-1'

        mock_redis.expire.assert_called_once_with('agent:container:claude-agent-proj-123', 7200)

    def test_container_id_passthrough(self):
        """container_id from docker run output is stored directly — no docker ps lookup."""
        runner, mock_redis = self._make_runner_with_redis()

        runner._register_active_container(
            'claude-agent-proj-456', 'code_agent', 'proj', 'task-2',
            {'context': {}},
            container_id='realid789'
        )

        mapping = mock_redis.hset.call_args[1]['mapping']
        assert mapping['container_id'] == 'realid789'

    def test_retry_on_transient_failure(self):
        """Transient Redis failure on first attempt succeeds on retry."""
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()

        mock_redis_good = MagicMock()
        mock_redis_good.ping.return_value = True
        mock_redis_good.hset.return_value = True
        mock_redis_good.expire.return_value = True

        call_count = 0

        def get_redis_side_effect():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ConnectionError("transient failure")
            return mock_redis_good

        with patch.object(runner, '_get_redis', side_effect=get_redis_side_effect):
            with patch('time.sleep'):
                runner._register_active_container(
                    'claude-agent-proj-789', 'code_agent', 'proj', 'task-3',
                    {'context': {}},
                    container_id='cid123'
                )

        mock_redis_good.hset.assert_called_once()

    def test_exhausted_retries_logs_error(self):
        """All 3 retries fail → ERROR log (not WARNING)."""
        from claude.docker_runner import DockerAgentRunner

        runner = DockerAgentRunner()

        with patch.object(runner, '_get_redis', return_value=None):
            with patch('time.sleep'):
                with patch('claude.docker_runner.logger') as mock_logger:
                    runner._register_active_container(
                        'claude-agent-proj-fail', 'code_agent', 'proj', 'task-4',
                        {'context': {}},
                        container_id='cid456'
                    )

                    # Should have called error (not just warning) on final attempt
                    error_calls = [c for c in mock_logger.error.call_args_list]
                    assert len(error_calls) == 1
                    assert 'UNTRACKED' in error_calls[0][0][0]
                    assert 'stuck execution checker' in error_calls[0][0][0]

    def test_no_docker_ps_called(self):
        """Registration no longer calls docker ps to discover container_id."""
        runner, mock_redis = self._make_runner_with_redis()

        with patch('subprocess.run') as mock_subprocess:
            runner._register_active_container(
                'claude-agent-proj-x', 'code_agent', 'proj', 'task-5',
                {'context': {}},
                container_id='directid'
            )

            # subprocess.run should NOT be called (no docker ps lookup)
            mock_subprocess.assert_not_called()


def _import_tracker_class(tmp_path):
    """Import WorkExecutionStateTracker with ORCHESTRATOR_ROOT set to tmp_path.

    The module-level singleton fires mkdir on import, so we must set the env
    var before the first import and force re-import if the module was already
    cached with a different ORCHESTRATOR_ROOT.
    """
    os.environ['ORCHESTRATOR_ROOT'] = str(tmp_path)
    # Force re-import if already cached (e.g. from another test)
    sys.modules.pop('services.work_execution_state', None)
    from services.work_execution_state import WorkExecutionStateTracker
    return WorkExecutionStateTracker


class TestRepairMissingRedisTracking:
    """Test _repair_missing_redis_tracking reads Docker labels and re-registers."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def test_repair_from_docker_labels(self, tmp_path):
        tracker = self._make_tracker(tmp_path)

        inspect_output = 'sha256abcdef|code_agent|myproject|task-99|42|run-7\n'
        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = inspect_output

        mock_redis = MagicMock()

        with patch('subprocess.run', return_value=mock_subprocess_result) as mock_run:
            with patch('redis.Redis', return_value=mock_redis):
                tracker._repair_missing_redis_tracking(
                    execution={
                        'issue_number': 42,
                        'agent': 'code_agent',
                        'task_id': 'task-99',
                        'column': 'Development'
                    },
                    project='myproject',
                    container_names=['claude-agent-myproject-task99']
                )

        # Verify docker inspect was called
        mock_run.assert_called_once()
        inspect_args = mock_run.call_args[0][0]
        assert 'docker' in inspect_args
        assert 'inspect' in inspect_args
        assert 'claude-agent-myproject-task99' in inspect_args

        # Verify Redis hset was called with correct data
        mock_redis.hset.assert_called_once()
        key = mock_redis.hset.call_args[0][0]
        mapping = mock_redis.hset.call_args[1]['mapping']

        assert key == 'agent:container:claude-agent-myproject-task99'
        assert mapping['container_id'] == 'sha256abcdef'
        assert mapping['agent'] == 'code_agent'
        assert mapping['project'] == 'myproject'
        assert mapping['task_id'] == 'task-99'
        assert mapping['issue_number'] == '42'
        assert mapping['pipeline_run_id'] == 'run-7'
        assert mapping['repaired'] == 'true'

        mock_redis.expire.assert_called_once_with('agent:container:claude-agent-myproject-task99', 7200)

    def test_repair_handles_inspect_failure(self, tmp_path):
        tracker = self._make_tracker(tmp_path)

        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 1
        mock_subprocess_result.stderr = 'No such container'

        with patch('subprocess.run', return_value=mock_subprocess_result):
            with patch('redis.Redis') as mock_redis_cls:
                # Should not raise
                tracker._repair_missing_redis_tracking(
                    execution={
                        'issue_number': 1,
                        'agent': 'agent',
                        'task_id': 'task-1'
                    },
                    project='proj',
                    container_names=['nonexistent-container']
                )
                # Redis should not be called
                mock_redis_cls.return_value.hset.assert_not_called()

    def test_repair_falls_back_to_function_args_when_labels_empty(self, tmp_path):
        """When Docker labels are empty, use the function arguments as fallback."""
        tracker = self._make_tracker(tmp_path)

        # Labels are empty (container has no org.clauditoreum.* labels)
        inspect_output = 'sha256abc||||||\n'
        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = inspect_output

        mock_redis = MagicMock()

        with patch('subprocess.run', return_value=mock_subprocess_result):
            with patch('redis.Redis', return_value=mock_redis):
                tracker._repair_missing_redis_tracking(
                    execution={
                        'issue_number': 99,
                        'agent': 'fallback_agent',
                        'task_id': None
                    },
                    project='fallback-proj',
                    container_names=['claude-agent-fallback']
                )

        mapping = mock_redis.hset.call_args[1]['mapping']
        assert mapping['agent'] == 'fallback_agent'
        assert mapping['project'] == 'fallback-proj'
        assert mapping['issue_number'] == '99'

    def test_repair_with_task_id_validation_pass(self, tmp_path):
        """When container's task_id matches execution's task_id, repair succeeds."""
        tracker = self._make_tracker(tmp_path)

        # Container has matching task_id
        inspect_output = 'sha256xyz|code_agent|myproject|task-123|42|run-5\n'
        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = inspect_output

        mock_redis = MagicMock()

        with patch('subprocess.run', return_value=mock_subprocess_result):
            with patch('redis.Redis', return_value=mock_redis):
                tracker._repair_missing_redis_tracking(
                    execution={
                        'issue_number': 42,
                        'agent': 'code_agent',
                        'task_id': 'task-123',
                        'column': 'Development'
                    },
                    project='myproject',
                    container_names=['claude-agent-myproject-task123']
                )

        # Verify repair succeeded
        mock_redis.hset.assert_called_once()
        mapping = mock_redis.hset.call_args[1]['mapping']
        assert mapping['task_id'] == 'task-123'
        assert mapping['issue_number'] == '42'
        assert mapping['repaired'] == 'true'

    def test_repair_with_task_id_validation_fail(self, tmp_path):
        """When container's task_id doesn't match execution's task_id, repair is skipped."""
        tracker = self._make_tracker(tmp_path)

        # Container has DIFFERENT task_id
        inspect_output = 'sha256xyz|code_agent|myproject|task-999|42|run-5\n'
        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = inspect_output

        mock_redis = MagicMock()

        with patch('subprocess.run', return_value=mock_subprocess_result):
            with patch('redis.Redis', return_value=mock_redis):
                tracker._repair_missing_redis_tracking(
                    execution={
                        'issue_number': 42,
                        'agent': 'code_agent',
                        'task_id': 'task-123',  # Different from container
                        'column': 'Development'
                    },
                    project='myproject',
                    container_names=['claude-agent-myproject-task999']
                )

        # Verify repair was SKIPPED due to task_id mismatch
        mock_redis.hset.assert_not_called()

    def test_repair_issue_validation_without_task_id(self, tmp_path):
        """When task_id not available, fall back to issue_number validation."""
        tracker = self._make_tracker(tmp_path)

        # Container has no task_id, but has issue_number
        inspect_output = 'sha256xyz|code_agent|myproject|unknown|42|run-5\n'
        mock_subprocess_result = MagicMock()
        mock_subprocess_result.returncode = 0
        mock_subprocess_result.stdout = inspect_output

        mock_redis = MagicMock()

        with patch('subprocess.run', return_value=mock_subprocess_result):
            with patch('redis.Redis', return_value=mock_redis):
                tracker._repair_missing_redis_tracking(
                    execution={
                        'issue_number': 42,
                        'agent': 'code_agent',
                        'task_id': None,  # No task_id available
                        'column': 'Development'
                    },
                    project='myproject',
                    container_names=['claude-agent-myproject-old']
                )

        # Verify repair succeeded via issue_number validation
        mock_redis.hset.assert_called_once()
        mapping = mock_redis.hset.call_args[1]['mapping']
        assert mapping['issue_number'] == '42'


class TestDiscoverContainersForExecution:
    """Tests for _discover_containers_for_execution hierarchical discovery."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def test_uses_provided_containers(self, tmp_path):
        """When containers provided, use them without querying Docker."""
        tracker = self._make_tracker(tmp_path)

        execution = {
            'issue_number': 42,
            'task_id': 'task-123',
            'agent': 'code_agent'
        }
        provided = ['container-1', 'container-2']

        with patch('subprocess.run') as mock_run:
            result = tracker._discover_containers_for_execution(
                'myproject', execution, provided
            )

        # Should return provided containers without calling docker
        assert result == provided
        mock_run.assert_not_called()

    def test_discover_by_task_id_label(self, tmp_path):
        """Discover containers via task_id label (most precise)."""
        tracker = self._make_tracker(tmp_path)

        execution = {
            'issue_number': 42,
            'task_id': 'task-123',
            'agent': 'code_agent'
        }

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = 'claude-agent-myproject-task123\n'

        with patch('subprocess.run', return_value=mock_result) as mock_run:
            containers = tracker._discover_containers_for_execution(
                'myproject', execution, []
            )

        assert containers == ['claude-agent-myproject-task123']

        # Verify docker ps called with task_id filter
        call_args = mock_run.call_args[0][0]
        assert 'docker' in call_args
        assert 'ps' in call_args
        assert any('label=org.clauditoreum.task_id=task-123' in str(arg) for arg in call_args)

    def test_discover_by_project_issue_labels(self, tmp_path):
        """When task_id unavailable, fall back to project+issue labels."""
        tracker = self._make_tracker(tmp_path)

        execution = {
            'issue_number': 42,
            'task_id': None,  # No task_id
            'agent': 'code_agent'
        }

        # First call (task_id) returns nothing, second call (project+issue) succeeds
        mock_result_empty = MagicMock()
        mock_result_empty.returncode = 0
        mock_result_empty.stdout = ''

        mock_result_success = MagicMock()
        mock_result_success.returncode = 0
        mock_result_success.stdout = 'claude-agent-myproject-legacy\n'

        with patch('subprocess.run', side_effect=[mock_result_success]) as mock_run:
            containers = tracker._discover_containers_for_execution(
                'myproject', execution, []
            )

        assert containers == ['claude-agent-myproject-legacy']

        # Verify docker ps called with project+issue filters
        call_args = mock_run.call_args[0][0]
        assert 'docker' in call_args
        assert 'ps' in call_args
        # Should have both project and issue_number filters
        assert any('label=org.clauditoreum.project=myproject' in str(arg) for arg in call_args)

    def test_discover_by_name_pattern_fallback(self, tmp_path):
        """When labels unavailable, fall back to name pattern matching."""
        tracker = self._make_tracker(tmp_path)

        execution = {
            'issue_number': 42,
            'task_id': None,
            'agent': 'code_agent'
        }

        # Task ID query fails, project+issue query fails, name pattern succeeds
        mock_result_empty = MagicMock()
        mock_result_empty.returncode = 0
        mock_result_empty.stdout = ''

        mock_result_name = MagicMock()
        mock_result_name.returncode = 0
        mock_result_name.stdout = 'claude-agent-myproject-old1\nclaude-agent-myproject-old2\n'

        with patch('subprocess.run', side_effect=[mock_result_empty, mock_result_name]) as mock_run:
            containers = tracker._discover_containers_for_execution(
                'myproject', execution, []
            )

        assert len(containers) == 2
        assert 'claude-agent-myproject-old1' in containers

    def test_discover_returns_empty_when_none_found(self, tmp_path):
        """When no containers found by any method, return empty list."""
        tracker = self._make_tracker(tmp_path)

        execution = {
            'issue_number': 42,
            'task_id': None,
            'agent': 'code_agent'
        }

        mock_result_empty = MagicMock()
        mock_result_empty.returncode = 0
        mock_result_empty.stdout = ''

        with patch('subprocess.run', return_value=mock_result_empty):
            containers = tracker._discover_containers_for_execution(
                'myproject', execution, []
            )

        assert containers == []


class TestCheckRedisTrackingUseScanIter:
    """Verify _check_redis_tracking_for_agent uses scan_iter, not keys()."""

    def _make_tracker(self, tmp_path):
        cls = _import_tracker_class(tmp_path)
        return cls(state_dir=tmp_path)

    def test_uses_scan_iter_not_keys(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter([])

        with patch('redis.Redis', return_value=mock_redis):
            result = tracker._check_redis_tracking_for_agent('proj', 'agent', 42)

        mock_redis.scan_iter.assert_called_once_with(match='agent:container:*', count=100)
        mock_redis.keys.assert_not_called()
        assert result is False

    def test_finds_matching_container(self, tmp_path):
        tracker = self._make_tracker(tmp_path)
        mock_redis = MagicMock()
        mock_redis.scan_iter.return_value = iter(['agent:container:claude-agent-proj-task1'])
        mock_redis.hgetall.return_value = {
            'project': 'proj',
            'agent': 'code_agent',
            'issue_number': '42'
        }

        with patch('redis.Redis', return_value=mock_redis):
            result = tracker._check_redis_tracking_for_agent('proj', 'code_agent', 42)

        assert result is True

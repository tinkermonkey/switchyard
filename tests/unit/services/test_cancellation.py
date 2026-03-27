"""
Unit tests for the cancellation subsystem.

Tests:
- CancellationSignal: cancel/is_cancelled/clear with Redis and in-memory fallback
- CancellationError: propagation through agent_executor retry loop
- Review cycle cancellation checks
- cancel_issue_work orchestration
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock


class TestCancellationSignal:
    """Test CancellationSignal with in-memory fallback (Redis unavailable locally)."""

    def test_cancel_and_is_cancelled(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        # Force in-memory mode by ensuring Redis fails
        signal._redis = None

        assert not signal.is_cancelled("proj", 42)

        signal.cancel("proj", 42, "test reason")
        assert signal.is_cancelled("proj", 42)

    def test_clear_removes_signal(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        signal._redis = None

        signal.cancel("proj", 42, "test")
        assert signal.is_cancelled("proj", 42)

        signal.clear("proj", 42)
        assert not signal.is_cancelled("proj", 42)

    def test_different_issues_are_independent(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        signal._redis = None

        signal.cancel("proj", 42, "test")
        assert signal.is_cancelled("proj", 42)
        assert not signal.is_cancelled("proj", 43)
        assert not signal.is_cancelled("other_proj", 42)

    def test_cancel_with_redis(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        mock_redis = MagicMock()
        mock_redis.ping.return_value = True
        mock_redis.setex.return_value = True
        mock_redis.exists.return_value = True
        signal._redis = mock_redis

        signal.cancel("proj", 42, "test reason")

        # Should set in both Redis and in-memory
        mock_redis.setex.assert_called_once_with(
            "cancelled:proj:42", 3600, "test reason"
        )
        assert (("proj", 42)) in signal._in_memory

    def test_is_cancelled_checks_redis_when_not_in_memory(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        mock_redis = MagicMock()
        mock_redis.exists.return_value = True
        signal._redis = mock_redis

        # Not in _in_memory, but Redis says yes
        assert signal.is_cancelled("proj", 42)
        mock_redis.exists.assert_called_once_with("cancelled:proj:42")
        # Should sync to in-memory
        assert ("proj", 42) in signal._in_memory

    def test_clear_with_redis(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        mock_redis = MagicMock()
        signal._redis = mock_redis

        signal._in_memory.add(("proj", 42))
        signal.clear("proj", 42)

        mock_redis.delete.assert_called_once_with("cancelled:proj:42")
        assert ("proj", 42) not in signal._in_memory

    def test_redis_failure_falls_back_to_memory(self):
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        mock_redis = MagicMock()
        mock_redis.setex.side_effect = Exception("Connection refused")
        mock_redis.exists.side_effect = Exception("Connection refused")
        signal._redis = mock_redis

        # cancel should still work via in-memory
        signal.cancel("proj", 42, "test")
        # is_cancelled should find it in memory despite Redis failure
        assert signal.is_cancelled("proj", 42)


class TestCancellationError:
    """Test CancellationError exception behavior."""

    def test_is_exception_subclass(self):
        from services.cancellation import CancellationError

        assert issubclass(CancellationError, Exception)

    def test_carries_message(self):
        from services.cancellation import CancellationError

        err = CancellationError("Work cancelled for proj/#42")
        assert "proj/#42" in str(err)

    def test_caught_before_generic_exception(self):
        """CancellationError can be caught specifically before generic Exception."""
        from services.cancellation import CancellationError

        caught_specific = False
        try:
            raise CancellationError("test")
        except CancellationError:
            caught_specific = True
        except Exception:
            pass

        assert caught_specific


class TestCancelIssueWork:
    """Test the cancel_issue_work orchestration function."""

    @patch('services.cancellation.kill_containers_for_issue')
    @patch('services.cancellation.get_cancellation_signal')
    def test_sets_signal_and_kills_containers(self, mock_get_signal, mock_kill):
        from services.cancellation import cancel_issue_work

        mock_signal = MagicMock()
        mock_get_signal.return_value = mock_signal
        mock_kill.return_value = 2

        mock_rce = MagicMock()
        mock_rce.active_cycles = {}
        mock_wet = MagicMock()
        mock_wet.get_execution_history.return_value = []

        mock_we_module = MagicMock()
        mock_we_module.work_execution_tracker = mock_wet

        with patch.dict('sys.modules', {
            'services.review_cycle': MagicMock(review_cycle_executor=mock_rce),
            'services.work_execution_state': mock_we_module,
        }):
            cancel_issue_work("proj", 42, "test reason")

        mock_signal.cancel.assert_called_once_with("proj", 42, "test reason")
        mock_kill.assert_called_once_with("proj", 42)

    @patch('services.cancellation.kill_containers_for_issue')
    @patch('services.cancellation.get_cancellation_signal')
    def test_removes_active_review_cycle(self, mock_get_signal, mock_kill):
        from services.cancellation import cancel_issue_work

        mock_signal = MagicMock()
        mock_get_signal.return_value = mock_signal
        mock_kill.return_value = 0

        mock_rce = MagicMock()
        mock_rce.active_cycles = {42: MagicMock()}
        mock_wet = MagicMock()
        mock_wet.get_execution_history.return_value = []

        mock_we_module = MagicMock()
        mock_we_module.work_execution_tracker = mock_wet

        with patch.dict('sys.modules', {
            'services.review_cycle': MagicMock(review_cycle_executor=mock_rce),
            'services.work_execution_state': mock_we_module,
        }):
            cancel_issue_work("proj", 42, "test reason")

        assert 42 not in mock_rce.active_cycles

    @patch('services.cancellation.kill_containers_for_issue')
    @patch('services.cancellation.get_cancellation_signal')
    def test_marks_in_progress_executions_as_cancelled(self, mock_get_signal, mock_kill):
        from services.cancellation import cancel_issue_work

        mock_signal = MagicMock()
        mock_get_signal.return_value = mock_signal
        mock_kill.return_value = 0

        mock_rce = MagicMock()
        mock_rce.active_cycles = {}
        mock_wet = MagicMock()
        mock_wet.get_execution_history.return_value = [
            {'outcome': 'success', 'agent': 'old_agent', 'column': 'Done'},
            {'outcome': 'in_progress', 'agent': 'active_agent', 'column': 'Testing'},
        ]

        mock_we_module = MagicMock()
        mock_we_module.work_execution_tracker = mock_wet

        with patch.dict('sys.modules', {
            'services.review_cycle': MagicMock(review_cycle_executor=mock_rce),
            'services.work_execution_state': mock_we_module,
        }):
            cancel_issue_work("proj", 42, "killed by user")

        mock_wet.record_execution_outcome.assert_called_once_with(
            issue_number=42,
            column='Testing',
            agent='active_agent',
            outcome='cancelled',
            project_name='proj',
            error='killed by user'
        )


class TestAgentExecutorCancellation:
    """Test CancellationError propagation through agent_executor retry loop."""

    @pytest.mark.asyncio
    async def test_cancellation_check_before_circuit_breaker(self):
        """If issue is cancelled, CancellationError is raised before circuit breaker."""
        import os
        if not os.path.isdir('/app'):
            pytest.skip("Requires Docker container environment")

        from services.cancellation import CancellationError

        mock_project_config = MagicMock()
        mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        mock_agent_config = MagicMock()
        mock_agent_config.retries = 2

        with patch('services.agent_executor.get_observability_manager'), \
             patch('services.agent_executor.PipelineFactory') as mock_factory_cls, \
             patch('services.agent_executor.GitHubIntegration'), \
             patch('services.agent_executor.config_manager') as mock_cm, \
             patch('services.cancellation.get_cancellation_signal') as mock_signal:

            mock_cm.get_project_config.return_value = mock_project_config
            mock_cm.get_project_agent_config.return_value = mock_agent_config

            mock_agent_stage = MagicMock()
            mock_agent_stage.agent_config = mock_agent_config
            mock_agent_stage.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_factory_cls.return_value.create_agent.return_value = mock_agent_stage

            mock_signal.return_value.is_cancelled.return_value = True

            from services.agent_executor import AgentExecutor
            executor = AgentExecutor()

            with pytest.raises(CancellationError):
                await executor.execute_agent(
                    agent_name="test_agent",
                    project_name="proj",
                    task_context={
                        'issue_number': 42,
                        'column': 'Testing',
                        'repository': 'org/repo',
                        'skip_workspace_prep': True,
                    }
                )


class TestWorkExecutionStateCancelled:
    """Test that 'cancelled' outcome is handled correctly in should_execute_work."""

    def test_cancelled_outcome_allows_re_execution(self, tmp_path, monkeypatch):
        """A cancelled execution should allow re-trigger (like failure)."""
        # Override ORCHESTRATOR_ROOT to avoid /app filesystem requirement
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        from services.work_execution_state import WorkExecutionStateTracker

        tracker = WorkExecutionStateTracker(state_dir=tmp_path)

        with patch.object(tracker, 'load_state') as mock_load:
            mock_load.return_value = {
                'execution_history': [
                    {
                        'column': 'Testing',
                        'agent': 'test_agent',
                        'outcome': 'cancelled',
                        'timestamp': '2025-01-01T00:00:00+00:00',
                    }
                ],
                'status_changes': []
            }

            should_execute, reason = tracker.should_execute_work(
                issue_number=42,
                column='Testing',
                agent='test_agent',
                trigger_source='failsafe',
                project_name='proj'
            )

            assert should_execute
            assert reason == "retry_after_cancelled"


class TestSignalClearingAfterKill:
    """Test that cancellation signals behave correctly after Web UI kills."""

    def test_signal_cleared_after_cancel_issue_work(self):
        """After cancel_issue_work + clear, the issue should not be cancelled."""
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        signal._redis = None

        signal.cancel("proj", 42, "killed via UI")
        assert signal.is_cancelled("proj", 42)

        signal.clear("proj", 42)
        assert not signal.is_cancelled("proj", 42)

    @patch('services.cancellation.kill_containers_for_issue')
    @patch('services.cancellation.get_cancellation_signal')
    def test_cancel_issue_work_leaves_signal_set(self, mock_get_signal, mock_kill):
        """cancel_issue_work must leave the signal set (not clear it).

        The kill endpoint used to call cancel + clear in sequence, which meant
        retry loops never saw the cancellation. Now the signal stays set until
        a new pipeline run clears it.
        """
        from services.cancellation import cancel_issue_work

        mock_signal = MagicMock()
        mock_get_signal.return_value = mock_signal
        mock_kill.return_value = 0

        mock_rce = MagicMock()
        mock_rce.active_cycles = {}
        mock_wet = MagicMock()
        mock_wet.get_execution_history.return_value = []

        mock_we_module = MagicMock()
        mock_we_module.work_execution_tracker = mock_wet

        with patch.dict('sys.modules', {
            'services.review_cycle': MagicMock(review_cycle_executor=mock_rce),
            'services.work_execution_state': mock_we_module,
        }):
            cancel_issue_work("proj", 42, "killed via Web UI")

        # Signal must be set...
        mock_signal.cancel.assert_called_once_with("proj", 42, "killed via Web UI")
        # ...and NOT cleared
        mock_signal.clear.assert_not_called()

    def test_cancel_is_idempotent(self):
        """Calling cancel() twice with the signal already set is harmless (Fix #4)."""
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        signal._redis = None

        signal.cancel("proj", 42, "first call")
        signal.cancel("proj", 42, "second call")
        assert signal.is_cancelled("proj", 42)

        signal.clear("proj", 42)
        assert not signal.is_cancelled("proj", 42)


class TestContainerRecoveryOutcome:
    """Test cleanup_execution_state outcome parameter (Fix #6)."""

    def test_cleanup_with_default_failed_outcome(self):
        """Default outcome is 'failed' for backward compatibility."""
        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            from services.agent_container_recovery import AgentContainerRecovery
            recovery = AgentContainerRecovery.__new__(AgentContainerRecovery)

            recovery.cleanup_execution_state("proj", 42, "test_agent", "orchestrator restart")

            mock_tracker.record_execution_outcome.assert_called_once_with(
                issue_number=42,
                column='unknown',
                agent='test_agent',
                outcome='failed',
                project_name='proj',
                error='orchestrator restart'
            )

    def test_cleanup_with_cancelled_outcome(self):
        """Cancellation path passes outcome='cancelled'."""
        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            from services.agent_container_recovery import AgentContainerRecovery
            recovery = AgentContainerRecovery.__new__(AgentContainerRecovery)

            recovery.cleanup_execution_state(
                "proj", 42, "test_agent",
                "Container killed: issue was cancelled",
                outcome='cancelled'
            )

            mock_tracker.record_execution_outcome.assert_called_once_with(
                issue_number=42,
                column='unknown',
                agent='test_agent',
                outcome='cancelled',
                project_name='proj',
                error='Container killed: issue was cancelled'
            )


class TestWorkerPoolCancellationMetrics:
    """Test that cancelled tasks record metrics and break the retry loop (Fix #8)."""

    @pytest.mark.asyncio
    async def test_cancellation_error_breaks_retry_loop_and_records_metrics(self):
        """CancellationError should break the retry loop and record failure metrics."""
        import sys
        from services.cancellation import CancellationError

        # Mock modules that trigger Docker-only imports (agents package)
        mock_agents_module = MagicMock()
        mock_state_module = MagicMock()
        modules_to_mock = {
            'agents': mock_agents_module,
            'agents.orchestrator_integration': mock_agents_module,
        }

        # Ensure worker_pool is freshly imported with mocked dependencies
        saved_modules = {k: sys.modules.pop(k) for k in ['services.worker_pool'] if k in sys.modules}
        try:
            with patch.dict('sys.modules', modules_to_mock):
                from services.worker_pool import TaskWorker

                mock_queue = MagicMock()
                mock_metrics = MagicMock()
                mock_logger = MagicMock()

                worker = TaskWorker(
                    worker_id=1,
                    task_queue=mock_queue,
                    metrics=mock_metrics,
                    orchestrator_logger=mock_logger,
                )

                mock_task = MagicMock()
                mock_task.id = "test-task-1"
                mock_task.agent = "test_agent"
                mock_task.project = "proj"
                mock_task.context = {}

                # Dequeue returns task once, then None (triggers sleep path, then loop exit)
                mock_queue.dequeue.side_effect = [mock_task, None]

                with patch('services.worker_pool.process_task_integrated', side_effect=CancellationError("cancelled")):
                    async def stop_after_first():
                        import asyncio
                        await asyncio.sleep(0.05)
                        worker.running = False

                    import asyncio
                    await asyncio.gather(worker.run(), stop_after_first())

                assert worker.tasks_failed == 1
                assert worker.tasks_processed == 0
                mock_metrics.record_task_complete.assert_called_once()
                call_args = mock_metrics.record_task_complete.call_args
                assert call_args[0][0] == "test_agent"
                assert call_args[1]['success'] is False
        finally:
            # Restore original modules
            sys.modules.pop('services.worker_pool', None)
            sys.modules.update(saved_modules)


class TestPipelineQueueBlockedIssues:
    """Test that get_blocked_issues includes cancelled outcomes (Fix #13)."""

    def test_cancelled_outcome_counts_as_blocked(self, tmp_path, monkeypatch):
        """An issue with 'cancelled' last outcome should appear as blocked."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        from services.pipeline_queue_manager import PipelineQueueManager

        manager = PipelineQueueManager("proj", "dev_board", state_dir=tmp_path)

        mock_state = {
            'execution_history': [
                {
                    'column': 'Development',
                    'agent': 'senior_software_engineer',
                    'outcome': 'cancelled',
                    'timestamp': '2025-01-01T00:00:00+00:00',
                    'error': 'killed by user',
                }
            ],
            'status_changes': []
        }

        with patch.object(manager, 'load_queue', return_value=[
            {'issue_number': 42, 'status': 'active', 'position_in_column': 0},
        ]), \
            patch('services.work_execution_state.work_execution_tracker') as mock_tracker, \
            patch('subprocess.run') as mock_subprocess:

            mock_tracker.load_state.return_value = mock_state
            # No running container
            mock_subprocess.return_value = MagicMock(stdout='', returncode=0)

            blocked = manager.get_blocked_issues()

        assert len(blocked) == 1
        assert blocked[0]['issue_number'] == 42
        assert blocked[0]['failed_agent'] == 'senior_software_engineer'

    def test_success_outcome_is_not_blocked(self, tmp_path, monkeypatch):
        """An issue with 'success' last outcome should NOT appear as blocked."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        from services.pipeline_queue_manager import PipelineQueueManager

        manager = PipelineQueueManager("proj", "dev_board", state_dir=tmp_path)

        mock_state = {
            'execution_history': [
                {
                    'column': 'Development',
                    'agent': 'senior_software_engineer',
                    'outcome': 'success',
                    'timestamp': '2025-01-01T00:00:00+00:00',
                }
            ],
            'status_changes': []
        }

        with patch.object(manager, 'load_queue', return_value=[
            {'issue_number': 42, 'status': 'active', 'position_in_column': 0},
        ]), \
            patch('services.work_execution_state.work_execution_tracker') as mock_tracker, \
            patch('subprocess.run') as mock_subprocess:

            mock_tracker.load_state.return_value = mock_state
            mock_subprocess.return_value = MagicMock(stdout='', returncode=0)

            blocked = manager.get_blocked_issues()

        assert len(blocked) == 0


class TestKillContainersForIssue:
    """Test kill_containers_for_issue with both Redis and Docker label strategies."""

    @patch('services.cancellation.subprocess.run')
    @patch('services.cancellation.get_cancellation_signal')
    def test_kills_containers_via_redis(self, mock_get_signal, mock_subprocess):
        """Strategy 1: Find and kill containers via Redis agent:container:* keys."""
        from services.cancellation import kill_containers_for_issue

        mock_redis = MagicMock()
        mock_signal = MagicMock()
        mock_signal._get_redis.return_value = mock_redis
        mock_get_signal.return_value = mock_signal

        # Redis has one matching container
        mock_redis.scan_iter.side_effect = [
            # First call: agent:container:* keys
            iter(['agent:container:abc123']),
            # Second call: repair_cycle:container:* keys
            iter([]),
        ]
        mock_redis.hgetall.return_value = {
            'project': 'proj',
            'issue_number': '42',
            'container_name': 'claude-agent-proj-42',
        }

        # docker rm -f succeeds
        mock_subprocess.return_value = MagicMock(returncode=0, stderr='')
        # Docker labels returns nothing (no label-based containers)
        docker_ps_result = MagicMock(returncode=0, stdout='', stderr='')

        def subprocess_side_effect(cmd, **kwargs):
            if cmd[1] == 'ps':
                return docker_ps_result
            return MagicMock(returncode=0, stderr='')

        mock_subprocess.side_effect = subprocess_side_effect

        killed = kill_containers_for_issue("proj", 42)

        assert killed == 1
        mock_redis.delete.assert_called_with('agent:container:abc123')

    @patch('services.cancellation.subprocess.run')
    @patch('services.cancellation.get_cancellation_signal')
    def test_kills_containers_via_docker_labels(self, mock_get_signal, mock_subprocess):
        """Strategy 2: Find and kill containers via Docker labels when Redis is unavailable."""
        from services.cancellation import kill_containers_for_issue

        mock_signal = MagicMock()
        mock_signal._get_redis.return_value = None  # Redis unavailable
        mock_get_signal.return_value = mock_signal

        # docker ps returns one container, docker rm -f succeeds
        docker_ps_result = MagicMock(returncode=0, stdout='abc123\n', stderr='')
        docker_rm_result = MagicMock(returncode=0, stderr='')

        call_count = [0]
        def subprocess_side_effect(cmd, **kwargs):
            call_count[0] += 1
            if cmd[1] == 'ps':
                return docker_ps_result
            return docker_rm_result

        mock_subprocess.side_effect = subprocess_side_effect

        killed = kill_containers_for_issue("proj", 42)

        assert killed == 1

    @patch('services.cancellation.subprocess.run')
    @patch('services.cancellation.get_cancellation_signal')
    def test_returns_zero_when_no_containers(self, mock_get_signal, mock_subprocess):
        """Returns 0 when no containers found via either strategy."""
        from services.cancellation import kill_containers_for_issue

        mock_redis = MagicMock()
        mock_signal = MagicMock()
        mock_signal._get_redis.return_value = mock_redis
        mock_get_signal.return_value = mock_signal

        # No matching Redis keys
        mock_redis.scan_iter.side_effect = [iter([]), iter([])]

        # No Docker label matches
        mock_subprocess.return_value = MagicMock(returncode=0, stdout='', stderr='')

        killed = kill_containers_for_issue("proj", 42)
        assert killed == 0

    @patch('services.cancellation.subprocess.run')
    @patch('services.cancellation.get_cancellation_signal')
    def test_skips_non_matching_redis_containers(self, mock_get_signal, mock_subprocess):
        """Containers for other issues should not be killed."""
        from services.cancellation import kill_containers_for_issue

        mock_redis = MagicMock()
        mock_signal = MagicMock()
        mock_signal._get_redis.return_value = mock_redis
        mock_get_signal.return_value = mock_signal

        mock_redis.scan_iter.side_effect = [
            iter(['agent:container:other']),
            iter([]),
        ]
        mock_redis.hgetall.return_value = {
            'project': 'proj',
            'issue_number': '99',  # Different issue
            'container_name': 'claude-agent-proj-99',
        }

        mock_subprocess.return_value = MagicMock(returncode=0, stdout='', stderr='')

        killed = kill_containers_for_issue("proj", 42)
        assert killed == 0

    @patch('services.cancellation.subprocess.run')
    @patch('services.cancellation.get_cancellation_signal')
    def test_handles_docker_rm_failure(self, mock_get_signal, mock_subprocess):
        """docker rm -f failure should not count as killed."""
        from services.cancellation import kill_containers_for_issue

        mock_signal = MagicMock()
        mock_signal._get_redis.return_value = None
        mock_get_signal.return_value = mock_signal

        # docker ps finds a container, but docker rm -f fails
        call_count = [0]
        def subprocess_side_effect(cmd, **kwargs):
            call_count[0] += 1
            if cmd[1] == 'ps':
                return MagicMock(returncode=0, stdout='abc123\n', stderr='')
            return MagicMock(returncode=1, stderr='No such container')

        mock_subprocess.side_effect = subprocess_side_effect

        killed = kill_containers_for_issue("proj", 42)
        assert killed == 0

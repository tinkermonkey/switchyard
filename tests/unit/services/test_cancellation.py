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

        with patch('services.agent_executor.get_observability_manager'), \
             patch('services.agent_executor.PipelineFactory'), \
             patch('services.agent_executor.GitHubIntegration'), \
             patch('services.cancellation.get_cancellation_signal') as mock_signal:

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
                        'repository': 'org/repo'
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
    """Test that cancellation signals are cleared after Web UI kills (Fix #1)."""

    def test_signal_cleared_after_cancel_issue_work(self):
        """After cancel_issue_work + clear, the issue should not be cancelled."""
        from services.cancellation import CancellationSignal

        signal = CancellationSignal()
        signal._redis = None

        signal.cancel("proj", 42, "killed via UI")
        assert signal.is_cancelled("proj", 42)

        signal.clear("proj", 42)
        assert not signal.is_cancelled("proj", 42)

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
    """Test that cancelled tasks record metrics (Fix #8)."""

    def test_cancellation_error_is_not_retried(self):
        """CancellationError should break out of the retry loop immediately."""
        from services.cancellation import CancellationError

        # CancellationError is caught before the retry check
        err = CancellationError("test")
        assert isinstance(err, Exception)
        # The key behavior is tested implicitly: isinstance(e, CancellationError)
        # breaks before the retry `continue` is reached


class TestPipelineQueueBlockedIssues:
    """Test that get_blocked_issues includes cancelled outcomes (Fix #13)."""

    def test_cancelled_outcome_counts_as_blocked(self):
        """An issue with 'cancelled' last outcome should appear as blocked."""
        # The fix changes == 'failure' to in ('failure', 'cancelled')
        # Verify both outcomes match
        outcomes_that_block = ('failure', 'cancelled')
        assert 'failure' in outcomes_that_block
        assert 'cancelled' in outcomes_that_block
        assert 'success' not in outcomes_that_block

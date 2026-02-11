"""
Unit tests for _move_card_with_retry() — the post-review-cycle card move
retry logic in ProjectMonitor.
"""
import pytest
from unittest.mock import Mock, patch, call
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


class TestMoveCardWithRetry:
    """Test ProjectMonitor._move_card_with_retry()."""

    @pytest.fixture
    def mock_config_manager(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        return config_manager

    @pytest.fixture
    def monitor(self, mock_config_manager):
        return ProjectMonitor(Mock(), mock_config_manager)

    @pytest.fixture
    def call_kwargs(self):
        """Common keyword arguments for _move_card_with_retry."""
        project_config = Mock()
        project_config.github = {'org': 'test-org'}
        return dict(
            project_name='test-project',
            board_name='SDLC Execution',
            issue_number=42,
            source_column='Code Review',
            target_column='Testing',
            project_config=project_config,
            repository='test-repo',
            workspace_type='issues',
            discussion_id='D_abc123',
            pipeline_run=Mock(id='run-1'),
            loop=Mock(),
        )

    def _call(self, monitor, call_kwargs, move_side_effects):
        """Invoke the real _move_card_with_retry with mocked dependencies."""
        mock_progression = Mock()

        effects = list(move_side_effects)
        def side_effect(**kwargs):
            eff = effects.pop(0)
            if isinstance(eff, Exception):
                raise eff
            return eff
        mock_progression.move_issue_to_column.side_effect = side_effect

        mock_emitter = Mock()
        mock_github = Mock()

        with patch('time.sleep') as mock_sleep, \
             patch('services.pipeline_progression.PipelineProgression', return_value=mock_progression), \
             patch('monitoring.decision_events.DecisionEventEmitter', return_value=mock_emitter), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('services.github_integration.GitHubIntegration', return_value=mock_github):

            result = monitor._move_card_with_retry(**call_kwargs)

        return {
            'result': result,
            'move_mock': mock_progression.move_issue_to_column,
            'emitter': mock_emitter,
            'github': mock_github,
            'sleep_calls': mock_sleep.call_args_list,
            'loop': call_kwargs['loop'],
        }

    # ---- Success paths ----

    def test_succeeds_on_first_attempt(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [True])

        assert r['result'] is True
        assert r['move_mock'].call_count == 1
        assert r['emitter'].emit_error_decision.call_count == 0
        assert r['loop'].run_until_complete.call_count == 0
        assert r['sleep_calls'] == []

    def test_succeeds_after_two_false_returns(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, False, True])

        assert r['result'] is True
        assert r['move_mock'].call_count == 3
        assert r['emitter'].emit_error_decision.call_count == 0
        assert r['sleep_calls'] == [call(5), call(10)]

    def test_succeeds_after_exception_then_true(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [RuntimeError("timeout"), True])

        assert r['result'] is True
        assert r['move_mock'].call_count == 2
        assert r['emitter'].emit_error_decision.call_count == 0

    def test_succeeds_on_second_attempt_sleeps_once(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, True])

        assert r['result'] is True
        assert r['move_mock'].call_count == 2
        assert r['sleep_calls'] == [call(5)]

    # ---- Failure paths ----

    def test_all_attempts_return_false(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, False, False])

        assert r['result'] is False
        assert r['move_mock'].call_count == 3
        r['emitter'].emit_error_decision.assert_called_once()
        r['loop'].run_until_complete.assert_called_once()
        assert r['sleep_calls'] == [call(5), call(10)]

    def test_all_attempts_raise_exceptions(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [
            RuntimeError("err1"), RuntimeError("err2"), RuntimeError("err3"),
        ])

        assert r['result'] is False
        assert r['move_mock'].call_count == 3
        r['emitter'].emit_error_decision.assert_called_once()
        r['loop'].run_until_complete.assert_called_once()

    def test_mixed_false_and_exceptions(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, RuntimeError("oops"), False])

        assert r['result'] is False
        assert r['move_mock'].call_count == 3
        r['emitter'].emit_error_decision.assert_called_once()
        r['loop'].run_until_complete.assert_called_once()

    # ---- Backoff timing ----

    def test_backoff_timing(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, False, False])

        assert r['sleep_calls'] == [call(5), call(10)]

    # ---- CancellationError propagation ----

    def test_cancellation_error_propagates_immediately(self, monitor, call_kwargs):
        from services.cancellation import CancellationError

        with pytest.raises(CancellationError):
            self._call(monitor, call_kwargs, [CancellationError("cancelled")])

    def test_cancellation_error_on_second_attempt_propagates(self, monitor, call_kwargs):
        from services.cancellation import CancellationError

        with pytest.raises(CancellationError):
            self._call(monitor, call_kwargs, [False, CancellationError("cancelled")])

    # ---- Error detail tracking ----

    def test_last_error_detail_from_false_in_error_event(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, False, False])

        event_kwargs = r['emitter'].emit_error_decision.call_args
        assert "returned False" in event_kwargs.kwargs['error_message']

    def test_last_error_detail_from_exception_in_error_event(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [
            False, False, RuntimeError("GraphQL timeout"),
        ])

        event_kwargs = r['emitter'].emit_error_decision.call_args
        assert "GraphQL timeout" in event_kwargs.kwargs['error_message']

    # ---- Error event arguments ----

    def test_error_event_has_correct_fields(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, False, False])

        event_kwargs = r['emitter'].emit_error_decision.call_args.kwargs
        assert event_kwargs['error_type'] == 'review_cycle_card_move_failure'
        assert event_kwargs['success'] is False
        assert event_kwargs['project'] == 'test-project'
        assert event_kwargs['pipeline_run_id'] == 'run-1'
        ctx = event_kwargs['context']
        assert ctx['issue_number'] == 42
        assert ctx['source_column'] == 'Code Review'
        assert ctx['target_column'] == 'Testing'
        assert ctx['board'] == 'SDLC Execution'

    def test_pipeline_run_none_handled(self, monitor, call_kwargs):
        call_kwargs['pipeline_run'] = None
        r = self._call(monitor, call_kwargs, [False, False, False])

        event_kwargs = r['emitter'].emit_error_decision.call_args.kwargs
        assert event_kwargs['pipeline_run_id'] is None

    # ---- move_issue_to_column called with correct args ----

    def test_move_called_with_correct_trigger(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [True])

        r['move_mock'].assert_called_once_with(
            project_name='test-project',
            board_name='SDLC Execution',
            issue_number=42,
            target_column='Testing',
            trigger='review_cycle_completion',
        )

    # ---- Error reporting isolation ----

    def test_emit_failure_does_not_prevent_github_comment(self, monitor, call_kwargs):
        mock_emitter = Mock()
        mock_emitter.emit_error_decision.side_effect = RuntimeError("emit boom")
        mock_github = Mock()

        with patch('time.sleep'), \
             patch('services.pipeline_progression.PipelineProgression') as mock_pp, \
             patch('monitoring.decision_events.DecisionEventEmitter', return_value=mock_emitter), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('services.github_integration.GitHubIntegration', return_value=mock_github):
            mock_pp.return_value.move_issue_to_column.return_value = False
            result = monitor._move_card_with_retry(**call_kwargs)

        assert result is False
        # GitHub comment should still be attempted despite emit failure
        call_kwargs['loop'].run_until_complete.assert_called_once()

    def test_github_comment_failure_does_not_raise(self, monitor, call_kwargs):
        call_kwargs['loop'].run_until_complete.side_effect = RuntimeError("comment boom")

        with patch('time.sleep'), \
             patch('services.pipeline_progression.PipelineProgression') as mock_pp, \
             patch('monitoring.decision_events.DecisionEventEmitter', return_value=Mock()), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('services.github_integration.GitHubIntegration', return_value=Mock()):
            mock_pp.return_value.move_issue_to_column.return_value = False
            # Should not raise despite comment failure
            result = monitor._move_card_with_retry(**call_kwargs)

        assert result is False


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

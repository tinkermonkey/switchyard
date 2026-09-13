"""
Unit tests for _move_card_with_retry() — the post-review-cycle card move
retry logic in ProjectMonitor.
"""
import pytest
from unittest.mock import Mock, patch, call
from services.project_monitor import (
    CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS,
    ProjectMonitor,
)
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

    def _call(self, monitor, call_kwargs, move_side_effects, reset_seconds=None):
        """Invoke the real _move_card_with_retry with mocked dependencies.

        `move_side_effects` entries may be:
          True                 -- the move succeeded
          False                -- it failed, reason unclassified (FAILURE_OTHER)
          CardMoveFailure(...) -- it failed with that exact classification
          Exception            -- the call raised

        `reset_seconds` stubs github_rate_limit_reset_seconds(), and it is
        patched unconditionally on purpose. Without it these assertions read
        the PROCESS-GLOBAL GitHub breaker (get_github_client() is a module
        singleton whose state outlives any one test), so a test that happened
        to trip it earlier in the run would silently turn `[call(5), call(10)]`
        into a quota-window wait. That is precisely the test-order coupling
        #211/#224 was written against; patching here makes "no rate limit in
        play" a stated premise rather than an accident of ordering.
        """
        from services.pipeline_progression import CardMoveFailure, FAILURE_OTHER

        mock_progression = Mock()

        effects = list(move_side_effects)
        def side_effect(**kwargs):
            eff = effects.pop(0)
            if isinstance(eff, Exception):
                raise eff
            if isinstance(eff, CardMoveFailure):
                return False, eff
            if eff:
                return True, None
            return False, CardMoveFailure(
                FAILURE_OTHER, "move_issue_to_column returned False"
            )
        mock_progression.move_issue_to_column_with_reason.side_effect = side_effect

        mock_emitter = Mock()
        mock_github = Mock()

        # Patch the WAIT, not time.sleep: _sleep_interruptibly() deliberately
        # chunks its wait into short slices so a cancellation is noticed during
        # a quarter-hour backoff, so time.sleep() no longer shows the duration
        # the retry policy actually asked for. Its own chunking is covered by
        # TestSleepInterruptibly below.
        with patch.object(ProjectMonitor, '_sleep_interruptibly') as mock_sleep, \
             patch('services.project_monitor.github_rate_limit_reset_seconds',
                   return_value=reset_seconds), \
             patch('services.pipeline_progression.PipelineProgression', return_value=mock_progression), \
             patch('monitoring.decision_events.DecisionEventEmitter', return_value=mock_emitter), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('services.github_integration.GitHubIntegration', return_value=mock_github):

            result = monitor._move_card_with_retry(**call_kwargs)

        return {
            'result': result,
            'move_mock': mock_progression.move_issue_to_column_with_reason,
            'emitter': mock_emitter,
            'github': mock_github,
            # The durations the retry policy asked to wait, in order.
            'sleep_calls': [c.args[0] for c in mock_sleep.call_args_list],
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
        assert r['sleep_calls'] == [5, 10]

    def test_succeeds_after_exception_then_true(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [RuntimeError("timeout"), True])

        assert r['result'] is True
        assert r['move_mock'].call_count == 2
        assert r['emitter'].emit_error_decision.call_count == 0

    def test_succeeds_on_second_attempt_sleeps_once(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, True])

        assert r['result'] is True
        assert r['move_mock'].call_count == 2
        assert r['sleep_calls'] == [5]

    # ---- Failure paths ----

    def test_all_attempts_return_false(self, monitor, call_kwargs):
        r = self._call(monitor, call_kwargs, [False, False, False])

        assert r['result'] is False
        assert r['move_mock'].call_count == 3
        r['emitter'].emit_error_decision.assert_called_once()
        r['loop'].run_until_complete.assert_called_once()
        assert r['sleep_calls'] == [5, 10]

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

        assert r['sleep_calls'] == [5, 10]

    # ---- Rate-limit regime (pipeline run 4cf816cf) ----
    #
    # These drive the loop itself rather than card_move_retry_delay() in
    # isolation. The pure function's own tests cannot show that its verdict
    # reaches time.sleep(), and "the decision was right but nobody acted on it"
    # is the exact shape of the incident.

    def test_a_confirmed_quota_exhaustion_waits_for_the_window(
        self, monitor, call_kwargs
    ):
        """The incident's own number: ~780s left on the window, against a
        ladder that slept 5s and 10s and burned all three attempts inside it."""
        from services.pipeline_progression import CardMoveFailure, FAILURE_RATE_LIMITED

        failure = CardMoveFailure(FAILURE_RATE_LIMITED, "rate_limited: quota gone")
        r = self._call(monitor, call_kwargs, [failure, True], reset_seconds=780.0)

        assert r['result'] is True
        assert r['sleep_calls'] == [785.0], (
            "must wait out the quota window, not retry inside it"
        )

    def test_a_window_beyond_the_cap_stops_retrying_at_once(
        self, monitor, call_kwargs
    ):
        """Beyond the cap there is nothing useful to wait for, so the move is
        abandoned after ONE attempt rather than sleeping through a ladder that
        cannot succeed."""
        from services.pipeline_progression import CardMoveFailure, FAILURE_RATE_LIMITED

        failure = CardMoveFailure(FAILURE_RATE_LIMITED, "rate_limited: quota gone")
        r = self._call(
            monitor, call_kwargs, [failure, failure, failure],
            reset_seconds=CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS + 60,
        )

        assert r['result'] is False
        assert r['move_mock'].call_count == 1
        assert r['sleep_calls'] == []

    def test_the_abandoned_attempt_count_is_reported_honestly(
        self, monitor, call_kwargs
    ):
        """Reports attempts MADE, not max_retries. The old code hard-coded the
        latter, so a move abandoned after one attempt told the operator it had
        been tried three times."""
        from services.pipeline_progression import CardMoveFailure, FAILURE_RATE_LIMITED

        failure = CardMoveFailure(FAILURE_RATE_LIMITED, "rate_limited: quota gone")
        r = self._call(
            monitor, call_kwargs, [failure],
            reset_seconds=CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS + 60,
        )

        message = r['emitter'].emit_error_decision.call_args.kwargs['error_message']
        assert "1 attempt" in message
        assert "3 attempt" not in message

    def test_a_shared_breaker_refusal_is_retried_not_treated_as_terminal(
        self, monitor, call_kwargs
    ):
        """THE REGRESSION GUARD.

        One GitHubBreaker gates the client's GraphQL, REST, HTTP and CLI paths,
        but GitHub meters those as separate quotas. Reading a breaker refusal as
        confirmed GraphQL exhaustion meant a REST exhaustion could abandon a
        card move on attempt 1 and — via card_move_failed — durably retain the
        board's lock over a quota that board never touched.

        A breaker refusal must therefore behave like any other transient
        failure: full ladder, and it can still succeed.
        """
        from services.pipeline_progression import CardMoveFailure, FAILURE_BREAKER_OPEN

        refusal = CardMoveFailure(
            FAILURE_BREAKER_OPEN,
            "GitHub API rate limit exceeded - circuit breaker open",
        )
        r = self._call(
            monitor, call_kwargs, [refusal, refusal, True],
            reset_seconds=3600.0,  # long window — would be 'stop' if misclassified
        )

        assert r['result'] is True, "a breaker refusal must not be terminal"
        assert r['move_mock'].call_count == 3
        assert r['sleep_calls'] == [5, 10], (
            "a breaker refusal takes the ordinary exponential ladder, because it "
            "is not proof that THIS call's quota is exhausted"
        )

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

    def test_the_real_reason_reaches_the_operator_not_check_the_logs(
        self, monitor, call_kwargs
    ):
        """The reason move_issue_to_column_with_reason() exists.

        The old code discarded the reason and substituted "check
        pipeline_progression logs for details" — a milder version of the
        "returned non-zero exit status 1" that made the original incident
        unattributable. The text the operator sees must be the text GitHub
        gave us.
        """
        from services.pipeline_progression import CardMoveFailure, FAILURE_RATE_LIMITED

        failure = CardMoveFailure(
            FAILURE_RATE_LIMITED, "rate_limited: API rate limit exceeded for user"
        )
        r = self._call(monitor, call_kwargs, [failure, failure, failure])

        message = r['emitter'].emit_error_decision.call_args.kwargs['error_message']
        assert "API rate limit exceeded for user" in message
        assert "check pipeline_progression logs" not in message

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


class TestSleepInterruptibly:
    """_move_card_with_retry()'s docstring promises CancellationError is
    honoured "immediately". A single time.sleep() of up to
    CARD_MOVE_MAX_RATE_LIMIT_WAIT_SECONDS made that false by a quarter of an
    hour — on a daemon thread holding the board's pipeline lock, which is
    exactly when an operator reaches for "stop this issue"."""

    @pytest.fixture
    def monitor(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        return ProjectMonitor(Mock(), config_manager)

    def _signal(self, cancelled):
        signal = Mock()
        signal.is_cancelled.return_value = cancelled
        return signal

    def test_a_long_wait_is_chunked_rather_than_one_call(self, monitor):
        with patch('time.sleep') as mock_sleep, \
             patch('services.cancellation.get_cancellation_signal',
                   return_value=self._signal(False)):
            monitor._sleep_interruptibly(900.0, 'proj', 42)

        slices = [c.args[0] for c in mock_sleep.call_args_list]
        assert len(slices) > 1, "a 15-minute wait must be interruptible"
        assert max(slices) <= monitor._CANCELLATION_POLL_SECONDS
        assert sum(slices) == pytest.approx(900.0)

    def test_a_cancellation_mid_wait_raises_without_serving_the_rest(self, monitor):
        from services.cancellation import CancellationError

        with patch('time.sleep') as mock_sleep, \
             patch('services.cancellation.get_cancellation_signal',
                   return_value=self._signal(True)):
            with pytest.raises(CancellationError):
                monitor._sleep_interruptibly(900.0, 'proj', 42)

        slept = sum(c.args[0] for c in mock_sleep.call_args_list)
        assert slept <= monitor._CANCELLATION_POLL_SECONDS, (
            "must stop at the first check, not serve out the full backoff"
        )

    def test_a_short_wait_still_sleeps_exactly_once(self, monitor):
        with patch('time.sleep') as mock_sleep, \
             patch('services.cancellation.get_cancellation_signal',
                   return_value=self._signal(False)):
            monitor._sleep_interruptibly(2.0, 'proj', 42)

        assert [c.args[0] for c in mock_sleep.call_args_list] == [2.0]

    def test_an_unreadable_cancellation_store_does_not_abort_the_wait(self, monitor):
        """A cancellation-store read failure must not turn a recoverable
        card-move backoff into a crash on a lock-holding thread."""
        with patch('time.sleep') as mock_sleep, \
             patch('services.cancellation.get_cancellation_signal',
                   side_effect=ConnectionError("redis down")):
            monitor._sleep_interruptibly(10.0, 'proj', 42)  # must not raise

        assert sum(c.args[0] for c in mock_sleep.call_args_list) == pytest.approx(10.0)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

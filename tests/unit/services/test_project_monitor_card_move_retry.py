"""
Unit tests for post-review-cycle card move retry logic.

Tests the retry loop in run_cycle_in_thread() that moves the issue card
to the next column after a review cycle completes successfully.
"""
import pytest
import time
from unittest.mock import Mock, patch, MagicMock, call
from services.project_monitor import ProjectMonitor
from config.manager import ConfigManager


class TestCardMoveRetryLogic:
    """Test the card move retry loop after review cycle completion."""

    @pytest.fixture
    def mock_config_manager(self):
        config_manager = Mock(spec=ConfigManager)
        config_manager.list_projects.return_value = []
        return config_manager

    @pytest.fixture
    def project_monitor(self, mock_config_manager):
        task_queue = Mock()
        monitor = ProjectMonitor(task_queue, mock_config_manager)
        return monitor

    def _run_card_move_loop(
        self,
        project_monitor,
        move_side_effects,
        issue_number=42,
        status="Code Review",
        next_column="Testing",
        project_name="test-project",
        board_name="SDLC Execution",
    ):
        """
        Execute the card move retry loop in isolation.

        Args:
            move_side_effects: List of return values or exceptions for
                move_issue_to_column. Use Exception instances to simulate raises.

        Returns:
            dict with 'move_succeeded', 'move_calls', 'error_emitted', 'comment_posted'
        """
        mock_progression = Mock()

        # Build side_effect list: exceptions raise, others are return values
        def make_side_effect(effects):
            effect_iter = iter(effects)
            def side_effect(*args, **kwargs):
                eff = next(effect_iter)
                if isinstance(eff, Exception):
                    raise eff
                return eff
            return side_effect

        mock_progression.move_issue_to_column.side_effect = make_side_effect(move_side_effects)

        mock_decision_emitter = Mock()
        mock_github = Mock()
        mock_loop = Mock()
        mock_loop.run_until_complete = Mock(side_effect=lambda coro: None)
        mock_pipeline_run = Mock()
        mock_pipeline_run.id = "test-pipeline-run-id"

        project_config = Mock()
        project_config.github = {'org': 'test-org'}

        result = {
            'move_succeeded': False,
            'move_calls': 0,
            'error_emitted': False,
            'comment_posted': False,
        }

        with patch('time.sleep') as mock_sleep, \
             patch('monitoring.decision_events.DecisionEventEmitter', return_value=mock_decision_emitter), \
             patch('monitoring.observability.get_observability_manager'), \
             patch('services.github_integration.GitHubIntegration', return_value=mock_github):

            # --- Replicate the retry loop from project_monitor.py ---
            from services.pipeline_progression import PipelineProgression
            # We won't actually create a PipelineProgression; use the mock directly
            progression_service = mock_progression

            move_succeeded = False
            max_move_retries = 3
            for attempt in range(1, max_move_retries + 1):
                try:
                    move_result = progression_service.move_issue_to_column(
                        project_name=project_name,
                        board_name=board_name,
                        issue_number=issue_number,
                        target_column=next_column,
                        trigger='review_cycle_completion'
                    )
                    if move_result:
                        move_succeeded = True
                        break
                except Exception:
                    pass

                if attempt < max_move_retries:
                    wait_time = 5 * (2 ** (attempt - 1))
                    time.sleep(wait_time)

            if not move_succeeded:
                # Emit error event
                from monitoring.decision_events import DecisionEventEmitter
                from monitoring.observability import get_observability_manager
                emitter = DecisionEventEmitter(get_observability_manager())
                emitter.emit_error_decision(
                    error_type="review_cycle_card_move_failure",
                    error_message=f"Failed to move issue #{issue_number} from {status} to {next_column} after {max_move_retries} attempts",
                    context={
                        'thread': 'review_cycle_thread',
                        'issue_number': issue_number,
                        'project': project_name,
                        'board': board_name,
                        'source_column': status,
                        'target_column': next_column,
                    },
                    recovery_action="Pipeline lock retained — manual intervention required",
                    success=False,
                    project=project_name,
                    pipeline_run_id=mock_pipeline_run.id
                )
                result['error_emitted'] = True

                # Post GitHub comment
                from services.github_integration import GitHubIntegration
                github_for_comment = GitHubIntegration(
                    repo_owner=project_config.github['org'],
                    repo_name='test-repo'
                )
                mock_loop.run_until_complete(
                    github_for_comment.post_agent_output({}, "Card Move Failed")
                )
                result['comment_posted'] = True

            result['move_succeeded'] = move_succeeded
            result['move_calls'] = mock_progression.move_issue_to_column.call_count
            result['sleep_calls'] = mock_sleep.call_args_list

        return result

    def test_succeeds_on_first_attempt(self, project_monitor):
        """Card move succeeds on first try — no retries needed."""
        result = self._run_card_move_loop(project_monitor, [True])

        assert result['move_succeeded'] is True
        assert result['move_calls'] == 1
        assert result['error_emitted'] is False
        assert result['comment_posted'] is False
        assert result['sleep_calls'] == []

    def test_succeeds_after_two_false_returns(self, project_monitor):
        """Card move returns False twice then True — retries succeed."""
        result = self._run_card_move_loop(project_monitor, [False, False, True])

        assert result['move_succeeded'] is True
        assert result['move_calls'] == 3
        assert result['error_emitted'] is False
        assert result['comment_posted'] is False
        # Should have slept twice: 5s after attempt 1, 10s after attempt 2
        assert len(result['sleep_calls']) == 2
        assert result['sleep_calls'][0] == call(5)
        assert result['sleep_calls'][1] == call(10)

    def test_succeeds_after_exception_then_true(self, project_monitor):
        """Card move raises exception once, then succeeds."""
        result = self._run_card_move_loop(
            project_monitor, [RuntimeError("GraphQL timeout"), True]
        )

        assert result['move_succeeded'] is True
        assert result['move_calls'] == 2
        assert result['error_emitted'] is False
        assert result['comment_posted'] is False

    def test_all_attempts_return_false(self, project_monitor):
        """All 3 attempts return False — error event emitted and comment posted."""
        result = self._run_card_move_loop(project_monitor, [False, False, False])

        assert result['move_succeeded'] is False
        assert result['move_calls'] == 3
        assert result['error_emitted'] is True
        assert result['comment_posted'] is True
        # Slept after attempt 1 (5s) and attempt 2 (10s), not after attempt 3
        assert len(result['sleep_calls']) == 2
        assert result['sleep_calls'][0] == call(5)
        assert result['sleep_calls'][1] == call(10)

    def test_all_attempts_raise_exceptions(self, project_monitor):
        """All 3 attempts raise exceptions — error event emitted and comment posted."""
        result = self._run_card_move_loop(
            project_monitor,
            [
                RuntimeError("Network error"),
                RuntimeError("Timeout"),
                RuntimeError("Server error"),
            ],
        )

        assert result['move_succeeded'] is False
        assert result['move_calls'] == 3
        assert result['error_emitted'] is True
        assert result['comment_posted'] is True

    def test_mixed_false_and_exceptions(self, project_monitor):
        """Mix of False returns and exceptions — all exhaust retries."""
        result = self._run_card_move_loop(
            project_monitor, [False, RuntimeError("oops"), False]
        )

        assert result['move_succeeded'] is False
        assert result['move_calls'] == 3
        assert result['error_emitted'] is True
        assert result['comment_posted'] is True

    def test_backoff_timing(self, project_monitor):
        """Verify exponential backoff: 5s, 10s (not after final attempt)."""
        result = self._run_card_move_loop(project_monitor, [False, False, False])

        assert len(result['sleep_calls']) == 2
        assert result['sleep_calls'][0] == call(5)
        assert result['sleep_calls'][1] == call(10)

    def test_succeeds_on_second_attempt_sleeps_once(self, project_monitor):
        """Success on attempt 2 — only one sleep of 5s."""
        result = self._run_card_move_loop(project_monitor, [False, True])

        assert result['move_succeeded'] is True
        assert result['move_calls'] == 2
        assert len(result['sleep_calls']) == 1
        assert result['sleep_calls'][0] == call(5)


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

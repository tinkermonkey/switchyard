"""
Regression tests for #148: a project resource-lock timeout on the conversational
path must release the pipeline run, not durably retain the board's lock.

HumanFeedbackLoopExecutor.start_loop() wraps the initial _execute_agent()
dispatch in a bare `except Exception`, and _conversational_loop()'s finally
block does the same for a failure mid-loop. Both routed everything —
including, now that the type survives the agent wrappers intact, a
ProjectCheckoutLockTimeoutError — to mark_failed(), which per its own inline
comment "durably retains the pipeline lock" and, on a planning board whose
conversational columns are that workflow's trigger columns, blocks the board
until an operator runs scripts/release_lock.py. All from a contention event
that resolves itself the moment the other holder finishes.

The correct treatment is the one
project_monitor._end_pr_review_pipeline_run_on_failure() already gives every
non-NonRetryableAgentError failure: end_pipeline_run(retain_lock=False), so the
next board poll retries.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.human_feedback_loop import HumanFeedbackLoopExecutor
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


def _column():
    column = MagicMock()
    column.agent = 'business_analyst'
    column.name = 'Research'
    return column


async def _run_start_loop_with_dispatch_error(error):
    """
    Drive start_loop() to its initial _execute_agent() dispatch, with that
    dispatch raising `error`, and return the pipeline-run manager mock so the
    caller can inspect which teardown decision was taken.
    """
    executor = HumanFeedbackLoopExecutor()

    redis_client = MagicMock()
    redis_client.get.return_value = None
    redis_client.set.return_value = True

    run_manager = MagicMock()
    run_manager.mark_failed.return_value = True

    with patch.object(executor, 'initialize', new=AsyncMock()), \
         patch('redis.Redis', return_value=redis_client), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
         patch.object(executor, '_load_previous_outputs_from_issue', new=AsyncMock()), \
         patch.object(executor, '_get_initial_user_request', new=AsyncMock(return_value=None)), \
         patch.object(executor, '_execute_agent', new=AsyncMock(side_effect=error)), \
         patch('services.human_feedback_loop.get_observability_manager', create=True), \
         patch('monitoring.observability.get_observability_manager'), \
         patch('monitoring.decision_events.DecisionEventEmitter'):

        with pytest.raises(Exception) as exc_info:
            await executor.start_loop(
                issue_number=900,
                repository='test-repo',
                project_name='test-project',
                board_name='Planning & Design',
                column=_column(),
                issue_data={'title': 't', 'body': 'b'},
                previous_stage_output=None,
                org='test-org',
                workspace_type='issues',
                pipeline_run_id='run-1',
            )

        return run_manager, exc_info.value


class TestLockTimeoutReleasesInsteadOfRetaining:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_mark_failed_is_not_called(self, error_cls):
        run_manager, raised = await _run_start_loop_with_dispatch_error(
            error_cls("could not acquire lock within 10900.0s")
        )

        run_manager.mark_failed.assert_not_called()
        assert type(raised) is error_cls

    @pytest.mark.asyncio
    async def test_the_run_is_released_for_the_next_poll(self):
        run_manager, _ = await _run_start_loop_with_dispatch_error(
            ProjectCheckoutLockTimeoutError("busy")
        )

        release_calls = [
            call for call in run_manager.end_pipeline_run.call_args_list
            if call.kwargs.get('retain_lock') is False
        ]
        assert release_calls, "the pipeline run must be released, not retained"
        assert release_calls[0].kwargs['issue_number'] == 900

    @pytest.mark.asyncio
    async def test_wrapped_lock_timeout_is_also_released(self):
        inner = ProjectCheckoutLockTimeoutError("busy")
        wrapper = Exception(f"Business Analyst execution failed: {inner}")
        wrapper.__cause__ = inner

        run_manager, _ = await _run_start_loop_with_dispatch_error(wrapper)

        run_manager.mark_failed.assert_not_called()


class TestOrdinaryFailuresStillMarkFailed:
    """Regression guard for the behavior the exemption is carved out of: a
    genuine dispatch failure must still be durably recorded (see the
    worktree-creation RuntimeError this handler was added for)."""

    @pytest.mark.asyncio
    async def test_ordinary_dispatch_failure_still_marks_failed(self):
        run_manager, _ = await _run_start_loop_with_dispatch_error(
            RuntimeError("worktree creation failed")
        )

        run_manager.mark_failed.assert_called_once()


async def _run_conversational_loop_with_error(error):
    """
    Drive _conversational_loop() to a mid-loop exception (raised from the
    heartbeat write, which sits inside the loop's try with no handler of its
    own) and return the pipeline-run manager mock. This covers the OTHER
    mark_failed() site: the loop's finally block, which runs before start_loop()'s
    handler and so has to make the same distinction independently.
    """
    from services.human_feedback_loop import HumanFeedbackState

    executor = HumanFeedbackLoopExecutor()
    state = HumanFeedbackState(
        issue_number=900,
        repository='test-repo',
        agent='business_analyst',
        project_name='test-project',
        board_name='Planning & Design',
        workspace_type='issues',
        discussion_id=None,
        pipeline_run_id='run-1',
    )
    state.agent_outputs = [{'timestamp': '2026-01-01T00:00:00'}]

    run_manager = MagicMock()
    run_manager.mark_failed.return_value = True

    with patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
         patch.object(executor, '_update_loop_heartbeat', side_effect=error), \
         patch.object(executor, '_get_human_feedback_since_last_agent',
                      new=AsyncMock(return_value=None)), \
         patch('services.human_feedback_loop.asyncio.sleep', new=AsyncMock()), \
         patch('monitoring.observability.get_observability_manager'), \
         patch('monitoring.decision_events.DecisionEventEmitter'):

        with pytest.raises(Exception):
            await executor._conversational_loop(state, _column(), {'title': 't'}, 'test-org')

        return run_manager


class TestConversationalLoopFinallyBlock:
    @pytest.mark.asyncio
    async def test_lock_timeout_mid_loop_releases_instead_of_retaining(self):
        run_manager = await _run_conversational_loop_with_error(
            ProjectCheckoutLockTimeoutError("busy")
        )

        run_manager.mark_failed.assert_not_called()
        release_calls = [
            call for call in run_manager.end_pipeline_run.call_args_list
            if call.kwargs.get('retain_lock') is False
        ]
        assert release_calls, "the pipeline run must be released, not retained"

    @pytest.mark.asyncio
    async def test_ordinary_mid_loop_failure_still_marks_failed(self):
        run_manager = await _run_conversational_loop_with_error(
            RuntimeError("redis unavailable")
        )

        run_manager.mark_failed.assert_called_once()

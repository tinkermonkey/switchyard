"""
Regression tests for #148: a project resource-lock timeout out of the review
loop must release the pipeline run and keep the cycle state, not mark the run
failed and delete the cycle.

ReviewCycleExecutor is the maker-checker executor behind every review column of
the sdlc_execution pipeline — the highest-traffic agent dispatch path in the
system — and both of start_review_cycle()'s `except Exception` handlers routed
everything to PipelineRunManager.mark_failed(), which durably retains the
BOARD's pipeline lock until an operator runs scripts/release_lock.py. Now that
agents/base_maker_agent.py no longer type-erases the wrapper and
AgentExecutor.execute_agent() re-raises rather than retrying, a
ProjectCheckoutLockTimeoutError reaches those handlers with its type intact —
so pure contention, on a lock working exactly as designed, would block every
sibling issue on the board.

The two handlers must also NOT call _remove_cycle_state(): the agent never ran,
so every accumulated maker/review output and the iteration counter is still
valid, and discarding them restarts the review from iteration 0.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.review_cycle import ReviewCycleExecutor, ReviewCycleState
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError

PROJECT = 'test-project'
BOARD = 'Development'
ISSUE = 4242
RUN_ID = 'run-abc'


def _column():
    column = MagicMock()
    column.agent = 'code_reviewer'
    column.maker_agent = 'senior_software_engineer'
    column.max_iterations = 3
    column.name = 'Code Review'
    return column


def _cycle_state():
    state = ReviewCycleState(
        issue_number=ISSUE,
        repository='test-repo',
        maker_agent='senior_software_engineer',
        reviewer_agent='code_reviewer',
        max_iterations=3,
        project_name=PROJECT,
        board_name=BOARD,
        pipeline_run_id=RUN_ID,
    )
    # Two iterations' worth of real work already accumulated — exactly what a
    # _remove_cycle_state() on the contention path would throw away.
    state.current_iteration = 2
    state.maker_outputs = [{'iteration': 0, 'output': 'first pass'}]
    state.review_outputs = [{'iteration': 1, 'output': 'changes requested'}]
    return state


async def _drive_start_review_cycle(error, *, existing_cycle: bool):
    """
    Drive start_review_cycle() to _execute_review_loop(), with that loop raising
    `error`, and return (executor, run_manager, remove_cycle_state_mock).

    existing_cycle=True exercises the reuse-an-active-cycle handler; False
    exercises the fresh-start handler. Both end in a mark_failed() today.
    """
    executor = ReviewCycleExecutor()
    executor.decision_events = MagicMock()

    run_manager = MagicMock()
    run_manager.mark_failed.return_value = True

    cycle_state = _cycle_state()
    key = executor._cycle_key(PROJECT, ISSUE)
    if existing_cycle:
        executor.active_cycles[key] = cycle_state

    remove_cycle_state = MagicMock()

    with patch.object(executor, '_execute_review_loop', new=AsyncMock(side_effect=error)), \
         patch.object(executor, '_remove_cycle_state', new=remove_cycle_state), \
         patch.object(executor, '_save_cycle_state', new=MagicMock()), \
         patch.object(executor, '_load_active_cycles', new=MagicMock(return_value=[])), \
         patch.object(executor, '_get_github_for_project', new=MagicMock(
             return_value=MagicMock(post_issue_comment=AsyncMock()))), \
         patch('services.review_cycle.PipelineContextWriter.setup',
               side_effect=RuntimeError("no context dir in unit tests")), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager):

        with pytest.raises(Exception) as exc_info:
            await executor.start_review_cycle(
                issue_number=ISSUE,
                repository='test-repo',
                project_name=PROJECT,
                board_name=BOARD,
                column=_column(),
                issue_data={'title': 't', 'body': 'b'},
                previous_stage_output='previous output',
                org='test-org',
                workspace_type='issues',
                pipeline_run_id=RUN_ID,
            )

    return executor, run_manager, remove_cycle_state, exc_info.value


@pytest.mark.parametrize("existing_cycle", [True, False], ids=["reuse_cycle", "fresh_cycle"])
class TestLockTimeoutReleasesInsteadOfRetaining:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_mark_failed_is_not_called(self, error_cls, existing_cycle):
        _, run_manager, _, _ = await _drive_start_review_cycle(
            error_cls("could not acquire lock within 10900.0s"),
            existing_cycle=existing_cycle,
        )
        run_manager.mark_failed.assert_not_called()

    @pytest.mark.asyncio
    async def test_run_is_released_without_retaining_the_lock(self, existing_cycle):
        _, run_manager, _, _ = await _drive_start_review_cycle(
            ProjectCheckoutLockTimeoutError("busy"), existing_cycle=existing_cycle,
        )
        run_manager.end_pipeline_run.assert_called_once()
        kwargs = run_manager.end_pipeline_run.call_args.kwargs
        assert kwargs['retain_lock'] is False
        assert kwargs['project'] == PROJECT
        assert kwargs['board'] == BOARD
        assert kwargs['issue_number'] == ISSUE

    @pytest.mark.asyncio
    async def test_cycle_state_survives_for_the_next_poll(self, existing_cycle):
        executor, _, remove_cycle_state, _ = await _drive_start_review_cycle(
            ProjectCheckoutLockTimeoutError("busy"), existing_cycle=existing_cycle,
        )
        remove_cycle_state.assert_not_called()
        assert executor._cycle_key(PROJECT, ISSUE) in executor.active_cycles

    @pytest.mark.asyncio
    async def test_a_wrapped_lock_timeout_is_recognised(self, existing_cycle):
        """Agent wrappers re-raise `Exception(...) from exc`; the __cause__ walk
        in resource_lock_errors must still see the timeout through them."""
        wrapped = Exception("Agent senior_software_engineer failed")
        wrapped.__cause__ = ProjectCheckoutLockTimeoutError("busy")

        _, run_manager, remove_cycle_state, _ = await _drive_start_review_cycle(
            wrapped, existing_cycle=existing_cycle,
        )
        run_manager.mark_failed.assert_not_called()
        remove_cycle_state.assert_not_called()


class TestOrdinaryFailureStillTearsDown:
    """Control: the exemption must be exactly and only for lock timeouts."""

    @pytest.mark.asyncio
    async def test_reuse_path_still_marks_the_run_failed(self):
        _, run_manager, _, _ = await _drive_start_review_cycle(
            RuntimeError("the reviewer agent crashed"), existing_cycle=True,
        )
        run_manager.mark_failed.assert_called_once()
        assert run_manager.mark_failed.call_args.kwargs['issue_number'] == ISSUE
        run_manager.end_pipeline_run.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("existing_cycle", [True, False], ids=["reuse_cycle", "fresh_cycle"])
    async def test_cycle_state_is_still_torn_down(self, existing_cycle):
        # The fresh-start handler deliberately leaves mark_failed() to
        # project_monitor's review-cycle thread (it re-raises); what both handlers
        # do own is the cycle teardown, so that is what this control pins.
        executor, _, remove_cycle_state, _ = await _drive_start_review_cycle(
            RuntimeError("the reviewer agent crashed"), existing_cycle=existing_cycle,
        )
        remove_cycle_state.assert_called_once()
        assert executor._cycle_key(PROJECT, ISSUE) not in executor.active_cycles

"""
Regression tests for #148: PRReviewStage must abandon the review on the first
project resource-lock timeout instead of paying that lock's timeout once per
phase.

Each phase is its own execute_agent() call that re-enters run_claude_code() and
re-acquires the same project_checkout lock. The phase handlers catch everything
and degrade to review text, so a held lock used to produce ~4 x ~3h of wall
clock in a single run_pr_review() call with the pipeline lock held throughout —
and then, with phases_completed == 0, a NonRetryableAgentError that
project_monitor routes to mark_failed(), durably retaining the board's lock
"pending explicit human review" for a problem that was only ever "someone else
was using the clone".

Propagating instead reaches _end_pr_review_pipeline_run_on_failure()'s release
branch (covered in tests/unit/services/test_pr_review_stage_failure_handling.py),
so the next board poll retries.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from pipeline.pr_review_stage import PRReviewStage
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


def _stage():
    with patch('pipeline.pr_review_stage.ConfigManager'), \
         patch('pipeline.pr_review_stage.GitHubStateManager'):
        # skip_ci_check keeps Phase 3 (a local gh CLI call) out of the picture;
        # Phase 1 is the first agent dispatch either way.
        return PRReviewStage(skip_ci_check=True)


def _context():
    return {
        'context': {
            'project': 'test-project',
            'issue_number': 900,
            'column': 'In Review',
        },
        'task_id': 'task-1',
    }


async def _run_stage_with_agent_error(stage, error):
    """
    Drive execute() far enough to reach Phase 1's agent dispatch, with
    execute_agent raising `error`, and return the executor mock so the caller
    can count how many phases were attempted.
    """
    project_config = MagicMock()
    project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
    stage.config_manager.get_project_config.return_value = project_config

    executor = MagicMock()
    executor.execute_agent = AsyncMock(side_effect=error)

    checkpoint = MagicMock()
    checkpoint.get_phase_output.return_value = None

    cancellation = MagicMock()
    cancellation.is_cancelled.return_value = False

    with patch.object(stage, '_find_pr_url', new=AsyncMock(return_value='https://pr/1')), \
         patch('pipeline.pr_review_stage.get_agent_executor', return_value=executor), \
         patch('pipeline.pr_review_stage.PRReviewCheckpoint', return_value=checkpoint), \
         patch('pipeline.pr_review_stage.get_cancellation_signal', return_value=cancellation), \
         patch('pipeline.pr_review_stage.pr_review_state_manager') as mock_state, \
         patch('services.work_execution_state.work_execution_tracker'), \
         patch.object(stage, '_load_discussion_outputs', return_value={}), \
         patch.object(stage, '_get_parent_issue_body', return_value='requirements'), \
         patch.object(stage, '_build_pr_review_prompt', return_value='prompt'):

        mock_state.get_review_count.return_value = 0

        with pytest.raises(Exception) as exc_info:
            await stage.execute(_context())

        return executor, exc_info.value


class TestLockTimeoutAbandonsTheReview:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_only_one_phase_is_attempted(self, error_cls):
        stage = _stage()
        executor, raised = await _run_stage_with_agent_error(
            stage, error_cls("could not acquire lock within 10900.0s")
        )

        assert executor.execute_agent.call_count == 1
        assert type(raised) is error_cls

    @pytest.mark.asyncio
    async def test_it_is_not_converted_into_a_non_retryable_all_phases_failed(self):
        """phases_completed == 0 raises NonRetryableAgentError, which
        project_monitor treats as requiring human intervention. The timeout must
        reach the caller as itself so the release branch is taken instead."""
        from agents.non_retryable import NonRetryableAgentError

        stage = _stage()
        _, raised = await _run_stage_with_agent_error(
            stage, ProjectCheckoutLockTimeoutError("busy")
        )

        assert not isinstance(raised, NonRetryableAgentError)


class TestOrdinaryPhaseFailuresStillDegrade:
    """Regression guard: an ordinary phase failure must still be caught and
    turned into review text, so the remaining phases run."""

    @pytest.mark.asyncio
    async def test_ordinary_error_attempts_every_phase(self):
        stage = _stage()
        executor, raised = await _run_stage_with_agent_error(
            stage, RuntimeError("reviewer container died")
        )

        # Phase 1 plus each context check that had content — more than one, which
        # is exactly what the lock-timeout case must NOT do.
        assert executor.execute_agent.call_count > 1

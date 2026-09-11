"""
Tests for how execute_agent() handles resolve_workspace()'s drifted-worktree
refusal (#163).

The condition is the same one #149's commit-time verification refuses over -- an
epic worktree on a branch belonging to no epic, holding uncommitted work nobody
has claimed -- caught BEFORE an agent runs against it rather than after. Two
things have to follow from that:

  * it must not be treated as an ordinary dispatch failure. Three of those reach
    project_monitor's MAX_CONSECUTIVE_DISPATCH_FAILURES before anything durable
    happens, and the two dispatches in between would each run a container against
    the same drifted directory, piling their work on top of the work already
    sitting there uncommitted;
  * the operator has to be told where the work is and what to do with it. #163's
    first requirement on any mechanism that stops production work is a supported
    way to un-wedge it -- named in the comment, the way the push-failure comment
    names release_lock.py.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_executor import AgentExecutor
from services.project_workspace import WorktreeBranchDriftError


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


def _drift_error():
    return WorktreeBranchDriftError(
        "Epic worktree for test-project epic #42 at /workspace/.orchestrator/"
        "worktrees/test-project/42 has drifted onto a branch that belongs to no "
        "epic.",
        project_name='test-project',
        epic_id='42',
        worktree_path='/workspace/.orchestrator/worktrees/test-project/42',
        expected_branch='feature/issue-42-epic',
        found_branch='scratch',
        dirty=True,
    )


async def _run(agent_executor, resolve_error):
    """Drive execute_agent() far enough to hit the workspace-resolution block."""
    task_context = {
        'issue_number': 903,
        'column': 'Development',
        'board': 'Dev Board',
        'workspace_type': 'issues',
        'pipeline_run_id': 'run-903',
    }
    tracker = MagicMock()
    tracker.load_state.return_value = {'execution_history': []}

    project_config = MagicMock()
    project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

    fake_run = MagicMock()
    fake_run.issue_number = 903

    prm = MagicMock()
    prm.get_pipeline_run.return_value = fake_run
    prm.resolve_workspace = AsyncMock(side_effect=resolve_error)
    prm.mark_failed.return_value = True

    github = MagicMock()
    github.post_comment = AsyncMock()

    with patch('services.agent_executor.config_manager') as mock_config, \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=prm), \
         patch('services.github_integration.GitHubIntegration', return_value=github), \
         patch.object(agent_executor.obs, 'emit_task_received'), \
         patch.object(agent_executor.obs, 'emit_agent_initialized'):
        mock_config.get_project_config.return_value = project_config

        raised = None
        try:
            await agent_executor.execute_agent(
                agent_name='developer',
                project_name='test-project',
                task_context=task_context,
            )
        except Exception as e:
            raised = e

    return {
        'exception': raised,
        'tracker': tracker,
        'prm': prm,
        'github': github,
    }


class TestADriftedWorktreeBlocksBeforeDispatch:

    @pytest.mark.asyncio
    async def test_it_retains_the_board_lock_on_the_first_occurrence(self, agent_executor):
        from agents.non_retryable import NonRetryableAgentError

        harness = await _run(agent_executor, _drift_error())

        assert isinstance(harness['exception'], NonRetryableAgentError)
        harness['prm'].mark_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_the_comment_names_the_worktree_the_branches_and_the_recovery(
        self, agent_executor
    ):
        harness = await _run(agent_executor, _drift_error())

        body = harness['github'].post_comment.await_args[0][1]
        assert 'Wrong Branch' in body
        assert 'scratch' in body
        assert 'feature/issue-42-epic' in body
        assert '/workspace/.orchestrator/worktrees/test-project/42' in body
        # #163's first requirement: the operator entry point is named, not left
        # as "go rm a file inside the container".
        assert 'scripts/inspect_epic_worktrees.py' in body
        assert 'scripts/release_lock.py' in body
        # It happened before any agent ran, so the comment must not claim the
        # agent completed work.
        assert 'no agent was dispatched' in body.lower()

    @pytest.mark.asyncio
    async def test_the_in_progress_entry_is_still_closed_out(self, agent_executor):
        """The refusal runs before execute_agent()'s own big try/except, so
        without the explicit record the 'in_progress' entry never gets a terminal
        outcome and should_execute_work() answers "work_already_in_progress" on
        every subsequent poll."""
        harness = await _run(agent_executor, _drift_error())

        outcomes = [
            call.kwargs.get('outcome')
            for call in harness['tracker'].record_execution_outcome.call_args_list
        ]
        assert outcomes == ['failure']

    @pytest.mark.asyncio
    async def test_an_ordinary_resolution_failure_is_not_escalated_this_way(
        self, agent_executor
    ):
        """The control: only the drift verdict gets the block-and-comment
        treatment. Everything else keeps the generic record-and-propagate path,
        this codebase's uniform retry/escalation pattern."""
        harness = await _run(agent_executor, RuntimeError("worktree add failed"))

        assert isinstance(harness['exception'], RuntimeError)
        harness['prm'].mark_failed.assert_not_called()
        harness['github'].post_comment.assert_not_awaited()

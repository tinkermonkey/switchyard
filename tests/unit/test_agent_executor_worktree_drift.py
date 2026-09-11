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


def _drift_error(dirty=True, unmerged_commits=None):
    return WorktreeBranchDriftError(
        "Epic worktree for test-project epic #42 at /workspace/.orchestrator/"
        "worktrees/test-project/42 has drifted onto a branch that belongs to no "
        "epic.",
        project_name='test-project',
        epic_id='42',
        worktree_path='/workspace/.orchestrator/worktrees/test-project/42',
        expected_branch='feature/issue-42-epic',
        found_branch='scratch',
        dirty=dirty,
        unmerged_commits=unmerged_commits,
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
    async def test_the_comment_does_not_claim_uncommitted_work_that_is_not_there(
        self, agent_executor
    ):
        """reconcile_worktree_branch() returns DRIFTED in four distinct shapes and
        only one of them is "it holds uncommitted changes" (code review on #163).

        Telling an operator to `git reset --hard` / `git clean -fd` a worktree
        `git status` reports as clean sends them to run a no-op, conclude the
        worktree is now fine, release the lock, and hit the identical refusal on
        the next poll -- while step 3's promise that the block self-clears is
        false for the shape that has nothing in it to clear."""
        harness = await _run(
            agent_executor, _drift_error(dirty=False, unmerged_commits=0)
        )

        body = harness['github'].post_comment.await_args[0][1]
        assert 'holds uncommitted changes' not in body
        assert 'nothing uncommitted in it' in body
        assert 'HEAD could not be moved back' in body
        # The actionable remedy, not "commit or discard work that is not there".
        assert 'checkout feature/issue-42-epic' in body
        assert 'does **not** clear itself' in body
        assert 'reset --hard' not in body

    @pytest.mark.asyncio
    async def test_an_unreadable_working_tree_is_described_as_unreadable(
        self, agent_executor
    ):
        """dirty is None means `git status --porcelain` itself failed -- most
        plausibly a stale index.lock left by a killed agent-side git. Asserting
        uncommitted changes exist is a claim the verdict never made."""
        harness = await _run(agent_executor, _drift_error(dirty=None))

        body = harness['github'].post_comment.await_args[0][1]
        assert 'working tree state could not be read' in body
        assert 'index.lock' in body

    @pytest.mark.asyncio
    async def test_commits_on_the_drifted_branch_are_named_as_commits(
        self, agent_executor
    ):
        """A clean tree on a branch carrying its own commits is a different thing
        to go looking for -- and a different recovery -- than a dirty tree."""
        harness = await _run(
            agent_executor, _drift_error(dirty=False, unmerged_commits=3)
        )

        body = harness['github'].post_comment.await_args[0][1]
        assert '3 commit(s)' in body
        assert 'not lost' in body
        assert 'feature/issue-42-epic..scratch' in body

    @pytest.mark.asyncio
    async def test_the_dirty_case_keeps_its_wording(self, agent_executor):
        """The control: the shape the comment was written for is unchanged."""
        harness = await _run(agent_executor, _drift_error(dirty=True))

        body = harness['github'].post_comment.await_args[0][1]
        assert 'holds uncommitted changes' in body
        assert 'reset --hard' in body
        assert 'restores the epic' in body

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

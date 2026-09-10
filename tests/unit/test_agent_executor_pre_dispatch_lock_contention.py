"""
Regression tests for the lock-timeout paths #151/WI-6 newly opened into
AgentExecutor, all of which predate this branch as code but none of which could
raise a ProjectCheckoutLockTimeoutError before it.

get_or_create_epic_worktree() now waits on the project's project_checkout lock,
so a cold epic worktree can raise ProjectCheckoutLockTimeoutError out of:
  * PipelineRunManager.resolve_workspace()   -> execute_agent's epic block,
  * workspace_manager.get_project_dir()      -> execute_agent's working-directory
                                                resolution and _failsafe_commit_check,
and finalize_feature_branch_work() acquires the same lock directly.

Each of those reaches a handler that was written before any of them could
produce a lock timeout:
  * the two pre-dispatch handlers run BEFORE execute_agent()'s own big
    try/except -- the one that classifies contention -- and the resolve_workspace
    one hard-coded outcome='failure'. 'failure' feeds
    count_consecutive_failures(), and three of those reach project_monitor's
    MAX_CONSECUTIVE_DISPATCH_FAILURES and mark_failed(), which durably retains
    the BOARD's pipeline lock over contention that clears itself (#148). The
    working-directory one recorded NOTHING at all, leaving the
    record_execution_start() 'in_progress' entry unpaired so should_execute_work()
    answers "work_already_in_progress" on every subsequent poll.
  * _failsafe_commit_check()'s catch-all returned None, which its own contract
    defines as "the check passed" -- so contention was reported as a clean
    failsafe over work still uncommitted on disk.
  * the finalization handler fell through to
    record_execution_outcome(outcome='success') with nothing staged, committed,
    pushed or PR'd.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_executor import AgentExecutor
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


def _tracker():
    tracker = MagicMock()
    tracker.load_state.return_value = {'execution_history': []}
    return tracker


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


class TestWorkspaceResolutionFailure:
    """execute_agent()'s epic block calls resolve_workspace() ~30 lines before
    its own big try/except, and records the outcome by hand."""

    async def _run(self, agent_executor, resolve_error):
        task_context = {
            'issue_number': 901,
            'column': 'Development',
            'workspace_type': 'issues',
            'pipeline_run_id': 'run-901',
        }
        tracker = _tracker()

        project_config = MagicMock()
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        fake_run = MagicMock()
        fake_run.issue_number = 901

        prm = MagicMock()
        prm.get_pipeline_run.return_value = fake_run
        prm.resolve_workspace = AsyncMock(side_effect=resolve_error)

        with patch('services.agent_executor.config_manager') as mock_config, \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=prm), \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'):
            mock_config.get_project_config.return_value = project_config

            with pytest.raises(type(resolve_error)):
                await agent_executor.execute_agent(
                    agent_name='developer',
                    project_name='test-project',
                    task_context=task_context,
                )

        return tracker

    @pytest.mark.asyncio
    async def test_a_lock_timeout_is_recorded_as_contention(self, agent_executor):
        tracker = await self._run(
            agent_executor, ProjectCheckoutLockTimeoutError("base clone busy")
        )
        assert _recorded_outcomes(tracker) == ['lock_contention'], (
            "a contended base clone must not count toward "
            "MAX_CONSECUTIVE_DISPATCH_FAILURES -- three of those retain the board lock"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_error_is_still_recorded_as_failure(self, agent_executor):
        tracker = await self._run(agent_executor, RuntimeError("worktree add failed"))
        assert _recorded_outcomes(tracker) == ['failure']


class TestWorkingDirectoryResolutionFailure:
    """The working-directory resolution sits between record_execution_start()
    and the big try/except, and had no outcome recording of any kind."""

    async def _run(self, agent_executor, resolve_error):
        task_context = {
            'issue_number': 902,
            'column': 'Development',
            # Keeps the epic-resolution blocks out of the picture: the
            # directory resolution below is the only thing under test. In
            # production the live shape is a 'discussions' dispatch, which sets
            # task_context['epic_id'] but never task_context['project_dir'].
            'skip_workspace_prep': True,
        }
        tracker = _tracker()

        with patch('services.agent_executor.config_manager'), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.project_workspace.workspace_manager.get_project_dir',
                   side_effect=resolve_error), \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'):
            with pytest.raises(type(resolve_error)):
                await agent_executor.execute_agent(
                    agent_name='developer',
                    project_name='test-project',
                    task_context=task_context,
                )

        return tracker

    @pytest.mark.asyncio
    async def test_a_lock_timeout_is_recorded_as_contention(self, agent_executor):
        tracker = await self._run(
            agent_executor, ProjectCheckoutLockTimeoutError("base clone busy")
        )
        assert _recorded_outcomes(tracker) == ['lock_contention'], (
            "without a terminal outcome the 'in_progress' entry is never closed and "
            "should_execute_work() skips this issue on every subsequent poll"
        )

    @pytest.mark.asyncio
    async def test_an_ordinary_error_is_recorded_as_failure(self, agent_executor):
        tracker = await self._run(agent_executor, RuntimeError("worktree is corrupted"))
        assert _recorded_outcomes(tracker) == ['failure']


class TestFailsafeCommitCheckDoesNotSwallowContention:
    """None means "the check passed and the failsafe ran as it always has"
    (this method's own docstring). A lock timeout means nothing ran and the
    agent's work is still uncommitted."""

    @pytest.mark.asyncio
    async def test_lock_timeout_propagates_instead_of_returning_none(self, agent_executor):
        task_context = {
            'issue_number': 903,
            'column': 'Development',
            'epic_id': '900',
            'branch_name': 'feature/issue-900-epic',
            # Deliberately absent: this is what sends the failsafe down the
            # re-derivation path that can reach the epic worktree creation.
            # 'project_dir': ...
        }

        with patch('services.project_workspace.workspace_manager.get_project_dir',
                   side_effect=ProjectCheckoutLockTimeoutError("base clone busy")):
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                await agent_executor._failsafe_commit_check(
                    project_name='test-project',
                    agent_name='developer',
                    task_context=task_context,
                    task_id='task-903',
                )

    @pytest.mark.asyncio
    async def test_an_ordinary_error_is_still_swallowed_to_none(self, agent_executor):
        """The catch-all's pre-existing best-effort behavior is unchanged for
        everything that is not a lock timeout."""
        task_context = {
            'issue_number': 904,
            'column': 'Development',
        }

        with patch('services.project_workspace.workspace_manager.get_project_dir',
                   side_effect=RuntimeError("worktree is corrupted")):
            assert await agent_executor._failsafe_commit_check(
                project_name='test-project',
                agent_name='developer',
                task_context=task_context,
                task_id='task-904',
            ) is None


class TestFinalizationContentionIsNotASuccess:
    """finalize_feature_branch_work() now takes the project_checkout lock for the
    shared base clone, so it can raise. The finalization handler special-cased
    only NonRetryableAgentError and PushFailedError, and everything else fell
    through to outcome='success'.

    What it must do instead is BLOCK, not record contention: this handler runs
    after the agent completed and after its output comment went out, so the
    contention path's retry would re-run both. That divide is exercised in full by
    tests/unit/test_agent_executor_post_completion_lock_timeout.py; this test
    keeps the original 'not a success' regression pinned at the same call site.
    """

    @pytest.mark.asyncio
    async def test_a_finalize_lock_timeout_blocks_rather_than_recording_success(self, agent_executor):
        task_context = {
            'issue_number': 905,
            'column': 'Development',
            'workspace_type': 'issues',
            # project_dir already resolved, so nothing in this dispatch reaches
            # the epic-worktree creation path -- the finalize lock is the only
            # thing under test. No pipeline_run_id, so the epic-resolution block
            # logs its "cannot resolve an isolated workspace" warning and moves
            # on rather than needing a whole PipelineRun mocked.
            'project_dir': '/workspace/.orchestrator/worktrees/test-project/900',
            'branch_name': 'feature/issue-900-epic',
        }
        tracker = _tracker()

        project_config = MagicMock()
        project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

        workspace = MagicMock()
        workspace.supports_git_operations = True
        workspace.prepare_execution = AsyncMock(
            return_value={'branch_name': 'feature/issue-900-epic',
                          'work_dir': '/workspace/.orchestrator/worktrees/test-project/900'}
        )
        workspace.finalize_execution = AsyncMock(
            side_effect=ProjectCheckoutLockTimeoutError("base clone busy")
        )

        agent = MagicMock()
        agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
        agent.agent_config = {}

        run_manager = MagicMock()
        run_manager.mark_failed.return_value = True
        github = MagicMock()
        github.post_comment = AsyncMock()

        with patch('services.agent_executor.config_manager') as mock_config, \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch('services.github_integration.GitHubIntegration', return_value=github), \
             patch.object(agent_executor.factory, 'create_agent', return_value=agent), \
             patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock), \
             patch.object(agent_executor, '_failsafe_commit_check', new_callable=AsyncMock,
                          return_value=None) as mock_failsafe, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch('asyncio.sleep', new_callable=AsyncMock):
            mock_config.get_project_config.return_value = project_config
            mock_factory.create.return_value = workspace

            from agents.non_retryable import NonRetryableAgentError
            with pytest.raises(NonRetryableAgentError):
                await agent_executor.execute_agent(
                    agent_name='developer',
                    project_name='test-project',
                    task_context=task_context,
                )

        outcomes = _recorded_outcomes(tracker)
        assert 'success' not in outcomes, (
            "the issue must not advance as a success with nothing staged, committed "
            "or pushed and the work left uncommitted on disk"
        )
        # ...and not 'lock_contention' either: the agent has already run and
        # already commented by this point, so that outcome's retry would launch a
        # second container and post a second comment (#151/WI-6 review).
        assert 'lock_contention' not in outcomes
        run_manager.mark_failed.assert_called_once()
        github.post_comment.assert_awaited_once()
        # The failsafe is not run either -- its own auto-commit would hit the
        # same contended lock, and blocking is what stops the run.
        mock_failsafe.assert_not_called()

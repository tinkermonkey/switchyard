"""
Regression tests for #151/WI-6 review: a resource-lock timeout raised AFTER the
agent has already completed must block the pipeline, not be recorded as
'lock_contention'.

'lock_contention' (#148) exists for timeouts raised BEFORE the guarded work runs
-- nothing ran, nothing is dirty, and should_execute_work() answering
"retry_after_lock_contention" on the next 30s poll is exactly right. WI-6 wired
the project_checkout lock into finalize_feature_branch_work()'s shared base-clone
fallback and into _failsafe_commit_check()'s directory resolution, both of which
run only after execute_agent() has emitted emit_agent_completed(success=True) and
already called _post_agent_output_to_github(). Recording contention there has the
next poll re-dispatch the SAME agent: a second container against a worktree still
holding the first run's uncommitted changes, and a second output comment on the
issue -- two or three of those before MAX_CONSECUTIVE_LOCK_CONTENTIONS escalates.

So all three post-completion sites route through
_handle_post_completion_lock_timeout(), which ends where the PushFailedError and
wrong-branch refusals end: mark_failed() (the board's pipeline lock is retained,
so no poll re-dispatches), an issue comment saying where the uncommitted work is,
and NonRetryableAgentError.

Without the fix each test here observes outcome='lock_contention', no
mark_failed() and no comment.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from agents.non_retryable import NonRetryableAgentError
from services.agent_executor import AgentExecutor
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


class _Harness:
    """The mocks a post-completion run leaves behind, for the assertions below."""

    def __init__(self, tracker, run_manager, github):
        self.tracker = tracker
        self.run_manager = run_manager
        self.github = github

    @property
    def outcomes(self):
        return [
            call.kwargs.get('outcome')
            for call in self.tracker.record_execution_outcome.call_args_list
        ]

    @property
    def comment_bodies(self):
        return [call.args[1] for call in self.github.post_comment.call_args_list]


async def _run(agent_executor, task_context, *, finalize_error=None,
               failsafe_error=None, workspace=True):
    """
    Drive execute_agent() all the way through a SUCCESSFUL agent run and into the
    post-completion commit paths, with the chosen one raising.

    workspace=False takes the `workspace_context is None` branch (every
    skip_workspace_prep dispatch, i.e. all of repair_cycle.py's inner agents).
    """
    tracker = MagicMock()
    tracker.load_state.return_value = {'execution_history': []}

    run_manager = MagicMock()
    run_manager.mark_failed.return_value = True

    github = MagicMock()
    github.post_comment = AsyncMock()

    mock_project_config = MagicMock()
    mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}

    mock_workspace = MagicMock()
    mock_workspace.supports_git_operations = False
    mock_workspace.prepare_execution = AsyncMock(
        return_value={'branch_name': 'feature/issue-900-epic'}
    )
    mock_workspace.finalize_execution = AsyncMock(
        side_effect=finalize_error, return_value={'success': True}
    )

    failsafe = AsyncMock(side_effect=failsafe_error, return_value=None)

    with patch('services.agent_executor.config_manager') as mock_config, \
         patch('services.workspace.WorkspaceContextFactory') as mock_factory, \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
         patch('services.github_integration.GitHubIntegration', return_value=github), \
         patch.object(agent_executor, '_failsafe_commit_check', failsafe), \
         patch.object(agent_executor, '_post_agent_output_to_github', new_callable=AsyncMock), \
         patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(agent_executor.obs, 'emit_task_received'), \
         patch.object(agent_executor.obs, 'emit_agent_initialized'), \
         patch.object(agent_executor.obs, 'emit_agent_completed'):

        mock_config.get_project_config.return_value = mock_project_config
        mock_factory.create.return_value = mock_workspace

        mock_agent = MagicMock()
        mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
        mock_agent.agent_config = {}
        mock_create_agent.return_value = mock_agent

        if not workspace:
            task_context['skip_workspace_prep'] = True

        with pytest.raises(NonRetryableAgentError) as raised:
            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

        return _Harness(tracker, run_manager, github), raised.value, mock_agent


def _task_context():
    return {
        'issue_number': 900,
        'column': 'Development',
        'board': 'dev_workflow',
        'branch_name': 'feature/issue-900-epic',
        # Short-circuits the off-loop get_project_dir() resolution; the lock this
        # test is about is the one taken further down, in the commit paths.
        'project_dir': '/workspace/test-project',
    }


@pytest.mark.asyncio
class TestFinalizationLockTimeoutBlocksInsteadOfRetrying:

    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_finalize_timeout_is_not_recorded_as_contention(
        self, agent_executor, error_cls
    ):
        """THE regression: finalize_execution()'s lock timeout used to reach the
        outer contention handler, whose 'the guarded execution never ran' premise
        is false once the agent has run and commented."""
        harness, _, _ = await _run(
            agent_executor, _task_context(), finalize_error=error_cls("busy")
        )

        assert 'lock_contention' not in harness.outcomes
        assert harness.outcomes == ['failure']

    async def test_finalize_timeout_retains_the_pipeline_lock(self, agent_executor):
        """mark_failed() is what actually stops the next poll re-dispatching the
        same agent -- 'lock_contention' left the board lock released."""
        harness, _, _ = await _run(
            agent_executor,
            _task_context(),
            finalize_error=ProjectCheckoutLockTimeoutError("busy"),
        )

        harness.run_manager.mark_failed.assert_called_once()
        assert harness.run_manager.mark_failed.call_args.kwargs['issue_number'] == 900
        assert harness.run_manager.mark_failed.call_args.kwargs['board'] == 'dev_workflow'

    async def test_finalize_timeout_explains_itself_on_the_issue(self, agent_executor):
        """The operator has to be told where the uncommitted work is; a silent
        duplicate run was the alternative."""
        harness, _, _ = await _run(
            agent_executor,
            _task_context(),
            finalize_error=ProjectCheckoutLockTimeoutError("held by issue #12"),
        )

        assert len(harness.comment_bodies) == 1
        body = harness.comment_bodies[0]
        assert 'Pipeline Blocked' in body
        assert 'uncommitted' in body
        assert 'held by issue #12' in body

    async def test_the_raised_error_does_not_chain_the_lock_timeout(self, agent_executor):
        """resource_lock_errors.is_lock_timeout_error() follows __cause__, so
        `raise NonRetryableAgentError(...) from timeout` would have the outer
        handler classify this as contention after all and undo the whole fix."""
        from services.resource_lock_errors import is_lock_timeout_error

        _, error, _ = await _run(
            agent_executor,
            _task_context(),
            finalize_error=ProjectCheckoutLockTimeoutError("busy"),
        )

        assert not is_lock_timeout_error(error)

    async def test_failsafe_timeout_after_a_finalization_crash_also_blocks(
        self, agent_executor
    ):
        """The second site: finalization fails for an ordinary reason, and the
        failsafe that runs to rescue it then loses the same lock."""
        harness, _, _ = await _run(
            agent_executor,
            _task_context(),
            finalize_error=RuntimeError("finalization exploded"),
            failsafe_error=ProjectCheckoutLockTimeoutError("busy"),
        )

        assert harness.outcomes == ['failure']
        harness.run_manager.mark_failed.assert_called_once()
        assert len(harness.comment_bodies) == 1

    async def test_failsafe_timeout_on_the_skip_prep_branch_also_blocks(
        self, agent_executor
    ):
        """The third site: `workspace_context is None`, which every
        skip_workspace_prep dispatch takes. It had no try of its own, so the
        timeout fell straight through to the outer contention path."""
        harness, _, _ = await _run(
            agent_executor,
            _task_context(),
            failsafe_error=ProjectCheckoutLockTimeoutError("busy"),
            workspace=False,
        )

        assert harness.outcomes == ['failure']
        harness.run_manager.mark_failed.assert_called_once()
        assert len(harness.comment_bodies) == 1

    async def test_non_lock_failsafe_errors_still_propagate_unchanged(
        self, agent_executor
    ):
        """The skip_prep branch's new try/except must not start swallowing (or
        re-labelling) anything else the failsafe raises."""
        task_context = _task_context()
        task_context['skip_workspace_prep'] = True

        tracker = MagicMock()
        tracker.load_state.return_value = {'execution_history': []}

        with patch('services.agent_executor.config_manager'), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch.object(agent_executor, '_failsafe_commit_check',
                          AsyncMock(side_effect=RuntimeError("disk full"))), \
             patch.object(agent_executor, '_post_agent_output_to_github',
                          new_callable=AsyncMock), \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'):

            mock_agent = MagicMock()
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_agent.agent_config = {}
            mock_create_agent.return_value = mock_agent

            with pytest.raises(RuntimeError, match="disk full"):
                await agent_executor.execute_agent(
                    agent_name='business_analyst',
                    project_name='test-project',
                    task_context=task_context
                )


@pytest.mark.asyncio
class TestPreDispatchContentionIsUnchanged:
    """The other half of the divide: a timeout raised before the agent ran still
    has to be contention, or #148's whole rationale is lost."""

    async def test_agent_run_timeout_is_still_recorded_as_contention(self, agent_executor):
        tracker = MagicMock()
        tracker.load_state.return_value = {'execution_history': []}

        task_context = {
            'issue_number': 901,
            'column': 'Development',
            'skip_workspace_prep': True,
        }

        with patch('services.agent_executor.config_manager'), \
             patch('services.work_execution_state.work_execution_tracker', tracker), \
             patch('services.dev_container_state.dev_container_state', MagicMock()), \
             patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
             patch.object(agent_executor.obs, 'emit_task_received'), \
             patch.object(agent_executor.obs, 'emit_agent_initialized'), \
             patch.object(agent_executor.obs, 'emit_agent_completed'), \
             patch('asyncio.sleep', new_callable=AsyncMock):

            mock_agent = MagicMock()
            mock_agent.run_with_circuit_breaker = AsyncMock(
                side_effect=ProjectCheckoutLockTimeoutError("busy")
            )
            mock_agent.agent_config = {'retries': 2}
            mock_create_agent.return_value = mock_agent

            with pytest.raises(ProjectCheckoutLockTimeoutError):
                await agent_executor.execute_agent(
                    agent_name='business_analyst',
                    project_name='test-project',
                    task_context=task_context
                )

        outcomes = [
            call.kwargs.get('outcome')
            for call in tracker.record_execution_outcome.call_args_list
        ]
        assert outcomes == ['lock_contention']

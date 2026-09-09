"""
Unit tests for FeatureBranchManager.finalize_feature_branch_work()'s
branch-target verification (issue #149 WI-4 review, closing #143 on the
ordinary-dispatch commit path).

auto_commit.py's commit_agent_changes() verifies the checked-out branch against
a caller-supplied expected_branch and refuses on a mismatch -- but that covers
only the review-cycle and repair-cycle paths. finalize_feature_branch_work() is
the commit/push/PR step for ordinary 'issues'/'hybrid' dispatch (the
higher-volume path), and it did the opposite: it read whatever branch was
checked out and, on a disagreement with the tracked feature branch, logged a
warning and ADOPTED the checked-out name as its push target ("git is the source
of truth"). Handed identical on-disk state the two paths returned opposite
verdicts.

The live mismatch source is the agent container's own git moving HEAD inside
the bind-mounted worktree: epic E's worktree is on feature/issue-E, sub-issue
#A's container runs `git switch -c scratch`, and the adopt-what-git-says rule
staged, committed and pushed #A's entire feature onto scratch, then opened a PR
against it.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, Mock, patch

from services.feature_branch_manager import FeatureBranchManager


@pytest.fixture
def manager():
    return FeatureBranchManager()


def _tracked_feature_branch(branch_name='feature/issue-5-epic', parent_issue=5):
    """A feature-branch state object as get_feature_branch_for_issue() returns."""
    fb = MagicMock()
    fb.branch_name = branch_name
    fb.parent_issue = parent_issue
    return fb


class TestExpectedBranchIsVerifiedBeforeCommitting:

    @pytest.mark.asyncio
    async def test_refuses_when_the_container_moved_head_to_another_branch(self, manager, tmp_path):
        """
        THE regression. The worktree resolve_workspace() checked out to
        feature/issue-5-epic is on 'scratch' by finalize time. Nothing may be
        staged, committed, pushed, or turned into a PR -- the changes stay on
        disk for the caller's failure path, exactly as auto_commit.py's
        _verify_commit_branch() leaves them.
        """
        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          return_value='scratch'), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock) as mock_get_fb, \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
             patch.object(manager, 'git_commit', new_callable=AsyncMock) as mock_commit, \
             patch.object(manager, 'git_push', new_callable=AsyncMock) as mock_push, \
             patch.object(manager, 'create_or_update_feature_pr', new_callable=AsyncMock) as mock_pr:

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=7,
                commit_message='Complete work for issue #7',
                github_integration=Mock(),
                project_dir_override=str(tmp_path),
                expected_branch='feature/issue-5-epic',
            )

            assert result['success'] is False
            assert result['branch_mismatch'] is True
            assert 'scratch' in result['error']
            assert 'feature/issue-5-epic' in result['error']

            mock_add.assert_not_called()
            mock_commit.assert_not_called()
            mock_push.assert_not_called()
            mock_pr.assert_not_called()
            # Refused ahead of the standalone/tracked fork, so no state lookup
            # (or PR work) happens against a workspace we won't commit from.
            mock_get_fb.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_a_standalone_issue_on_the_wrong_branch_too(self, manager, tmp_path):
        """The no-feature-branch-state path commits and pushes ambient HEAD as
        well, so the guard has to sit ahead of the fork rather than inside the
        tracked branch."""
        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          return_value='scratch'), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
             patch.object(manager, 'git_commit', new_callable=AsyncMock) as mock_commit, \
             patch.object(manager, 'git_push', new_callable=AsyncMock) as mock_push:

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=7,
                commit_message='Complete work for issue #7',
                github_integration=Mock(),
                project_dir_override=str(tmp_path),
                expected_branch='feature/issue-7',
            )

            assert result['success'] is False
            assert result['branch_mismatch'] is True
            mock_add.assert_not_called()
            mock_commit.assert_not_called()
            mock_push.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_when_the_branch_cannot_be_read_at_all(self, manager, tmp_path):
        """get_current_branch() raises on a git failure. Letting that propagate
        would reach agent_executor.py's generic finalization handler as an
        unexplained exception; it is this method's own documented refusal
        instead."""
        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          side_effect=RuntimeError('not a git repository')), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
             patch.object(manager, 'git_commit', new_callable=AsyncMock) as mock_commit:

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=7,
                commit_message='Complete work for issue #7',
                github_integration=Mock(),
                project_dir_override=str(tmp_path),
                expected_branch='feature/issue-5-epic',
            )

            assert result['success'] is False
            assert result['branch_mismatch'] is True
            mock_add.assert_not_called()
            mock_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_commits_normally_when_the_branch_matches(self, manager, tmp_path):
        """The ordinary case: nothing moved HEAD, so the verification is
        invisible and the whole commit/push/PR flow runs."""
        feature_branch = _tracked_feature_branch()

        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          return_value='feature/issue-5-epic'), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=feature_branch), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
             patch.object(manager, 'git_commit', new_callable=AsyncMock,
                          return_value=True) as mock_commit, \
             patch.object(manager, 'branch_exists', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'git_push', new_callable=AsyncMock) as mock_push, \
             patch.object(manager, 'mark_sub_issue_complete'), \
             patch.object(manager, 'create_or_update_feature_pr', new_callable=AsyncMock,
                          return_value={'success': True, 'pr_url': 'https://x/pull/1'}) as mock_pr:

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=7,
                commit_message='Complete work for issue #7',
                github_integration=Mock(),
                project_dir_override=str(tmp_path),
                expected_branch='feature/issue-5-epic',
            )

            assert result.get('branch_mismatch') is None
            mock_add.assert_called_once()
            mock_commit.assert_called_once()
            mock_push.assert_called_once_with(str(tmp_path), 'feature/issue-5-epic')
            mock_pr.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_expected_branch_keeps_the_pre_existing_git_wins_behavior(self, manager, tmp_path):
        """The standalone/test callers documented on this method have no
        resolved workspace to read an expectation from, so they must be
        degraded rather than blocked -- the same trade
        commit_agent_changes() makes for a missing expected_branch."""
        feature_branch = _tracked_feature_branch()

        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          return_value='scratch') as mock_branch, \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=feature_branch), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock), \
             patch.object(manager, 'git_commit', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'branch_exists', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'git_push', new_callable=AsyncMock) as mock_push, \
             patch.object(manager, 'mark_sub_issue_complete'), \
             patch.object(manager, 'create_or_update_feature_pr', new_callable=AsyncMock,
                          return_value={'success': True}):

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=7,
                commit_message='Complete work for issue #7',
                github_integration=Mock(),
                project_dir_override=str(tmp_path),
            )

            assert result.get('branch_mismatch') is None
            mock_push.assert_called_once_with(str(tmp_path), 'scratch')
            # Verification is skipped entirely, not run against a None
            # expectation: only the tracked-branch reconciliation reads git.
            assert mock_branch.call_count == 1


class TestWorkspaceContextsBindTheExpectation:
    """
    The kwarg has to actually reach finalize_feature_branch_work() from the
    two production call sites, or the guard is inert. Dropping it would
    otherwise be silent -- the parameter is Optional.
    """

    def _pipeline_run(self):
        run = MagicMock()
        run.project_dir = '/workspace/.orchestrator/worktrees/test-project/5'
        run.branch_name = 'feature/issue-5-epic'
        return run

    @pytest.mark.asyncio
    async def test_issues_context_passes_pipeline_run_branch_name(self):
        from services.workspace.issues_context import IssuesWorkspaceContext

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm:
            mock_fbm.finalize_feature_branch_work = AsyncMock(return_value={'success': True})

            context = IssuesWorkspaceContext(
                project='test-project',
                issue_number=7,
                task_context={'pipeline_run_id': 'run-1'},
                github_integration=MagicMock(),
                pipeline_run=self._pipeline_run(),
            )

            await context.finalize_execution(result={}, commit_message='msg')

            kwargs = mock_fbm.finalize_feature_branch_work.call_args[1]
            assert kwargs['expected_branch'] == 'feature/issue-5-epic'
            assert kwargs['project_dir_override'] == \
                '/workspace/.orchestrator/worktrees/test-project/5'

    @pytest.mark.asyncio
    async def test_hybrid_context_passes_pipeline_run_branch_name(self):
        from services.workspace.hybrid_context import HybridWorkspaceContext

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm:
            mock_fbm.finalize_feature_branch_work = AsyncMock(return_value={'success': True})

            context = HybridWorkspaceContext(
                project='test-project',
                issue_number=7,
                task_context={'pipeline_run_id': 'run-1'},
                github_integration=MagicMock(),
                pipeline_run=self._pipeline_run(),
            )
            context._current_workspace = 'issues'

            await context.finalize_execution(result={}, commit_message='msg')

            kwargs = mock_fbm.finalize_feature_branch_work.call_args[1]
            assert kwargs['expected_branch'] == 'feature/issue-5-epic'


class TestFailsafeDoesNotUndoTheRefusal:
    """
    agent_executor.py's finalization-failure path runs _failsafe_commit_check(),
    an unguarded `git add -A` + commit + push of ambient HEAD. Running it after
    a branch-mismatch refusal would land exactly the work the refusal protected
    on exactly the wrong branch -- so the refusal has to be distinguishable from
    an ordinary finalization failure, which is what the 'branch_mismatch' key is
    for.
    """

    @pytest.mark.asyncio
    async def test_branch_mismatch_skips_the_failsafe_commit(self):
        failsafe = await _run_finalization({
            'success': False,
            'branch_mismatch': True,
            'error': "is on 'scratch' but this dispatch's target is 'feature/issue-5-epic'",
        })
        failsafe.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_ordinary_finalization_failure_still_runs_the_failsafe(self):
        """The pre-existing behavior for every other failure must be
        untouched -- the skip is scoped to the wrong-branch case."""
        failsafe = await _run_finalization({
            'success': False,
            'error': 'PR creation failed',
        })
        failsafe.assert_called_once()


async def _run_finalization(finalize_result):
    """
    Drive execute_agent() through a real IssuesWorkspaceContext to the
    finalization block, with the agent run and the underlying
    FeatureBranchManager stubbed out, and return the _failsafe_commit_check
    mock. Mirrors tests/unit/test_workspace_contexts.py's harness.
    """
    from services.agent_executor import AgentExecutor

    async def fake_resolve_workspace(pipeline_run, github, workspace_type):
        pipeline_run.branch_name = 'feature/issue-5-epic'
        pipeline_run.project_dir = '/workspace/.orchestrator/worktrees/test-project/5'
        return pipeline_run

    mock_prm = MagicMock()
    mock_prm.get_pipeline_run.return_value = MagicMock(id='run-1')
    mock_prm.resolve_workspace = AsyncMock(side_effect=fake_resolve_workspace)

    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        executor = AgentExecutor()

    with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
         patch('services.agent_executor.config_manager') as mock_config, \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=mock_prm), \
         patch.object(executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(executor, '_post_agent_output_to_github', new_callable=AsyncMock), \
         patch.object(executor, '_failsafe_commit_check', new_callable=AsyncMock) as mock_failsafe:

        mock_fbm.finalize_feature_branch_work = AsyncMock(return_value=finalize_result)

        mock_project_config = MagicMock()
        mock_project_config.github = {'org': 'test-org', 'repo': 'test-repo'}
        mock_config.get_project_config.return_value = mock_project_config
        mock_config.get_project_agent_config.return_value = {}

        mock_agent = MagicMock()
        mock_agent.execute = AsyncMock(return_value={'status': 'success'})
        mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
        mock_agent.agent_config = {}
        mock_create_agent.return_value = mock_agent

        await executor.execute_agent(
            agent_name='test_agent',
            project_name='test-project',
            task_context={
                'issue_number': 7,
                'issue_title': 'Test feature',
                'workspace_type': 'issues',
                'pipeline_run_id': 'run-1',
            },
        )

        # The harness must actually have reached the finalization block --
        # otherwise both assertions below would pass vacuously.
        mock_fbm.finalize_feature_branch_work.assert_called_once()
        return mock_failsafe

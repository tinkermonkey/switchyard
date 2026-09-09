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
            # Carried out structurally so the caller can quarantine the worktree
            # without re-parsing the message.
            assert result['expected_branch'] == 'feature/issue-5-epic'
            assert result['current_branch'] == 'scratch'

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
    async def test_an_unreadable_branch_is_its_own_verdict_not_a_mismatch(self, manager, tmp_path):
        """get_current_branch() runs with check=True, so a transient
        `.git/index.lock` or a timed-out read raises here exactly like a genuine
        wrong branch does. Reporting that as branch_mismatch would suppress
        agent_executor.py's failsafe commit -- the path that used to SAVE the
        work in precisely this case -- so it gets its own key."""
        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          side_effect=RuntimeError('index.lock exists')) as mock_branch, \
             patch('services.feature_branch_manager.asyncio.sleep', new_callable=AsyncMock), \
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
            assert result['branch_unverifiable'] is True
            assert result.get('branch_mismatch') is None
            assert result['expected_branch'] == 'feature/issue-5-epic'
            # Retried once before giving up -- the dominant cause is transient.
            assert mock_branch.call_count == 2
            mock_add.assert_not_called()
            mock_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_transient_read_failure_is_retried_and_then_proceeds(self, manager, tmp_path):
        """The retry is the point: a lock held by a dying container is gone a
        second later, and the run commits normally instead of stalling."""
        feature_branch = _tracked_feature_branch()

        with patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          side_effect=[RuntimeError('index.lock exists'),
                                       'feature/issue-5-epic',
                                       'feature/issue-5-epic']), \
             patch('services.feature_branch_manager.asyncio.sleep', new_callable=AsyncMock), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=feature_branch), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
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
                expected_branch='feature/issue-5-epic',
            )

            assert result.get('branch_unverifiable') is None
            assert result.get('branch_mismatch') is None
            mock_add.assert_called_once()
            mock_push.assert_called_once_with(str(tmp_path), 'feature/issue-5-epic')

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


class TestARefusalStopsTheRunInsteadOfAdvancingIt:
    """
    The refusal itself was only half the fix. agent_executor.py's handler logged
    it and fell through -- reaching record_execution_outcome(outcome='success')
    and returning normally, so orchestrator_integration.py advanced the issue to
    review against a branch holding NONE of the agent's work, and released the
    pipeline lock as a success (#149 WI-4 review).

    Two things must therefore hold for a branch_mismatch: the failsafe commit is
    skipped (it is an ambient-HEAD `git add -A` + push, i.e. exactly the thing
    the refusal declined to do), and the run does not reach the success path.
    """

    @pytest.mark.asyncio
    async def test_branch_mismatch_skips_the_failsafe_commit(self):
        harness = await _run_finalization({
            'success': False,
            'branch_mismatch': True,
            'expected_branch': 'feature/issue-5-epic',
            'current_branch': 'scratch',
            'error': "is on 'scratch' but this dispatch's target is 'feature/issue-5-epic'",
        })
        harness['failsafe'].assert_not_called()

    @pytest.mark.asyncio
    async def test_branch_mismatch_never_records_a_successful_execution(self):
        """THE critical regression: a refusal reported as success auto-advances
        the issue with the agent's work uncommitted on disk."""
        harness = await _run_finalization({
            'success': False,
            'branch_mismatch': True,
            'expected_branch': 'feature/issue-5-epic',
            'current_branch': 'scratch',
            'error': "is on 'scratch' but this dispatch's target is 'feature/issue-5-epic'",
        })

        from agents.non_retryable import NonRetryableAgentError
        assert isinstance(harness['exception'], NonRetryableAgentError)

        outcomes = [
            call.kwargs.get('outcome')
            for call in harness['tracker'].record_execution_outcome.call_args_list
        ]
        assert 'success' not in outcomes

    @pytest.mark.asyncio
    async def test_branch_mismatch_retains_the_pipeline_lock_and_explains_itself(self):
        """Same escalation the push-rejection path already does: mark_failed()
        so the board lock is durably retained, plus an issue comment -- a log
        line in the orchestrator container is not an operator signal."""
        harness = await _run_finalization({
            'success': False,
            'branch_mismatch': True,
            'expected_branch': 'feature/issue-5-epic',
            'current_branch': 'scratch',
            'error': "is on 'scratch' but this dispatch's target is 'feature/issue-5-epic'",
        })

        harness['prm'].mark_failed.assert_called_once()
        assert harness['prm'].mark_failed.call_args.kwargs['issue_number'] == 7

        harness['github'].post_comment.assert_awaited_once()
        body = harness['github'].post_comment.await_args[0][1]
        assert 'Wrong Branch' in body
        assert 'feature/issue-5-epic' in body
        assert 'scratch' in body

    @pytest.mark.asyncio
    async def test_branch_mismatch_quarantines_the_epic_worktree(self):
        """The refusal is otherwise one-shot: the NEXT dispatch is a fresh
        PipelineRun whose resolve_workspace() re-reads the drifted branch and
        persists it as its own expectation, at which point the guard compares
        'scratch' against 'scratch', passes, and commits both issues' work onto
        it -- #143, one dispatch later."""
        harness = await _run_finalization({
            'success': False,
            'branch_mismatch': True,
            'expected_branch': 'feature/issue-5-epic',
            'current_branch': 'scratch',
            'error': "is on 'scratch' but this dispatch's target is 'feature/issue-5-epic'",
        })

        harness['workspace_manager'].quarantine_epic_worktree.assert_called_once()
        kwargs = harness['workspace_manager'].quarantine_epic_worktree.call_args.kwargs
        assert kwargs['epic_id'] == '5'
        assert kwargs['expected_branch'] == 'feature/issue-5-epic'
        assert kwargs['actual_branch'] == 'scratch'

    @pytest.mark.asyncio
    async def test_an_ordinary_finalization_failure_still_runs_the_failsafe(self):
        """The pre-existing behavior for every other failure must be
        untouched -- the skip and the escalation are scoped to the branch
        verdicts."""
        harness = await _run_finalization({
            'success': False,
            'error': 'PR creation failed',
        })
        harness['failsafe'].assert_called_once()
        assert harness['exception'] is None
        harness['prm'].mark_failed.assert_not_called()


class TestAnUnverifiableBranchKeepsItsFailsafeRecovery:
    """
    An unreadable branch is much commoner than a genuine wrong branch (a
    transient index.lock, a timed-out read) and used to be RECOVERED: the
    exception reached agent_executor.py's generic handler, which ran the
    failsafe and committed the work. Collapsing it into the mismatch verdict
    would have silently converted those runs into ones that commit nothing.

    The failsafe now verifies the branch itself, so letting it try is safe: it
    either confirms the branch and saves the work, or refuses -- and only then
    is there nothing left but to block the pipeline.
    """

    _UNVERIFIABLE = {
        'success': False,
        'branch_unverifiable': True,
        'expected_branch': 'feature/issue-5-epic',
        'error': 'Cannot verify the branch: index.lock exists',
    }

    @pytest.mark.asyncio
    async def test_the_failsafe_still_runs_and_the_run_continues_when_it_succeeds(self):
        harness = await _run_finalization(dict(self._UNVERIFIABLE))

        harness['failsafe'].assert_called_once()
        assert harness['exception'] is None
        harness['prm'].mark_failed.assert_not_called()
        harness['workspace_manager'].quarantine_epic_worktree.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_pipeline_is_blocked_when_the_failsafe_cannot_confirm_either(self):
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            dict(self._UNVERIFIABLE),
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_UNVERIFIABLE),
        )

        harness['failsafe'].assert_called_once()
        from agents.non_retryable import NonRetryableAgentError
        assert isinstance(harness['exception'], NonRetryableAgentError)
        harness['prm'].mark_failed.assert_called_once()

        body = harness['github'].post_comment.await_args[0][1]
        assert 'Branch Unverifiable' in body

    @pytest.mark.asyncio
    async def test_an_unreadable_branch_does_not_quarantine_the_epic(self):
        """The escalation window is a few seconds wide -- two reads in
        _verify_finalize_branch() a second apart, then one more in
        _verify_failsafe_branch(). An unlucky index.lock across all three used to
        quarantine the epic (blocking every issue under it until a human deletes a
        JSON file inside the container) on top of mark_failed()'s board-wide lock
        retention, on evidence of exactly nothing: the marker would have recorded
        actual_branch: null and the comment 'Found: could not be determined'
        (#149 WI-4 review)."""
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            dict(self._UNVERIFIABLE),
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_UNVERIFIABLE),
        )

        harness['workspace_manager'].quarantine_epic_worktree.assert_not_called()
        # Blocking the run is still the right response to an unreadable branch.
        harness['prm'].mark_failed.assert_called_once()

        body = harness['github'].post_comment.await_args[0][1]
        # ...and the recovery steps must not tell an operator to delete a marker
        # that was never written.
        assert 'branch-quarantine.json' not in body

    @pytest.mark.asyncio
    async def test_an_unreadable_branch_the_failsafe_CAN_read_still_quarantines(self):
        """The other half: finalization could not read the branch, but the failsafe
        could -- and found drift. That IS on-disk evidence nothing repairs, so the
        next dispatch must not be allowed to adopt it."""
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            dict(self._UNVERIFIABLE),
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_DRIFTED, 'scratch'),
        )

        harness['workspace_manager'].quarantine_epic_worktree.assert_called_once()
        kwargs = harness['workspace_manager'].quarantine_epic_worktree.call_args.kwargs
        assert kwargs['actual_branch'] == 'scratch'

        body = harness['github'].post_comment.await_args[0][1]
        assert 'branch-quarantine.json' in body
        # The branch the failsafe read, not 'could not be determined'.
        assert 'scratch' in body

    @pytest.mark.asyncio
    async def test_a_failsafe_that_committed_nothing_blocks_the_run(self):
        """`handled` used to be True for untracked-only changes, for a `git commit`
        that exited non-zero, for a failed `git add`, and for a push that threw --
        every one of which let this path record outcome='success' with the work
        uncommitted on disk."""
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            dict(self._UNVERIFIABLE),
            failsafe_result=FailsafeResult(
                FailsafeOutcome.NOT_COMMITTED, 'feature/issue-5-epic'
            ),
        )

        from agents.non_retryable import NonRetryableAgentError
        assert isinstance(harness['exception'], NonRetryableAgentError)
        harness['prm'].mark_failed.assert_called_once()

        outcomes = [
            call.kwargs.get('outcome')
            for call in harness['tracker'].record_execution_outcome.call_args_list
        ]
        assert 'success' not in outcomes

        # Nothing drifted -- the branch was fine, the commit just never happened --
        # so the epic is not quarantined over it.
        harness['workspace_manager'].quarantine_epic_worktree.assert_not_called()
        body = harness['github'].post_comment.await_args[0][1]
        assert 'Failsafe' in body


class TestTheFailsafesOwnRefusalIsNotSilent:
    """
    _verify_failsafe_branch()'s refusal reached only ONE of
    _failsafe_commit_check()'s four call sites. The other three discarded it, and
    the highest-frequency of those is the workspace_context is None branch that
    every skip_workspace_prep dispatch takes -- all of repair_cycle.py's inner
    agents. A repair_fix container that left the epic worktree on a scratch branch
    got: no comment, no mark_failed(), no exception, and
    record_execution_outcome(outcome='success'). The repair cycle then carried on
    believing the fix had landed, repair_test passed against the still-uncommitted
    tree, and the PR never contained the fix (#149 WI-4 review).
    """

    _REPAIR_CONTEXT = {
        'branch_name': 'feature/issue-5-epic',
        'project_dir': '/workspace/.orchestrator/worktrees/test-project/5',
        'epic_id': '5',
    }

    @pytest.mark.asyncio
    async def test_a_skipped_workspace_prep_refusal_blocks_instead_of_succeeding(self):
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            None,
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_DRIFTED, 'fix-attempt'),
            task_context_extra=dict(self._REPAIR_CONTEXT),
            workspace_context=False,
        )

        from agents.non_retryable import NonRetryableAgentError
        assert isinstance(harness['exception'], NonRetryableAgentError)
        harness['prm'].mark_failed.assert_called_once()

        outcomes = [
            call.kwargs.get('outcome')
            for call in harness['tracker'].record_execution_outcome.call_args_list
        ]
        assert 'success' not in outcomes

        body = harness['github'].post_comment.await_args[0][1]
        assert 'Wrong Branch' in body
        assert 'fix-attempt' in body

    @pytest.mark.asyncio
    async def test_a_skipped_workspace_prep_refusal_quarantines_confirmed_drift(self):
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            None,
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_DRIFTED, 'fix-attempt'),
            task_context_extra=dict(self._REPAIR_CONTEXT),
            workspace_context=False,
        )

        harness['workspace_manager'].quarantine_epic_worktree.assert_called_once()
        kwargs = harness['workspace_manager'].quarantine_epic_worktree.call_args.kwargs
        assert kwargs['epic_id'] == '5'
        assert kwargs['actual_branch'] == 'fix-attempt'

    @pytest.mark.asyncio
    async def test_a_skipped_workspace_prep_run_that_commits_cleanly_still_succeeds(self):
        """The control: the ordinary repair-cycle dispatch must be untouched."""
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            None,
            failsafe_result=FailsafeResult(FailsafeOutcome.COMMITTED, 'feature/issue-5-epic'),
            task_context_extra=dict(self._REPAIR_CONTEXT),
            workspace_context=False,
        )

        assert harness['exception'] is None
        harness['prm'].mark_failed.assert_not_called()
        harness['workspace_manager'].quarantine_epic_worktree.assert_not_called()

    @pytest.mark.asyncio
    async def test_an_ordinary_finalization_failures_refusal_is_not_silent_either(self):
        """The second ignoring call site: finalization failed for some other
        reason (a failed PR creation, say), and the failsafe then refused on branch
        grounds. That refusal used to be discarded exactly as above."""
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            {'success': False, 'error': 'PR creation failed'},
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_DRIFTED, 'scratch'),
        )

        from agents.non_retryable import NonRetryableAgentError
        assert isinstance(harness['exception'], NonRetryableAgentError)
        harness['prm'].mark_failed.assert_called_once()

    @pytest.mark.asyncio
    async def test_a_finalization_exceptions_refusal_is_not_silent_either(self):
        """The third: the failsafe runs from inside the finalization exception
        handler, whose own `except Exception` would have swallowed the escalation
        if it were raised in there."""
        from services.agent_executor import FailsafeOutcome, FailsafeResult

        harness = await _run_finalization(
            RuntimeError('finalization blew up'),
            failsafe_result=FailsafeResult(FailsafeOutcome.BRANCH_DRIFTED, 'scratch'),
        )

        from agents.non_retryable import NonRetryableAgentError
        assert isinstance(harness['exception'], NonRetryableAgentError)
        harness['prm'].mark_failed.assert_called_once()


async def _run_finalization(
    finalize_result,
    failsafe_result=None,
    task_context_extra=None,
    workspace_context=True,
):
    """
    Drive execute_agent() through a real IssuesWorkspaceContext to the
    finalization block, with the agent run and the underlying
    FeatureBranchManager stubbed out. Mirrors tests/unit/test_workspace_contexts.py's
    harness.

    Returns a dict of the mocks the assertions above read, plus the exception
    execute_agent() raised (None when it returned normally) -- the escalation
    paths deliberately raise, so the harness cannot simply let it propagate.

    failsafe_result is the FailsafeResult _failsafe_commit_check() is stubbed to
    answer (defaulting to a committed-and-pushed workspace). workspace_context=False
    drives the OTHER branch of the finalization block instead -- the one every
    skip_workspace_prep dispatch takes, where there is no workspace context at all.
    """
    from services.agent_executor import AgentExecutor, FailsafeOutcome, FailsafeResult

    if failsafe_result is None:
        failsafe_result = FailsafeResult(FailsafeOutcome.COMMITTED, 'feature/issue-5-epic')

    async def fake_resolve_workspace(pipeline_run, github, workspace_type):
        pipeline_run.branch_name = 'feature/issue-5-epic'
        pipeline_run.project_dir = '/workspace/.orchestrator/worktrees/test-project/5'
        pipeline_run.epic_id = '5'
        return pipeline_run

    mock_prm = MagicMock()
    mock_prm.get_pipeline_run.return_value = MagicMock(id='run-1')
    mock_prm.resolve_workspace = AsyncMock(side_effect=fake_resolve_workspace)
    mock_prm.mark_failed.return_value = True

    mock_github = MagicMock()
    mock_github.post_comment = AsyncMock()

    mock_workspace_manager = MagicMock()
    mock_tracker = MagicMock()

    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        executor = AgentExecutor()

    with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
         patch('services.agent_executor.config_manager') as mock_config, \
         patch('services.pipeline_run.get_pipeline_run_manager', return_value=mock_prm), \
         patch('services.github_integration.GitHubIntegration', return_value=mock_github), \
         patch('services.project_workspace.workspace_manager', mock_workspace_manager), \
         patch('services.work_execution_state.work_execution_tracker', mock_tracker), \
         patch.object(executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(executor, '_post_agent_output_to_github', new_callable=AsyncMock), \
         patch.object(executor, '_failsafe_commit_check', new_callable=AsyncMock,
                      return_value=failsafe_result) as mock_failsafe:

        if isinstance(finalize_result, Exception):
            # Drives the finalization EXCEPTION handler rather than a verdict dict.
            mock_fbm.finalize_feature_branch_work = AsyncMock(side_effect=finalize_result)
        else:
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

        raised = None
        try:
            task_context = {
                'issue_number': 7,
                'issue_title': 'Test feature',
                'workspace_type': 'issues',
                'pipeline_run_id': 'run-1',
                'board': 'dev_workflow',
            }
            if not workspace_context:
                # What skip_workspace_prep dispatches look like: execute_agent()
                # builds no workspace context, so the failsafe is the ONLY commit
                # path (pipeline/repair_cycle.py's inner agents all set this).
                task_context['skip_workspace_prep'] = True
            task_context.update(task_context_extra or {})

            await executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context,
            )
        except Exception as e:
            raised = e

        # The harness must actually have reached the finalization block --
        # otherwise every assertion above would pass vacuously.
        if workspace_context:
            mock_fbm.finalize_feature_branch_work.assert_called_once()
        else:
            mock_failsafe.assert_called_once()

        return {
            'failsafe': mock_failsafe,
            'exception': raised,
            'prm': mock_prm,
            'github': mock_github,
            'workspace_manager': mock_workspace_manager,
            'tracker': mock_tracker,
        }

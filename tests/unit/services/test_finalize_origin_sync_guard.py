"""
#264 and #265, both from pipeline run fbd185c9-ec5b-475c-83de-769531240c8d.

That run failed on 2026-09-17 with a non-fast-forward rejection AFTER #261 was
already deployed (guard live 19:14:10Z, run 19:16:34Z-19:32:41Z). Two reasons:

  #264  #261 wired sync_epic_worktree_before_commit() into auto_commit and
        agent_executor, but NOT into finalize_feature_branch_work() -- the path
        the review cycle actually finalizes on, and the one all four of that
        run's pushes went through.

  #265  push_branch() then asserted "the agent likely amended or rewrote
        history" for every rejection. It was right that time, but it is an
        inference, and it recommends a force-push that would destroy origin's
        commits if the branch were merely behind.

The underlying cause -- an agent resetting past pushed commits and
cherry-picking one back inside its own container -- is #266.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from services.git_workflow_manager import GitWorkflowManager

# feature_branch_manager.py imports subprocess INSIDE each function (its house
# style -- see lines 412 and 1323), so there is no module attribute to patch;
# these target subprocess.run directly. git_workflow_manager imports it at
# module level, hence the different target in the class above.


def _counts(ahead, behind):
    """subprocess.run side effect: fetch, then ahead, then behind."""
    def _run(cmd, **kwargs):
        r = MagicMock(returncode=0, stderr='')
        if 'rev-list' in cmd:
            r.stdout = f"{ahead}\n" if any('..HEAD' in c for c in cmd) else f"{behind}\n"
        else:
            r.stdout = ''
        return r
    return _run


class TestARejectedPushIsMeasuredNotGuessed:
    """#265."""

    def _manager(self):
        return GitWorkflowManager.__new__(GitWorkflowManager)

    def test_divergence_is_named_and_force_push_is_warned_against(self):
        """The shape that actually occurred: local history rewritten, so origin
        has commits local lacks. Force-pushing here destroys them."""
        m = self._manager()
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=_counts(ahead=1, behind=1)):
            msg = m._describe_rejected_push('/wt', 'feature/issue-1018-feature', 'rejected')

        assert 'DIVERGED' in msg
        assert '1 local commit(s) are not on origin' in msg
        assert '1 origin' in msg
        assert 'Do NOT force-push' in msg

    def test_merely_behind_is_not_reported_as_a_rewrite(self):
        """The case the old fixed message got wrong. A stale branch wants a
        fetch and a retry -- telling an operator to force-push it is advice to
        delete origin's commits."""
        m = self._manager()
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=_counts(ahead=0, behind=3)):
            msg = m._describe_rejected_push('/wt', 'feature/x', 'rejected')

        assert 'advanced by 3 commit(s)' in msg
        assert 'not a rewrite' in msg
        assert 'No force-push is warranted' in msg
        assert 'DIVERGED' not in msg

    def test_a_fast_forward_rejection_claims_no_cause(self):
        """Ahead and not behind should have fast-forwarded. Whatever rejected
        it is not divergence, so the message must not invent one."""
        m = self._manager()
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=_counts(ahead=2, behind=0)):
            msg = m._describe_rejected_push('/wt', 'feature/x', 'rejected')

        assert 'should have fast-forwarded' in msg
        assert 'protected branch' in msg
        assert 'rewrote history' not in msg

    def test_an_unmeasurable_rejection_claims_no_cause(self):
        """Runs inside an error path that is about to raise. Failing to measure
        must not replace the push failure with a measurement failure, and must
        not fall back to the guess this replaced."""
        m = self._manager()
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=OSError("git missing")):
            msg = m._describe_rejected_push('/wt', 'feature/x', 'the raw stderr')

        assert 'no cause is claimed' in msg
        assert 'the raw stderr' in msg, "git's own output must survive"
        assert 'likely amended' not in msg

    def test_git_stderr_is_always_preserved(self):
        m = self._manager()
        with patch('services.git_workflow_manager.subprocess.run',
                   side_effect=_counts(ahead=1, behind=1)):
            msg = m._describe_rejected_push('/wt', 'feature/x', 'STDERR-MARKER')
        assert 'STDERR-MARKER' in msg


class TestFinalizeChecksOriginBeforePushing:
    """#264."""

    def _manager(self):
        from services.feature_branch_manager import FeatureBranchManager
        return FeatureBranchManager.__new__(FeatureBranchManager)

    @pytest.mark.asyncio
    async def test_a_diverged_epic_worktree_is_refused(self):
        """The regression. Before this, finalize pushed unchecked and the
        rejection crashed the review-cycle thread."""
        m = self._manager()
        sync = MagicMock(ok=False, detail='ahead 1, behind 1')

        with patch('services.project_workspace.workspace_manager') as ws, \
             patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='git@github.com:a/b.git\n')), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.sync_epic_worktree_before_commit', AsyncMock(return_value=sync)):
            ws.is_base_clone_dir.return_value = False
            detail = await m._refuse_if_out_of_sync_with_origin('proj', '/wt', 'feature/x')

        assert detail == 'ahead 1, behind 1'

    @pytest.mark.asyncio
    async def test_a_synced_worktree_proceeds(self):
        m = self._manager()
        sync = MagicMock(ok=True, detail=None)

        with patch('services.project_workspace.workspace_manager') as ws, \
             patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='git@github.com:a/b.git\n')), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.sync_epic_worktree_before_commit', AsyncMock(return_value=sync)):
            ws.is_base_clone_dir.return_value = False
            assert await m._refuse_if_out_of_sync_with_origin('proj', '/wt', 'feature/x') is None

    @pytest.mark.asyncio
    async def test_the_base_clone_is_exempt(self):
        """Matches the two existing call sites: the guard is about epic
        worktrees, whose branch is shared across an epic's sub-issues."""
        m = self._manager()
        called = AsyncMock()

        # subprocess is stubbed to report a healthy origin so that, if the
        # base-clone exemption were removed, control would reach the sync call
        # and this test would fail. Without it the real `git remote get-url`
        # fails on a fake path, the no-origin skip fires, and the test passes
        # whether or not the exemption exists.
        with patch('services.project_workspace.workspace_manager') as ws, \
             patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='git@github.com:a/b.git\n')), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.sync_epic_worktree_before_commit', called):
            ws.is_base_clone_dir.return_value = True
            assert await m._refuse_if_out_of_sync_with_origin('proj', '/base', 'main') is None

        assert not called.called, "a base clone must not be checked against origin"

    @pytest.mark.asyncio
    async def test_no_origin_remote_skips_rather_than_refuses(self):
        """"No origin configured" is a conclusive local fact, not an out-of-sync
        condition. Refusing here would block every workspace that has no remote,
        which is how this first showed up: 7 pre-existing tests using a bare
        temp repo started failing."""
        m = self._manager()
        called = AsyncMock()

        with patch('services.project_workspace.workspace_manager') as ws, \
             patch('subprocess.run',
                   return_value=MagicMock(returncode=2, stdout='', stderr='no such remote')), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.sync_epic_worktree_before_commit', called):
            ws.is_base_clone_dir.return_value = False
            assert await m._refuse_if_out_of_sync_with_origin('proj', '/wt', 'feature/x') is None

        assert not called.called

    @pytest.mark.asyncio
    async def test_an_unreachable_origin_still_refuses(self):
        """The narrow counterpart to the skip above: the remote EXISTS but the
        question went unanswered, so the branch may well have moved. Skipping
        that would turn the guard off exactly when it is needed."""
        m = self._manager()
        sync = MagicMock(ok=False, detail="Could not query origin for branch 'feature/x'")

        with patch('services.project_workspace.workspace_manager') as ws, \
             patch('subprocess.run',
                   return_value=MagicMock(returncode=0, stdout='git@github.com:a/b.git\n')), \
             patch('services.git_workflow_manager.git_workflow_manager'
                   '.sync_epic_worktree_before_commit', AsyncMock(return_value=sync)):
            ws.is_base_clone_dir.return_value = False
            detail = await m._refuse_if_out_of_sync_with_origin('proj', '/wt', 'feature/x')

        assert detail is not None and 'Could not query origin' in detail


class TestTheWiring:
    """Each of these was written because a mutation survived without it.

    The helpers above can all be correct while nothing calls them -- which is
    precisely the shape of #264 itself, where #261's guard existed and worked
    but was never wired into this path.
    """

    def test_push_branch_raises_the_measured_message(self):
        """m1: reverting push_branch() to the old fixed guess passed every test
        above, because they all called _describe_rejected_push() directly."""
        from services.git_workflow_manager import GitWorkflowManager, PushFailedError
        import asyncio

        m = GitWorkflowManager.__new__(GitWorkflowManager)

        def _run(cmd, **kwargs):
            if 'push' in cmd:
                return MagicMock(returncode=1, stdout='',
                                 stderr='! [rejected] main -> main (non-fast-forward)')
            r = MagicMock(returncode=0, stderr='')
            r.stdout = '1\n' if 'rev-list' in cmd else ''
            return r

        with patch('services.git_workflow_manager.subprocess.run', side_effect=_run):
            with pytest.raises(PushFailedError) as exc:
                asyncio.run(m.push_branch('/wt', 'main'))

        assert 'DIVERGED' in str(exc.value), (
            "push_branch must use the measured description, not a fixed guess"
        )
        assert 'likely amended' not in str(exc.value)

    def test_ahead_behind_fetches_before_counting(self):
        """m4: after a rejected push the local remote-tracking ref can be stale,
        which is exactly the state that makes the counts lie. Dropping the fetch
        is invisible to a test that stubs subprocess wholesale."""
        from services.git_workflow_manager import GitWorkflowManager

        m = GitWorkflowManager.__new__(GitWorkflowManager)
        seen = []

        def _run(cmd, **kwargs):
            seen.append(cmd)
            r = MagicMock(returncode=0, stderr='')
            r.stdout = '0\n' if 'rev-list' in cmd else ''
            return r

        with patch('services.git_workflow_manager.subprocess.run', side_effect=_run):
            m._ahead_behind('/wt', 'feature/x')

        assert any('fetch' in c for c in seen), "counts against a stale ref are wrong"
        assert seen[0].index('fetch') if 'fetch' in seen[0] else True, \
            "the fetch must come before the counts"
        assert sum(1 for c in seen if 'rev-list' in c) == 2


class TestFinalizeActuallyCallsTheGuard:
    """m9 / m10: the helper being right is not the fix -- calling it is."""

    def _fbm(self, tmp_path):
        from services.feature_branch_manager import FeatureBranchManager
        (tmp_path / 'test-project').mkdir(exist_ok=True)
        return FeatureBranchManager(workspace_root=str(tmp_path))

    @pytest.mark.asyncio
    async def test_the_feature_branch_path_refuses_a_diverged_worktree(self, tmp_path):
        from services.feature_branch_manager import FeatureBranch

        fbm = self._fbm(tmp_path)
        mock_fb = FeatureBranch(parent_issue=50,
                                branch_name='feature/issue-50-parent',
                                created_at='2025-10-07T00:00:00')
        pushed = AsyncMock()

        with patch.object(fbm, 'get_feature_branch_for_issue', return_value=mock_fb), \
             patch.object(fbm, 'get_current_branch', new_callable=AsyncMock,
                          return_value='feature/issue-50-parent'), \
             patch.object(fbm, 'branch_exists', new_callable=AsyncMock, return_value=True), \
             patch.object(fbm, 'git_add_all', new_callable=AsyncMock), \
             patch.object(fbm, 'git_commit', new_callable=AsyncMock), \
             patch.object(fbm, 'git_push', pushed), \
             patch.object(fbm, '_refuse_if_out_of_sync_with_origin',
                          new_callable=AsyncMock, return_value='ahead 1, behind 1'):
            result = await fbm.finalize_feature_branch_work(
                project='test-project', issue_number=51,
                commit_message='Test commit', github_integration=MagicMock(),
            )

        assert result['success'] is False
        assert result.get('origin_out_of_sync') is True
        assert 'ahead 1, behind 1' in result['error']
        assert not pushed.called, (
            "this is the whole point: a diverged worktree must not reach git push"
        )

    @pytest.mark.asyncio
    async def test_the_standalone_path_refuses_a_diverged_worktree(self, tmp_path):
        fbm = self._fbm(tmp_path)
        pushed = AsyncMock()

        with patch.object(fbm, 'get_feature_branch_for_issue', return_value=None), \
             patch.object(fbm, 'git_add_all', new_callable=AsyncMock), \
             patch.object(fbm, 'git_commit', new_callable=AsyncMock, return_value=True), \
             patch('services.git_workflow_manager.git_workflow_manager.get_current_branch',
                   new_callable=AsyncMock, return_value='feature/standalone'), \
             patch.object(fbm, 'git_push', pushed), \
             patch.object(fbm, '_refuse_if_out_of_sync_with_origin',
                          new_callable=AsyncMock, return_value='ahead 2, behind 1'):
            result = await fbm.finalize_feature_branch_work(
                project='test-project', issue_number=77,
                commit_message='Test commit', github_integration=MagicMock(),
            )

        assert result['success'] is False
        assert result.get('origin_out_of_sync') is True
        assert not pushed.called

    @pytest.mark.asyncio
    async def test_a_synced_feature_branch_still_pushes(self, tmp_path):
        """Control: the guard must not stand between every finalize and origin."""
        from services.feature_branch_manager import FeatureBranch

        fbm = self._fbm(tmp_path)
        mock_fb = FeatureBranch(parent_issue=50,
                                branch_name='feature/issue-50-parent',
                                created_at='2025-10-07T00:00:00')
        pushed = AsyncMock()

        with patch.object(fbm, 'get_feature_branch_for_issue', return_value=mock_fb), \
             patch.object(fbm, 'get_current_branch', new_callable=AsyncMock,
                          return_value='feature/issue-50-parent'), \
             patch.object(fbm, 'branch_exists', new_callable=AsyncMock, return_value=True), \
             patch.object(fbm, 'git_add_all', new_callable=AsyncMock), \
             patch.object(fbm, 'git_commit', new_callable=AsyncMock), \
             patch.object(fbm, 'git_push', pushed), \
             patch.object(fbm, 'mark_sub_issue_complete'), \
             patch.object(fbm, 'check_all_sub_issues_complete', return_value=False), \
             patch.object(fbm, 'create_or_update_feature_pr', new_callable=AsyncMock,
                          return_value={'pr_url': 'https://x/pull/1'}), \
             patch.object(fbm, '_refuse_if_out_of_sync_with_origin',
                          new_callable=AsyncMock, return_value=None):
            result = await fbm.finalize_feature_branch_work(
                project='test-project', issue_number=51,
                commit_message='Test commit', github_integration=MagicMock(),
            )

        assert result['success'] is True
        assert pushed.called

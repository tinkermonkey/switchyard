"""
Unit tests for AutoCommitService.commit_agent_changes()'s project_dir contract
(issue #123, WI-D of #119).

commit_agent_changes() used to independently re-resolve its own working
directory via workspace_manager.get_project_dir(epic_id=..., branch_name=...)
(#48 fast-follow) -- a second, divergence-prone resolution of the SAME
directory the caller's agent already worked in (resolved once, upstream, via
PipelineRunManager.resolve_workspace()). #123 removed that independent
resolution entirely: callers now pass their already-resolved project_dir
straight through. These tests confirm the directory passed in is used
directly (no re-derivation), and that it genuinely flows into the
branch-check/change-detection calls downstream -- a real tmp_path (which
exists()) lets the commit flow proceed past the existence check, unlike a
bare Mock/fake path.
"""

import pytest
from contextlib import asynccontextmanager
from unittest.mock import patch

from services.auto_commit import AutoCommitService, CommitResult


@pytest.fixture
def service():
    return AutoCommitService()


class TestCommitAgentChangesProjectDir:
    """commit_agent_changes() must use exactly the project_dir the caller passes --
    no independent re-resolution."""

    @pytest.mark.asyncio
    async def test_passed_project_dir_is_used_directly(self, service, tmp_path):
        """The directory passed in must genuinely flow into the
        branch-check/change-detection calls, not just be discarded -- a real
        tmp_path (which exists()) lets the commit flow proceed past the
        existence check into _get_current_branch/_check_for_changes."""
        with patch.object(service, '_get_current_branch', return_value='feature/issue-7-epic') as mock_branch, \
             patch.object(service, '_check_for_changes', return_value=False) as mock_changes:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='repair_cycle',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=42,
            )

            mock_branch.assert_called_once_with(tmp_path)
            mock_changes.assert_called_once_with(tmp_path)
            assert result is CommitResult.NOTHING_TO_COMMIT

    @pytest.mark.asyncio
    async def test_accepts_project_dir_as_string(self, service, tmp_path):
        """Callers may pass project_dir as a plain str (e.g. pipeline_run.project_dir,
        which is Optional[str]) -- must be coerced to a Path, not require the caller
        to do it."""
        with patch.object(service, '_get_current_branch', return_value='feature/issue-7-epic') as mock_branch, \
             patch.object(service, '_check_for_changes', return_value=False):

            result = await service.commit_agent_changes(
                project='test-project',
                agent='repair_cycle',
                task_id='task-1',
                project_dir=str(tmp_path),
                issue_number=42,
            )

            assert mock_branch.call_args[0][0] == tmp_path
            assert result is CommitResult.NOTHING_TO_COMMIT

    @pytest.mark.asyncio
    async def test_missing_project_dir_returns_false_not_raise(self, service):
        """A falsy project_dir (e.g. None, from an old context.json predating
        this field) must return False, not raise a bare Path(None) TypeError."""
        result = await service.commit_agent_changes(
            project='test-project',
            agent='repair_cycle',
            task_id='task-1',
            project_dir=None,
            issue_number=42,
        )

        assert result is CommitResult.FAILED

    @pytest.mark.asyncio
    async def test_nonexistent_project_dir_returns_false_not_raise(self, service, tmp_path):
        """A project_dir that doesn't exist on disk (e.g. a stale/mistaken path
        threaded through by a caller) must return False, not raise -- repair-cycle
        callers run this inside a bare threading.Thread with no exception handling
        of their own."""
        missing_dir = tmp_path / "does-not-exist"

        result = await service.commit_agent_changes(
            project='test-project',
            agent='repair_cycle',
            task_id='task-1',
            project_dir=missing_dir,
            issue_number=42,
        )

        assert result is CommitResult.FAILED


class TestWorkflowBugBranchCheckFailsFastBeforeTheLock:
    """
    #56 review: the main/master workflow-bug check must run BEFORE the
    project_checkout lock is ever acquired -- its outcome can't change based
    on lock state, so checking it first means a project stuck on main/master
    fails instantly instead of first polling for up to
    project_checkout_lock's own (multi-thousand-second) timeout if the lock
    happened to be contended.
    """

    @pytest.mark.asyncio
    async def test_main_branch_returns_false_without_ever_acquiring_the_lock(self, service, tmp_path):
        with patch.object(service, '_get_current_branch', return_value='main') as mock_branch, \
             patch('services.project_workspace.workspace_manager.is_base_clone_dir', return_value=True) as mock_is_base, \
             patch('services.project_checkout_lock.project_checkout_lock_async') as mock_lock:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='some_agent',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=42,
            )

            assert result is CommitResult.FAILED
            mock_branch.assert_called_once_with(tmp_path)
            # The fast-fail must happen before the lock decision is even
            # consulted -- is_base_clone_dir()/the lock context manager
            # itself must never be reached for this outcome.
            mock_is_base.assert_not_called()
            mock_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_master_branch_also_fails_fast(self, service, tmp_path):
        with patch.object(service, '_get_current_branch', return_value='master'), \
             patch('services.project_checkout_lock.project_checkout_lock_async') as mock_lock:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='some_agent',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=42,
            )

            assert result is CommitResult.FAILED
            mock_lock.assert_not_called()

    @pytest.mark.asyncio
    async def test_feature_branch_proceeds_past_the_check_and_does_not_recompute_it(self, service, tmp_path):
        """current_branch is resolved exactly once (by the caller, before the
        lock) and threaded into _commit_and_push -- not re-read a second time
        there."""
        with patch.object(service, '_get_current_branch', return_value='feature/issue-9') as mock_branch, \
             patch.object(service, '_check_for_changes', return_value=False):

            result = await service.commit_agent_changes(
                project='test-project',
                agent='some_agent',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=42,
            )

            assert result is CommitResult.NOTHING_TO_COMMIT
            mock_branch.assert_called_once_with(tmp_path)


class TestCurrentBranchReReadAfterTheLock:
    """
    CRITICAL regression (found in final whole-PR review, #60): when the
    project_checkout lock IS taken, current_branch must be re-read AFTER
    acquiring it, not reused from the pre-lock fast-fail read. This lock
    exists specifically because a DIFFERENT operation (another board of the
    same project) can check out a DIFFERENT branch in this same shared
    directory while we wait for it -- using the stale pre-lock branch name
    would push the on-disk tree (whatever the lock's previous holder left
    checked out) to the WRONG branch ref.
    """

    @staticmethod
    def _async_noop_lock_cm(*args, **kwargs):
        @asynccontextmanager
        async def _cm():
            yield
        return _cm()

    @pytest.mark.asyncio
    async def test_branch_is_re_read_after_acquiring_the_lock_and_the_fresh_value_is_used(self, service, tmp_path):
        with patch('services.project_workspace.workspace_manager.is_base_clone_dir', return_value=True), \
             patch(
                 'services.project_checkout_lock.project_checkout_lock_async',
                 side_effect=self._async_noop_lock_cm,
             ), \
             patch.object(service, '_get_current_branch', side_effect=['feature/stale', 'feature/fresh']) as mock_branch, \
             patch.object(service, '_check_for_changes', return_value=False), \
             patch.object(service, '_push_branch', return_value=True) as mock_push:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='some_agent',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=42,
            )

            assert result is CommitResult.NOTHING_TO_COMMIT
            # Read twice: once for the pre-lock fast-fail check, once again
            # after acquiring the lock.
            assert mock_branch.call_count == 2
            # The push must use the SECOND (post-lock, fresh) value, never
            # the first (stale, pre-lock) one.
            mock_push.assert_called_once_with(tmp_path, 'feature/fresh')

    @pytest.mark.asyncio
    async def test_branch_racing_onto_main_during_the_lock_wait_refuses_to_commit(self, service, tmp_path):
        """
        CRITICAL regression, found in the SAME final review pass that added
        this class: the fresh post-lock re-read closes the stale-branch-push
        bug, but re-reading alone isn't enough -- _commit_and_push() itself
        has no main/master guard (it only gates the PUSH, not staging/
        committing). Without re-validating the fresh value here too, a
        branch that raced onto main/master during the lock wait would still
        get a real commit staged onto the shared clone's main branch, only
        silently skipping the push.
        """
        with patch('services.project_workspace.workspace_manager.is_base_clone_dir', return_value=True), \
             patch(
                 'services.project_checkout_lock.project_checkout_lock_async',
                 side_effect=self._async_noop_lock_cm,
             ), \
             patch.object(service, '_get_current_branch', side_effect=['feature/stale', 'main']) as mock_branch, \
             patch.object(service, '_check_for_changes') as mock_check, \
             patch.object(service, '_stage_changes') as mock_stage, \
             patch.object(service, '_commit') as mock_commit:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='some_agent',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=42,
            )

            assert result is CommitResult.FAILED
            assert mock_branch.call_count == 2
            # Must refuse before ever touching git -- no staging, no commit.
            mock_check.assert_not_called()
            mock_stage.assert_not_called()
            mock_commit.assert_not_called()

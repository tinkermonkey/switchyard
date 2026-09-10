"""
Tests for FeatureBranchManager.finalize_feature_branch_work()'s project_checkout
lock coverage (issue #151/WI-6 item 16, from #140 item 16).

The method's `project_dir_override or os.path.join(self.workspace_root, project)`
fallback resolves to the SHARED base clone, and everything from the branch
verification down to the push stages, commits and pushes from whatever directory
it lands on -- ungated, unlike every other base-clone writer #54/#56 wired up.
Latent rather than live (the workspace-context callers always pass an override,
which resolves to an epic worktree), but the standalone callers the fallback
exists for are exactly the ones that reach the shared clone.

The hold deliberately spans the branch verification as well as the git work:
verifying before acquiring would re-create the staleness auto_commit.py had to
fix by re-reading its branch AFTER the lock. It ends at the push -- the PR and
completion-detection tail touches no git.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from services.feature_branch_manager import FeatureBranchManager


@pytest.fixture
def manager():
    return FeatureBranchManager(workspace_root='/workspace')


class _RecordingLock:
    """Stand-in for project_checkout_lock_async() that records its call and the
    order of enter/exit relative to the operations it is meant to bracket."""

    def __init__(self, events):
        self.events = events
        self.calls = []

    def __call__(self, project, issue_number=None, **kwargs):
        self.calls.append({'project': project, 'issue_number': issue_number, **kwargs})
        events = self.events

        @asynccontextmanager
        async def _ctx():
            events.append('lock_acquired')
            try:
                yield
            finally:
                events.append('lock_released')

        return _ctx()


def _tracked_feature_branch(branch_name='feature/issue-5-epic', parent_issue=5):
    fb = MagicMock()
    fb.branch_name = branch_name
    fb.parent_issue = parent_issue
    return fb


@pytest.mark.asyncio
class TestBaseCloneFallbackIsLocked:

    async def test_lock_brackets_the_branch_check_and_every_git_write(self, manager):
        """THE regression: with no project_dir_override the whole
        verify/stage/commit/push sequence used to run against the shared base
        clone with no lock at all."""
        events = []
        recording_lock = _RecordingLock(events)

        async def _verify(**kwargs):
            events.append('verify_branch')
            return None

        async def _add_all(*a, **k):
            events.append('git_add_all')

        async def _commit(*a, **k):
            events.append('git_commit')
            return True

        async def _push(*a, **k):
            events.append('git_push')

        with patch('services.project_workspace.workspace_manager.is_base_clone_dir',
                   return_value=True), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   recording_lock), \
             patch.object(manager, '_verify_finalize_branch', side_effect=_verify), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'git_add_all', side_effect=_add_all), \
             patch.object(manager, 'git_commit', side_effect=_commit), \
             patch.object(manager, 'git_push', side_effect=_push), \
             patch('services.git_workflow_manager.git_workflow_manager') as mock_gwm:
            mock_gwm.get_current_branch = AsyncMock(return_value='feature/issue-88')

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=88,
                commit_message='Complete work for issue #88',
                github_integration=Mock(),
            )

        assert result['success'] is True
        assert events[0] == 'lock_acquired'
        assert events[-1] == 'lock_released'
        for guarded in ('verify_branch', 'git_add_all', 'git_commit', 'git_push'):
            assert guarded in events, f"{guarded} did not run"
            assert 0 < events.index(guarded) < len(events) - 1

    async def test_lock_is_scoped_to_the_project_and_attributed_to_the_issue(self, manager):
        recording_lock = _RecordingLock([])

        with patch('services.project_workspace.workspace_manager.is_base_clone_dir',
                   return_value=True), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   recording_lock), \
             patch.object(manager, '_verify_finalize_branch', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock), \
             patch.object(manager, 'git_commit', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'git_push', new_callable=AsyncMock), \
             patch('services.git_workflow_manager.git_workflow_manager') as mock_gwm:
            mock_gwm.get_current_branch = AsyncMock(return_value='feature/issue-88')

            await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=88,
                commit_message='msg',
                github_integration=Mock(),
            )

        assert len(recording_lock.calls) == 1
        assert recording_lock.calls[0]['project'] == 'test-project'
        # Log attribution only -- the lock mints its own holder identity.
        assert recording_lock.calls[0]['issue_number'] == 88

    async def test_a_branch_mismatch_refusal_still_releases_the_lock(self, manager):
        """The verification sits INSIDE the lock, so its early return is the one
        exit path most likely to leak a hold."""
        events = []
        recording_lock = _RecordingLock(events)

        with patch('services.project_workspace.workspace_manager.is_base_clone_dir',
                   return_value=True), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   recording_lock), \
             patch.object(manager, '_verify_finalize_branch', new_callable=AsyncMock,
                          return_value={'success': False, 'branch_mismatch': True}), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add:
            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=88,
                commit_message='msg',
                github_integration=Mock(),
            )

        assert result['branch_mismatch'] is True
        mock_add.assert_not_called()
        assert events == ['lock_acquired', 'lock_released']

    async def test_a_push_failure_still_releases_the_lock(self, manager):
        """git_push raises PushFailedError straight out of the method -- the lock
        must not survive it."""
        from services.git_workflow_manager import PushFailedError

        events = []
        recording_lock = _RecordingLock(events)

        with patch('services.project_workspace.workspace_manager.is_base_clone_dir',
                   return_value=True), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   recording_lock), \
             patch.object(manager, '_verify_finalize_branch', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=_tracked_feature_branch()), \
             patch.object(manager, 'get_current_branch', new_callable=AsyncMock,
                          return_value='feature/issue-5-epic'), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock), \
             patch.object(manager, 'git_commit', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'branch_exists', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'git_push', new_callable=AsyncMock,
                          side_effect=PushFailedError('remote rejected')):
            with pytest.raises(PushFailedError):
                await manager.finalize_feature_branch_work(
                    project='test-project',
                    issue_number=6,
                    commit_message='msg',
                    github_integration=Mock(),
                )

        assert events == ['lock_acquired', 'lock_released']


@pytest.mark.asyncio
class TestEpicWorktreeOverrideIsNotLocked:

    async def test_no_lock_for_a_resolved_epic_worktree(self, manager):
        """An isolated epic worktree shares its directory with nothing; locking it
        would serialize sibling epics for no reason (is_base_clone_dir()'s whole
        point)."""
        recording_lock = _RecordingLock([])

        with patch('services.project_workspace.workspace_manager.is_base_clone_dir',
                   return_value=False), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   recording_lock), \
             patch.object(manager, '_verify_finalize_branch', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'get_feature_branch_for_issue', new_callable=AsyncMock,
                          return_value=None), \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
             patch.object(manager, 'git_commit', new_callable=AsyncMock, return_value=True), \
             patch.object(manager, 'git_push', new_callable=AsyncMock), \
             patch('services.git_workflow_manager.git_workflow_manager') as mock_gwm:
            mock_gwm.get_current_branch = AsyncMock(return_value='feature/issue-88')

            result = await manager.finalize_feature_branch_work(
                project='test-project',
                issue_number=88,
                commit_message='msg',
                github_integration=Mock(),
                project_dir_override='/workspace/.orchestrator/worktrees/test-project/5',
            )

        assert result['success'] is True
        mock_add.assert_awaited_once()
        assert recording_lock.calls == []

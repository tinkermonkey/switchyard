"""
Unit tests for AutoCommitService's branch-target verification (issue #149,
covering #143 and #140 items 23/33).

Before this, commit_agent_changes() re-read the checked-out branch under the
project_checkout lock and refused only the two values that are always wrong
(main/master). The branch it structurally could not catch was another issue's
feature branch: in the shared base clone, board B can take the lock in the gap
between board A's agent container releasing it (claude_integration.py holds it
only for the container's lifetime) and A's commit_agent_changes() re-acquiring
it, check out B's own branch, and leave A committing + pushing A's work onto
B's branch.

The fix verifies the checked-out branch against an expectation resolved
OUTSIDE the shared directory's ambient git state -- expected_branch, which
callers read off the same pipeline_run.branch_name / resolve_workspace() result
they already read project_dir from.
"""

import logging
import subprocess
import pytest
from contextlib import asynccontextmanager
from unittest.mock import patch

from services.auto_commit import AutoCommitService, CommitResult


@pytest.fixture
def service():
    return AutoCommitService()


def _async_noop_lock_cm(*args, **kwargs):
    """Stand-in for project_checkout_lock_async() that acquires nothing."""
    @asynccontextmanager
    async def _cm():
        yield
    return _cm()


def _shared_base_clone():
    """Patch context making project_dir look like the shared base clone (locked path)."""
    return patch('services.project_workspace.workspace_manager.is_base_clone_dir', return_value=True)


def _epic_worktree():
    """Patch context making project_dir look like an isolated epic worktree (unlocked path)."""
    return patch('services.project_workspace.workspace_manager.is_base_clone_dir', return_value=False)


class TestExpectedBranchVerification:
    """
    #143 / #149 finding A: the branch actually checked out at commit time must
    match this commit's own target, not merely be something other than
    main/master.
    """

    @pytest.mark.asyncio
    async def test_commits_when_the_checked_out_branch_matches_expected_branch(self, service, tmp_path):
        """The ordinary case: nothing raced us, so the verification is invisible."""
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-7-epic', 'feature/issue-7-epic']), \
             patch.object(service, '_check_for_changes', return_value=True), \
             patch.object(service, '_stage_changes', return_value=True) as mock_stage, \
             patch.object(service, '_commit', return_value=True) as mock_commit, \
             patch.object(service, '_push_branch', return_value=True) as mock_push:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
            )

            assert result is CommitResult.COMMITTED
            mock_stage.assert_called_once_with(tmp_path)
            assert mock_commit.call_count == 1
            mock_push.assert_called_once_with(tmp_path, 'feature/issue-7-epic')

    @pytest.mark.asyncio
    async def test_refuses_when_another_board_checked_out_its_own_branch_during_the_wait(self, service, tmp_path):
        """
        THE #143 regression. Board A waits on the project_checkout lock, board B
        checks out B's own feature branch in the same shared base clone and
        releases, A acquires and re-reads a branch that is NOT main/master --
        and must refuse rather than push A's work onto B's branch.
        """
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-7-epic', 'feature/issue-90-other-board']) as mock_branch, \
             patch.object(service, '_check_for_changes') as mock_check, \
             patch.object(service, '_stage_changes') as mock_stage, \
             patch.object(service, '_commit') as mock_commit, \
             patch.object(service, '_push_branch') as mock_push:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
            )

            assert result is CommitResult.FAILED
            assert mock_branch.call_count == 2
            # Must refuse before touching git at all: the agent's work stays on
            # disk, uncommitted, for the caller's own failure path.
            mock_check.assert_not_called()
            mock_stage.assert_not_called()
            mock_commit.assert_not_called()
            mock_push.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_when_the_pre_lock_read_was_already_poisoned(self, service, tmp_path):
        """
        The case a pre-lock-vs-post-lock equality check could never catch, and
        the reason expected_branch exists at all: the agent container and the
        commit take the project_checkout lock in two SEPARATE acquisitions, so
        another board can win the gap between them and be checked out before
        commit_agent_changes() takes its very first branch reading. Both reads
        then agree -- on the wrong branch.
        """
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-90-other-board',
                                       'feature/issue-90-other-board']), \
             patch.object(service, '_check_for_changes') as mock_check, \
             patch.object(service, '_stage_changes') as mock_stage, \
             patch.object(service, '_commit') as mock_commit:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
            )

            assert result is CommitResult.FAILED
            mock_check.assert_not_called()
            mock_stage.assert_not_called()
            mock_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_expected_branch_falls_back_to_the_pre_lock_snapshot(self, service, tmp_path):
        """
        A caller that supplies no expectation is degraded, not exempted: the
        branch seen before the lock stands in as the target, which still refuses
        a branch that changed during the wait.
        """
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-7-epic', 'feature/issue-90-other-board']), \
             patch.object(service, '_check_for_changes') as mock_check, \
             patch.object(service, '_commit') as mock_commit:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
            )

            assert result is CommitResult.FAILED
            mock_check.assert_not_called()
            mock_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_expected_branch_and_a_stable_branch_still_commits(self, service, tmp_path):
        """The fallback must not turn every expectation-less caller into a refusal."""
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-7-epic', 'feature/issue-7-epic']), \
             patch.object(service, '_check_for_changes', return_value=False), \
             patch.object(service, '_push_branch', return_value=True) as mock_push:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
            )

            assert result is CommitResult.NOTHING_TO_COMMIT
            mock_push.assert_called_once_with(tmp_path, 'feature/issue-7-epic')

    @pytest.mark.asyncio
    async def test_epic_worktree_mismatch_warns_but_still_commits(self, service, tmp_path, caplog):
        """
        A mismatch is fatal only where the corruption is possible. An epic
        worktree is shared with no other board, so its checked-out branch IS
        this epic's own branch -- a disagreement means the expectation went
        stale (get_or_create_epic_worktree() ignores branch_name on reuse), and
        refusing there would strand a real fix over bookkeeping.
        """
        with _epic_worktree(), \
             patch.object(service, '_get_current_branch', return_value='feature/issue-7-epic'), \
             patch.object(service, '_check_for_changes', return_value=False), \
             patch.object(service, '_push_branch', return_value=True) as mock_push, \
             caplog.at_level(logging.WARNING, logger='services.auto_commit'):

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-stale-name',
            )

            assert result is CommitResult.NOTHING_TO_COMMIT
            mock_push.assert_called_once_with(tmp_path, 'feature/issue-7-epic')
            assert any('branch mismatch' in r.message for r in caplog.records)

    @pytest.mark.asyncio
    async def test_epic_worktree_does_not_re_read_the_branch(self, service, tmp_path):
        """
        Nothing else can move an epic worktree's HEAD and nothing was waited on,
        so the pre-lock read stays authoritative -- re-reading would only be a
        second pointless subprocess.
        """
        with _epic_worktree(), \
             patch.object(service, '_get_current_branch',
                          return_value='feature/issue-7-epic') as mock_branch, \
             patch.object(service, '_check_for_changes', return_value=False):

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
            )

            assert result is CommitResult.NOTHING_TO_COMMIT
            mock_branch.assert_called_once_with(tmp_path)


class TestUnknownBranchIsRefused:
    """
    #149 finding B (#140 item 33): _get_current_branch() returning None used to
    sail straight through every `branch in ['main', 'master']` guard, because
    None is not in that list.
    """

    @pytest.mark.asyncio
    async def test_unreadable_branch_fails_fast_before_the_lock(self, service, tmp_path):
        with patch.object(service, '_get_current_branch', return_value=None), \
             patch('services.project_workspace.workspace_manager.is_base_clone_dir') as mock_is_base, \
             patch.object(service, '_check_for_changes') as mock_check:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
            )

            assert result is CommitResult.FAILED
            # Same fast-fail contract the main/master check has: refuse before
            # the lock decision is even consulted.
            mock_is_base.assert_not_called()
            mock_check.assert_not_called()

    @pytest.mark.asyncio
    async def test_unreadable_branch_after_the_lock_is_refused_too(self, service, tmp_path):
        """The post-lock read is a fresh subprocess, so the pre-lock refusal
        above does not cover it -- e.g. the other lock holder left the shared
        clone mid-rebase."""
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-7-epic', None]), \
             patch.object(service, '_check_for_changes') as mock_check, \
             patch.object(service, '_stage_changes') as mock_stage, \
             patch.object(service, '_commit') as mock_commit:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
            )

            assert result is CommitResult.FAILED
            mock_check.assert_not_called()
            mock_stage.assert_not_called()
            mock_commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_commit_and_push_refuses_none_as_its_structural_backstop(self, service, tmp_path):
        """_commit_and_push()'s own guard, which holds even if a future call
        site skips the checks above."""
        with patch.object(service, '_check_for_changes') as mock_check, \
             patch.object(service, '_stage_changes') as mock_stage, \
             patch.object(service, '_commit') as mock_commit:

            result = await service._commit_and_push(
                'test-project', 'senior_software_engineer', 'task-1',
                tmp_path, None, 7, None,
            )

            assert result is CommitResult.FAILED
            mock_check.assert_not_called()
            mock_stage.assert_not_called()
            mock_commit.assert_not_called()

    def test_get_current_branch_logs_when_git_fails(self, service, tmp_path, caplog):
        """A non-zero `git rev-parse` used to return None with nothing said."""
        completed = subprocess.CompletedProcess(
            args=['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            returncode=128,
            stdout='',
            stderr='fatal: not a git repository\n',
        )
        with patch('services.auto_commit.subprocess.run', return_value=completed), \
             caplog.at_level(logging.ERROR, logger='services.auto_commit'):

            assert service._get_current_branch(tmp_path) is None
            assert any('not a git repository' in r.message for r in caplog.records)

    def test_get_current_branch_logs_when_output_is_empty(self, service, tmp_path, caplog):
        """Same silent-None gap, reached with a zero exit code and no output."""
        completed = subprocess.CompletedProcess(
            args=['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
            returncode=0,
            stdout='  \n',
            stderr='',
        )
        with patch('services.auto_commit.subprocess.run', return_value=completed), \
             caplog.at_level(logging.ERROR, logger='services.auto_commit'):

            assert service._get_current_branch(tmp_path) is None
            assert any('printed nothing' in r.message for r in caplog.records)


class TestSingleCommitAndPushCallSite:
    """
    #149 finding C (#140 item 23): the locked and unlocked paths used to be two
    separate self._commit_and_push(...) calls with identical argument lists,
    so an argument change had to be applied to both. contextlib.nullcontext()
    collapsed them onto one.
    """

    @pytest.mark.asyncio
    async def test_unlocked_path_calls_commit_and_push_exactly_once(self, service, tmp_path):
        with _epic_worktree(), \
             patch.object(service, '_get_current_branch', return_value='feature/issue-7-epic'), \
             patch.object(service, '_commit_and_push',
                          return_value=CommitResult.COMMITTED) as mock_cp:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
                custom_message='msg',
            )

            assert result is CommitResult.COMMITTED
            mock_cp.assert_called_once_with(
                'test-project', 'senior_software_engineer', 'task-1', tmp_path,
                'feature/issue-7-epic', 7, 'msg',
            )

    @pytest.mark.asyncio
    async def test_locked_path_calls_the_same_single_site_with_the_fresh_branch(self, service, tmp_path):
        with _shared_base_clone(), \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   side_effect=_async_noop_lock_cm), \
             patch.object(service, '_get_current_branch',
                          side_effect=['feature/issue-7-epic', 'feature/issue-7-epic']), \
             patch.object(service, '_commit_and_push',
                          return_value=CommitResult.COMMITTED) as mock_cp:

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='task-1',
                project_dir=tmp_path,
                issue_number=7,
                expected_branch='feature/issue-7-epic',
                custom_message='msg',
            )

            assert result is CommitResult.COMMITTED
            mock_cp.assert_called_once_with(
                'test-project', 'senior_software_engineer', 'task-1', tmp_path,
                'feature/issue-7-epic', 7, 'msg',
            )

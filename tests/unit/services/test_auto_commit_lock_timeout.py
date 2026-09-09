"""
Regression tests for #148: commit_agent_changes() must propagate a
project_checkout lock timeout instead of collapsing it into `return False`.

False is the same value this method returns for "nothing to commit", so a
contention-blocked commit was indistinguishable from an agent that produced no
changes at all — on the one path where contention can cost real work rather
than just wall clock. review_cycle.py logs a False return as "No changes to
commit for iteration N" and carries on with the maker's work still uncommitted,
and agent_container_recovery.py escalates `overall_success and not
commit_success` to mark_failed("Repair cycle passed but its fix was not
committed"), retaining the board's lock.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from services.auto_commit import AutoCommitService, CommitResult
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError


@asynccontextmanager
async def _lock_that_times_out(project, issue_number=None, **kwargs):
    raise ProjectCheckoutLockTimeoutError(
        f"Could not acquire 'project_checkout' lock for project {project!r} within 10900.0s: busy"
    )
    yield  # pragma: no cover - unreachable, keeps this a generator


@asynccontextmanager
async def _lock_that_succeeds(project, issue_number=None, **kwargs):
    yield


async def _commit_with(lock_cm, commit_and_push_result=CommitResult.COMMITTED):
    service = AutoCommitService()

    with patch.object(Path, 'exists', return_value=True), \
         patch.object(service, '_get_current_branch', return_value='feature/issue-1'), \
         patch.object(service, '_commit_and_push',
                      new=AsyncMock(return_value=commit_and_push_result)), \
         patch('services.project_workspace.workspace_manager') as workspace_manager, \
         patch('services.project_checkout_lock.project_checkout_lock_async', lock_cm):

        workspace_manager.is_base_clone_dir.return_value = True

        return await service.commit_agent_changes(
            project='test-project',
            agent='senior_software_engineer',
            task_id='review_cycle_iter_2',
            project_dir='/workspace/test-project',
            issue_number=1,
        )


class TestLockTimeoutPropagates:
    @pytest.mark.asyncio
    async def test_lock_timeout_is_raised_not_swallowed(self):
        with pytest.raises(ProjectCheckoutLockTimeoutError):
            await _commit_with(_lock_that_times_out)

    @pytest.mark.asyncio
    async def test_ordinary_error_still_returns_false(self):
        """Control: every other failure mode is CommitResult.FAILED — falsy, so
        every pre-existing truthiness-based caller behaves exactly as before, and
        now nameable, so the callers that must distinguish it from an empty diff
        can (#148 I1)."""
        service = AutoCommitService()

        with patch.object(Path, 'exists', return_value=True), \
             patch.object(service, '_get_current_branch', return_value='feature/issue-1'), \
             patch.object(service, '_commit_and_push',
                          new=AsyncMock(side_effect=RuntimeError("git exploded"))), \
             patch('services.project_workspace.workspace_manager') as workspace_manager:

            workspace_manager.is_base_clone_dir.return_value = False

            result = await service.commit_agent_changes(
                project='test-project',
                agent='senior_software_engineer',
                task_id='review_cycle_iter_2',
                project_dir='/workspace/test-project',
                issue_number=1,
            )

        assert result is CommitResult.FAILED

    @pytest.mark.asyncio
    async def test_successful_locked_commit_still_returns_a_non_failure(self):
        assert await _commit_with(_lock_that_succeeds) is CommitResult.COMMITTED

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
from unittest.mock import patch

from services.auto_commit import AutoCommitService


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
            assert result is True  # no changes -> nothing to commit, but not a failure

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
            assert result is True

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

        assert result is False

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

        assert result is False

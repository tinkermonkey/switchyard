"""
Unit tests for AutoCommitService.commit_agent_changes()'s epic_id/branch_name
threading (issue #48 fast-follow).

Covers the gap found while implementing #48: repair-cycle callers (project_monitor.py,
agent_container_recovery.py) mount an epic-scoped worktree unconditionally for 'issues'
workspace type (exempted from #46's EPIC_WORKTREE_SAFE_WORKSPACE_TYPES gate), but
commit_agent_changes() resolved the shared base clone unconditionally -- meaning a
successful repair cycle's actual changes, written into the epic worktree, were never
found/committed. These tests confirm get_project_dir() is called with whatever
epic_id/branch_name the caller passes (or omits), AND that the resolved directory is
actually used downstream (not just passed as an unused argument) -- a real tmp_path is
used as the "resolved" directory so it genuinely exists() and the commit flow proceeds
past the existence check, unlike a bare Mock/fake path.
"""

import pytest
from unittest.mock import patch

from services.auto_commit import AutoCommitService


@pytest.fixture
def service():
    return AutoCommitService()


class TestCommitAgentChangesEpicWorktree:
    """commit_agent_changes() must resolve the SAME directory the caller's agent
    actually wrote to -- the base clone by default, or an epic worktree when the
    caller (a repair-cycle path) passes epic_id/branch_name."""

    async def _run(self, service, tmp_path, **kwargs):
        with patch('services.auto_commit.workspace_manager') as mock_wsm, \
             patch.object(service, '_get_current_branch', return_value='feature/issue-42'), \
             patch.object(service, '_check_for_changes', return_value=False):
            mock_wsm.get_project_dir.return_value = tmp_path

            await service.commit_agent_changes(
                project='test-project',
                agent=kwargs.pop('agent', 'senior_software_engineer'),
                task_id='task-1',
                issue_number=42,
                **kwargs
            )

            return mock_wsm.get_project_dir

    @pytest.mark.asyncio
    async def test_default_omits_epic_id_resolves_base_clone(self, service, tmp_path):
        """Every non-repair-cycle caller (e.g. review_cycle.py's maker-commit calls)
        omits epic_id/branch_name -- must resolve exactly like before #48's fix."""
        mock_get_project_dir = await self._run(service, tmp_path)

        mock_get_project_dir.assert_called_once_with(
            'test-project', epic_id=None, branch_name=None
        )

    @pytest.mark.asyncio
    async def test_repair_cycle_caller_threads_epic_id_and_branch(self, service, tmp_path):
        """A repair-cycle caller passing epic_id/branch_name must have them threaded
        straight through to get_project_dir(), so this resolves the SAME epic
        worktree the repair-cycle container actually wrote its fix into."""
        mock_get_project_dir = await self._run(
            service, tmp_path, agent='repair_cycle', epic_id='7', branch_name='feature/issue-7-epic'
        )

        mock_get_project_dir.assert_called_once_with(
            'test-project', epic_id='7', branch_name='feature/issue-7-epic'
        )

    @pytest.mark.asyncio
    async def test_resolved_directory_is_actually_used_downstream(self, service, tmp_path):
        """The path get_project_dir() returns must genuinely flow into the
        branch-check/change-detection calls, not just be passed as an unused arg --
        a real tmp_path (which exists()) lets the commit flow proceed past the
        existence check into _get_current_branch/_check_for_changes."""
        with patch('services.auto_commit.workspace_manager') as mock_wsm, \
             patch.object(service, '_get_current_branch', return_value='feature/issue-7-epic') as mock_branch, \
             patch.object(service, '_check_for_changes', return_value=False) as mock_changes:
            mock_wsm.get_project_dir.return_value = tmp_path

            result = await service.commit_agent_changes(
                project='test-project', agent='repair_cycle', task_id='task-1',
                issue_number=42, epic_id='7', branch_name='feature/issue-7-epic'
            )

            mock_branch.assert_called_once_with(tmp_path)
            mock_changes.assert_called_once_with(tmp_path)
            assert result is True  # no changes -> nothing to commit, but not a failure

    @pytest.mark.asyncio
    async def test_resolution_failure_returns_false_not_raise(self, service):
        """get_project_dir() (epic_id path -> get_or_create_epic_worktree()) can
        raise ValueError/RuntimeError on a genuine failure -- must not propagate an
        unhandled exception, since repair-cycle callers run this inside a bare
        threading.Thread with no exception handling of their own."""
        with patch('services.auto_commit.workspace_manager') as mock_wsm:
            mock_wsm.get_project_dir.side_effect = RuntimeError("worktree add failed")

            result = await service.commit_agent_changes(
                project='test-project', agent='repair_cycle', task_id='task-1',
                issue_number=42, epic_id='7', branch_name='feature/issue-7-epic'
            )

            assert result is False

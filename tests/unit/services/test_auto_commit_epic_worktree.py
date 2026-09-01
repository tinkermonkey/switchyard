"""
Unit tests for AutoCommitService.commit_agent_changes()'s epic_id/branch_name
threading (issue #48 fast-follow).

Covers the gap found while implementing #48: repair-cycle callers (project_monitor.py,
agent_container_recovery.py) mount an epic-scoped worktree unconditionally for 'issues'
workspace type (exempted from #46's EPIC_WORKTREE_SAFE_WORKSPACE_TYPES gate), but
commit_agent_changes() resolved the shared base clone unconditionally -- meaning a
successful repair cycle's actual changes, written into the epic worktree, were never
found/committed. These tests confirm get_project_dir() is called with whatever
epic_id/branch_name the caller passes (or omits).
"""

import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

from services.auto_commit import AutoCommitService


@pytest.fixture
def service():
    return AutoCommitService()


class TestCommitAgentChangesEpicWorktree:
    """commit_agent_changes() must resolve the SAME directory the caller's agent
    actually wrote to -- the base clone by default, or an epic worktree when the
    caller (a repair-cycle path) passes epic_id/branch_name."""

    async def _run(self, service, **kwargs):
        with patch('services.auto_commit.workspace_manager') as mock_wsm, \
             patch.object(service, '_get_current_branch', return_value='feature/issue-42'), \
             patch.object(service, '_check_for_changes', return_value=False):
            mock_wsm.get_project_dir.return_value = Path('/fake/project-dir')

            await service.commit_agent_changes(
                project='test-project',
                agent=kwargs.pop('agent', 'senior_software_engineer'),
                task_id='task-1',
                issue_number=42,
                **kwargs
            )

            return mock_wsm.get_project_dir

    @pytest.mark.asyncio
    async def test_default_omits_epic_id_resolves_base_clone(self, service):
        """Every non-repair-cycle caller (e.g. review_cycle.py's maker-commit calls)
        omits epic_id/branch_name -- must resolve exactly like before #48's fix."""
        mock_get_project_dir = await self._run(service)

        mock_get_project_dir.assert_called_once_with(
            'test-project', epic_id=None, branch_name=None
        )

    @pytest.mark.asyncio
    async def test_repair_cycle_caller_threads_epic_id_and_branch(self, service):
        """A repair-cycle caller passing epic_id/branch_name must have them threaded
        straight through to get_project_dir(), so this resolves the SAME epic
        worktree the repair-cycle container actually wrote its fix into."""
        mock_get_project_dir = await self._run(
            service, agent='repair_cycle', epic_id='7', branch_name='feature/issue-7-epic'
        )

        mock_get_project_dir.assert_called_once_with(
            'test-project', epic_id='7', branch_name='feature/issue-7-epic'
        )

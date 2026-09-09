"""
Unit tests for the review cycle's half of #149/#143: the branch it hands
commit_agent_changes() as expected_branch.

The point of _resolve_workspace_for_cycle() is that project_dir and branch_name
come out of the SAME resolve_workspace() result. An expectation re-derived
somewhere else would reintroduce the divergence #123 removed, and an
"expectation" read back off the checkout is what #143 found to be no
verification at all.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch

from services.review_cycle import ReviewCycleExecutor, ReviewCycleState


def make_state(**overrides):
    defaults = {
        'issue_number': 101,
        'repository': 'test-org/test-repo',
        'maker_agent': 'maker',
        'reviewer_agent': 'reviewer',
        'max_iterations': 3,
        'project_name': 'test-project',
        'board_name': 'dev',
        'workspace_type': 'issues',
        'pipeline_run_id': 'run-123',
    }
    defaults.update(overrides)
    return ReviewCycleState(**defaults)


@pytest.fixture
def executor():
    return ReviewCycleExecutor()


def _patched_pipeline_run(project_dir, branch_name):
    """Patch context yielding a resolved PipelineRun with these two fields."""
    run = Mock()
    run.id = 'run-123'
    run.project_dir = project_dir
    run.branch_name = branch_name

    manager = Mock()
    manager.get_pipeline_run = Mock(return_value=run)
    manager.resolve_workspace = AsyncMock(return_value=run)
    return patch('services.pipeline_run.get_pipeline_run_manager', return_value=manager)


class TestResolveWorkspaceForCycle:

    @pytest.mark.asyncio
    async def test_returns_the_branch_alongside_the_directory(self, executor, tmp_path):
        state = make_state()
        with _patched_pipeline_run(str(tmp_path), 'feature/issue-42-epic'), \
             patch.object(executor, '_get_github_integration', return_value=Mock()):

            project_dir, branch_name = await executor._resolve_workspace_for_cycle(state)

            assert project_dir == tmp_path
            assert branch_name == 'feature/issue-42-epic'

    @pytest.mark.asyncio
    async def test_project_dir_only_helper_still_returns_a_bare_path(self, executor, tmp_path):
        """_resolve_project_dir_for_cycle() is now a wrapper; its many
        directory-only call sites must be unaffected by the tuple."""
        state = make_state()
        with _patched_pipeline_run(str(tmp_path), 'feature/issue-42-epic'), \
             patch.object(executor, '_get_github_integration', return_value=Mock()):

            assert await executor._resolve_project_dir_for_cycle(state) == tmp_path

    @pytest.mark.asyncio
    async def test_non_git_workspace_type_has_no_resolved_branch(self, executor):
        """'discussions' is git-free -- resolve_workspace() is a no-op for it,
        so there is no branch to expect."""
        state = make_state(workspace_type='discussions')
        with patch('services.project_workspace.workspace_manager.get_project_dir',
                   return_value='/workspace/test-project'):

            project_dir, branch_name = await executor._resolve_workspace_for_cycle(state)

            assert str(project_dir) == '/workspace/test-project'
            assert branch_name is None

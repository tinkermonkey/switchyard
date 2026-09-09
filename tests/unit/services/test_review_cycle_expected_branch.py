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
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from services.review_cycle import ReviewCycleExecutor, ReviewCycleState
from services.review_parser import ReviewStatus


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


class _StopAfterCommit(Exception):
    """Sentinel raised from the auto-commit stub once its arguments have been
    captured. _continue_cycle_from_review()'s own outer `except Exception`
    swallows it, so the test needs nothing downstream of the call site."""


class TestTheResolvedBranchReachesTheCommitCallSite:
    """
    Resolving the branch is only half the fix -- each call site has to actually
    hand it over as expected_branch, and dropping that kwarg fails SILENTLY:
    commit_agent_changes() falls back to the pre-lock snapshot (which its own
    docstring says cannot catch #143) and logs, so nothing turns red.

    This drives the real call site in _continue_cycle_from_review() rather than
    asserting on _resolve_workspace_for_cycle()'s return value alone, so the
    binding between the two -- not just the resolution -- is what is covered.
    """

    async def _captured_commit_kwargs(self, executor, tmp_path, *, makes_code_changes=True):
        captured = {}

        async def _commit(**kwargs):
            captured.update(kwargs)
            raise _StopAfterCommit()

        state = make_state()
        state.current_iteration = 1
        state.review_outputs = [{'iteration': 1, 'output': 'changes requested'}]

        review_result = Mock()
        review_result.status = ReviewStatus.CHANGES_REQUESTED
        review_result.high_severity_count = 0
        review_result.blocking_count = 0

        column = MagicMock()
        column.agent = 'reviewer'
        column.name = 'Code Review'
        workflow_template = MagicMock()
        workflow_template.columns = [column]

        agent_config = Mock()
        agent_config.makes_code_changes = makes_code_changes

        config_manager = MagicMock()
        config_manager.get_project_workflow.return_value = workflow_template
        config_manager.get_project_agent_config.return_value = agent_config

        github = MagicMock()
        github.get_issue_details = AsyncMock(return_value={'title': 't', 'body': 'b'})

        auto_commit_service = MagicMock()
        auto_commit_service.commit_agent_changes = _commit

        with patch('config.manager.config_manager', config_manager), \
             patch('services.auto_commit.auto_commit_service', auto_commit_service), \
             _patched_pipeline_run(str(tmp_path), 'feature/issue-42-epic'), \
             patch.object(executor, '_get_github_integration', return_value=github), \
             patch.object(executor.review_parser, 'parse_review', return_value=review_result), \
             patch.object(executor, '_ensure_context_writer', return_value=None), \
             patch.object(executor, '_create_maker_revision_task_context', return_value={}), \
             patch.object(executor, '_get_git_commit_hash', return_value='abc1234'), \
             patch.object(executor, '_execute_agent_directly', new=AsyncMock()), \
             patch.object(executor, '_save_cycle_state', new=Mock()):

            await executor._continue_cycle_from_review(state, 'test-org')

        return captured

    @pytest.mark.asyncio
    async def test_expected_branch_is_the_branch_resolve_workspace_picked(self, executor, tmp_path):
        captured = await self._captured_commit_kwargs(executor, tmp_path)

        assert captured['expected_branch'] == 'feature/issue-42-epic'

    @pytest.mark.asyncio
    async def test_it_comes_from_the_same_resolution_as_project_dir(self, executor, tmp_path):
        """The whole point of the (dir, branch) tuple: an expectation resolved
        somewhere other than where project_dir came from is the divergence #123
        removed."""
        captured = await self._captured_commit_kwargs(executor, tmp_path)

        assert captured['project_dir'] == tmp_path

"""
Unit tests for the epic-worktree branch quarantine (issue #149 WI-4 review).

A wrong-branch refusal only leaves the drift on disk; it does not repair it, and
nothing else did either. The NEXT dispatch for the same epic is a fresh
PipelineRun, so resolve_workspace()'s idempotency guard does not apply,
get_or_create_epic_worktree() takes its cache-hit path and returns the same
directory without touching git, and _current_worktree_branch() then reads the
drifted branch and PERSISTS it as that run's expectation. The next
finalization compares the drifted branch against itself, passes, and commits
both issues' work onto it, with a PR opened against it -- #143's outcome,
deferred by exactly one dispatch, on the same precondition the guard exists for.

The quarantine marker is what makes the refusal outlive its own dispatch.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, Mock, patch

from services.pipeline_run import PipelineRun, PipelineRunManager
from services.project_workspace import ProjectWorkspaceManager


@pytest.fixture
def manager(tmp_path):
    return ProjectWorkspaceManager(workspace_root=tmp_path)


class TestQuarantineMarkerLifecycle:

    def test_marker_is_written_and_read_back(self, manager):
        assert manager.get_epic_worktree_quarantine('test-project', '5') is None

        assert manager.quarantine_epic_worktree(
            project_name='test-project',
            epic_id='5',
            expected_branch='feature/issue-5-epic',
            actual_branch='scratch',
            reason='container moved HEAD',
        ) is True

        record = manager.get_epic_worktree_quarantine('test-project', '5')
        assert record['expected_branch'] == 'feature/issue-5-epic'
        assert record['actual_branch'] == 'scratch'
        assert record['reason'] == 'container moved HEAD'

    def test_marker_is_a_sibling_of_the_worktree_not_a_file_inside_it(self, manager):
        """Inside the worktree it would be swept up by the very `git add -A`
        the quarantine exists to stop, and committed onto the drifted branch."""
        manager.quarantine_epic_worktree('test-project', '5', 'feature/issue-5-epic',
                                         'scratch', 'reason')

        worktree = manager._epic_worktree_path('test-project', '5')
        marker = manager._epic_worktree_quarantine_path('test-project', '5')

        assert marker.parent == worktree.parent
        assert worktree not in marker.parents
        assert marker.is_file()

    def test_prune_skips_the_marker(self, manager):
        """prune_epic_worktrees() only iterates directories, so the marker has
        to survive the startup sweep it exists to outlive."""
        manager.quarantine_epic_worktree('test-project', '5', 'feature/issue-5-epic',
                                         'scratch', 'reason')
        marker = manager._epic_worktree_quarantine_path('test-project', '5')

        manager.prune_epic_worktrees()

        assert marker.is_file()

    def test_an_unreadable_marker_still_counts_as_quarantined(self, manager):
        """Fails closed: its mere presence is the whole signal, and returning
        None for it would resume exactly the adoption it blocks."""
        marker = manager._epic_worktree_quarantine_path('test-project', '5')
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text('{ not json')

        assert manager.get_epic_worktree_quarantine('test-project', '5') is not None

    def test_clearing_the_marker_re_enables_dispatch(self, manager):
        manager.quarantine_epic_worktree('test-project', '5', 'feature/issue-5-epic',
                                         'scratch', 'reason')

        assert manager.clear_epic_worktree_quarantine('test-project', '5') is True
        assert manager.get_epic_worktree_quarantine('test-project', '5') is None
        # Idempotent -- nothing left to clear.
        assert manager.clear_epic_worktree_quarantine('test-project', '5') is False


class TestResolveWorkspaceRefusesAQuarantinedWorktree:
    """
    The refusal has to bite BEFORE the _current_worktree_branch() re-derivation,
    which is the one place a fresh run would otherwise normalize the drift into
    its own expectation.
    """

    @pytest.fixture
    def pipeline_run_manager(self):
        with patch('services.pipeline_run.Elasticsearch'), \
             patch('services.pipeline_run.redis.Redis'):
            manager = PipelineRunManager()
            manager.es = MagicMock()
            manager.redis = MagicMock()
            return manager

    @pytest.fixture
    def pipeline_run(self):
        return PipelineRun(
            id="run-2",
            issue_number=8,
            issue_title="Next sub-issue of the same epic",
            issue_url="https://github.com/org/repo/issues/8",
            project="test-project",
            board="Dev Board",
            started_at="2026-09-04T00:00:00Z",
        )

    @pytest.mark.asyncio
    async def test_a_quarantined_epic_refuses_instead_of_adopting_the_drifted_branch(
        self, pipeline_run_manager, pipeline_run
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=5)), \
             patch.object(workspace_manager, 'get_epic_worktree_quarantine',
                          return_value={'expected_branch': 'feature/issue-5-epic',
                                        'actual_branch': 'scratch',
                                        'reason': 'container moved HEAD'}), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name') as mock_resolve_branch, \
             patch.object(workspace_manager, 'get_or_create_epic_worktree') as mock_worktree, \
             patch.object(workspace_manager, '_current_worktree_branch') as mock_current:

            with pytest.raises(RuntimeError, match='quarantined'):
                await pipeline_run_manager.resolve_workspace(
                    pipeline_run, Mock(), workspace_type='issues'
                )

        # Refused ahead of every step that could normalize or persist the drift.
        mock_resolve_branch.assert_not_called()
        mock_worktree.assert_not_called()
        mock_current.assert_not_called()
        assert pipeline_run.branch_name is None
        assert pipeline_run.project_dir is None

    @pytest.mark.asyncio
    async def test_an_unquarantined_epic_resolves_as_before(
        self, pipeline_run_manager, pipeline_run
    ):
        from services.feature_branch_manager import feature_branch_manager
        from services.project_workspace import workspace_manager

        with patch.object(feature_branch_manager, 'get_parent_issue', new=AsyncMock(return_value=5)), \
             patch.object(workspace_manager, 'get_epic_worktree_quarantine', return_value=None), \
             patch.object(feature_branch_manager, 'resolve_epic_branch_name',
                          return_value='feature/issue-5-epic'), \
             patch.object(workspace_manager, 'get_or_create_epic_worktree',
                          return_value='/workspace/.orchestrator/worktrees/test-project/5'), \
             patch.object(workspace_manager, '_current_worktree_branch',
                          return_value='feature/issue-5-epic'):

            result = await pipeline_run_manager.resolve_workspace(
                pipeline_run, Mock(), workspace_type='issues'
            )

        assert result.branch_name == 'feature/issue-5-epic'
        assert result.epic_id == '5'

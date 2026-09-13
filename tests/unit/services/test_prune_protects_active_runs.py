"""
Regression tests for the second failure in pipeline run 4cf816cf: startup
deleting the workspace it was about to re-trigger an agent into.

The sequence, from one startup's own logs:

    16:00:41  === Orchestrator starting up ===
    16:00:47  Pruning stale epic worktrees
    16:00:48  Epic worktree prune complete          <- removed worktrees/codetoreum/1016
    16:00:51  Re-triggering agent for recovered lock holder issue #1045
    16:00:55  Review cycle failed ... no git changes found

Issue #1045's pipeline run had project_dir=/workspace/.orchestrator/worktrees/
codetoreum/1016 — the directory the prune had removed four seconds earlier. The
run had already been reviewed and approved two hours before; this second,
derived failure is what actually ended it, and it ended it with an error that
named neither the directory nor the prune.

prune_epic_worktrees()'s four existing skip rules could not see this case:
after a restart nothing is tracked in-process (rule 1), a mid-pipeline run's
agent container finished long ago (rule 2), and the worktree is a perfectly
healthy, clean checkout (rules 3 and 4). The pipeline run record is the only
thing that still knows the directory matters, which is what the fifth rule
reads.
"""

import os
import pytest
from pathlib import Path
from unittest.mock import Mock, patch

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.project_workspace import ProjectWorkspaceManager


def _ok(stdout: str = "") -> Mock:
    result = Mock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


@pytest.fixture
def manager(tmp_path):
    return ProjectWorkspaceManager(workspace_root=tmp_path)


def _make_base_clone(workspace_root: Path, project_name: str) -> Path:
    project_dir = workspace_root / project_name
    (project_dir / '.git').mkdir(parents=True)
    return project_dir


def _make_worktree(workspace_root: Path, project: str, epic_id: str) -> Path:
    path = workspace_root / '.orchestrator' / 'worktrees' / project / epic_id
    (path / '.git').mkdir(parents=True)
    return path


class TestActiveRunWorktreesSurvivePrune:
    """The fifth skip rule, driven through _prune_project_staging() directly —
    the same entry point #169 split out so the per-project unit is testable
    without the lock machinery around it."""

    def test_the_incidents_worktree_is_not_removed(self, manager, tmp_path):
        """codetoreum/1016, held by an active run, must survive the sweep."""
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "1016")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()) as mock_run:
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces={'codetoreum': {'1016'}},
            )

        assert worktree.is_dir(), "an active run's workspace must not be pruned"
        mock_push.assert_not_called()
        assert not any(
            'remove' in ' '.join(str(a) for a in call.args[0])
            for call in mock_run.call_args_list
        ), "no `git worktree remove` may be issued for a protected worktree"

    def test_matching_by_recorded_project_dir_also_protects(self, manager, tmp_path):
        """Either identifier is enough: the epic id is robust to path
        formatting, the path is robust to a layout change."""
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "1016")

        with patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces={'*paths*': {str(worktree)}},
            )

        assert worktree.is_dir()

    def test_a_trailing_slash_on_the_recorded_path_still_matches(self, manager, tmp_path):
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "1016")

        with patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces={'*paths*': {str(worktree) + '/'}},
            )

        assert worktree.is_dir()

    def test_another_projects_active_epic_id_does_not_protect_this_one(
        self, manager, tmp_path
    ):
        """Epic ids are small integers and collide across projects constantly.
        Scoping by project is what stops one project's run pinning another's
        stale worktrees on disk forever."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")

        with patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()) as mock_run:
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces={'heimdall': {'1016'}},
            )

        assert mock_run.called, "an unprotected worktree is still an ordinary candidate"

    def test_an_idle_worktree_is_still_pruned(self, manager, tmp_path):
        """Control. The rule must protect active runs WITHOUT turning the sweep
        into a no-op — leaving every worktree on disk indefinitely is its own
        failure mode."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "999")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces={'codetoreum': {'1016'}},
            )

        mock_push.assert_called_once(), "an unprotected worktree still gets its commits saved"

    def test_omitting_the_argument_keeps_the_pre_existing_behaviour(
        self, manager, tmp_path
    ):
        """The parameter is additive — every pre-existing two-argument call
        prunes exactly as it did before."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "999")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
            )

        mock_push.assert_called_once()


class TestTheSweepStillRunsWhenTheLookupFails:
    def test_an_unavailable_pipeline_run_manager_does_not_stop_the_sweep(
        self, manager, tmp_path
    ):
        """prune_epic_worktrees() runs unguarded at every startup. A failure to
        read active runs degrades to the four pre-existing rules — it must not
        be able to fail the boot."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "999")

        with patch('services.pipeline_run.get_pipeline_run_manager',
                   side_effect=RuntimeError("redis down")), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager.prune_epic_worktrees()  # must not raise

    def test_the_sweep_passes_the_lookup_result_through_to_each_project(
        self, manager, tmp_path
    ):
        """Computed once for the whole sweep, like running_mount_sources, so one
        Redis/ES read covers every worktree under consideration."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")
        expected = {'codetoreum': {'1016'}}

        seen = {}

        def _capture(project_staging, running_mount_sources, active_run_workspaces=None):
            seen['value'] = active_run_workspaces

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = expected

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_prune_project_staging', _capture):
            manager.prune_epic_worktrees()

        assert seen['value'] == expected
        run_manager.get_active_run_workspaces.assert_called_once()


class TestGetActiveRunWorkspaces:
    """PipelineRunManager's side of the rule."""

    def _manager(self, redis_client):
        """Redis-only, with Elasticsearch explicitly disabled.

        PipelineRunManager's constructor builds a REAL Elasticsearch client
        when elasticsearch_client is None, so these tests would otherwise read
        the running deployment's own active runs — both non-hermetic and a read
        against live state the unit suite has no business touching (see #223).
        """
        from services.pipeline_run import PipelineRunManager
        with patch('services.pipeline_run.Elasticsearch', side_effect=RuntimeError('no ES')):
            manager = PipelineRunManager(redis_client=redis_client)
        assert manager.es is None
        return manager

    def test_an_active_run_contributes_its_epic_id_and_path(self):
        import json
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = json.dumps({
            'id': 'run-abc',
            'issue_number': 1045,
            'issue_title': 'Phase 1',
            'issue_url': 'https://example.invalid/1045',
            'project': 'codetoreum',
            'board': 'SDLC Execution',
            'started_at': '2026-09-13T13:50:12Z',
            'status': 'active',
            'epic_id': '1016',
            'project_dir': '/workspace/.orchestrator/worktrees/codetoreum/1016',
        })

        result = self._manager(redis_client).get_active_run_workspaces()

        assert result['codetoreum'] == {'1016'}
        assert '/workspace/.orchestrator/worktrees/codetoreum/1016' in result['*paths*']

    def test_a_finished_run_contributes_nothing(self):
        import json
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = json.dumps({
            'id': 'run-abc',
            'issue_number': 1045,
            'issue_title': 'Phase 1',
            'issue_url': 'https://example.invalid/1045',
            'project': 'codetoreum',
            'board': 'SDLC Execution',
            'started_at': '2026-09-13T13:50:12Z',
            'ended_at': '2026-09-13T16:00:56Z',
            'status': 'completed',
            'epic_id': '1016',
            'project_dir': '/workspace/.orchestrator/worktrees/codetoreum/1016',
        })

        assert self._manager(redis_client).get_active_run_workspaces() == {}

    def test_a_feedback_listening_run_is_protected_too(self):
        """It holds its board's lock and is waiting on a human, which is the
        longest a workspace ever has to survive."""
        import json
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = json.dumps({
            'id': 'run-abc',
            'issue_number': 1045,
            'issue_title': 'Phase 1',
            'issue_url': 'https://example.invalid/1045',
            'project': 'codetoreum',
            'board': 'SDLC Execution',
            'started_at': '2026-09-13T13:50:12Z',
            'status': 'feedback_listening',
            'epic_id': '1016',
        })

        assert self._manager(redis_client).get_active_run_workspaces()['codetoreum'] == {'1016'}

    def test_a_redis_failure_returns_empty_rather_than_raising(self):
        """It runs inside a best-effort startup sweep."""
        redis_client = Mock()
        redis_client.hgetall.side_effect = ConnectionError("redis down")

        assert self._manager(redis_client).get_active_run_workspaces() == {}

    def test_unparseable_run_data_does_not_stop_the_scan(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {'a:1': 'run-a'}
        redis_client.get.return_value = 'not json'

        assert self._manager(redis_client).get_active_run_workspaces() == {}

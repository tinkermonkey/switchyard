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
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.pipeline_run import ActiveRunWorkspaces
from services.project_workspace import ProjectWorkspaceManager


def _ok(stdout: str = "") -> Mock:
    result = Mock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


@pytest.fixture(autouse=True)
def _uncontended_checkout_lock():
    """Keep this module's sweeps off the live deployment's lock store.

    prune_epic_worktrees() takes each project's project_checkout lock before
    touching that project (#169). The lock is backed by Redis, and
    ORCHESTRATOR_ROOT redirects only the YAML store -- so an unstubbed acquire
    here polls the *running* orchestrator's lock and blocks for the whole
    timeout whenever it happens to be held, which made two of these cases a
    120s pass/fail coin flip on production state (#230). It also meant a unit
    test sat in the queue for a lock real dispatch waits on.

    Locking is #169's subject and is covered against a stubbed store in
    test_epic_worktree_checkout_lock.py; here it is a seam, held by no one.
    Autouse rather than per-test so a case added later cannot reintroduce the
    leak by forgetting it.
    """
    @contextmanager
    def _acquire(project, issue_number=None, **kwargs):
        yield None

    with patch(
        'services.project_checkout_lock.project_checkout_lock_sync', _acquire
    ):
        yield


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
                active_run_workspaces=ActiveRunWorkspaces({'codetoreum': {'1016'}}, set()),
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
                active_run_workspaces=ActiveRunWorkspaces({}, {str(worktree)}),
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
                active_run_workspaces=ActiveRunWorkspaces({}, {str(worktree) + '/'}),
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
                active_run_workspaces=ActiveRunWorkspaces({'heimdall': {'1016'}}, set()),
            )

        assert any(
            'remove' in ' '.join(str(a) for a in c.args[0])
            for c in mock_run.call_args_list
        ), "an unprotected worktree is still an ordinary removal candidate"

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
                active_run_workspaces=ActiveRunWorkspaces({'codetoreum': {'1016'}}, set()),
            )

        # NB: a bare `mock.assert_called_once(), "msg"` is a discarded tuple --
        # the call still raises if unmet, but the message is inert.
        mock_push.assert_called_once()

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


class TestAnIncompleteAnswerAbortsTheSweep:
    """The unknown-vs-empty rule, applied to a destructive operation.

    An earlier revision degraded to "prune anyway, without the protection" when
    the active-run lookup failed. That is the same defect this change set
    removes from _get_sub_issues_from_parent(), pointed at `rm -rf`: an
    unreadable Redis during a restart — the very moment this sweep runs — would
    read as "no run owns any of these worktrees" and delete a live workspace,
    reproducing the incident with the protection silently absent.
    """

    def test_an_incomplete_lookup_prunes_nothing(self, manager, tmp_path):
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "999")

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = ActiveRunWorkspaces.unknown()

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()) as mock_run:
            manager.prune_epic_worktrees()

        assert worktree.is_dir(), (
            "an unanswerable lookup must not be read as 'nothing is active'"
        )
        mock_push.assert_not_called()
        assert not mock_run.called

    def test_a_raising_lookup_also_prunes_nothing(self, manager, tmp_path):
        """prune_epic_worktrees() runs unguarded at every startup, so it must
        not raise — but "did not raise" must not mean "deleted everything"."""
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "999")

        with patch('services.pipeline_run.get_pipeline_run_manager',
                   side_effect=RuntimeError("redis down")), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager.prune_epic_worktrees()  # must not raise

        assert worktree.is_dir()

    def test_a_complete_answer_still_prunes(self, manager, tmp_path):
        """Control: the abort must not turn the sweep into a permanent no-op."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "999")

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = ActiveRunWorkspaces(
            {}, set(), complete=True
        )

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager.prune_epic_worktrees()

        mock_push.assert_called_once()

    def test_the_sweep_passes_the_lookup_result_through_to_each_project(
        self, manager, tmp_path
    ):
        """Computed once for the whole sweep, like running_mount_sources, so one
        Redis/ES read covers every worktree under consideration."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")
        expected = ActiveRunWorkspaces({'codetoreum': {'1016'}}, set(), complete=True)

        seen = {}

        def _capture(project_staging, running_mount_sources, active_run_workspaces=None):
            seen['value'] = active_run_workspaces

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = expected

        with patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_prune_project_staging', _capture):
            manager.prune_epic_worktrees()

        assert seen['value'] is expected
        run_manager.get_active_run_workspaces.assert_called_once()


class TestActiveRunWorkspacesValue:
    """The value type itself — the magic-key shape it replaced could not
    express either of the two things the prune decision depends on."""

    def test_protects_matches_on_epic_id(self):
        w = ActiveRunWorkspaces({'codetoreum': {'1016'}}, set())
        assert w.protects('codetoreum', Path('/w/worktrees/codetoreum/1016')) is True

    def test_protects_matches_on_recorded_path(self):
        w = ActiveRunWorkspaces({}, {'/w/worktrees/codetoreum/1016'})
        assert w.protects('codetoreum', Path('/w/worktrees/codetoreum/1016')) is True

    def test_paths_are_normalised_once_at_construction(self):
        """The consumer used to re-do this, with a comment explaining that the
        duplication was deliberate. A value object normalises once."""
        w = ActiveRunWorkspaces({}, {'/w/worktrees/codetoreum/1016/'})
        assert w.protects('codetoreum', Path('/w/worktrees/codetoreum/1016')) is True

    def test_another_projects_epic_id_does_not_match(self):
        w = ActiveRunWorkspaces({'heimdall': {'1016'}}, set())
        assert w.protects('codetoreum', Path('/w/worktrees/codetoreum/1016')) is False

    def test_a_project_named_like_the_old_magic_key_is_just_a_project(self):
        """The shape this replaced reserved '*paths*', so a project of that
        name overwrote its own protection and matched nothing."""
        w = ActiveRunWorkspaces({'*paths*': {'1016'}}, set())
        assert w.protects('*paths*', Path('/w/worktrees/*paths*/1016')) is True

    def test_unknown_is_not_empty(self):
        assert ActiveRunWorkspaces.unknown().complete is False
        assert ActiveRunWorkspaces({}, set()).complete is True


class TestGetActiveRunWorkspaces:
    """PipelineRunManager's side of the rule."""

    def _manager(self, redis_client, es_client=None):
        """Redis-only by default, with Elasticsearch explicitly disabled.

        PipelineRunManager's constructor builds a REAL Elasticsearch client
        when elasticsearch_client is None, so these tests would otherwise read
        the running deployment's own active runs — both non-hermetic and a read
        against live state the unit suite has no business touching (see #223).
        """
        from services.pipeline_run import PipelineRunManager
        with patch('services.pipeline_run.Elasticsearch', side_effect=RuntimeError('no ES')):
            manager = PipelineRunManager(redis_client=redis_client)
        assert manager.es is None
        manager.es = es_client
        return manager

    def _run_blob(self, **overrides):
        import json
        blob = {
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
        }
        blob.update(overrides)
        return json.dumps(blob)

    def _es(self, hits):
        es = Mock()
        es.search.return_value = {'hits': {'hits': [{'_source': h} for h in hits]}}
        return es

    def test_an_active_run_contributes_its_epic_id_and_path(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = self._run_blob()

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.epic_ids_by_project['codetoreum'] == {'1016'}
        assert '/workspace/.orchestrator/worktrees/codetoreum/1016' in result.paths
        assert result.complete is True

    def test_a_finished_run_contributes_nothing(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = self._run_blob(
            status='completed', ended_at='2026-09-13T16:00:56Z'
        )

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.epic_ids_by_project == {}
        assert result.complete is True

    def test_a_feedback_listening_run_is_protected_too(self):
        """It holds its board's lock and is waiting on a human, which is the
        longest a workspace ever has to survive."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = self._run_blob(status='feedback_listening')

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.epic_ids_by_project['codetoreum'] == {'1016'}

    def test_a_redis_failure_is_reported_as_incomplete_not_as_empty(self):
        """THE REGRESSION GUARD. Returning a bare {} here made an unreadable
        Redis indistinguishable from a healthy system with no active runs — and
        the caller deletes directories on the difference."""
        redis_client = Mock()
        redis_client.hgetall.side_effect = ConnectionError("redis down")

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.complete is False
        assert result.epic_ids_by_project == {}

    def test_one_unparseable_record_does_not_drop_the_others(self):
        """The try used to wrap the whole loop, so a single bad blob aborted the
        scan and every run AFTER it silently lost protection — while the result
        was returned as though it were complete."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'a:1': 'run-bad', 'b:2': 'run-good'}
        redis_client.get.side_effect = ['not json', self._run_blob()]

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.epic_ids_by_project['codetoreum'] == {'1016'}, (
            "the readable run must still be protected"
        )
        assert result.complete is False, "but the answer is known to be partial"

    def test_an_elasticsearch_failure_is_also_incomplete(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = Mock()
        es.search.side_effect = RuntimeError("es down")

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is False

    def test_no_elasticsearch_client_is_incomplete(self):
        """Runs whose Redis blob expired live only in ES — exactly the
        long-running mid-pipeline population this protects best."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {}

        result = self._manager(redis_client, None).get_active_run_workspaces()

        assert result.complete is False

    def test_an_elasticsearch_only_run_is_protected(self):
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = self._es([{
            'project': 'codetoreum',
            'epic_id': '1016',
            'project_dir': '/w/worktrees/codetoreum/1016',
            'status': 'active',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.epic_ids_by_project['codetoreum'] == {'1016'}

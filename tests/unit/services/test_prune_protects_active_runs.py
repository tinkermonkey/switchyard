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


class TestTheSurveyAsksTheSameQuestionTheSweepDoes:
    """#231: survey_epic_worktrees() backs the operator diagnostic and the
    /api/epic-worktrees endpoint, but reported a verdict derived from the drift
    rule alone -- so the fifth rule, which is the whole subject of this module,
    was invisible to the one place an operator looks before removing a
    directory by hand.
    """

    def _survey(self, manager, workspaces):
        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = workspaces
        with patch('services.pipeline_run.get_pipeline_run_reader',
                   return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            return manager.survey_epic_worktrees()

    def test_an_active_runs_worktree_is_reported_as_protected(self, manager, tmp_path):
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")

        rows = self._survey(
            manager, ActiveRunWorkspaces({'codetoreum': {'1016'}}, set())
        )

        assert [r['active_run_protected'] for r in rows] == [True]

    def test_an_unowned_worktree_is_not(self, manager, tmp_path):
        """Control: the field must not be constant."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "999")

        rows = self._survey(manager, ActiveRunWorkspaces({}, set(), complete=True))

        assert [r['active_run_protected'] for r in rows] == [False]

    def test_an_unanswerable_lookup_reports_none_not_false(self, manager, tmp_path):
        """False would read as "no run owns it" and license "prune: eligible".
        The sweep's actual behaviour on this answer is to prune nothing at all,
        so the only honest report is "could not ask"."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")

        rows = self._survey(manager, ActiveRunWorkspaces.unknown())

        assert [r['active_run_protected'] for r in rows] == [None]

    def test_a_raising_run_store_keeps_the_survey_alive(self, manager, tmp_path):
        """survey_epic_worktrees() promises it never raises -- it backs an HTTP
        handler. An unreadable run store must degrade to "unknown", not 500."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")

        with patch('services.pipeline_run.get_pipeline_run_reader',
                   side_effect=RuntimeError("redis down")), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            rows = manager.survey_epic_worktrees()

        assert [r['active_run_protected'] for r in rows] == [None]

    def test_the_run_store_is_read_once_for_the_whole_survey(self, manager, tmp_path):
        """Same reason running_mount_sources is one round trip: this backs a web
        request handler, and a per-worktree read would scale with the board."""
        _make_base_clone(tmp_path, "codetoreum")
        for epic in ("1015", "1016", "1017"):
            _make_worktree(tmp_path, "codetoreum", epic)

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = ActiveRunWorkspaces(
            {}, set(), complete=True
        )
        with patch('services.pipeline_run.get_pipeline_run_reader',
                   return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            rows = manager.survey_epic_worktrees()

        assert len(rows) == 3
        run_manager.get_active_run_workspaces.assert_called_once()

    def test_the_recorded_path_alone_is_enough_to_protect(self, manager, tmp_path):
        """ActiveRunWorkspaces matches on epic id OR recorded project_dir, and
        _record() populates them independently -- a run carrying project_dir but
        no epic id is protected by the path form alone. The sweep's side covers
        both; without this the survey's side covered only the epic-id map, and a
        mutation killing path matching passed the whole file."""
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "1016")

        rows = self._survey(
            manager, ActiveRunWorkspaces({}, {str(worktree)})
        )

        assert [r['active_run_protected'] for r in rows] == [True]
        assert [r['prune_verdict'] for r in rows] == ['skipped_active_run']

    def test_a_failing_import_still_does_not_raise(self, manager, tmp_path):
        """The contract is "Never raises", and the handler used to re-import the
        very module whose import may have failed -- which re-raises, out of a
        method backing an HTTP endpoint."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")

        import builtins
        real_import = builtins.__import__

        def _boom(name, *args, **kwargs):
            if name == 'services.pipeline_run':
                raise ImportError("circular import during startup")
            return real_import(name, *args, **kwargs)

        with patch.object(builtins, '__import__', _boom), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            rows = manager.survey_epic_worktrees()

        assert [r['active_run_protected'] for r in rows] == [None]
        assert [r['prune_verdict'] for r in rows] == ['unknown']


class TestTheVerdictMirrorsTheSweepsOwnRules:
    """One composed answer, computed beside the sweep, so no consumer has to
    re-derive it -- the arrangement that caused #231."""

    def _verdict(self, **kw):
        kw.setdefault('active_runs_known', True)
        kw.setdefault('active_run_protected', False)
        kw.setdefault('container_live', False)
        kw.setdefault('corrupted', False)
        kw.setdefault('drift_holds_work', False)
        return ProjectWorkspaceManager._prune_verdict(**kw)

    def test_an_unreadable_run_store_outranks_everything(self):
        """The sweep aborts in FULL on that answer, so no row is eligible."""
        assert self._verdict(active_runs_known=False, corrupted=True) == 'unknown'

    def test_an_active_run_outranks_the_later_rules(self):
        assert self._verdict(active_run_protected=True,
                             drift_holds_work=True) == 'skipped_active_run'

    def test_a_corrupted_worktree_is_skipped(self):
        assert self._verdict(corrupted=True) == 'skipped_corrupted'

    def test_a_live_container_is_skipped(self):
        assert self._verdict(container_live=True) == 'skipped_container'

    def test_drift_holding_work_is_skipped(self):
        assert self._verdict(drift_holds_work=True) == 'skipped_drift'

    def test_an_unanswerable_liveness_check_is_flagged_not_plain_eligible(self):
        """The sweep prunes this -- it fails open on `is True`, deliberately, or
        a docker outage would pin the staging tree on disk forever. But an
        operator has no such deadline, so it must not read as a bare
        "eligible"."""
        assert self._verdict(container_live=None) == 'eligible_liveness_unknown'

    def test_nothing_holding_it_is_eligible(self):
        """Control: the verdict must not collapse into always-skipped."""
        assert self._verdict() == 'eligible'


class TestACorruptedWorktreeIsReportedAsSuch:
    """The sweep's corruption rule skips a non-empty directory with no .git
    because it "may hold real uncommitted work". It is a pure filesystem check,
    answerable from any process -- it was simply never wired into the survey,
    leaving the shape most likely to hold unrecoverable work reported as
    removable."""

    def test_a_git_less_non_empty_worktree_is_not_eligible(self, manager, tmp_path):
        _make_base_clone(tmp_path, "codetoreum")
        path = tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum' / '1016'
        path.mkdir(parents=True)
        (path / 'uncommitted_work.py').write_text("# real work, no .git\n")

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = ActiveRunWorkspaces(
            {}, set(), complete=True
        )
        with patch('services.pipeline_run.get_pipeline_run_reader',
                   return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            rows = manager.survey_epic_worktrees()

        assert [r['corrupted'] for r in rows] == [True]
        assert [r['prune_verdict'] for r in rows] == ['skipped_corrupted']


class TestAProjectWithAnUnaccountableRunIsNotPruned:
    """#233: the whole-answer abort does not cover a lookup that SUCCEEDED but
    could not account for one run. That run's record is gone, so its worktree
    cannot be protected by name -- the only honest move is to leave the project
    alone, and only that project."""

    def _sweep(self, manager, tmp_path, workspaces):
        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = workspaces
        with patch('services.pipeline_run.get_pipeline_run_manager',
                   return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager.prune_epic_worktrees()

    def test_its_worktrees_survive(self, manager, tmp_path):
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "1016")

        self._sweep(manager, tmp_path, ActiveRunWorkspaces(
            {}, set(), complete=True, unresolved_projects={'codetoreum'}
        ))

        assert worktree.is_dir(), (
            "a project whose run mapping references a run neither store knows "
            "must not have its worktrees deleted"
        )

    def test_an_unaffected_project_is_still_pruned(self, manager, tmp_path):
        """The reason this is per-project: production had 9 dangling pointers,
        so a global abort would have made the sweep a permanent no-op."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_base_clone(tmp_path, "heimdall")
        _make_worktree(tmp_path, "codetoreum", "1016")
        heimdall_worktree = _make_worktree(tmp_path, "heimdall", "237")

        self._sweep(manager, tmp_path, ActiveRunWorkspaces(
            {}, set(), complete=True, unresolved_projects={'codetoreum'}
        ))

        assert not heimdall_worktree.is_dir(), (
            "one project's dangling pointer must not stop every other "
            "project's worktrees being collected"
        )


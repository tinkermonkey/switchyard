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
from services.run_ownership import RunOwnership


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
            # `remove` as an ARGV ELEMENT, not as a substring of the joined
            # command: tmp_path carries the test's own name, so a test whose
            # name contains "remove" made this assertion fire on `git worktree
            # prune` in the checkout path.
            'remove' in [str(a) for a in call.args[0]]
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
            'remove' in [str(a) for a in c.args[0]]
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

    def test_the_active_run_answer_is_a_required_argument(self, manager, tmp_path):
        """DELIBERATELY REPLACES test_omitting_the_argument_keeps_the_pre_existing
        _behaviour (#229), which pinned the opposite (#240).

        That test was written when the parameter was genuinely additive, and it
        asserted that a two-argument call still pruned. What it actually pinned,
        once the parameter became the input to a rule, was a doubt-carrying
        argument defaulting to ActiveRunWorkspaces.unknown() — i.e. the default
        for a caller who had not thought about active runs was "delete". The
        same value meant "abort the sweep" one method up and "prune everything"
        here.

        There is no pre-existing behaviour left to keep: the one production
        caller has always passed the argument. A forgotten argument must be a
        TypeError, not a deletion.
        """
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "999")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            with pytest.raises(TypeError):
                manager._prune_project_staging(
                    tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                    running_mount_sources=set(),
                )

        assert worktree.is_dir()
        mock_push.assert_not_called()

    def test_an_unknown_answer_deletes_nothing_with_no_outer_gate_at_all(
        self, manager, tmp_path
    ):
        """THE #240 test. Driven through _prune_project_staging() DIRECTLY, so
        prune_epic_worktrees()'s `if not complete: return` never runs.

        Before ownership_of(), an unknown answer reaching this method — by a
        caller forgetting the gate, by the gate being removed, or by the old
        permissive default — matched nothing and therefore deleted everything.
        The rule is now inside the value: UNOWNED is the only removable answer,
        so this method is safe on its own.
        """
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "999")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()) as mock_run:
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces=ActiveRunWorkspaces.unknown(),
            )

        assert worktree.is_dir(), (
            "an unanswerable lookup must not be read as 'nothing is active', "
            "even with no outer gate in front of this method"
        )
        mock_push.assert_not_called()
        assert not any(
            # `remove` as an ARGV ELEMENT, not as a substring of the joined
            # command: tmp_path carries the test's own name, so a test whose
            # name contains "remove" made this assertion fire on `git worktree
            # prune` in the checkout path.
            'remove' in [str(a) for a in call.args[0]]
            for call in mock_run.call_args_list
        )

    def test_a_future_unknown_member_also_removes_nothing(self, manager, tmp_path):
        """`is not UNOWNED`, not `if owned`. #240 sketched splitting the unknown
        into UNKNOWN_ALL/UNKNOWN_PROJECT; whatever member is added next must
        fail closed here WITHOUT this call site being revisited, which is the
        whole point of a closed set of answers. Stands in for that member with
        an answer this sweep has never seen.
        """
        _make_base_clone(tmp_path, "codetoreum")
        worktree = _make_worktree(tmp_path, "codetoreum", "999")

        class _FutureAnswer:
            value = 'unknown_project'

        workspaces = Mock()
        workspaces.ownership_of.return_value = _FutureAnswer()

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'codetoreum',
                running_mount_sources=set(),
                active_run_workspaces=workspaces,
            )

        assert worktree.is_dir()
        mock_push.assert_not_called()


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

    def test_the_abort_happens_before_any_project_checkout_lock_is_taken(
        self, manager, tmp_path
    ):
        """What the outer gate is FOR, now that it is not what makes the sweep
        safe (#240): _prune_project_staging() protects every worktree on an
        unknown answer all by itself, so the early return exists to turn a
        whole-sweep condition into one operator-facing log line instead of a
        full pass that takes every project's project_checkout lock to decide
        nothing."""
        for project in ("codetoreum", "heimdall"):
            _make_base_clone(tmp_path, project)
            _make_worktree(tmp_path, project, "999")

        acquired = []

        @contextmanager
        def _counting_lock(project, issue_number=None, **kwargs):
            acquired.append(project)
            yield None

        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = ActiveRunWorkspaces.unknown()

        with patch('services.project_checkout_lock.project_checkout_lock_sync',
                   _counting_lock), \
             patch('services.pipeline_run.get_pipeline_run_manager', return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources', return_value=set()), \
             patch.object(manager, '_push_local_commits_if_any'), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            manager.prune_epic_worktrees()

        assert acquired == []

    def test_the_sweep_passes_the_lookup_result_through_to_each_project(
        self, manager, tmp_path
    ):
        """Computed once for the whole sweep, like running_mount_sources, so one
        Redis/ES read covers every worktree under consideration."""
        _make_base_clone(tmp_path, "codetoreum")
        _make_worktree(tmp_path, "codetoreum", "1016")
        expected = ActiveRunWorkspaces({'codetoreum': {'1016'}}, set(), complete=True)

        seen = {}

        def _capture(project_staging, running_mount_sources, active_run_workspaces):
            # No default: the real method has none either (#240), and a stub
            # that keeps one would let the sweep stop passing the argument
            # without this test noticing.
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

    def test_ownership_matches_on_epic_id(self):
        w = ActiveRunWorkspaces({'codetoreum': {'1016'}}, set())
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_ownership_matches_on_recorded_path(self):
        w = ActiveRunWorkspaces({}, {'/w/worktrees/codetoreum/1016'})
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_paths_are_normalised_once_at_construction(self):
        """The consumer used to re-do this, with a comment explaining that the
        duplication was deliberate. A value object normalises once."""
        w = ActiveRunWorkspaces({}, {'/w/worktrees/codetoreum/1016/'})
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_another_projects_epic_id_is_unowned(self):
        w = ActiveRunWorkspaces({'heimdall': {'1016'}}, set())
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNOWNED

    def test_a_project_named_like_the_old_magic_key_is_just_a_project(self):
        """The shape this replaced reserved '*paths*', so a project of that
        name overwrote its own protection and matched nothing."""
        w = ActiveRunWorkspaces({'*paths*': {'1016'}}, set())
        assert w.ownership_of(
            '*paths*', Path('/w/worktrees/*paths*/1016')
        ) is RunOwnership.OWNED

    def test_unknown_is_not_empty(self):
        assert ActiveRunWorkspaces.unknown().complete is False
        assert ActiveRunWorkspaces({}, set()).complete is True


class TestTheGateIsInsideTheAnswer:
    """#240. The predicate this replaced was public, returned a bare bool, and
    returned False when the lookup had FAILED — indistinguishable, at a call
    site that deletes directories, from "no run owns it". Every caller had to
    remember to test `complete` first and the type could not see whether they
    had; three review rounds on #237 each found another consumer that had not.
    """

    def test_an_incomplete_answer_is_unknown_not_unowned(self):
        """The exact substitution the old bool could not express: nothing
        matches, and the honest answer is still not "remove it"."""
        w = ActiveRunWorkspaces({}, set(), complete=False)
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNKNOWN

    def test_an_incomplete_answer_is_unknown_even_where_it_would_have_matched(self):
        """A partial answer is not a partial licence either. Whatever it
        happens to contain, it cannot be read per-worktree."""
        w = ActiveRunWorkspaces({'codetoreum': {'1016'}}, set(), complete=False)
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNKNOWN

    def test_unknown_is_unknown_for_every_worktree(self):
        assert ActiveRunWorkspaces.unknown().ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/999')
        ) is RunOwnership.UNKNOWN

    def test_the_ungated_match_is_not_public(self):
        """`protects()` was the whole hazard: a public, ungated bool. It may
        survive as a private helper, but nothing outside the type may reach an
        answer that has not been through the completeness gate.

        Asserted as the type's whole public callable surface rather than as
        `not hasattr(w, 'protects')` alone, so re-exposing the ungated match
        under any other name fails here too.
        """
        import inspect

        public = {
            name
            for name, _member in inspect.getmembers(ActiveRunWorkspaces, callable)
            if not name.startswith('_')
        }
        assert public == {'unknown', 'ownership_of'}, (
            f"unexpected public surface on ActiveRunWorkspaces: {sorted(public)}"
        )

    def test_only_one_answer_licenses_a_removal(self):
        """The closed set exists so the removal site can be `is not UNOWNED`.
        Keep the membership honest: an answer added later is not removable
        unless someone deliberately says so here."""
        assert {o.name for o in RunOwnership} == {'OWNED', 'UNOWNED', 'UNKNOWN'}


class TestTheValueIsActuallyImmutable:
    """`frozen=True` freezes the bindings, not the containers. `paths` was
    copied at construction; `epic_ids_by_project` and its inner sets — the half
    that GRANTS protection — were left aliased to whatever the caller passed
    (#240)."""

    def test_mutating_the_caller_s_dict_cannot_grant_protection(self):
        source = {}
        w = ActiveRunWorkspaces(source, set())
        source['codetoreum'] = {'1016'}
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNOWNED

    def test_mutating_a_caller_s_inner_set_cannot_revoke_protection(self):
        ids = {'1016'}
        w = ActiveRunWorkspaces({'codetoreum': ids}, set())
        ids.clear()
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_mutating_the_caller_s_path_set_cannot_revoke_protection(self):
        paths = {'/w/worktrees/codetoreum/1016'}
        w = ActiveRunWorkspaces({}, paths)
        paths.clear()
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_mutating_the_caller_s_doubt_set_cannot_revoke_protection(self):
        """The newest container behind a frozen binding (#233). It is the field
        that turns a whole project's worktrees from removable into kept, so an
        aliased copy is a caller who can silently disarm the protection this
        value promises."""
        doubted = {'context-studio'}
        w = ActiveRunWorkspaces(
            {}, set(), projects_with_unaccountable_runs=doubted
        )
        doubted.clear()
        assert w.ownership_of(
            'context-studio', Path('/w/worktrees/context-studio/1140')
        ) is RunOwnership.UNKNOWN

    def test_epic_ids_are_matched_as_strings(self):
        """Ids are compared against a DIRECTORY NAME. A run record whose
        epic_id came back from JSON as an int would otherwise never match the
        directory it named."""
        w = ActiveRunWorkspaces({'codetoreum': {1016}}, set())
        assert w.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED


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

    # ---------------------------------------------------------------- #233 --
    # An issue->run mapping entry whose record is gone from Redis is an
    # UNKNOWN. `continue` alone reported it to a caller that deletes
    # directories as though the scan had answered.

    def test_a_mapping_entry_whose_record_is_gone_is_doubt_not_silence(self):
        """THE #233 REGRESSION GUARD.

        Nothing in either store says whether this run is active: its Redis
        record is gone and Elasticsearch never saw it (a run written while ES
        was unavailable, whose record later expired -- the population Redis
        expiry selects for is precisely the long-running, mid-pipeline one
        whose worktree holds the most unpushed work).

        Asserted through ownership_of(), not through the field: what must not
        happen is a worktree of that project coming back UNOWNED, which is the
        one answer the sweep removes on.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {'context-studio:1140': 'run-vanished'}
        redis_client.get.return_value = None

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.ownership_of(
            'context-studio', Path('/w/worktrees/context-studio/1140')
        ) is RunOwnership.UNKNOWN

    def test_the_doubt_is_scoped_to_that_entrys_own_project(self):
        """And NOT to the whole answer, which is why this is not the one-liner
        the issue first suggested.

        Measured on the reference deployment (2026-09-15): 9 of the 11
        mapping entries were unresolvable, across two of three projects.
        `complete = False` there makes prune_epic_worktrees() abort on every
        startup -- a permanent
        no-op that still looks like protection, which is strictly worse than
        the defect it fixes. A dangling pointer in test-project is no reason to
        stop collecting heimdall's worktrees.

        The key here is the board-scoped 3-field form; the project is the first
        field in both that and the legacy form.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {'test-project:dev:2100': 'run-vanished'}
        redis_client.get.return_value = None

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.complete is True, (
            "one project's unresolvable entry must not disable every project's "
            "sweep"
        )
        assert result.ownership_of(
            'test-project', Path('/w/worktrees/test-project/2100')
        ) is RunOwnership.UNKNOWN
        assert result.ownership_of(
            'heimdall', Path('/w/worktrees/heimdall/7')
        ) is RunOwnership.UNOWNED, (
            "a project with no unresolvable entry of its own is still swept"
        )

    def test_a_legacy_two_field_key_is_attributed_the_same_way(self):
        """Both key forms are live in the mapping simultaneously -- a run ending
        cleans up both -- so reading the project from either must work."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'test-project:2100': 'run-vanished'}
        redis_client.get.return_value = None

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.ownership_of(
            'test-project', Path('/w/worktrees/test-project/2100')
        ) is RunOwnership.UNKNOWN
        assert result.ownership_of(
            'heimdall', Path('/w/worktrees/heimdall/7')
        ) is RunOwnership.UNOWNED

    def test_elasticsearch_accounting_for_the_run_clears_the_doubt(self):
        """The ES pass is what an expired Redis record was always meant to be
        answered BY -- that is why it reads "as well, not instead".

        Without this, every long-running run whose record expired would raise
        permanent doubt over its own project even though it is fully accounted
        for, and the sweep would stop collecting that project's stale worktrees
        for as long as the run lasts. The run is protected AND the rest of the
        project stays sweepable.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = None
        es = self._es([{
            'id': 'run-abc',
            'project': 'codetoreum',
            'epic_id': '1016',
            'project_dir': '/w/worktrees/codetoreum/1016',
            'status': 'active',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED
        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/999')
        ) is RunOwnership.UNOWNED, (
            "a run Elasticsearch accounts for leaves no doubt over its "
            "project's other worktrees"
        )

    def test_a_different_run_in_elasticsearch_clears_nothing(self):
        """Control for the clearing above: the ids have to be COMPARED. A pass
        that merely notices Elasticsearch answered would clear doubt about runs
        it never saw."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-vanished'}
        redis_client.get.return_value = None
        es = self._es([{
            'id': 'some-other-run',
            'project': 'codetoreum',
            'epic_id': '1016',
            'project_dir': '/w/worktrees/codetoreum/1016',
            'status': 'active',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/999')
        ) is RunOwnership.UNKNOWN

    def test_a_positive_match_still_wins_inside_a_doubted_project(self):
        """Doubt about a sibling entry does not make a run we CAN see less
        certain, and OWNED is the answer that tells an operator the true reason
        the directory is being kept."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {
            'codetoreum:1045': 'run-abc',
            'codetoreum:1046': 'run-vanished',
        }
        blobs = {
            'orchestrator:pipeline_run:run-abc': self._run_blob(),
            'orchestrator:pipeline_run:run-vanished': None,
        }
        redis_client.get.side_effect = lambda key: blobs[key]

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED
        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/999')
        ) is RunOwnership.UNKNOWN

    def test_a_mapping_key_that_names_no_project_is_whole_answer_doubt(self):
        """A doubt that cannot be attributed cannot be scoped, so it has to be
        spent on the whole answer -- the conservative direction, and a shape no
        writer in pipeline_run.py produces."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'malformed-no-colon': 'run-vanished'}
        redis_client.get.return_value = None

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.complete is False

    def test_an_active_run_with_no_project_is_still_protected_by_its_path(self):
        """_record()'s sibling early return (#233): a record without `project`
        contributed NOTHING and left the answer looking complete. Most such
        records are not unindexable at all -- path matching does not consult
        the project.

        Driven through Elasticsearch because that is where a record with the
        `project` KEY MISSING lands: PipelineRun.from_dict() has no default for
        it, so such a Redis blob raises into the per-run handler and is already
        reported as incomplete. An EMPTY project is a different matter -- it
        deserializes cleanly and reaches this branch through Redis too; see
        test_a_redis_record_with_an_empty_project_is_also_incomplete.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = self._es([{
            'id': 'run-abc',
            'epic_id': '1016',
            'project_dir': '/w/worktrees/codetoreum/1016',
            'status': 'active',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is True
        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_a_redis_record_with_an_empty_project_is_also_incomplete(self):
        """The Redis pass reaches _record()'s unindexable branch too.

        `project: str` on PipelineRun has no default, so a MISSING key raises
        -- but an empty string satisfies it, deserializes, and then names a
        worktree by epic id with nothing to key it under. Untested, the Redis
        pass's own `complete = False` was a mutation survivor: deleting it
        broke nothing.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = self._run_blob(project='', project_dir=None)

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.complete is False
        assert result.epic_ids_by_project == {}, (
            "and it is not keyed under the empty project either"
        )

    def test_an_active_run_naming_only_an_epic_id_is_reported_as_incomplete(self):
        """The residue of the same early return: an active run that names a
        worktree by epic id with nothing to key it under cannot be protected by
        EITHER matching form, so the answer is not the whole answer."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = self._es([{'id': 'run-abc', 'epic_id': '1016', 'status': 'active'}])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is False

    def test_a_run_owning_no_workspace_at_all_is_not_doubt(self):
        """Control, and the common shape: an active run that has not resolved a
        workspace yet owns no worktree, which is an ANSWER -- there is nothing
        to protect. Treating it as doubt would make every board with a run in
        its first stage suppress the sweep."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = self._es([{
            'id': 'run-abc',
            'project': 'codetoreum',
            'epic_id': None,
            'project_dir': None,
            'status': 'feedback_listening',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is True
        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNOWNED

    # ------------------------------------------------------- #233, round 4 --
    # Clearing the doubt is an ANSWER about that run, so only a hit that
    # actually answers may clear it. `_record()` returning True for both
    # "indexed a workspace" and "there was nothing to index" let the second
    # one through.

    def test_a_stale_active_es_doc_naming_no_workspace_clears_nothing(self):
        """THE #233 DEFECT CLASS, one store over.

        _persist_to_elasticsearch() swallows every exception, so the ES write
        that first carries `project_dir` can fail silently and leave the
        creation-time doc -- active, no workspace fields -- as the only ES copy
        of a run that has since resolved a worktree. That doc proves nothing
        about the worktree; treating its mere existence as an answer let the
        run's own project come back UNOWNED and the sweep delete a live run's
        worktree.

        Asserted through ownership_of(), which is what the sweep asks.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = None
        es = self._es([{
            'id': 'run-abc',
            'project': 'codetoreum',
            'status': 'active',
            'epic_id': None,
            'project_dir': None,
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNKNOWN, (
            "an ES doc that contributed no workspace key answered nothing "
            "about the run its mapping entry points at"
        )
        assert result.complete is True, (
            "and the doubt stays scoped to that project -- it does not "
            "disable every project's sweep"
        )

    def test_an_ended_es_doc_still_clears_the_doubt(self):
        """The other shape that genuinely answers, and the control that keeps
        the fix above from being 'never clear anything': a run Elasticsearch
        shows as finished owns no worktree, which is exactly what the
        unresolvable mapping entry asked."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = None
        es = self._es([{
            'id': 'run-abc',
            'project': 'codetoreum',
            'status': 'active',
            'ended_at': '2026-09-14T09:00:00Z',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.UNOWNED

    def test_an_es_doc_keyed_only_by_epic_id_clears_the_doubt(self):
        """The third answering shape: no `project_dir`, but `project` +
        `epic_id` is a key the epic-id half of the match can use, so the
        workspace IS protected and the run IS accounted for."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {'codetoreum:1045': 'run-abc'}
        redis_client.get.return_value = None
        es = self._es([{
            'id': 'run-abc',
            'project': 'codetoreum',
            'epic_id': '1016',
            'status': 'active',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED
        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/999')
        ) is RunOwnership.UNOWNED, "the run is accounted for, so no doubt remains"

    # ------------------------------------------------ a truncated ES read --

    def test_a_full_elasticsearch_page_is_not_reported_as_the_whole_answer(self):
        """One un-paginated search returns at most ES_ACTIVE_RUN_SCAN_SIZE
        docs and nothing in the response shape says the rest were dropped, so
        a full page has to spend `complete`. Otherwise every active run past
        the limit loses protection silently -- the partial-answer-as-whole-
        answer shape `complete` exists to prevent."""
        from services.pipeline_run import ES_ACTIVE_RUN_SCAN_SIZE
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = self._es([
            {'id': f'run-{i}', 'project': 'codetoreum', 'status': 'active'}
            for i in range(ES_ACTIVE_RUN_SCAN_SIZE)
        ])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is False

    def test_a_reported_total_beyond_the_page_is_not_the_whole_answer(self):
        """The cheaper signal when the cluster sends one, including the ES 7+
        dict form -- and it fires before the page is full, e.g. when `size` is
        smaller than the hit count for any other reason."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = Mock()
        es.search.return_value = {'hits': {
            'total': {'value': 4000, 'relation': 'eq'},
            'hits': [{'_source': {
                'id': 'run-abc', 'project': 'codetoreum', 'status': 'active',
                'project_dir': '/w/worktrees/codetoreum/1016',
            }}],
        }}

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is False
        assert '/w/worktrees/codetoreum/1016' in result.paths, (
            "the hits that DID arrive still protect their worktrees"
        )

    def test_a_short_page_with_a_matching_total_is_the_whole_answer(self):
        """Control: the truncation check must not mark every healthy read
        incomplete, which would disable the sweep permanently."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {}
        es = Mock()
        es.search.return_value = {'hits': {
            'total': {'value': 1, 'relation': 'eq'},
            'hits': [{'_source': {
                'id': 'run-abc', 'project': 'codetoreum', 'status': 'active',
                'project_dir': '/w/worktrees/codetoreum/1016',
            }}],
        }}

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.complete is True

    # ------------------------- a client without decode_responses (#233) --

    def test_a_bytes_run_id_still_resolves_its_record(self):
        """Both halves of a mapping field decode by the same rule.

        A redis client built without `decode_responses=True` hands back bytes
        for the value as well as the field name. `str(b'run-abc')` is
        "b'run-abc'", which builds a Redis key that cannot hit -- so a live
        run's record reads as missing and its worktree is only saved by the
        doubt that follows.

        Driven through a get() that answers only the correct key, so the
        assertion fails on the real consequence rather than on a call
        signature.
        """
        redis_client = Mock()
        redis_client.hgetall.return_value = {b'codetoreum:1045': b'run-abc'}
        blobs = {'orchestrator:pipeline_run:run-abc': self._run_blob()}
        redis_client.get.side_effect = lambda key: blobs.get(key)

        result = self._manager(redis_client, self._es([])).get_active_run_workspaces()

        assert result.ownership_of(
            'codetoreum', Path('/workspace/.orchestrator/worktrees/codetoreum/1016')
        ) is RunOwnership.OWNED

    def test_a_bytes_run_id_can_still_be_cleared_by_elasticsearch(self):
        """The same decode on the comparison side: ids read out of
        Elasticsearch are JSON text, so a bytes-derived "b'run-abc'" could
        never equal one and the doubt could never be cleared."""
        redis_client = Mock()
        redis_client.hgetall.return_value = {b'codetoreum:1045': b'run-abc'}
        redis_client.get.return_value = None
        es = self._es([{
            'id': 'run-abc',
            'project': 'codetoreum',
            'epic_id': '1016',
            'project_dir': '/w/worktrees/codetoreum/1016',
            'status': 'active',
        }])

        result = self._manager(redis_client, es).get_active_run_workspaces()

        assert result.projects_with_unaccountable_runs == set(), (
            "the ids have to COMPARE EQUAL -- asserting only on "
            "ownership_of('codetoreum', ...) would also pass if the project "
            "itself decoded to \"b'codetoreum\" and the doubt landed under a "
            "name no caller ever asks about"
        )
        assert result.ownership_of(
            'codetoreum', Path('/w/worktrees/codetoreum/999')
        ) is RunOwnership.UNOWNED


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
        The sweep's actual behaviour on this answer is to keep the directory,
        so the only honest report is "could not be answered"."""
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
        kw.setdefault('ownership', RunOwnership.UNOWNED)
        kw.setdefault('container_live', False)
        kw.setdefault('corrupted', False)
        kw.setdefault('drift_holds_work', False)
        return ProjectWorkspaceManager._prune_verdict(**kw)

    def test_an_unreadable_run_store_outranks_everything(self):
        """The sweep keeps every worktree it gets this answer for, so no row
        carrying it is eligible whatever the other rules say."""
        assert self._verdict(
            ownership=RunOwnership.UNKNOWN, corrupted=True
        ) == 'unknown'

    def test_an_active_run_outranks_the_later_rules(self):
        assert self._verdict(ownership=RunOwnership.OWNED,
                             drift_holds_work=True) == 'skipped_active_run'

    def test_the_verdict_takes_one_ownership_argument_not_a_correlated_pair(self):
        """#240. The pair it replaced had a combination that could be
        constructed but not meant — known=False with protected=True — so a
        caller that got the pairing wrong got a verdict the sweep did not
        share. One argument has no invalid combinations."""
        import inspect

        params = inspect.signature(ProjectWorkspaceManager._prune_verdict).parameters
        assert 'ownership' in params
        assert 'active_runs_known' not in params
        assert 'active_run_protected' not in params

    def test_an_unanswerable_lookup_can_never_reach_eligible(self):
        """Every other rule saying "remove" must still not produce an eligible
        verdict while the fifth rule is unanswerable."""
        for container_live in (True, False, None):
            assert self._verdict(
                ownership=RunOwnership.UNKNOWN, container_live=container_live
            ) == 'unknown'

    def test_an_answer_this_verdict_has_never_seen_is_not_eligible(self):
        """Same fail-closed rule as the sweep's own gate: only UNOWNED may
        reach the later branches, so a member added to the enum after this was
        written reports as unknown rather than silently becoming removable."""

        class _FutureAnswer:
            value = 'unknown_project'

        assert self._verdict(ownership=_FutureAnswer()) == 'unknown'

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



class TestOneProjectsDoubtDoesNotDisarmTheOthers:
    """#233's consumer side: every reader of the answer moves with it.

    The doubt raised by an unresolvable mapping entry is carried by the VALUE
    (ActiveRunWorkspaces.projects_with_unaccountable_runs), so the sweep, the
    survey and the composed verdict all get it from the one call they already
    make -- ownership_of(). A rule added to the sweep alone leaves the operator
    diagnostic printing "prune: eligible" for directories the sweep refuses to
    touch, which is #231 verbatim.
    """

    def _doubted(self, project: str) -> ActiveRunWorkspaces:
        return ActiveRunWorkspaces(
            {}, set(), complete=True, projects_with_unaccountable_runs={project}
        )

    def _survey(self, manager, workspaces):
        run_manager = Mock()
        run_manager.get_active_run_workspaces.return_value = workspaces
        with patch('services.pipeline_run.get_pipeline_run_reader',
                   return_value=run_manager), \
             patch.object(manager, '_get_running_container_mount_sources',
                          return_value=set()), \
             patch('services.project_workspace.subprocess.run', return_value=_ok()):
            return {r['project']: r for r in manager.survey_epic_worktrees()}

    def test_the_sweep_keeps_the_doubted_projects_worktree(self, manager, tmp_path):
        """The directory is clean, untracked, has no container inside and is
        matched by nothing in the answer -- every rule but this one says
        remove. The unaccounted-for run is exactly the one whose project_dir
        cannot be seen, so "nothing claims it" is not "nothing owns it"."""
        _make_base_clone(tmp_path, "context-studio")
        worktree = _make_worktree(tmp_path, "context-studio", "1140")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()) as mock_run:
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'context-studio',
                running_mount_sources=set(),
                active_run_workspaces=self._doubted('context-studio'),
            )

        assert worktree.is_dir()
        mock_push.assert_not_called()
        assert not any(
            'remove' in [str(a) for a in call.args[0]]
            for call in mock_run.call_args_list
        )

    def test_an_unaffected_projects_sweep_still_runs(self, manager, tmp_path):
        """THE POINT OF SCOPING IT. Measured (2026-09-15): 9 of the 11
        mapping entries on the reference deployment were unresolvable. Global doubt would keep this
        worktree too -- forever, on every startup -- which is the permanent
        no-op that looks like protection."""
        _make_base_clone(tmp_path, "heimdall")
        _make_worktree(tmp_path, "heimdall", "7")

        with patch.object(manager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.subprocess.run', return_value=_ok()) as mock_run:
            manager._prune_project_staging(
                tmp_path / '.orchestrator' / 'worktrees' / 'heimdall',
                running_mount_sources=set(),
                active_run_workspaces=self._doubted('context-studio'),
            )

        mock_push.assert_called_once()
        assert any(
            'remove' in [str(a) for a in call.args[0]]
            for call in mock_run.call_args_list
        ), "a project with no doubt of its own is still an ordinary sweep"

    def test_the_survey_reports_the_doubt_the_sweep_acts_on(self, manager, tmp_path):
        """Both rows come from ONE lookup, so the operator diagnostic cannot
        disagree with the sweep about either project."""
        for project, epic in (("context-studio", "1140"), ("heimdall", "7")):
            _make_base_clone(tmp_path, project)
            _make_worktree(tmp_path, project, epic)

        rows = self._survey(manager, self._doubted('context-studio'))

        assert rows['context-studio']['prune_verdict'] == 'unknown'
        assert rows['context-studio']['active_run_protected'] is None, (
            "False would read as 'no run owns it' and license a hand removal"
        )
        assert rows['heimdall']['prune_verdict'] == 'eligible'
        assert rows['heimdall']['active_run_protected'] is False


class _FakeRedisStore:
    """A plain dict-backed stand-in for the hash and key space this path uses.

    It stores and returns, and that is all: none of the retirement rules under
    test are re-derived inside it. (A fake that reimplements the logic it is
    meant to check is how an earlier round's compare-and-delete tests passed
    against gutted Lua.)
    """

    def __init__(self, mapping=None):
        self.mapping = dict(mapping or {})
        self.kv = {}

    def hget(self, name, field):
        return self.mapping.get(field)

    def hset(self, name, field, value):
        self.mapping[field] = value

    def hdel(self, name, field):
        return 1 if self.mapping.pop(field, None) is not None else 0

    def hgetall(self, name):
        return dict(self.mapping)

    def get(self, key):
        return self.kv.get(key)

    def setex(self, key, ttl, value):
        self.kv[key] = value

    def exists(self, key):
        return 1 if key in self.kv else 0


class TestALookupCannotEraseTheSweepsDoubt:
    """#233, the half the doubt signal itself depends on.

    get_active_run_workspaces() raises its doubt from ONE observable: an
    issue->run mapping field whose record Redis can no longer resolve. That
    signal is only as good as its survival, and get_active_pipeline_run() --
    a neighbouring writer on the same hash -- used to HDEL exactly those
    fields, under exactly the condition that raises the doubt.

    Ordering made it a live path rather than a theoretical one: main.py runs
    container recovery BEFORE prune_epic_worktrees(), and
    recover_or_cleanup_repair_cycle_containers() /
    _process_completed_repair_cycle() both call get_active_pipeline_run() for
    any project/issue holding an orphaned repair-cycle result -- the
    mid-pipeline restart case, which is the #233 case. By the time the sweep
    ran its hgetall, the field it needed was gone, the answer looked complete,
    and the live run's worktree came back UNOWNED.

    The fix is at the lookup, not at the ordering, so these cases drive the
    lookup directly; no call order can reintroduce it.
    """

    def _manager(self, redis_client, es_client=None):
        from services.pipeline_run import PipelineRunManager
        with patch('services.pipeline_run.Elasticsearch',
                   side_effect=RuntimeError('no ES')):
            manager = PipelineRunManager(redis_client=redis_client)
        assert manager.es is None
        manager.es = es_client
        return manager

    def _es(self, docs):
        """A double that answers BOTH searches this path makes: the id lookup
        that retires accounted-for entries, and the active-run fallback.

        Dispatching on the query shape is load-bearing. A double that answered
        everything with one empty page would make the fallback raise on its
        missing `total`, and these assertions would pass without the
        retirement having been reached at all -- the failure mode that made an
        earlier round's ES test prove nothing.
        """
        def search(index=None, body=None, **kwargs):
            query = (body or {}).get('query', {})
            if 'ids' in query:
                wanted = set(query['ids']['values'])
                hits = [d for d in docs if d.get('id') in wanted]
            else:
                hits = [d for d in docs
                        if d.get('status') in ('active', 'feedback_listening')]
            return {
                'hits': {
                    'total': {'value': len(hits), 'relation': 'eq'},
                    'hits': [{'_id': d.get('id'), '_source': d} for d in hits],
                }
            }

        es = Mock()
        es.search.side_effect = search
        return es

    def _ownership(self, manager):
        return manager.get_active_run_workspaces().ownership_of(
            'context-studio', Path('/w/worktrees/context-studio/1140')
        )

    def test_a_lookup_for_the_same_issue_leaves_the_doubt_standing(self):
        """THE #233 INTERACTION GUARD.

        Nothing has learned anything about run-vanished between the two
        sweeps: Elasticsearch has never heard of it either. The lookup in
        between must therefore not be able to turn UNKNOWN into UNOWNED, which
        is the one answer prune_epic_worktrees() removes on.
        """
        redis_client = _FakeRedisStore({
            'context-studio:SDLC Execution:1140': 'run-vanished',
            'context-studio:1140': 'run-vanished',
        })
        manager = self._manager(redis_client, self._es([]))

        assert self._ownership(manager) is RunOwnership.UNKNOWN

        assert manager.get_active_pipeline_run(
            'context-studio', 1140, board='SDLC Execution'
        ) is None

        assert self._ownership(manager) is RunOwnership.UNKNOWN, (
            "the lookup deleted the only field that still said a run might "
            "own this worktree"
        )
        assert redis_client.mapping, "and it deleted it from the hash itself"

    def test_an_unreadable_record_keeps_its_entry_too(self):
        """Same rule one branch over: a record that will not parse says
        nothing about whether its run is active either, and the sweep treats
        an unparseable blob as doubt only for as long as the field pointing at
        it survives."""
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-corrupt'})
        manager = self._manager(redis_client, self._es([]))
        # Through the manager's own key builder: a hand-written key that does
        # not match simply reads as "no record", which is the NEIGHBOURING
        # branch -- the test would then pass while never reaching the
        # deserialization failure it names.
        redis_client.kv[manager._get_redis_key('run-corrupt')] = 'not json at all'
        assert manager.get_active_run_workspaces().complete is False, (
            "precondition: the record is present and unparseable, which is "
            "whole-answer doubt rather than the scoped kind"
        )

        assert manager.get_active_pipeline_run('context-studio', 1140) is None

        assert redis_client.mapping == {'context-studio:1140': 'run-corrupt'}
        assert manager.get_active_run_workspaces().complete is False

    def test_the_entry_is_retired_once_elasticsearch_says_the_run_ended(self):
        """The retirement path is not disabled, only conditioned. An ended run
        owns no workspace, so its field is debris and the hash still
        self-heals -- otherwise every expired mapping would accumulate
        forever."""
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-over'})
        es = self._es([{
            'id': 'run-over',
            'project': 'context-studio',
            'issue_number': 1140,
            'status': 'completed',
            'ended_at': '2026-09-14T10:00:00Z',
        }])
        manager = self._manager(redis_client, es)

        assert manager.get_active_pipeline_run('context-studio', 1140) is None

        assert redis_client.mapping == {}
        assert self._ownership(manager) is RunOwnership.UNOWNED

    def test_an_entry_elasticsearch_still_shows_as_active_is_kept(self):
        """An active doc accounts for nothing that would license removing the
        field: that run is not over.

        Driven through the read-only lookup (restore_to_redis=False, what the
        maintenance sweeps in project_monitor pass). On the restoring path the
        fallback HSETs the same field back a moment later, so a retirement
        here would be invisible -- the assertion would hold while the rule it
        names had been deleted.
        """
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-live'})
        es = self._es([{
            'id': 'run-live',
            'project': 'context-studio',
            'issue_number': 1140,
            'issue_title': 'Phase 1',
            'issue_url': 'https://example.invalid/1140',
            'board': 'SDLC Execution',
            'started_at': '2026-09-13T13:50:12Z',
            'status': 'active',
        }])
        manager = self._manager(redis_client, es)

        assert manager.get_active_pipeline_run(
            'context-studio', 1140, restore_to_redis=False
        ) is not None
        assert redis_client.mapping.get('context-studio:1140') == 'run-live'
        assert redis_client.kv == {}, (
            "precondition: nothing was restored, so nothing could mask a "
            "retirement of the field"
        )

    def test_a_failing_elasticsearch_retires_nothing(self):
        """It accounts for nothing while it is down, and "I could not ask" is
        not "the run ended"."""
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-vanished'})
        es = Mock()
        es.search.side_effect = RuntimeError('es down')
        manager = self._manager(redis_client, es)

        assert manager.get_active_pipeline_run('context-studio', 1140) is None

        assert redis_client.mapping == {'context-studio:1140': 'run-vanished'}
        assert self._ownership(manager) is RunOwnership.UNKNOWN

    def test_no_elasticsearch_client_retires_nothing_either(self):
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-vanished'})
        manager = self._manager(redis_client, None)

        assert manager.get_active_pipeline_run('context-studio', 1140) is None

        assert redis_client.mapping == {'context-studio:1140': 'run-vanished'}

    # The same hash has a second pruner. It has no callers today, which is
    # exactly why it is worth pinning: wiring it up is the obvious way to
    # retire the doubt, and on its old rule ("the record is gone") that
    # wiring would have been #233 all over again, from a periodic sweep.

    def test_the_periodic_cleanup_keeps_what_it_cannot_account_for(self):
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-vanished'})
        manager = self._manager(redis_client, self._es([]))

        manager.cleanup_expired_mappings()

        assert redis_client.mapping == {'context-studio:1140': 'run-vanished'}
        assert self._ownership(manager) is RunOwnership.UNKNOWN

    def test_the_periodic_cleanup_still_collects_real_debris(self):
        """It is still a cleanup: an entry whose run Elasticsearch shows as
        ended names no workspace anyone could lose."""
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-over'})
        es = self._es([{
            'id': 'run-over',
            'project': 'context-studio',
            'issue_number': 1140,
            'status': 'completed',
            'ended_at': '2026-09-14T10:00:00Z',
        }])
        manager = self._manager(redis_client, es)

        manager.cleanup_expired_mappings()

        assert redis_client.mapping == {}

    def test_the_periodic_cleanup_leaves_resolvable_entries_alone(self):
        """Control: a live run's field is not even a candidate."""
        redis_client = _FakeRedisStore({'context-studio:1140': 'run-live'})
        manager = self._manager(redis_client, self._es([]))
        redis_client.kv[manager._get_redis_key('run-live')] = '{}'

        manager.cleanup_expired_mappings()

        assert redis_client.mapping == {'context-studio:1140': 'run-live'}

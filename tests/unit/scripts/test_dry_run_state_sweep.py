"""
Tests for scripts/dry_run_state_sweep.py — the dry-run harness a sweep that
mutates execution records has to pass before it is activated (#158/#166).

The harness only has value if its own guarantees hold, so what is pinned here is
exactly those guarantees, not the report's prose:

  * the copy is verified against the snapshot it was made from,
  * a manager that resolves to a live path aborts BEFORE the sweep runs,
  * the report names every mutated record by project/issue/agent,
  * a sweep that writes into the live tree is reported as a leak and fails,
  * concurrent third-party writes are distinguished from a leak.

Deliberately does NOT import anything from services/ — the harness's whole
premise is that orchestrator singletons bind their state directory at import
time, so these tests drive it with a synthetic SweepSpec instead of the real
watchdog.
"""

import logging
import os
import sys
from pathlib import Path

import pytest
import yaml

from scripts.dry_run_state_sweep import (
    _RUNTIME_SINGLETONS,
    ExternalWriteRefused,
    Gate,
    IsolationError,
    SweepSpec,
    bind_runtime_singletons,
    classify_records,
    copy_tree_from_manifest,
    describe_execution_record_changes,
    diff_manifests,
    is_under,
    main,
    manifest_digest,
    print_report,
    run_dry_run,
    snapshot_tree,
    SWEEPS,
)


def _write_record(root: Path, project: str, issue: int, agent: str, outcome: str = 'success'):
    """One execution-history state file in the shape the real tracker writes."""
    path = root / 'execution_history' / f'{project}_issue_{issue}.yaml'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.dump(
            {
                'project_name': project,
                'issue_number': issue,
                'execution_history': [
                    {
                        'agent': agent,
                        'column': 'Development',
                        'task_id': f'task-{issue}',
                        'timestamp': '2026-01-01T00:00:00Z',
                        'outcome': outcome,
                    }
                ],
            }
        )
    )
    return path


@pytest.fixture
def deployment(tmp_path):
    """A stand-in deployment: state/ with three records, plus a config/ root."""
    root = tmp_path / 'deployment'
    state = root / 'state'
    _write_record(state, 'alpha', 1, 'senior_software_engineer')
    _write_record(state, 'alpha', 2, 'code_reviewer')
    _write_record(state, 'beta', 7, 'business_analyst')
    (root / 'config' / 'projects').mkdir(parents=True)
    (root / 'config' / 'foundations').mkdir(parents=True)
    return root


class _Manager:
    """Minimal stand-in for a manager under test."""

    def __init__(self, state_dir: Path):
        self.state_dir = Path(state_dir)


def _spec(
    run,
    resolved_dir=None,
    gates=(),
    examined=(),
    terminal=(),
    candidates=(),
    owned=(),
    external_writes=(),
):
    """A SweepSpec whose `run` is whatever the test needs it to do."""
    return SweepSpec(
        name='synthetic',
        description='synthetic sweep used by the harness tests',
        build=lambda state_root: _Manager(
            resolved_dir if resolved_dir is not None else state_root / 'execution_history'
        ),
        resolved_dirs=lambda m: {'Manager.state_dir': m.state_dir},
        run=run,
        gates=tuple(gates),
        examined_patterns=tuple(examined),
        terminal_patterns=tuple(terminal),
        candidate_patterns=tuple(candidates),
        owned_state_subtrees=tuple(owned),
        external_writes=tuple(external_writes),
        loggers=('synthetic_sweep',),
    )


def _run(spec, deployment, tmp_path, **kwargs):
    return run_dry_run(
        spec,
        deployment_root=deployment,
        scratch_root=tmp_path / 'scratch',
        config_root=deployment / 'config',
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _restore_process_globals():
    """run_dry_run() repoints process-wide state by design — undo all of it.

    The harness deliberately mutates ORCHESTRATOR_ROOT, ConfigManager's
    directories and the state-owning singletons, because that is the only way to
    make a sweep read and write the copy. Left in place, every one of them
    points a later test at a scratch directory this test is about to delete —
    the same order-pollution class the suite already fights, and the reason a
    stray write into the live state/ tree was possible at all (#181).

    The singletons need two cases, not one. A module already in sys.modules has
    a value worth saving. A module NOT yet imported — the normal case here,
    since this file deliberately imports nothing from services/ — is imported
    for the first time BY the harness, while ORCHESTRATOR_ROOT is the scratch
    dir, so its singleton is constructed against a directory pytest is about to
    delete and there is no saved value to put back. Those are rebuilt from the
    ORCHESTRATOR_ROOT that was in effect before the test, which is exactly what
    the module would have resolved had it been imported then.
    """
    previous_root = os.environ.get('ORCHESTRATOR_ROOT')

    from config.manager import config_manager
    from config.state_manager import state_manager

    config_saved = {
        attr: getattr(config_manager, attr)
        for attr in (
            'config_root', 'foundations_dir', 'projects_dir', '_agents',
            '_mcp_servers', '_pipeline_templates', '_workflow_templates',
            '_project_configs',
        )
    }
    state_saved = {
        attr: getattr(state_manager, attr)
        for attr in ('state_root', 'projects_state_dir', 'orchestrator_state_dir')
    }
    singletons_saved = []
    absent_before = []
    for module_name, singleton_name, path_attr, subdir in _RUNTIME_SINGLETONS:
        module = sys.modules.get(module_name)
        if module is None:
            absent_before.append((module_name, singleton_name, path_attr, subdir))
            continue
        singleton = getattr(module, singleton_name, None)
        if singleton is not None:
            singletons_saved.append((singleton, path_attr, getattr(singleton, path_attr)))

    yield

    if previous_root is None:
        os.environ.pop('ORCHESTRATOR_ROOT', None)
    else:
        os.environ['ORCHESTRATOR_ROOT'] = previous_root
    for attr, value in config_saved.items():
        setattr(config_manager, attr, value)
    for attr, value in state_saved.items():
        setattr(state_manager, attr, value)
    for singleton, path_attr, value in singletons_saved:
        setattr(singleton, path_attr, value)

    restored_root = Path(previous_root) if previous_root else Path('/app')
    for module_name, singleton_name, path_attr, subdir in absent_before:
        module = sys.modules.get(module_name)
        if module is None:
            continue
        singleton = getattr(module, singleton_name, None)
        if singleton is None:
            continue
        setattr(singleton, path_attr, restored_root / 'state' / subdir)


class TestSnapshotAndDiff:
    def test_snapshot_hashes_every_file_and_survives_a_rewrite(self, tmp_path):
        (tmp_path / 'a.yaml').write_text('one')
        (tmp_path / 'sub').mkdir()
        (tmp_path / 'sub' / 'b.yaml').write_text('two')

        before = snapshot_tree(tmp_path)
        assert set(before) == {'a.yaml', 'sub/b.yaml'}

        (tmp_path / 'sub' / 'b.yaml').write_text('two!')
        (tmp_path / 'c.yaml').write_text('three')
        (tmp_path / 'a.yaml').unlink()

        diff = diff_manifests(before, snapshot_tree(tmp_path))
        assert diff == {'added': ['c.yaml'], 'removed': ['a.yaml'], 'changed': ['sub/b.yaml']}

    def test_manifest_digest_is_content_sensitive_and_order_independent(self, tmp_path):
        (tmp_path / 'a').write_text('x')
        (tmp_path / 'b').write_text('y')
        first = manifest_digest(snapshot_tree(tmp_path))

        assert manifest_digest(dict(reversed(list(snapshot_tree(tmp_path).items())))) == first

        (tmp_path / 'b').write_text('z')
        assert manifest_digest(snapshot_tree(tmp_path)) != first


class TestCopyVerification:
    def test_copy_reproduces_the_manifest_exactly(self, tmp_path):
        source, dest = tmp_path / 'src', tmp_path / 'dst'
        _write_record(source, 'alpha', 1, 'agent')
        manifest = snapshot_tree(source)

        vanished, unstable = copy_tree_from_manifest(source, dest, manifest)

        assert (vanished, unstable) == ([], [])
        assert snapshot_tree(dest) == manifest

    def test_a_file_that_vanishes_mid_copy_is_reported_and_dropped(self, tmp_path):
        """state/ is a bind-mounted directory under a running orchestrator, so a
        file disappearing between the walk and the copy is an ordinary event —
        it must be reported, not crash the run and not silently inflate the
        'verified' count."""
        source, dest = tmp_path / 'src', tmp_path / 'dst'
        _write_record(source, 'alpha', 1, 'agent')
        _write_record(source, 'alpha', 2, 'agent')
        manifest = snapshot_tree(source)
        (source / 'execution_history' / 'alpha_issue_2.yaml').unlink()

        vanished, unstable = copy_tree_from_manifest(source, dest, manifest)

        assert vanished == ['execution_history/alpha_issue_2.yaml']
        assert unstable == []
        assert 'execution_history/alpha_issue_2.yaml' not in manifest
        assert snapshot_tree(dest) == manifest

    def test_a_corrupted_copy_aborts_before_the_sweep_runs(self, deployment, tmp_path, monkeypatch):
        """REGRESSION: the verification exists so a bad copy fails loudly rather
        than producing a confident, wrong report about production data."""
        def _lying_copy(src, dst, *args, **kwargs):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text('not the source contents')
            return dst

        monkeypatch.setattr('scripts.dry_run_state_sweep.shutil.copy2', _lying_copy)

        ran = []
        report = _run(_spec(lambda m: ran.append('ran')), deployment, tmp_path)

        assert ran == [], "the sweep must not run against an unverified copy"
        assert report['exit_code'] == 1
        assert report['copy']['verdict'] == 'COPY VERIFICATION FAILED'


class TestIsolation:
    def test_is_under_accepts_the_root_itself_and_rejects_a_sibling(self, tmp_path):
        (tmp_path / 'root' / 'inner').mkdir(parents=True)
        (tmp_path / 'rootlike').mkdir()

        assert is_under(tmp_path / 'root', tmp_path / 'root')
        assert is_under(tmp_path / 'root' / 'inner', tmp_path / 'root')
        # A prefix match on the STRING would wrongly accept this one.
        assert not is_under(tmp_path / 'rootlike', tmp_path / 'root')

    def test_a_manager_pointed_at_live_state_aborts_before_the_sweep(
        self, deployment, tmp_path
    ):
        """REGRESSION: this is the mistake the whole harness exists to catch —
        a manager that resolved to the production tree. It must raise, and the
        sweep must never be called."""
        ran = []
        spec = _spec(
            lambda m: ran.append('ran'),
            resolved_dir=deployment / 'state' / 'execution_history',
        )

        with pytest.raises(IsolationError) as excinfo:
            _run(spec, deployment, tmp_path)

        assert ran == []
        assert 'Manager.state_dir' in str(excinfo.value)

    def test_orchestrator_root_is_repointed_at_the_scratch_copy(self, deployment, tmp_path):
        """Every state-owning singleton in services/ derives its directory from
        ORCHESTRATOR_ROOT at import time, so the repoint has to be in effect
        while the sweep runs, not merely reported afterwards."""
        seen = {}
        report = _run(
            _spec(lambda m: seen.setdefault('root', os.environ.get('ORCHESTRATOR_ROOT'))),
            deployment,
            tmp_path,
        )

        assert seen['root'] == str(tmp_path / 'scratch')
        assert report['isolation']['ORCHESTRATOR_ROOT'] == str(tmp_path / 'scratch')

    def test_config_root_is_repointed_at_the_deployment(self, deployment, tmp_path):
        """A worktree usually has no config/projects/ at all, which silently
        reduces a 17-project sweep to zero configured projects."""
        report = _run(_spec(lambda m: None), deployment, tmp_path)

        assert report['isolation']['ConfigManager.projects_dir'] == str(
            deployment / 'config' / 'projects'
        )

    def test_the_cwd_is_the_scratch_root_while_the_sweep_runs(self, deployment, tmp_path):
        """Not every writer resolves a root: services/review_cycle.py builds
        os.path.join('state', 'projects', ...) against the process CWD and
        os.makedirs() it. Repointing ORCHESTRATOR_ROOT does nothing for that, so
        the CWD has to be inside the scratch root too — and has to be put back."""
        before = os.getcwd()
        seen = {}

        _run(_spec(lambda m: seen.setdefault('cwd', os.getcwd())), deployment, tmp_path)

        assert seen['cwd'] == str(tmp_path / 'scratch')
        assert os.getcwd() == before

    def test_the_cwd_is_restored_even_when_the_sweep_raises(self, deployment, tmp_path):
        before = os.getcwd()

        def _boom(manager):
            raise RuntimeError('nope')

        _run(_spec(_boom), deployment, tmp_path)

        assert os.getcwd() == before

    def test_config_state_manager_is_repointed_too(self, deployment, tmp_path):
        """#181: config/state_manager.py derives its root from
        Path(__file__).parent.parent with no environment override, so
        ORCHESTRATOR_ROOT alone does not move it."""
        report = _run(_spec(lambda m: None), deployment, tmp_path)

        assert report['isolation']['config.state_manager.state_manager.state_root'] == str(
            tmp_path / 'scratch' / 'state'
        )


class TestReporting:
    def test_names_every_mutated_record_by_project_issue_and_agent(
        self, deployment, tmp_path
    ):
        def _mutate(manager):
            path = manager.state_dir / 'alpha_issue_1.yaml'
            state = yaml.safe_load(path.read_text())
            state['execution_history'][-1]['outcome'] = 'failure'
            state['execution_history'][-1]['watchdog_retry_triggered'] = True
            path.write_text(yaml.dump(state))
            return 1

        report = _run(_spec(_mutate), deployment, tmp_path)

        assert report['sweep_return'] == 1
        assert report['copy_diff']['changed'] == ['execution_history/alpha_issue_1.yaml']

        (mutation,) = report['mutations']
        assert mutation['project'] == 'alpha'
        assert mutation['issue_number'] == 1
        (change,) = mutation['record_changes']
        assert change['agent'] == 'senior_software_engineer'
        assert change['fields']['outcome'] == {'before': 'success', 'after': 'failure'}
        assert change['fields']['watchdog_retry_triggered']['after'] is True

    def test_an_untouched_tree_reports_no_mutations_and_passes(self, deployment, tmp_path):
        report = _run(_spec(lambda m: 0), deployment, tmp_path)

        assert report['mutations'] == []
        assert report['copy_diff'] == {'added': [], 'removed': [], 'changed': []}
        assert report['live_check']['identical'] is True
        assert report['exit_code'] == 0

    def test_gate_accounting_comes_from_the_sweeps_own_log_output(
        self, deployment, tmp_path
    ):
        """The point of running the REAL sweep is that its accounting is read
        out of what it actually did, rather than re-derived by a parallel
        implementation that could disagree with it."""
        sweep_logger = logging.getLogger('synthetic_sweep')

        def _log_only(manager):
            sweep_logger.info('Checking 3 execution state files for empty outputs')
            sweep_logger.debug('Skipping alpha/#1: work already in progress')
            sweep_logger.debug('Skipping alpha/#2: work already in progress')
            sweep_logger.warning('Detected empty output for beta/#7 - marking as failure to trigger retry')
            sweep_logger.info('something nobody wrote a bucket for')
            return 1

        spec = _spec(
            _log_only,
            gates=(Gate('PROTECTION 1', 'work in progress', (r'work already in progress',)),),
            examined=(r'Checking (\d+) execution state files',),
            terminal=(r'marking as failure to trigger retry',),
        )
        report = _run(spec, deployment, tmp_path)

        assert report['classification']['examined'] == 3
        assert report['gates'] == [
            {
                'name': 'PROTECTION 1',
                'why': 'work in progress',
                'degradation': False,
                'count': 2,
            }
        ]
        assert len(report['classification']['terminal']) == 1
        # An unrecognised line must surface verbatim rather than vanish into a
        # bucket that does not exist yet.
        assert report['classification']['unclassified'] == [
            'something nobody wrote a bucket for'
        ]
        # 2 skipped + 1 terminal accounts for all 3 examined.
        assert report['unaccounted'] == 0

    def test_records_dropped_before_the_first_gate_are_reported_as_such(
        self, deployment, tmp_path
    ):
        """Every sweep drops records before its first named gate -- the real
        watchdog silently `continue`s on any record whose last execution is not
        a 'success'. Without this residual, "0 reached the terminal decision"
        reads as "every record was individually considered and protected", which
        is the reassurance this harness must never give by accident."""
        sweep_logger = logging.getLogger('synthetic_sweep')

        def _log_only(manager):
            sweep_logger.info('Checking 100 execution state files for empty outputs')
            sweep_logger.debug('Skipping alpha/#1: work already in progress')
            return 0

        spec = _spec(
            _log_only,
            gates=(Gate('PROTECTION 1', 'work in progress', (r'work already in progress',)),),
            examined=(r'Checking (\d+) execution state files',),
            terminal=(r'marking as failure to trigger retry',),
        )
        report = _run(spec, deployment, tmp_path)

        assert report['unaccounted'] == 99

    def test_classify_records_routes_errors_to_their_own_bucket(self):
        spec = _spec(lambda m: None, gates=(), examined=(), terminal=())
        classified = classify_records(
            spec,
            [('synthetic_sweep', 'ERROR', 'Watchdog: Error processing x.yaml: boom')],
        )

        assert classified['errors'] == ['Watchdog: Error processing x.yaml: boom']
        assert classified['unclassified'] == []

    def test_describe_execution_record_changes_reports_an_appended_record(self, tmp_path):
        before_root, after_root = tmp_path / 'before', tmp_path / 'after'
        _write_record(before_root, 'alpha', 1, 'agent_a')
        _write_record(after_root, 'alpha', 1, 'agent_a')

        path = after_root / 'execution_history' / 'alpha_issue_1.yaml'
        state = yaml.safe_load(path.read_text())
        state['execution_history'].append(
            {'agent': 'agent_b', 'column': 'Testing', 'task_id': 't2',
             'timestamp': '2026-01-02T00:00:00Z', 'outcome': 'in_progress'}
        )
        path.write_text(yaml.dump(state))

        (described,) = describe_execution_record_changes(
            before_root, after_root, ['execution_history/alpha_issue_1.yaml']
        )

        (change,) = described['record_changes']
        assert change['new_record'] is True
        assert change['agent'] == 'agent_b'


class TestLiveTreeProof:
    def test_a_sweep_that_writes_live_is_reported_as_a_leak_and_fails(
        self, deployment, tmp_path
    ):
        """REGRESSION: the ONLY outcome that must never be reported as success.
        A leak is recognised by the same relpath changing in the copy AND in the
        live tree, which is what a sweep escaping its isolation produces."""
        def _write_both(manager):
            for root in (manager.state_dir, deployment / 'state' / 'execution_history'):
                path = root / 'alpha_issue_1.yaml'
                state = yaml.safe_load(path.read_text())
                state['execution_history'][-1]['outcome'] = 'failure'
                path.write_text(yaml.dump(state))
            return 1

        report = _run(_spec(_write_both), deployment, tmp_path)

        assert report['live_check']['identical'] is False
        assert report['live_check']['leaked'] == ['execution_history/alpha_issue_1.yaml']
        assert report['exit_code'] == 1
        assert 'not isolated' in report['verdict']

    def test_unattributable_live_drift_is_unverified_not_passed(
        self, deployment, tmp_path
    ):
        """The orchestrator is normally running and writing its own state. That
        is not a leak, but it is not proof either — it must not silently read as
        a clean pass."""
        def _third_party_write(manager):
            _write_record(deployment / 'state', 'gamma', 99, 'someone_else')
            return 0

        report = _run(_spec(_third_party_write), deployment, tmp_path)

        assert report['live_check']['identical'] is False
        assert report['live_check']['leaked'] == []
        assert report['exit_code'] == 3
        assert 'UNVERIFIED' in report['verdict']

    def test_allow_concurrent_writes_downgrades_drift_but_never_a_same_path_leak(
        self, deployment, tmp_path
    ):
        def _write_both(manager):
            path = manager.state_dir / 'alpha_issue_1.yaml'
            state = yaml.safe_load(path.read_text())
            state['execution_history'][-1]['outcome'] = 'failure'
            path.write_text(yaml.dump(state))
            (deployment / 'state' / 'execution_history' / 'alpha_issue_1.yaml').write_text(
                yaml.dump(state)
            )
            return 1

        drift = _run(
            _spec(lambda m: _write_record(deployment / 'state', 'gamma', 99, 'x') and 0),
            deployment,
            tmp_path / 'a',
            allow_concurrent_writes=True,
        )
        assert drift['exit_code'] == 0
        assert 'PASSED WITH DRIFT' in drift['verdict']

        leak = _run(
            _spec(_write_both),
            deployment,
            tmp_path / 'b',
            allow_concurrent_writes=True,
        )
        assert leak['exit_code'] == 1

    def test_a_live_only_write_in_an_owned_subtree_is_never_a_clean_pass(
        self, deployment, tmp_path
    ):
        """REGRESSION: leak detection used to be `changed_live & mutated_in_copy`,
        an intersection of relpaths — and a sweep that escapes its isolation
        writes ONLY the live path and never the copy, so that intersection is
        empty by construction for exactly the failure this harness exists to
        catch. With --allow-concurrent-writes (the documented way to run against
        a live orchestrator) it read as 'PASSED WITH DRIFT', exit 0.

        Any live change under a subtree the sweep writes must stay unverified."""
        def _escape(manager):
            path = deployment / 'state' / 'execution_history' / 'alpha_issue_1.yaml'
            state = yaml.safe_load(path.read_text())
            state['execution_history'][-1]['outcome'] = 'failure'
            path.write_text(yaml.dump(state))
            return 1

        report = _run(
            _spec(_escape, owned=('execution_history',)),
            deployment,
            tmp_path,
            allow_concurrent_writes=True,
        )

        assert report['copy_diff'] == {'added': [], 'removed': [], 'changed': []}
        assert report['live_check']['leaked'] == []
        assert report['live_check']['unattributed_owned'] == [
            'execution_history/alpha_issue_1.yaml'
        ]
        assert report['exit_code'] == 3
        assert 'UNVERIFIED' in report['verdict']

    def test_drift_outside_an_owned_subtree_is_still_downgradable(
        self, deployment, tmp_path
    ):
        """The flag has to keep working for what it is for: the orchestrator
        writing its own unrelated state while the harness runs."""
        (deployment / 'state' / 'orchestrator').mkdir(parents=True, exist_ok=True)

        def _third_party(manager):
            (deployment / 'state' / 'orchestrator' / 'something_else.yaml').write_text('x')
            return 0

        report = _run(
            _spec(_third_party, owned=('execution_history',)),
            deployment,
            tmp_path,
            allow_concurrent_writes=True,
        )

        assert report['live_check']['unattributed_owned'] == []
        assert report['exit_code'] == 0
        assert 'PASSED WITH DRIFT' in report['verdict']

    def test_a_flock_artifact_present_in_both_trees_is_not_a_leak(
        self, deployment, tmp_path
    ):
        """utils.file_lock leaves a `<state file>.lock` beside every record it
        locks, so the copy grows a set of them on every run and the live tree
        grows the same set whenever the orchestrator touches the same records.
        Counting that collision as a leak would make exit 1 the routine outcome
        of running against the live deployment, which is what this harness is
        primarily for."""
        def _touch_locks(manager):
            (manager.state_dir / 'alpha_issue_1.yaml.lock').write_text('')
            (deployment / 'state' / 'execution_history' / 'alpha_issue_1.yaml.lock').write_text('')
            return 0

        report = _run(
            _spec(_touch_locks, owned=('execution_history',)),
            deployment,
            tmp_path,
            allow_concurrent_writes=True,
        )

        assert report['live_check']['leaked'] == []
        assert report['live_check']['unattributed_owned'] == []
        assert report['live_check']['lock_artifacts'] == [
            'execution_history/alpha_issue_1.yaml.lock'
        ]
        assert report['exit_code'] == 0
        # And it is not described as a record the sweep would mutate.
        assert report['mutations'] == []

    def test_a_leak_at_a_different_relpath_is_attributed_by_record_content(
        self, deployment, tmp_path
    ):
        """Path intersection cannot see a leak that lands under another name.
        The same record identity with the same field delta is this run's work
        wherever it was written."""
        live_state = deployment / 'state'
        # A second file naming the SAME record — the shape a rename, a legacy
        # filename convention, or a second writer produces.
        alias = live_state / 'execution_history' / 'alpha_issue_1.alias.yaml'
        alias.write_text(
            (live_state / 'execution_history' / 'alpha_issue_1.yaml').read_text()
        )

        def _mutate_copy_and_leak_to_the_alias(manager):
            path = manager.state_dir / 'alpha_issue_1.yaml'
            state = yaml.safe_load(path.read_text())
            state['execution_history'][-1]['outcome'] = 'failure'
            path.write_text(yaml.dump(state))
            alias.write_text(yaml.dump(state))
            return 1

        report = _run(
            _spec(_mutate_copy_and_leak_to_the_alias, owned=('execution_history',)),
            deployment,
            tmp_path,
            allow_concurrent_writes=True,
        )

        assert report['live_check']['leaked'] == [
            'execution_history/alpha_issue_1.alias.yaml'
        ]
        assert report['exit_code'] == 1
        assert 'not isolated' in report['verdict']

    def test_the_proof_is_printable_before_and_after_digests(self, deployment, tmp_path):
        report = _run(_spec(lambda m: 0), deployment, tmp_path)

        live = report['live_check']
        assert live['before_count'] == 3
        assert live['after_count'] == 3
        assert live['before_digest'] == live['after_digest']
        assert len(live['before_digest']) == 64


class TestCommandLine:
    def test_list_names_every_registered_sweep(self, capsys):
        assert main(['--list']) == 0

        out = capsys.readouterr().out
        for name in SWEEPS:
            assert name in out

    def test_a_sweep_that_writes_outside_state_is_refused_when_not_neutralized(
        self, capsys
    ):
        """The harness checksums state/ and nothing else. A sweep that also
        writes to production Redis cannot be proven harmless by this run, so it
        must not be dry-run as though it could be — cleanup_stuck_in_progress_states()
        claims a cleanup key per candidate (suppressing the REAL sweep for the
        claim TTL) and DELETES a real agent's persisted result. Those effects are
        intercepted by default; asking for them to reach production for real
        needs the explicit flag."""
        writing = [name for name, spec in SWEEPS.items() if spec.external_writes]
        assert writing, 'this test needs at least one sweep with declared writes'

        assert main(['--sweep', writing[0], '--no-neutralize-external-effects']) == 2
        assert 'REFUSING to run' in capsys.readouterr().out

    def test_the_refusal_gate_is_in_run_dry_run_not_only_in_main(
        self, deployment, tmp_path
    ):
        """REGRESSION: the gate used to live only in main(), so a programmatic
        caller ran a sweep's real production writes just by not passing a flag
        it never saw."""
        spec = _spec(lambda m: 0, external_writes=('Redis: something destructive',))

        with pytest.raises(ExternalWriteRefused):
            _run(
                spec, deployment, tmp_path,
                neutralize_external_effects_enabled=False,
            )

        # Neutralized (the default) it is allowed, because the harness now
        # intercepts what it cannot checksum.
        assert _run(spec, deployment, tmp_path)['exit_code'] == 0

    def test_an_unknown_sweep_is_a_usage_error(self):
        with pytest.raises(SystemExit) as excinfo:
            main(['--sweep', 'no-such-sweep'])
        assert excinfo.value.code == 2

    def test_a_deployment_root_without_state_is_a_usage_error(self, tmp_path, capsys):
        assert main(['--sweep', 'empty_output_watchdog', '--deployment-root', str(tmp_path)]) == 2
        assert 'No state/ directory' in capsys.readouterr().out

    def test_a_clean_run_exits_zero_and_prints_the_whole_report(
        self, deployment, tmp_path, monkeypatch, capsys
    ):
        """main()'s success path — print_report(), the exit-code propagation and
        the scratch teardown — had no coverage at all."""
        monkeypatch.setitem(SWEEPS, 'synthetic', _spec(lambda m: 0))

        exit_code = main([
            '--sweep', 'synthetic',
            '--deployment-root', str(deployment),
            '--config-root', str(deployment / 'config'),
            '--scratch', str(tmp_path / 'scratch'),
        ])

        out = capsys.readouterr().out
        assert exit_code == 0
        assert 'PROOF THE state/ TREE WAS NOT TOUCHED' in out
        assert 'BYTE-IDENTICAL' in out
        assert 'EXTERNAL EFFECTS' in out

    def test_a_scratch_directory_the_harness_created_is_removed(
        self, deployment, tmp_path, monkeypatch
    ):
        monkeypatch.setitem(SWEEPS, 'synthetic', _spec(lambda m: 0))
        scratch = tmp_path / 'scratch'

        assert main([
            '--sweep', 'synthetic',
            '--deployment-root', str(deployment),
            '--config-root', str(deployment / 'config'),
            '--scratch', str(scratch),
        ]) == 0

        assert not scratch.exists()

    def test_an_existing_non_empty_scratch_directory_is_refused_not_deleted(
        self, deployment, tmp_path, monkeypatch, capsys
    ):
        """REGRESSION: --scratch used to accept any existing directory and then
        shutil.rmtree() it wholesale, with ignore_errors=True hiding even the
        failures. `--scratch ~/work` removed ~/work. A tool whose premise is
        'prove nothing was destroyed' does not get an unguarded recursive delete
        on an operator-supplied path."""
        monkeypatch.setitem(SWEEPS, 'synthetic', _spec(lambda m: 0))
        scratch = tmp_path / 'my-working-dir'
        scratch.mkdir()
        (scratch / 'important.txt').write_text('do not delete me')

        exit_code = main([
            '--sweep', 'synthetic',
            '--deployment-root', str(deployment),
            '--config-root', str(deployment / 'config'),
            '--scratch', str(scratch),
        ])

        assert exit_code == 2
        assert 'already exists and is not empty' in capsys.readouterr().out
        assert (scratch / 'important.txt').read_text() == 'do not delete me'

    def test_an_empty_operator_supplied_scratch_directory_survives_the_run(
        self, deployment, tmp_path, monkeypatch
    ):
        """An empty directory the operator made is still one the harness did not
        create, so it is left where it was found."""
        monkeypatch.setitem(SWEEPS, 'synthetic', _spec(lambda m: 0))
        scratch = tmp_path / 'mine'
        scratch.mkdir()

        assert main([
            '--sweep', 'synthetic',
            '--deployment-root', str(deployment),
            '--config-root', str(deployment / 'config'),
            '--scratch', str(scratch),
        ]) == 0

        assert scratch.is_dir()


class TestAccounting:
    """The residual arithmetic — the number that stops '0 reached the terminal
    decision' from being read as 'every record was individually considered'."""

    def _log_spec(self, lines, gates, examined, terminal=(), candidates=()):
        sweep_logger = logging.getLogger('synthetic_sweep')

        def _log_only(manager):
            for level, message in lines:
                getattr(sweep_logger, level)(message)
            return 0

        return _spec(
            _log_only, gates=gates, examined=examined, terminal=terminal,
            candidates=candidates,
        )

    def test_a_degradation_and_a_skip_on_one_record_account_for_one_record(
        self, deployment, tmp_path
    ):
        """REGRESSION: gate hits were counted per LOG LINE, and several
        protections log an annotation and then fall THROUGH to a later gate —
        'could not date this one', 'project config unavailable', 'PROTECTION 2
        failed'. One record produced two hits, `accounted` overshot `examined`,
        and the residual printed as a negative under a sentence claiming a
        specific safety meaning."""
        spec = self._log_spec(
            lines=[
                ('info', 'Checking 1 execution state files for empty outputs'),
                ('debug', 'Watchdog: Could not date alpha/#1 (None) -- age gate skipped: boom'),
                ('debug', 'Watchdog: Skipping alpha/#1: work already in progress'),
            ],
            gates=(
                Gate('PROTECTION 1', 'work in progress', (r'work already in progress',)),
                Gate('INFO: undated', 'age gate skipped',
                     (r'Could not date \S+/#\d+ .* age gate skipped',), degradation=True),
            ),
            examined=(r'Checking (\d+) execution state files',),
        )

        report = _run(spec, deployment, tmp_path)

        counts = {gate['name']: gate['count'] for gate in report['gates']}
        assert counts == {'PROTECTION 1': 1, 'INFO: undated': 1}
        assert report['unaccounted'] == 0
        assert report['accounting_anomaly'] is None

    def test_counts_are_records_not_lines_when_one_record_logs_twice(
        self, deployment, tmp_path
    ):
        spec = self._log_spec(
            lines=[
                ('info', 'Checking 2 execution state files for empty outputs'),
                ('debug', 'Watchdog: Skipping alpha/#1: work already in progress'),
                ('debug', 'Watchdog: Skipping alpha/#1: work already in progress'),
                ('debug', 'Watchdog: Skipping alpha/#2: work already in progress'),
            ],
            gates=(Gate('PROTECTION 1', 'work in progress', (r'work already in progress',)),),
            examined=(r'Checking (\d+) execution state files',),
        )

        report = _run(spec, deployment, tmp_path)

        assert report['gates'][0]['count'] == 2
        assert report['unaccounted'] == 0

    def test_the_residual_is_never_printed_as_a_negative(self, deployment, tmp_path):
        """If the arithmetic still cannot be reconciled, that is a harness
        defect and has to say so, not print '-10 records were never a
        candidate'."""
        spec = self._log_spec(
            lines=[
                ('info', 'Checking 1 execution state files for empty outputs'),
                ('debug', 'Watchdog: Skipping alpha/#1: work already in progress'),
                ('debug', 'Watchdog: Skipping alpha/#2: work already in progress'),
                ('debug', 'Watchdog: Skipping alpha/#3: work already in progress'),
            ],
            gates=(Gate('PROTECTION 1', 'work in progress', (r'work already in progress',)),),
            examined=(r'Checking (\d+) execution state files',),
        )

        report = _run(spec, deployment, tmp_path)

        assert report['unaccounted'] == 0
        assert 'harness defect' in report['accounting_anomaly']

    def test_a_record_that_leaves_the_guard_chain_undecided_is_not_never_a_candidate(
        self, deployment, tmp_path
    ):
        """cleanup_stuck_in_progress_states() skips a record whose cleanup claim
        another mechanism holds with a bare `continue`. Without a candidate
        bucket that record fell into the residual, which is printed as 'NOT
        protected, just never a candidate' — the exact opposite of what
        happened."""
        spec = self._log_spec(
            lines=[
                ('info', 'Checking 2 execution state files for stuck in_progress states'),
                ('info', 'Found stuck in_progress execution: alpha/#1 agent in col from t'),
                ('info', 'Found stuck in_progress execution: alpha/#2 agent in col from t'),
                ('warning', 'Marked stuck execution as failed: alpha/#2 agent in col'),
            ],
            gates=(),
            examined=(r'Checking (\d+) execution state files for stuck in_progress',),
            terminal=(r'Marked stuck execution as failed:',),
            candidates=(r'Found stuck in_progress execution:',),
        )

        report = _run(spec, deployment, tmp_path)

        assert report['classification']['candidate_records'] == ['alpha/#1', 'alpha/#2']
        assert report['classification']['undecided_candidates'] == ['alpha/#1']
        assert report['unaccounted'] == 0


class TestRegisteredSweepPatterns:
    """The registered specs' patterns pinned against the REAL message text from
    services/work_execution_state.py. Nothing exercised them beyond --list, and
    a pattern that matches the wrong line is invisible until an operator reads a
    number that is 5x too large."""

    def test_stuck_in_progress_does_not_count_pre_guard_candidates_as_terminal(self):
        """REGRESSION: terminal_patterns was 'Found stuck in_progress execution:',
        which work_execution_state.py logs the instant it sees an in_progress
        entry — before the cleanup claim and before all five guards. Every
        guard-skipped record was reported as one the sweep would rewrite."""
        spec = SWEEPS['stuck_in_progress']
        classified = classify_records(spec, [
            ('services.work_execution_state', 'INFO',
             'Checking 3 execution state files for stuck in_progress states'),
            ('services.work_execution_state', 'INFO',
             'Found stuck in_progress execution: proj/#1 senior_software_engineer in Dev from t'),
            ('services.work_execution_state', 'INFO',
             'Found stuck in_progress execution: proj/#2 code_reviewer in Review from t'),
            ('services.work_execution_state', 'INFO',
             'Issue proj/#1 holds pipeline lock - skipping stuck state cleanup'),
            ('services.work_execution_state', 'WARNING',
             'Marked stuck execution as failed: proj/#2 code_reviewer in Review '
             '(no container found, outcome not recorded). Pipeline is now blocked'),
        ])

        assert classified['examined'] == 3
        assert classified['candidate_records'] == ['proj/#1', 'proj/#2']
        assert classified['terminal_records'] == ['proj/#2']
        assert classified['gate_records']['GUARD: holds the pipeline lock'] == ['proj/#1']

    def test_stuck_in_progress_counts_a_redis_recovery_as_terminal_too(self):
        spec = SWEEPS['stuck_in_progress']
        classified = classify_records(spec, [
            ('services.work_execution_state', 'INFO',
             'Reconciled successful execution from Redis: proj/#7 business_analyst in '
             'Analysis. Monitoring loop will re-detect card position and continue pipeline.'),
        ])

        assert classified['terminal_records'] == ['proj/#7']

    def test_a_claim_taken_by_another_mechanism_is_a_named_gate(self):
        """try_claim_cleanup()'s skip is a bare `continue` whose only log line
        is on services.cleanup_guard, at DEBUG. Without that logger and a gate
        for it, a claimed record hit no bucket at all and landed in the residual
        labelled 'never a candidate'."""
        spec = SWEEPS['stuck_in_progress']
        assert 'services.cleanup_guard' in spec.loggers

        classified = classify_records(spec, [
            ('services.cleanup_guard', 'DEBUG',
             'Cleanup for proj/#42 already claimed by zombie_watchdog, skipping in '
             'stuck_state_cleanup'),
        ])

        assert classified['gate_records']['GUARD: cleanup already claimed'] == ['proj/#42']
        assert classified['unclassified'] == []

    def test_the_feedback_loop_guard_has_a_gate(self):
        """It skips the record with a `continue` exactly like the review-cycle
        guard next to it, and had no bucket at all."""
        spec = SWEEPS['stuck_in_progress']
        classified = classify_records(spec, [
            ('services.work_execution_state', 'INFO',
             'Issue proj/#5 has active feedback loop - skipping stuck state cleanup'),
            ('services.work_execution_state', 'WARNING',
             'Failed to check feedback loop state for proj/#6: boom'),
        ])

        assert classified['gate_records']['GUARD: active feedback loop'] == ['proj/#5']
        assert classified['gate_records'][
            'GUARD: a guard raised, record skipped (fail-safe)'
        ] == ['proj/#6']
        assert classified['unclassified'] == []

    def test_fail_safe_guard_failures_are_not_labelled_as_missing_safety(self):
        """The lock / review-cycle / feedback-loop handlers log and then
        `continue` — the record is skipped, which is the safest outcome, not
        'the sweep proceeded WITHOUT the guard'. Only the queue check and the
        cleanup claim actually fall through."""
        spec = SWEEPS['stuck_in_progress']
        by_name = {gate.name: gate for gate in spec.gates}

        assert not by_name['GUARD: a guard raised, record skipped (fail-safe)'].degradation
        assert by_name['DEGRADED: a guard could not be evaluated'].degradation
        assert by_name['DEGRADED: a guard could not be evaluated'].patterns == (
            r'Cleanup guard unavailable, proceeding without coordination',
            r'Failed to check pipeline queue',
        )

    def test_empty_output_watchdog_terminal_and_gates_match_the_real_messages(self):
        spec = SWEEPS['empty_output_watchdog']
        classified = classify_records(spec, [
            ('services.work_execution_state', 'INFO',
             'Watchdog: Checking 4 execution state files for empty outputs'),
            ('services.work_execution_state', 'DEBUG',
             'Watchdog: Skipping proj/#1: work already in progress'),
            ('services.work_execution_state', 'DEBUG',
             "Watchdog: Skipping proj/#2: already 'waiting' in pipeline queue for board 'b'"),
            ('services.work_execution_state', 'DEBUG',
             'Watchdog: Not eligible for retry proj/#3: retry budget exhausted'),
            ('services.work_execution_state', 'WARNING',
             'Watchdog: Detected successful execution with no output for proj/#4 '
             '- marking as failure to trigger retry'),
        ])

        assert classified['examined'] == 4
        assert classified['terminal_records'] == ['proj/#4']
        assert classified['gate_records']['PROTECTION 1 (active execution)'] == ['proj/#1']
        assert classified['gate_records']['PROTECTION 3 (queue status)'] == ['proj/#2']
        assert classified['gate_records']['PROTECTION 4 (retry eligibility)'] == ['proj/#3']
        assert classified['unclassified'] == []

    def test_every_registered_sweep_declares_the_subtrees_it_writes(self):
        """Leak attribution turns on this list, so a spec added later without
        one is silently the weakest possible check."""
        for name, spec in SWEEPS.items():
            assert spec.owned_state_subtrees, f'{name} declares no owned state subtrees'


class TestExternalEffects:
    """The harness checksums state/ and nothing else, so everything a sweep
    writes elsewhere is by construction something it cannot prove it left
    alone. Both registered sweeps write to production Redis and to the
    production observability stream."""

    def test_both_registered_sweeps_declare_their_production_writes(self):
        """REGRESSION: empty_output_watchdog declared none at all while emitting
        RETRY_ATTEMPTED into the production event stream once per record that
        reaches the terminal decision — which is precisely the run this harness
        exists for. stuck_in_progress declared only the claim key while also
        DELETING a real agent's persisted result."""
        watchdog = ' | '.join(SWEEPS['empty_output_watchdog'].external_writes)
        assert 'RETRY_ATTEMPTED' in watchdog

        stuck = ' | '.join(SWEEPS['stuck_in_progress'].external_writes)
        assert 'agent_result' in stuck and 'DELETES' in stuck
        assert 'repair_cycle:container' in stuck
        assert 'agent:container' in stuck
        assert 'emit_error_decision' in stuck or 'PIPELINE_RUN_FAILED' in stuck

    def test_stuck_in_progress_declares_the_guards_that_cannot_fire(self):
        """Both of those guards read an in-memory dict on a singleton owned by
        the RUNNING orchestrator. This is a fresh process, so they see it empty
        and can never fire — and immediately past them sits the destructive
        Redis delete. 'skipped by active review cycle: 0' must not read as 'no
        record needed that protection'."""
        inert = ' | '.join(SWEEPS['stuck_in_progress'].inert_guards)
        assert 'review cycle' in inert
        assert 'feedback loop' in inert

    def test_redis_writes_are_intercepted_and_reported(self, deployment, tmp_path):
        """A neutralized client still READS production — a guard reading an
        empty scratch Redis answers 'no lock, not queued' and makes the dry run
        strictly less protected than production — but writes nowhere."""
        redis_module = pytest.importorskip('redis')

        def _write_redis(manager):
            client = redis_module.Redis(host='redis', port=6379, decode_responses=True)
            # The claim has to look like it succeeded, or every candidate would
            # be reported as skipped-by-claim and the sweep as inert.
            assert client.set('orchestrator:cleanup_guard:p:1', 'x', nx=True, ex=300) is True
            client.delete('agent_result:p:1:task-abc')
            return 0

        report = _run(_spec(_write_redis), deployment, tmp_path)

        effects = report['external_effects']['redis_writes']
        assert effects['total'] == 2
        assert effects['by_command'] == {'delete': 1, 'set': 1}
        assert {s['key'] for s in effects['samples']} == {
            'orchestrator:cleanup_guard:p:1', 'agent_result:p:1:task-abc'
        }

    def test_observability_emission_is_intercepted_and_reported(
        self, deployment, tmp_path
    ):
        observability = pytest.importorskip('monitoring.observability')

        def _emit(manager):
            from monitoring.observability import get_observability_manager, EventType
            get_observability_manager().emit(
                EventType.RETRY_ATTEMPTED, 'watchdog', 'task-1', 'alpha',
                {'issue_number': 1, 'reason': 'empty_output_on_success'},
            )
            return 0

        report = _run(_spec(_emit), deployment, tmp_path)

        events = report['external_effects']['observability_events']
        assert events['total'] == 1
        assert events['by_type'] == {
            observability.EventType.RETRY_ATTEMPTED.value: 1
        }

    def test_turning_neutralization_off_says_so_in_the_report(
        self, deployment, tmp_path
    ):
        report = _run(
            _spec(lambda m: 0), deployment, tmp_path,
            neutralize_external_effects_enabled=False,
        )

        assert report['external_effects']['neutralized'] is False
        assert any('NEUTRALIZATION OFF' in n for n in report['external_effects']['notes'])


class TestSweepFailure:
    def test_a_sweep_that_raises_still_gets_the_live_tree_proof(
        self, deployment, tmp_path
    ):
        """REGRESSION: the exception propagated out of run_dry_run(), main()
        caught only IsolationError, and Python exited 1 — the code documented as
        'a leak'. The sweep had already done part of its work and nobody learned
        whether the partial run leaked."""
        def _boom(manager):
            (manager.state_dir / 'alpha_issue_1.yaml').write_text(
                yaml.dump({'project_name': 'alpha', 'issue_number': 1,
                           'execution_history': [{'agent': 'a', 'outcome': 'failure'}]})
            )
            raise RuntimeError('docker socket not reachable')

        report = _run(_spec(_boom), deployment, tmp_path)

        assert report['exit_code'] == 4
        assert 'docker socket not reachable' in report['sweep_error']
        # The proof still ran, and so did the copy diff.
        assert report['live_check']['identical'] is True
        assert report['copy_diff']['changed'] == ['execution_history/alpha_issue_1.yaml']
        assert report['mutations']

    def test_a_sweep_that_raises_after_leaking_still_reports_the_leak(
        self, deployment, tmp_path
    ):
        def _leak_then_boom(manager):
            for root in (manager.state_dir, deployment / 'state' / 'execution_history'):
                path = root / 'alpha_issue_1.yaml'
                state = yaml.safe_load(path.read_text())
                state['execution_history'][-1]['outcome'] = 'failure'
                path.write_text(yaml.dump(state))
            raise RuntimeError('and then it fell over')

        report = _run(_spec(_leak_then_boom), deployment, tmp_path)

        assert report['live_check']['leaked'] == ['execution_history/alpha_issue_1.yaml']
        assert report['exit_code'] == 1


class TestUnreadableFiles:
    def test_a_file_that_cannot_be_read_downgrades_the_verdict(
        self, deployment, tmp_path
    ):
        """REGRESSION: snapshot_tree() swallowed PermissionError with the same
        `continue` it uses for a file that vanished mid-walk. An unreadable file
        is in NEITHER the before nor the after manifest, so it is in no diff, in
        no leak set and in no printed count — while the report says the tree is
        byte-identical."""
        if os.geteuid() == 0:
            pytest.skip('root can read anything, so this mode cannot be simulated')

        locked = deployment / 'state' / 'execution_history' / 'alpha_issue_1.yaml'
        locked.chmod(0o000)
        try:
            report = _run(_spec(lambda m: 0), deployment, tmp_path)
        finally:
            locked.chmod(0o644)

        assert report['unreadable'] == ['execution_history/alpha_issue_1.yaml']
        assert report['exit_code'] == 3
        assert 'UNVERIFIED' in report['verdict']
        assert 'could not be read' in report['verdict']


class TestRuntimeSingletonBinding:
    """bind_runtime_singletons() carries the harness's strongest claim: an
    isolation check a caller can defeat by importing a module in the wrong
    order is not a check. It had no direct coverage at all."""

    def _fake_singleton_module(self, monkeypatch, name, state_dir):
        import types

        module = types.ModuleType(name)
        singleton = type('FakeSingleton', (), {})()
        singleton.state_dir = Path(state_dir)
        module.fake_singleton = singleton
        monkeypatch.setitem(sys.modules, name, module)
        return singleton

    def test_a_singleton_bound_to_a_live_path_is_forced_onto_the_copy(
        self, tmp_path, monkeypatch
    ):
        singleton = self._fake_singleton_module(
            monkeypatch, 'fake_live_singleton_module', '/app/state/execution_history'
        )
        monkeypatch.setattr(
            'scripts.dry_run_state_sweep._RUNTIME_SINGLETONS',
            (('fake_live_singleton_module', 'fake_singleton', 'state_dir',
              'execution_history'),),
        )
        scratch = tmp_path / 'scratch'

        resolved, unbound = bind_runtime_singletons(scratch)

        label = 'fake_live_singleton_module.fake_singleton.state_dir'
        assert unbound == []
        assert Path(singleton.state_dir) == scratch / 'state' / 'execution_history'
        assert resolved[label] == str(scratch / 'state' / 'execution_history')

    def test_a_singleton_that_will_not_import_is_reported_not_swallowed(
        self, deployment, tmp_path, monkeypatch
    ):
        """REGRESSION: an import failure did `logger.debug(...); continue`, so
        that singleton's label never entered the resolved map — absent from the
        printed ISOLATION section, while the report still said 'every
        state-owning path resolved inside the scratch root'. An operator read a
        list that quietly lacked one."""
        monkeypatch.setattr(
            'scripts.dry_run_state_sweep._RUNTIME_SINGLETONS',
            (('no_such_module_anywhere', 'nothing', 'state_dir', 'execution_history'),),
        )

        report = _run(_spec(lambda m: 0), deployment, tmp_path)

        assert report['isolation_unbound']
        label, reason = report['isolation_unbound'][0]
        assert label.startswith('no_such_module_anywhere')
        assert 'import failed' in reason
        assert 'could NOT be bound' in report['isolation_verdict']

    def test_a_lazy_singleton_nothing_constructed_is_named_as_such(
        self, tmp_path, monkeypatch
    ):
        """The lock/semaphore managers are module globals that are None until a
        getter runs. Nothing to repoint, and forcing construction would create
        state directories the sweep may never touch — but the report still has
        to say the path was considered."""
        import types

        module = types.ModuleType('fake_lazy_module')
        module.lazy_singleton = None
        monkeypatch.setitem(sys.modules, 'fake_lazy_module', module)
        monkeypatch.setattr(
            'scripts.dry_run_state_sweep._RUNTIME_SINGLETONS',
            (('fake_lazy_module', 'lazy_singleton', 'state_dir', 'pipeline_locks'),),
        )

        resolved, unbound = bind_runtime_singletons(tmp_path / 'scratch')

        assert unbound == []
        assert resolved['fake_lazy_module.lazy_singleton.state_dir'].startswith(
            'not constructed'
        )

    def test_step_one_a_run_binds_the_real_tracker_to_its_scratch_root(
        self, deployment, tmp_path
    ):
        """Half one of a pair — see the test below it, which is the assertion
        that matters. This one only establishes the precondition: a run really
        does bind the process-wide singleton to a directory pytest deletes."""
        _run(_spec(lambda m: 0), deployment, tmp_path)

        import services.work_execution_state as wes

        assert is_under(wes.work_execution_tracker.state_dir, tmp_path / 'scratch')

    def test_step_two_the_restore_fixture_puts_the_real_tracker_back(self):
        """REGRESSION (#181 family): run_dry_run() imports
        services.work_execution_state for the FIRST time in this process, while
        ORCHESTRATOR_ROOT is the scratch dir, so its module-level singleton is
        constructed against a directory pytest deletes. The restore fixture
        built its save list from sys.modules.get(), which returns None for a
        module not yet imported — so there was nothing saved to put back, and
        every later test in the same process that touched the real singleton
        read and wrote a path that no longer existed.

        Deliberately paired with the test above and dependent on running after
        it: the restore only happens in that test's teardown, which is not
        observable from inside it."""
        import services.work_execution_state as wes

        expected_root = Path(os.environ.get('ORCHESTRATOR_ROOT', '/app'))
        assert Path(wes.work_execution_tracker.state_dir) == (
            expected_root / 'state' / 'execution_history'
        )


class TestReportRendering:
    def test_print_report_renders_every_report_shape(self, deployment, tmp_path, capsys):
        """print_report() is ~110 lines with no coverage, and a formatting crash
        in it destroys the run's entire output after the sweep already ran."""
        sweep_logger = logging.getLogger('synthetic_sweep')

        def _busy(manager):
            sweep_logger.info('Checking 3 execution state files for empty outputs')
            sweep_logger.debug('Watchdog: Skipping alpha/#1: work already in progress')
            sweep_logger.debug('Watchdog: Could not date alpha/#2 (None) -- age gate skipped: x')
            sweep_logger.error('Watchdog: Error processing beta_issue_7.yaml: boom')
            sweep_logger.info('a line nobody wrote a bucket for')
            path = manager.state_dir / 'alpha_issue_1.yaml'
            state = yaml.safe_load(path.read_text())
            state['execution_history'][-1]['outcome'] = 'failure'
            path.write_text(yaml.dump(state))
            return 1

        spec = _spec(
            _busy,
            gates=(
                Gate('PROTECTION 1', 'work in progress', (r'work already in progress',)),
                Gate('INFO: undated', 'age gate skipped',
                     (r'Could not date \S+/#\d+ .* age gate skipped',), degradation=True),
            ),
            examined=(r'Checking (\d+) execution state files',),
            terminal=(r'marking as failure to trigger retry',),
            owned=('execution_history',),
        )
        report = _run(spec, deployment, tmp_path)
        print_report(report)

        out = capsys.readouterr().out
        assert 'RECORDS THIS SWEEP WOULD MUTATE' in out
        assert 'alpha/#1' in out
        assert 'ERRORS raised inside the sweep' in out
        assert 'an annotation, not a disposition' in out

    def test_print_report_renders_an_aborted_copy_verification(
        self, deployment, tmp_path, monkeypatch, capsys
    ):
        def _lying_copy(src, dst, *args, **kwargs):
            Path(dst).parent.mkdir(parents=True, exist_ok=True)
            Path(dst).write_text('not the source contents')
            return dst

        monkeypatch.setattr('scripts.dry_run_state_sweep.shutil.copy2', _lying_copy)
        report = _run(_spec(lambda m: 0), deployment, tmp_path)
        print_report(report)

        out = capsys.readouterr().out
        assert 'COPY VERIFICATION FAILED' in out
        assert 'ABORTED' in out

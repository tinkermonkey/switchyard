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
    Gate,
    IsolationError,
    SweepSpec,
    classify_records,
    copy_tree_from_manifest,
    describe_execution_record_changes,
    diff_manifests,
    is_under,
    main,
    manifest_digest,
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


def _spec(run, resolved_dir=None, gates=(), examined=(), terminal=()):
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
    for module_name, singleton_name, path_attr, _subdir in _RUNTIME_SINGLETONS:
        module = sys.modules.get(module_name)
        if module is None:
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

    def test_allow_concurrent_writes_downgrades_drift_but_never_a_leak(
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

    def test_a_sweep_that_writes_outside_state_is_refused_by_default(self, capsys):
        """The harness checksums state/ and nothing else. A sweep that also
        writes to production Redis cannot be proven harmless by this run, so it
        must not be dry-run as though it could be — cleanup_stuck_in_progress_states()
        claims a cleanup key per candidate, which suppresses the REAL sweep for
        the claim TTL."""
        writing = [name for name, spec in SWEEPS.items() if spec.external_writes]
        assert writing, 'this test needs at least one sweep with declared writes'

        assert main(['--sweep', writing[0]]) == 2
        assert 'REFUSING to run' in capsys.readouterr().out

    def test_an_unknown_sweep_is_a_usage_error(self):
        with pytest.raises(SystemExit) as excinfo:
            main(['--sweep', 'no-such-sweep'])
        assert excinfo.value.code == 2

    def test_a_deployment_root_without_state_is_a_usage_error(self, tmp_path, capsys):
        assert main(['--sweep', 'empty_output_watchdog', '--deployment-root', str(tmp_path)]) == 2
        assert 'No state/ directory' in capsys.readouterr().out

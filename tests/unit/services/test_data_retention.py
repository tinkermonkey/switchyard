"""Age-based retention for the directories nothing ever cleaned.

Measured on the live deployment before this existed: 9.2GB of unrotated logs,
191 daily metrics files back to November, 76 container-failure logs back to
July, and 69 repair-cycle scratch directories back to October -- against 31MB
of actual state. Every one of them was write-only.

The tests that matter here are the ones about what the sweep must NOT do. A
retention sweep that deletes slightly too little is invisible; one that deletes
slightly too much removes the evidence of whatever incident someone is about to
investigate.
"""

import os
import time
from pathlib import Path

import pytest

from config.retention import RETENTION_DAYS  # noqa: E402
from services.data_retention import (  # noqa: E402
    RETENTION_RULES,
    RetentionRule,
    _execution_history_records,
    _execution_still_running,
    _repair_cycle_issue_dirs,
    _same_directory,
    resolve_roots,
    run_scheduled_sweep,
    sweep,
    sweep_rule,
)

# One representative entry per rule, at the path its WRITER actually uses --
# copied from the writer rather than from the rule, so a rule pointed at the
# wrong depth or the wrong directory shows up as a failure instead of as a
# quiet zero. `pipeline_context_scratch` shipped selecting two levels down
# against a tree that is one level deep: it examined nothing, reported a clean
# directory, and every test passed.
RULE_FIXTURES = {
    # rule name: (a path that must expire, a sibling entry that must survive)
    #
    # claude/docker_runner.py writes <container>.log here
    'container_failure_logs': (
        'orchestrator_data/logs/container-failures/claude-agent-demo-1.log',
        'orchestrator_data/logs/container-failures/claude-agent-demo-2.log',
    ),
    # services/agent_container_recovery.py: repair_cycles/<project>/<issue>/
    'repair_cycle_scratch': (
        'orchestrator_data/repair_cycles/demo/100/context.json',
        'orchestrator_data/repair_cycles/demo/200/context.json',
    ),
    # monitoring/metrics.py
    'metrics_backup': (
        'orchestrator_data/metrics/task_metrics_2026-01-01.jsonl',
        'orchestrator_data/metrics/task_metrics_2026-02-02.jsonl',
    ),
    # written from inside an agent container: medic/advisor_reports/<project>/
    'medic_advisor_reports': (
        'orchestrator_data/medic/advisor_reports/demo/advisor_report_20260101.md',
        'orchestrator_data/medic/advisor_reports/demo/advisor_report_20260202.md',
    ),
    # services/conversational_session_state.py:get_state_file()
    'conversational_sessions': (
        'state/conversational_sessions/demo_issue_1.yaml',
        'state/conversational_sessions/demo_issue_2.yaml',
    ),
    # services/work_execution_state.py:get_state_file()
    'execution_history': (
        'state/execution_history/demo_issue_1.yaml',
        'state/execution_history/demo_issue_2.yaml',
    ),
    # config/state_manager.py's point-in-time copies
    'state_backups': (
        'state/projects/demo/github_state_backup_20260101_000000.yaml',
        'state/projects/demo/github_state_backup_20260202_000000.yaml',
    ),
    # claude/docker_runner.py's per-launch MCP config
    'agent_launch_scratch': (
        '.orchestrator/tmp/mcp_config_code_reviewer_abc123_1767568952.json',
        '.orchestrator/tmp/mcp_config_code_reviewer_def456_1767568953.json',
    ),
    # services/pipeline_context_writer.py:setup() -- <issue>_<run prefix>/
    'pipeline_context_scratch': (
        '.orchestrator/tmp/pipeline_context/123_0707c3be/initial_request.md',
        '.orchestrator/tmp/pipeline_context/456_0ac09792/initial_request.md',
    ),
}

# A finished execution record, so the execution_history rule's `keep` veto
# (which holds back anything still in_progress) does not mask the sweep.
FINISHED_RECORD = (
    "issue_number: 1\n"
    "project_name: demo\n"
    "execution_history:\n"
    "  - agent: code_reviewer\n"
    "    outcome: success\n"
)


DAY = 86400


def _aged_file(path: Path, days_old: float, content: str = 'x') -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    when = time.time() - days_old * DAY
    os.utime(path, (when, when))
    return path


def _rule(**overrides) -> RetentionRule:
    base = dict(
        name='test_rule',
        relative_path='data/things',
        entries=lambda root: sorted(p for p in root.glob('*.log') if p.is_file()),
        description='things',
    )
    base.update(overrides)
    return RetentionRule(**base)


class TestAgeBoundary:

    def test_entries_older_than_the_window_are_removed(self, tmp_path):
        old = _aged_file(tmp_path / 'data/things/old.log', days_old=30)
        fresh = _aged_file(tmp_path / 'data/things/fresh.log', days_old=1)

        outcome = sweep_rule(_rule(), tmp_path, apply=True)

        assert outcome.removed == [old]
        assert not old.exists()
        assert fresh.exists()

    def test_an_entry_exactly_at_the_boundary_is_kept(self, tmp_path):
        """Strictly older-than, so a boundary rounding error keeps rather than
        deletes."""
        now = time.time()
        edge = _aged_file(tmp_path / 'data/things/edge.log', days_old=RETENTION_DAYS)
        os.utime(edge, (now - RETENTION_DAYS * DAY, now - RETENTION_DAYS * DAY))

        outcome = sweep_rule(_rule(), tmp_path, apply=True, now=now)

        assert outcome.removed == []
        assert edge.exists()

    def test_report_mode_deletes_nothing(self, tmp_path):
        """The default for both the script and any new caller."""
        old = _aged_file(tmp_path / 'data/things/old.log', days_old=90)

        outcome = sweep_rule(_rule(), tmp_path, apply=False)

        assert outcome.expired == [old]
        assert outcome.removed == []
        assert old.exists()
        assert outcome.bytes_removed > 0, "report mode still reports the size"


class TestFailureHandling:

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        outcome = sweep_rule(_rule(), tmp_path, apply=True)
        assert outcome.missing is True
        assert outcome.errors == []

    def test_one_undeletable_entry_does_not_abandon_the_sweep(self, tmp_path, monkeypatch):
        """This runs unattended every night; a single stuck file must not mean
        the rest of the backlog is never cleared."""
        first = _aged_file(tmp_path / 'data/things/a.log', days_old=30)
        second = _aged_file(tmp_path / 'data/things/b.log', days_old=30)
        third = _aged_file(tmp_path / 'data/things/c.log', days_old=30)

        real_unlink = Path.unlink

        def flaky(self, *a, **kw):
            if self.name == 'b.log':
                raise OSError("permission denied")
            return real_unlink(self, *a, **kw)

        monkeypatch.setattr(Path, 'unlink', flaky)

        outcome = sweep_rule(_rule(), tmp_path, apply=True)

        assert sorted(p.name for p in outcome.removed) == ['a.log', 'c.log']
        assert second.exists()
        assert any('b.log' in e for e in outcome.errors)
        assert not first.exists() and not third.exists()

    def test_an_unstattable_entry_is_left_alone_not_assumed_old(self, tmp_path, monkeypatch):
        """"Could not read its age" must never resolve to "delete it"."""
        entry = _aged_file(tmp_path / 'data/things/mystery.log', days_old=365)

        import services.data_retention as dr
        monkeypatch.setattr(dr, '_entry_mtime', lambda p: None)

        outcome = sweep_rule(_rule(), tmp_path, apply=True)

        assert outcome.removed == []
        assert entry.exists()
        assert any('mystery.log' in e for e in outcome.errors)

    def test_sweep_never_raises(self, tmp_path):
        """A listing that blows up is reported, not propagated -- the caller is
        an unattended scheduler job."""
        def explode(root):
            raise OSError("disk on fire")

        (tmp_path / 'data/things').mkdir(parents=True)

        outcomes = sweep(
            root=tmp_path, apply=True,
            rules=(_rule(entries=explode),),
        )

        assert len(outcomes) == 1
        assert outcomes[0].missing is False
        assert any('disk on fire' in e for e in outcomes[0].errors)
        assert outcomes[0].removed == []

    def test_a_rule_that_raises_anything_does_not_abandon_the_rules_after_it(
        self, tmp_path
    ):
        """"Never raises" has to mean any exception, not just OSError.

        A selector with a plain bug in it -- an AttributeError, a TypeError --
        used to propagate out of sweep() and be caught by the scheduler
        wrapper, which looks identical to success: every rule listed after the
        broken one silently stopped running, for as long as the bug lived.
        """
        def explode(root):
            raise RuntimeError("not an OSError")

        doomed = _aged_file(tmp_path / 'data/things/old.log', days_old=400)
        (tmp_path / 'data/broken').mkdir(parents=True)

        outcomes = sweep(
            root=tmp_path, apply=True,
            rules=(
                _rule(name='broken', relative_path='data/broken', entries=explode),
                _rule(name='after'),
            ),
        )

        assert len(outcomes) == 2, "the second rule must still have run"
        assert any('not an OSError' in e for e in outcomes[0].errors)
        assert outcomes[1].removed == [doomed]
        assert not doomed.exists()


class TestRepairCycleDirectories:
    """The one rule whose entries are directories, not files."""

    def test_the_issue_directory_is_the_unit_not_the_project(self, tmp_path):
        root = tmp_path / 'repair_cycles'
        _aged_file(root / 'proj-a/100/context.json', days_old=90)
        _aged_file(root / 'proj-a/200/context.json', days_old=1)

        entries = list(_repair_cycle_issue_dirs(root))

        assert [p.name for p in entries] == ['100', '200']

    def test_a_directory_ages_from_its_NEWEST_file(self, tmp_path):
        """A repair cycle writes context.json at launch and result.json at the
        end. Ageing from the oldest would expire a run that only just
        finished."""
        rule = _rule(
            relative_path='repair_cycles',
            entries=_repair_cycle_issue_dirs,
        )
        issue_dir = tmp_path / 'repair_cycles/proj/100'
        _aged_file(issue_dir / 'context.json', days_old=90)
        _aged_file(issue_dir / 'result.json', days_old=2)

        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert outcome.removed == []
        assert issue_dir.exists()

    def test_an_expired_directory_is_removed_whole(self, tmp_path):
        rule = _rule(
            relative_path='repair_cycles',
            entries=_repair_cycle_issue_dirs,
        )
        stale = tmp_path / 'repair_cycles/proj/100'
        _aged_file(stale / 'context.json', days_old=90)
        _aged_file(stale / 'result.json', days_old=88)
        kept = tmp_path / 'repair_cycles/proj/200'
        _aged_file(kept / 'context.json', days_old=3)

        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert not stale.exists()
        assert kept.exists()
        assert outcome.bytes_removed > 0


class TestConfiguredRules:

    def test_every_rule_uses_the_one_configured_window(self):
        """The whole point of this change: there is no per-location number.

        Eleven disagreeing windows is how the metrics JSONL "backup" came to be
        kept 90 days against an Elasticsearch original deleted after 7."""
        for rule in RETENTION_RULES:
            assert rule.retention_days == RETENTION_DAYS, rule.name

    def test_live_state_directories_are_not_swept(self):
        """Aging these would be destructive, not tidy.

        A pipeline lock, a queue entry, a verified dev-container record and a
        board's node IDs describe what is true NOW -- none is less valid for
        being three months old. What they accumulate is entries for projects
        whose config is gone, and that is orphan detection's job
        (scripts/inspect_project_state.py), keyed on the config list rather
        than on a clock."""
        swept = {r.relative_path for r in RETENTION_RULES}
        for live in (
            'state/pipeline_locks',
            'state/pipeline_queues',
            'state/dev_containers',
        ):
            assert live not in swept, (
                f"{live} holds live state; age is not a reason to delete from it"
            )

    def test_execution_history_sweeps_records_but_never_lock_sidecars(self, tmp_path):
        """0 bytes each, and deleting one a process holds breaks the mutual
        exclusion it exists to provide."""
        history = tmp_path / 'state/execution_history'
        record = _aged_file(history / 'proj_issue_1.yaml', days_old=365)
        sidecar = _aged_file(history / 'proj_issue_1.yaml.lock', days_old=365, content='')

        entries = list(_execution_history_records(history))
        assert entries == [record]

        rule = next(r for r in RETENTION_RULES if r.name == 'execution_history')
        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert outcome.removed == [record]
        assert sidecar.exists()

    def test_state_backups_are_swept_but_the_live_state_file_is_not(self, tmp_path):
        """github_state.yaml is the only local record of a project's board and
        column node IDs. Its point-in-time copies age; it does not."""
        project = tmp_path / 'state/projects/demo'
        live = _aged_file(project / 'github_state.yaml', days_old=365)
        backup = _aged_file(project / 'github_state_backup_20250101_000000.yaml',
                            days_old=365)

        rule = next(r for r in RETENTION_RULES if r.name == 'state_backups')
        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert outcome.removed == [backup]
        assert live.exists()

    def test_the_rotating_log_files_are_not_swept_by_this(self):
        """orchestrator_data/logs/*.log and each checkout's .repair_cycle.log are
        bounded by monitoring/log_rotation.py. A retention rule over the same
        files would race the handler that owns them, and aging out a live append
        target just truncates it at an arbitrary moment."""
        paths = [r.relative_path for r in RETENTION_RULES]
        assert 'orchestrator_data/logs' not in paths

    @pytest.mark.parametrize('name', [r.name for r in RETENTION_RULES])
    def test_each_rule_survives_a_sweep_of_an_empty_root(self, name, tmp_path):
        rule = next(r for r in RETENTION_RULES if r.name == name)
        outcome = sweep_rule(rule, tmp_path, apply=True)
        assert outcome.missing is True

    @pytest.mark.parametrize('name', [r.name for r in RETENTION_RULES])
    def test_each_rule_actually_matches_what_is_written_there(self, name, tmp_path):
        """A rule that selects nothing is invisible in every other test here.

        `missing is True` against an empty tmp_path passes just as happily for
        a rule pointed at the wrong directory or the wrong nesting depth, and
        the sweep then reports a clean directory forever while it grows. That
        is not hypothetical: pipeline_context_scratch shipped looking two
        levels down a tree that is one level deep, and examined 0 of 1,475
        directories on the live deployment.

        The fixture paths come from each rule's WRITER, so this asserts the
        rule agrees with the code that produces the files rather than with
        itself.
        """
        rule = next(r for r in RETENTION_RULES if r.name == name)
        expiring, surviving = RULE_FIXTURES[name]
        old = _aged_file(tmp_path / expiring, days_old=400, content=FINISHED_RECORD)
        fresh = _aged_file(tmp_path / surviving, days_old=1, content=FINISHED_RECORD)

        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert outcome.missing is False
        assert outcome.examined > 0, (
            f"{name} selected nothing from a directory laid out the way its "
            f"writer lays it out -- the rule is a no-op"
        )
        assert not old.exists(), f"{name} did not remove a 400-day-old entry"
        assert fresh.exists(), f"{name} removed a one-day-old entry"

    def test_no_rule_can_reach_live_state(self, tmp_path):
        """Behavioural, because the string check below cannot be the guard.

        A rule spelled `relative_path='state'` with a recursive selector sweeps
        every one of these and still passes a `'state/pipeline_locks' not in
        {paths}` assertion. What has to hold is that running the REAL rule set
        over a tree containing live state leaves it alone.
        """
        survivors = [
            _aged_file(tmp_path / 'state/pipeline_locks/demo_board.json', days_old=400),
            _aged_file(tmp_path / 'state/pipeline_locks/demo_board.json.lock',
                       days_old=400, content=''),
            _aged_file(tmp_path / 'state/pipeline_queues/demo_board.yaml', days_old=400),
            _aged_file(tmp_path / 'state/dev_containers/demo_verified.yaml', days_old=400),
            _aged_file(tmp_path / 'state/projects/demo/github_state.yaml', days_old=400),
            _aged_file(tmp_path / 'state/projects/demo/pr_review_state.yaml', days_old=400),
            _aged_file(tmp_path / 'state/execution_history/demo_issue_1.yaml.lock',
                       days_old=400, content=''),
            _aged_file(tmp_path / 'orchestrator_data/logs/orchestrator_all.log',
                       days_old=400),
            _aged_file(tmp_path / 'orchestrator_data/logs/orchestrator_all.log.1',
                       days_old=400),
        ]

        sweep(root=tmp_path, workspace_root=tmp_path, apply=True)

        for path in survivors:
            assert path.exists(), (
                f"{path.relative_to(tmp_path)} is live state or a rotating log "
                f"handler's own file; no retention rule may delete it"
            )

    def test_rule_names_are_unique(self):
        names = [r.name for r in RETENTION_RULES]
        assert len(names) == len(set(names))


class TestRootResolution:
    """Two roots, because in the container /app IS /workspace/switchyard."""

    def test_a_scratch_root_moves_the_workspace_rules_too(self, tmp_path):
        """Otherwise a test or a --root run would sweep the REAL /workspace --
        which holds every managed project checkout."""
        roots = resolve_roots(root=tmp_path)
        assert roots['orchestrator'] == tmp_path
        assert roots['workspace'] == tmp_path

    def test_the_two_roots_can_be_set_independently(self, tmp_path):
        roots = resolve_roots(root=tmp_path / 'app', workspace_root=tmp_path / 'ws')
        assert roots['orchestrator'] == tmp_path / 'app'
        assert roots['workspace'] == tmp_path / 'ws'

    def test_an_env_orchestrator_root_carries_the_workspace_rules_with_it(
        self, tmp_path, monkeypatch
    ):
        """The same protection as an explicit root=, for the spelling the test
        suite actually uses. Without it, a sweep with no arguments under
        ORCHESTRATOR_ROOT=/tmp/scratch would age that scratch tree for seven
        rules and the REAL /workspace -- every managed project checkout -- for
        the other two."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path))
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

        roots = resolve_roots()

        assert roots['orchestrator'] == tmp_path
        assert roots['workspace'] == tmp_path

    def test_an_env_workspace_root_is_honoured(self, tmp_path, monkeypatch):
        """Read at call time, not at import: a monkeypatch.setenv written by
        someone reaching for the obvious guard has to actually take effect."""
        monkeypatch.setenv('ORCHESTRATOR_ROOT', str(tmp_path / 'app'))
        monkeypatch.setenv('WORKSPACE_ROOT', str(tmp_path / 'ws'))

        roots = resolve_roots()

        assert roots['orchestrator'] == tmp_path / 'app'
        assert roots['workspace'] == tmp_path / 'ws'

    def test_the_production_defaults_apply_when_nothing_is_set(self, monkeypatch):
        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

        roots = resolve_roots()

        assert roots['orchestrator'] == Path('/app')
        assert roots['workspace'] == Path('/workspace')

    def test_the_script_and_the_nightly_job_resolve_the_same_roots(
        self, monkeypatch, capsys
    ):
        """The script used to materialise the ORCHESTRATOR_ROOT default itself,
        which made `root` non-None on every run and collapsed the workspace
        root onto it -- so a plain `inspect_data_retention.py` skipped both
        workspace rules while printing "Workspace root: /workspace" above the
        result."""
        import scripts.inspect_data_retention as script

        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)
        monkeypatch.setattr(script.sys, 'argv', ['inspect_data_retention.py', '--json'])

        captured = {}
        monkeypatch.setattr(
            script, 'sweep',
            lambda root, apply, workspace_root: captured.update(
                root=root, workspace_root=workspace_root
            ) or []
        )
        script.main()
        capsys.readouterr()

        assert captured == {
            'root': Path('/app'),
            'workspace_root': Path('/workspace'),
        }

    def test_a_workspace_rooted_rule_sweeps_under_the_workspace_root(self, tmp_path):
        old = _aged_file(
            tmp_path / 'ws/.orchestrator/tmp/mcp_config_agent_1.json', days_old=365
        )
        fresh = _aged_file(
            tmp_path / 'ws/.orchestrator/tmp/mcp_config_agent_2.json', days_old=1
        )

        outcomes = sweep(
            root=tmp_path / 'app',
            workspace_root=tmp_path / 'ws',
            apply=True,
        )

        by_name = {o.rule.name: o for o in outcomes}
        assert by_name['agent_launch_scratch'].removed == [old]
        assert fresh.exists()


class TestStillRunningRecordsAreNotDeleted:
    """An unfinished execution is live state, and this module does not age that.

    Deleting a record whose last entry is still `in_progress` flips
    has_active_execution() from True to False under a container that may well
    still be running, which un-blocks the issue for a duplicate dispatch. The
    stuck ones are resolved by cleanup_stuck_in_progress_states() instead --
    and resolving one rewrites the file, after which it ages normally.
    """

    def _record(self, outcome: str) -> str:
        return (
            "issue_number: 1\n"
            "project_name: demo\n"
            "execution_history:\n"
            "  - agent: code_reviewer\n"
            f"    outcome: {outcome}\n"
        )

    def test_a_finished_record_past_the_window_is_removed(self, tmp_path):
        record = _aged_file(
            tmp_path / 'state/execution_history/demo_issue_1.yaml',
            days_old=400, content=self._record('success'),
        )
        rule = next(r for r in RETENTION_RULES if r.name == 'execution_history')

        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert outcome.removed == [record]
        assert outcome.kept_live == 0

    def test_an_in_progress_record_past_the_window_is_kept(self, tmp_path):
        record = _aged_file(
            tmp_path / 'state/execution_history/demo_issue_1.yaml',
            days_old=400, content=self._record('in_progress'),
        )
        rule = next(r for r in RETENTION_RULES if r.name == 'execution_history')

        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert record.exists(), "an unfinished execution is live state"
        assert outcome.removed == []
        assert outcome.kept_live == 1

    def test_an_unreadable_record_is_kept_rather_than_assumed_finished(self, tmp_path):
        """Same policy as an unstattable entry: "I could not tell" keeps it."""
        record = _aged_file(
            tmp_path / 'state/execution_history/demo_issue_1.yaml',
            days_old=400, content='{[ not: valid: yaml',
        )
        rule = next(r for r in RETENTION_RULES if r.name == 'execution_history')

        outcome = sweep_rule(rule, tmp_path, apply=True)

        assert record.exists()
        assert outcome.kept_live == 1

    def test_the_veto_only_runs_for_entries_already_past_the_window(self, tmp_path):
        """Otherwise the nightly cost scales with the directory, not the backlog.

        There are ~4,700 of these files live; parsing all of them every night
        to decide nothing would be ~20 seconds of pointless work on a directory
        that is already bounded.
        """
        _aged_file(tmp_path / 'data/things/fresh.log', days_old=1)
        seen = []

        rule = _rule(keep=lambda p: seen.append(p) or False)
        sweep_rule(rule, tmp_path, apply=True)

        assert seen == [], "a fresh entry must not be parsed at all"


class TestTestIsolation:

    def test_applying_to_the_real_roots_from_a_test_is_refused(self, monkeypatch):
        """The whole suite runs with ORCHESTRATOR_ROOT pointed at scratch
        (issue #181). A sweep that resolves to /app or /workspace anyway has
        lost its isolation, and this is the first code in the tree that deletes
        under WORKSPACE_ROOT -- so it fails loudly rather than quietly emptying
        a production directory.

        `rules=()` for the same reason as in
        TestEverySpellingOfTheDeploymentIsRefused: with the env cleared this
        call resolves to the real /app and /workspace, so with the real rule
        set a regressed guard would not fail this test, it would run the
        deletion the test exists to prevent. The guard is checked before
        `rules` is iterated, so the assertion is unchanged."""
        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(apply=True, rules=())

        # Reporting is always safe, so it is never blocked. Scoped to one rule
        # pointed at a directory that does not exist, so the assertion does not
        # turn into a full walk of the live /app and /workspace trees.
        outcomes = sweep(apply=False, rules=(_rule(relative_path='nowhere-at-all'),))
        assert [o.missing for o in outcomes] == [True]


def _same_inode(a: str, b: str) -> bool:
    """Deliberately not services.data_retention._same_directory.

    The skip conditions below are built on this. If they were built on the
    function under test, a _same_directory() broken to always return False
    would make the tests that prove it works quietly skip instead of fail.
    """
    import os as _os
    try:
        sa, sb = _os.stat(a), _os.stat(b)
    except OSError:
        return False
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


_APP_EXISTS = Path('/app').is_dir()
_BOTH_DEPLOYMENT_PATHS_EXIST = (
    Path('/app').is_dir() and Path('/workspace/switchyard').is_dir()
)
_APP_IS_MOUNTED_TWICE = _same_inode('/app', '/workspace/switchyard')
_NO_DEPLOYMENT = (
    'not running in the deployment container: /app and /workspace/switchyard '
    'do not both exist here'
)
_NOT_THE_DEPLOYMENT = (
    'not running in the deployment container: /app and /workspace/switchyard '
    'are not the same directory here'
)


class TestEverySpellingOfTheDeploymentIsRefused:
    """The guard in front of shutil.rmtree compares identity, not path text.

    Measured in the live container: /app and /workspace/switchyard are both
    st_dev=66311 st_ino=12583264 -- one directory under two names, because
    docker-compose mounts the checkout twice (./:/app and ..:/workspace).

    With the previous `Path(resolved) in _PROTECTED_ROOTS`, the five spellings
    of that one directory measured as:

        /app                   refused
        /app/                  refused
        /workspace/switchyard  NOT refused
        /app/../app            NOT refused
        .   (cwd /app)         NOT refused

    so three of five walked past the only thing standing between an
    unisolated test and a recursive delete of the deployment.

    Every refusal test below passes `rules=()` (the one test that must NOT be
    refused, test_a_scratch_root_is_still_swept, keeps the real rule set and is
    pointed at tmp_path), and that is load-bearing rather than tidiness. These
    refusal tests point a sweep at the LIVE deployment
    roots on purpose; with the real rule set they are safe only for exactly as
    long as the guard they exist to test works, and the moment it regresses the
    test does not fail -- it performs the deletion it was written to prevent,
    on production. sweep() checks the guard before it iterates `rules`
    (test_the_guard_is_checked_before_any_rule_is_swept pins that), so an empty
    tuple leaves the assertion byte-identical and leaves a regression harmless.

    Measured with the guard neutered in-process and sweep_rule replaced by a
    recorder: `rules=RETENTION_RULES` dispatched 9 rules rooted at
    /workspace/switchyard; `rules=()` dispatched 0. With the guard intact,
    `rules=()` still raises the same RuntimeError.
    """

    @pytest.fixture(autouse=True)
    def _no_scratch_root(self, monkeypatch):
        """Every test here passes an explicit root; clearing the env just
        keeps which of the two roots trips the guard deterministic."""
        monkeypatch.delenv('ORCHESTRATOR_ROOT', raising=False)
        monkeypatch.delenv('WORKSPACE_ROOT', raising=False)

    @pytest.mark.skipif(not _BOTH_DEPLOYMENT_PATHS_EXIST, reason=_NO_DEPLOYMENT)
    def test_the_checkout_really_is_mounted_at_two_paths(self):
        """The measured fact the identity comparison exists for.

        Gated on the two paths EXISTING, deliberately not on them being the
        same inode. Gating it on _APP_IS_MOUNTED_TWICE -- which is what the
        first version of this did -- makes the skip condition the exact
        negation of the assertion, so the test can only pass or skip and can
        never report the thing its docstring promises to report. Confirmed by
        running that shape against two real non-aliased directories: pytest
        said `1 skipped`, not `1 failed`.

        So: if the compose mounts ever change and the checkout stops being
        mounted twice, this FAILS, and the reader is told that
        test_the_workspace_switchyard_spelling_is_refused and
        test_a_second_mount_is_caught_even_when_it_is_not_in_the_list -- which
        do have to skip, because their case no longer exists -- have gone
        quiet and the (st_dev, st_ino) half of the guard is now uncovered.
        """
        app, ws = os.stat('/app'), os.stat('/workspace/switchyard')
        assert (app.st_dev, app.st_ino) == (ws.st_dev, ws.st_ino), (
            "/app and /workspace/switchyard are no longer one directory. The "
            "identity half of _is_protected_root() is now untested: the two "
            "tests gated on _APP_IS_MOUNTED_TWICE are skipping."
        )

    @pytest.mark.skipif(not _APP_IS_MOUNTED_TWICE, reason=_NOT_THE_DEPLOYMENT)
    def test_the_workspace_switchyard_spelling_is_refused(self):
        """The bind-mount alias, and the reason a textual comparison is not
        enough: resolve() cannot collapse this one, because it is a second
        mount rather than a symlink. Only (st_dev, st_ino) catches it."""
        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=Path('/workspace/switchyard'), apply=True, rules=())

    def test_a_dot_dot_spelling_is_refused(self):
        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=Path('/app/../app'), apply=True, rules=())

    @pytest.mark.skipif(not _APP_EXISTS, reason='no /app to chdir into')
    def test_a_relative_root_with_the_cwd_at_the_deployment_is_refused(
        self, monkeypatch
    ):
        monkeypatch.chdir('/app')
        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=Path('.'), apply=True, rules=())

    @pytest.mark.skipif(not _APP_EXISTS, reason='no /app to link to')
    def test_a_symlink_to_a_protected_root_is_refused(self, tmp_path):
        link = tmp_path / 'deployment'
        link.symlink_to('/app')
        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=link, apply=True, rules=())

    def test_the_workspace_root_is_checked_as_well_as_the_orchestrator_one(
        self, tmp_path
    ):
        """Both entries of the roots dict go through the guard. The
        workspace-rooted rules are the ones that delete under every managed
        project checkout, so an alias reaching only that one still matters."""
        with pytest.raises(RuntimeError, match='workspace root'):
            sweep(
                root=tmp_path,
                workspace_root=Path('/workspace/../workspace'),
                apply=True,
                rules=(),
            )

    @pytest.mark.skipif(not _APP_IS_MOUNTED_TWICE, reason=_NOT_THE_DEPLOYMENT)
    def test_a_second_mount_is_caught_even_when_it_is_not_in_the_list(
        self, monkeypatch
    ):
        """Isolates the identity half of the comparison.

        _PROTECTED_ROOTS names /workspace/switchyard as well as /app, so the
        test above passes on the name alone -- which would leave
        (st_dev, st_ino) untested and the next bind mount nobody thought to
        list unguarded. With only /app in the list, the alias must still be
        refused, and nothing else in _is_protected_root() can do it: the two
        paths share no prefix and neither is a symlink, so resolve() cannot
        turn one into the other.
        """
        import services.data_retention as data_retention

        monkeypatch.setattr(data_retention, '_PROTECTED_ROOTS', (Path('/app'),))

        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=Path('/workspace/switchyard'), apply=True, rules=())

    def test_an_alias_of_a_protected_root_that_does_not_exist_is_refused(
        self, monkeypatch
    ):
        """Isolates the resolve-then-compare-by-name half.

        Resolving is what turns '..', '.' and a symlink back into the
        protected name; with nothing on disk to stat, it is the only thing
        that can. So the two comparisons are not redundant -- each one is the
        only cover for a case the other misses.
        """
        import services.data_retention as data_retention

        absent = Path('/no-such-deployment-root')
        assert not absent.exists()
        monkeypatch.setattr(data_retention, '_PROTECTED_ROOTS', (absent,))

        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(
                root=Path('/no-such-deployment-root/../no-such-deployment-root'),
                apply=True,
                rules=(),
            )

    def test_a_protected_root_that_does_not_exist_is_still_refused(
        self, monkeypatch
    ):
        """Identity cannot stat what is not there. Without the textual
        comparison beside it, running this suite anywhere without an /app --
        on the host, in CI -- would turn a loud refusal into a silent pass,
        which is the worse of the two failures for a guard in front of
        rmtree."""
        import services.data_retention as data_retention

        absent = Path('/no-such-deployment-root')
        assert not absent.exists()
        monkeypatch.setattr(data_retention, '_PROTECTED_ROOTS', (absent,))

        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=absent, apply=True, rules=())

    def test_the_guard_is_checked_before_any_rule_is_swept(self, monkeypatch):
        """The fact that lets every refusal test above pass `rules=()`.

        If the guard ever moved below the loop, `rules=()` would still make
        those tests pass while production went unguarded -- so the ordering has
        to be pinned somewhere, and it cannot be pinned by a test that lets a
        real sweep run. sweep_rule is replaced by a recorder here, so this is
        the one place the full RETENTION_RULES set is handed a live deployment
        root and still nothing can be deleted whatever the guard does.
        """
        import services.data_retention as data_retention

        swept = []
        monkeypatch.setattr(
            data_retention, 'sweep_rule',
            lambda rule, root, **kw: swept.append((rule.name, str(root))),
        )

        with pytest.raises(RuntimeError, match='Refusing to apply'):
            sweep(root=Path('/app/../app'), apply=True, rules=RETENTION_RULES)

        assert swept == [], (
            "the guard must refuse before sweep_rule is reached -- otherwise "
            "the rules=() in the tests above is hiding a real sweep of the "
            f"deployment, not preventing one. Reached: {swept}"
        )

    def test_same_directory_compares_the_inode_and_not_the_name(self, tmp_path):
        real = tmp_path / 'real'
        real.mkdir()
        link = tmp_path / 'link'
        link.symlink_to(real)
        other = tmp_path / 'other'
        other.mkdir()

        assert _same_directory(link, real), 'two names, one inode'
        assert not _same_directory(other, real), 'two inodes'
        assert not _same_directory(tmp_path / 'absent', real), 'nothing to stat'

    def test_a_scratch_root_is_still_swept(self, tmp_path):
        """The guard has to refuse the deployment and nothing else. An
        over-broad version of it -- one that treats any ancestor relationship
        as a match, say -- would refuse tmp_path too and take every other
        apply=True test in this file down with it."""
        sweep(root=tmp_path, apply=True)


class TestTheSingleConfiguredValue:

    def test_a_malformed_value_falls_back_rather_than_failing_startup(self, monkeypatch):
        import importlib
        import config.retention as retention

        monkeypatch.setenv('RETENTION_DAYS', 'not-a-number')
        reloaded = importlib.reload(retention)
        try:
            assert reloaded.RETENTION_DAYS == 30
        finally:
            monkeypatch.delenv('RETENTION_DAYS', raising=False)
            importlib.reload(retention)

    def test_zero_is_refused(self, monkeypatch):
        """0 means "delete immediately" to ILM and "delete everything" to the
        file sweep. It must never be reachable by typo."""
        import importlib
        import config.retention as retention

        monkeypatch.setenv('RETENTION_DAYS', '0')
        reloaded = importlib.reload(retention)
        try:
            assert reloaded.RETENTION_DAYS == 30
        finally:
            monkeypatch.delenv('RETENTION_DAYS', raising=False)
            importlib.reload(retention)

    def test_a_valid_value_reaches_both_halves(self, monkeypatch):
        import importlib
        import config.retention as retention

        monkeypatch.setenv('RETENTION_DAYS', '7')
        reloaded = importlib.reload(retention)
        try:
            assert reloaded.RETENTION_DAYS == 7
            policy = reloaded.build_ilm_policy()
            assert policy['policy']['phases']['delete']['min_age'] == '7d'
        finally:
            monkeypatch.delenv('RETENTION_DAYS', raising=False)
            importlib.reload(retention)

    def test_the_warm_phase_stays_strictly_below_delete_at_every_window(self, monkeypatch):
        """Elasticsearch rejects a policy whose phases are not strictly
        increasing, and a rejected put means NO retention is applied at all --
        a far worse outcome than losing a performance tier. A hard-coded
        "warm at 7d" would do exactly that at RETENTION_DAYS=3."""
        import importlib
        import config.retention as retention

        try:
            for days in ('1', '2', '3', '4', '7', '30', '365'):
                monkeypatch.setenv('RETENTION_DAYS', days)
                reloaded = importlib.reload(retention)
                phases = reloaded.build_ilm_policy()['policy']['phases']
                assert phases['delete']['min_age'] == f'{days}d'
                if 'warm' in phases:
                    warm = int(phases['warm']['min_age'].rstrip('d'))
                    assert 0 < warm < int(days), f"warm={warm} delete={days}"
        finally:
            monkeypatch.delenv('RETENTION_DAYS', raising=False)
            importlib.reload(retention)

    def test_extra_hot_actions_merge_rather_than_replace(self):
        """The metrics families roll over on size/age as well as on date -- the
        one genuine difference between any two policies."""
        from config.retention import build_ilm_policy

        policy = build_ilm_policy(
            hot_actions={"rollover": {"max_age": "1d", "max_size": "5gb"}}
        )
        hot = policy['policy']['phases']['hot']['actions']
        assert 'rollover' in hot
        assert hot['set_priority'] == {'priority': 100}


class TestScheduledEntryPoint:

    def test_the_scheduled_sweep_applies_and_does_not_raise(self, tmp_path):
        old = _aged_file(
            tmp_path / 'orchestrator_data/logs/container-failures/old.log',
            days_old=365,
        )

        outcomes = run_scheduled_sweep(root=tmp_path)

        assert not old.exists()
        assert any(o.removed for o in outcomes)


class TestElasticsearchParity:
    """Every ILM policy resolves the same window as the file sweep.

    This is the half that is easy to let rot. A filesystem rule that disagrees
    with itself is visible in one file; an ILM policy that disagrees with the
    filesystem lives in a different module, is written as a nested dict, and is
    only observable by querying a running cluster. Before this, eight policies
    had hand-written ages of 7d, 14d, 30d and 180d, and ten indices had no
    policy at all.
    """

    def _all_policies(self):
        from monitoring.observability import DECISION_EVENTS_ILM_POLICY
        from services.agent_container_recovery import REPAIR_CYCLE_RECOVERY_ILM_POLICY
        from services.pattern_detection_schema import (
            AGENT_LOGS_ILM_POLICY,
            CLAUDE_OTEL_ILM_POLICY,
            PROJECT_METRICS_ILM_POLICY,
            TEST_CYCLE_RECORDS_ILM_POLICY,
        )
        from services.pipeline_run import PIPELINE_RUNS_ILM_POLICY

        from monitoring.metrics import MetricsCollector  # noqa: F401  (import check)
        from config.retention import MONTHLY_INDEX_PERIOD_DAYS, build_ilm_policy

        # (policy body, the index period the family declares). A family whose
        # indices are named per month holds a month of data in one index, and
        # ILM ages an un-rolled index from its creation date -- so the delete
        # age is the window PLUS that period. See delete_phase_days().
        return {
            'decision-events': (DECISION_EVENTS_ILM_POLICY, 0),
            'repair-cycle-recovery': (REPAIR_CYCLE_RECOVERY_ILM_POLICY, 0),
            'agent-logs': (AGENT_LOGS_ILM_POLICY, 0),
            'claude-otel': (CLAUDE_OTEL_ILM_POLICY, 0),
            'pipeline-runs': (PIPELINE_RUNS_ILM_POLICY, 0),
            'project-metrics': (PROJECT_METRICS_ILM_POLICY, MONTHLY_INDEX_PERIOD_DAYS),
            'test-cycle-records': (
                TEST_CYCLE_RECORDS_ILM_POLICY, MONTHLY_INDEX_PERIOD_DAYS
            ),
            # Built inline at call time by monitoring/metrics.py and
            # services/token_metrics_service.py rather than held in a module
            # constant, so reproduce the exact calls those sites make. Without
            # these two the parity check simply does not cover them.
            'orchestrator-metrics': (
                build_ilm_policy(
                    hot_actions={"rollover": {"max_age": "1d", "max_size": "5gb"}}
                ),
                0,
            ),
            'token-metrics': (
                build_ilm_policy(index_period_days=MONTHLY_INDEX_PERIOD_DAYS),
                MONTHLY_INDEX_PERIOD_DAYS,
            ),
        }

    def test_every_ilm_policy_deletes_at_the_configured_window(self):
        from config.retention import delete_phase_days

        for name, (policy, period) in self._all_policies().items():
            actual = policy['policy']['phases']['delete']['min_age']
            expected = f'{delete_phase_days(period)}d'
            assert actual == expected, (
                f"{name} deletes at {actual}, not the {expected} its declared "
                f"index period and the configured {RETENTION_DAYS}d window imply"
            )

    def test_no_ilm_policy_deletes_sooner_than_the_configured_window(self):
        """The direction that loses data.

        Erring long costs disk; erring short deletes records from inside the
        window we told the operator we were keeping them for -- which for a
        monthly index is what a flat 30d does, taking that morning's writes
        along with the rest of the month.
        """
        for name, (policy, _) in self._all_policies().items():
            days = int(policy['policy']['phases']['delete']['min_age'].rstrip('d'))
            assert days >= RETENTION_DAYS, (
                f"{name} deletes after {days}d, inside the configured "
                f"{RETENTION_DAYS}d window"
            )

    def test_ilm_and_the_file_sweep_agree(self):
        """The one assertion this whole change exists to make true.

        Every window on both sides has to be RETENTION_DAYS plus the index
        period of whatever holds it -- which is zero for the filesystem, where
        an entry is its own unit.
        """
        from config.retention import delete_phase_days

        derived = {
            f'{delete_phase_days(period)}d'
            for _, period in self._all_policies().values()
        }
        derived |= {f'{r.retention_days}d' for r in RETENTION_RULES}

        actual = {
            p['policy']['phases']['delete']['min_age']
            for p, _ in self._all_policies().values()
        }
        actual |= {f'{r.retention_days}d' for r in RETENTION_RULES}

        assert actual == derived, (
            f"Elasticsearch and the filesystem disagree: {sorted(actual)} "
            f"against {sorted(derived)} derived from RETENTION_DAYS"
        )
        assert f'{RETENTION_DAYS}d' in actual, (
            "nothing resolves the base window -- RETENTION_DAYS has stopped "
            "reaching anything"
        )

    def test_no_module_hard_codes_an_ilm_phase_age_any_more(self):
        """A new literal min_age is how the eleven windows came back last time.

        Scoped to the phase dicts rather than the string "min_age" anywhere, so
        config/retention.py -- which is where the value legitimately lives --
        is exempt by path rather than by pattern.
        """
        import re

        root = Path(__file__).parent.parent.parent.parent
        exempt = {'config/retention.py'}
        offenders = []
        for source in root.rglob('*.py'):
            relative = source.relative_to(root)
            parts = relative.parts
            if parts[0] in ('tests', '.claude', 'node_modules', 'venv', '.venv'):
                continue
            if str(relative) in exempt:
                continue
            text = source.read_text(errors='ignore')
            for match in re.finditer(r'["\']min_age["\']\s*:\s*["\']([^"\']+)["\']', text):
                if match.group(1) == '0ms':
                    continue
                offenders.append(f"{relative}: min_age {match.group(1)}")

        assert offenders == [], (
            f"hard-coded ILM phase ages found: {offenders}. "
            f"Use config.retention.build_ilm_policy() instead."
        )

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
    _repair_cycle_issue_dirs,
    resolve_roots,
    run_scheduled_sweep,
    sweep,
    sweep_rule,
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

        return {
            'decision-events': DECISION_EVENTS_ILM_POLICY,
            'repair-cycle-recovery': REPAIR_CYCLE_RECOVERY_ILM_POLICY,
            'agent-logs': AGENT_LOGS_ILM_POLICY,
            'claude-otel': CLAUDE_OTEL_ILM_POLICY,
            'project-metrics': PROJECT_METRICS_ILM_POLICY,
            'test-cycle-records': TEST_CYCLE_RECORDS_ILM_POLICY,
            'pipeline-runs': PIPELINE_RUNS_ILM_POLICY,
        }

    def test_every_ilm_policy_deletes_at_the_configured_window(self):
        for name, policy in self._all_policies().items():
            actual = policy['policy']['phases']['delete']['min_age']
            assert actual == f'{RETENTION_DAYS}d', (
                f"{name} deletes at {actual}, not the configured "
                f"{RETENTION_DAYS}d"
            )

    def test_ilm_and_the_file_sweep_agree(self):
        """The one assertion this whole change exists to make true."""
        windows = {
            p['policy']['phases']['delete']['min_age']
            for p in self._all_policies().values()
        }
        windows |= {f'{r.retention_days}d' for r in RETENTION_RULES}
        assert windows == {f'{RETENTION_DAYS}d'}, (
            f"Elasticsearch and the filesystem disagree: {sorted(windows)}"
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

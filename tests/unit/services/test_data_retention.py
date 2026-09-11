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

from services.data_retention import (  # noqa: E402
    RETENTION_RULES,
    RetentionRule,
    _repair_cycle_issue_dirs,
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
        retention_days=14,
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
        edge = _aged_file(tmp_path / 'data/things/edge.log', days_old=14)
        os.utime(edge, (now - 14 * DAY, now - 14 * DAY))

        outcome = sweep_rule(_rule(retention_days=14), tmp_path, apply=True, now=now)

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
            retention_days=30,
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
            retention_days=30,
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

    def test_execution_history_is_not_swept(self):
        """Deliberate: it is the empty-output watchdog's corpus and the record
        that an issue was worked. Ageing it out is a behaviour change, not
        housekeeping, and its .yaml.lock sidecars must not be deleted at all.
        If a rule for it is ever added, that decision needs its own review --
        this assertion is the prompt for it."""
        paths = [r.relative_path for r in RETENTION_RULES]
        assert not any('execution_history' in p for p in paths)

    def test_the_rotating_log_files_are_not_swept_by_this(self):
        """orchestrator_data/logs/*.log is bounded by monitoring/log_rotation.py.
        A retention rule over the same files would race the handler that owns
        them."""
        paths = [r.relative_path for r in RETENTION_RULES]
        assert 'orchestrator_data/logs' not in paths

    def test_every_rule_has_a_positive_window(self):
        for rule in RETENTION_RULES:
            assert rule.retention_days > 0, rule.name

    @pytest.mark.parametrize('name', [r.name for r in RETENTION_RULES])
    def test_each_rule_survives_a_sweep_of_an_empty_root(self, name, tmp_path):
        rule = next(r for r in RETENTION_RULES if r.name == name)
        outcome = sweep_rule(rule, tmp_path, apply=True)
        assert outcome.missing is True


class TestEnvironmentOverrides:

    def test_a_bad_override_falls_back_rather_than_failing_startup(self, monkeypatch):
        import importlib
        import services.data_retention as dr

        monkeypatch.setenv('METRICS_BACKUP_RETENTION_DAYS', 'not-a-number')
        reloaded = importlib.reload(dr)
        try:
            assert reloaded.METRICS_BACKUP_RETENTION_DAYS == 90
        finally:
            monkeypatch.delenv('METRICS_BACKUP_RETENTION_DAYS', raising=False)
            importlib.reload(dr)

    def test_a_nonpositive_override_is_refused(self, monkeypatch):
        """0 would mean "delete everything, always"."""
        import importlib
        import services.data_retention as dr

        monkeypatch.setenv('CONTAINER_FAILURE_LOG_RETENTION_DAYS', '0')
        reloaded = importlib.reload(dr)
        try:
            assert reloaded.CONTAINER_FAILURE_LOG_RETENTION_DAYS == 14
        finally:
            monkeypatch.delenv('CONTAINER_FAILURE_LOG_RETENTION_DAYS', raising=False)
            importlib.reload(dr)


class TestScheduledEntryPoint:

    def test_the_scheduled_sweep_applies_and_does_not_raise(self, tmp_path):
        old = _aged_file(
            tmp_path / 'orchestrator_data/logs/container-failures/old.log',
            days_old=365,
        )

        outcomes = run_scheduled_sweep(root=tmp_path)

        assert not old.exists()
        assert any(o.removed for o in outcomes)

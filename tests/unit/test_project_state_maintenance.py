"""Project state directories: bounded backups, and state no config claims.

Two conditions in `state/projects/` that nothing in the codebase used to
notice, both found on the live deployment (#175's siblings):

  * `backup_state()` wrote one `github_state_backup_*.yaml` per project per
    reconciliation and never deleted one. Nothing has ever read one --
    `github_state_backup_` appeared exactly twice in the codebase, both in the
    write. 3,196 files / 31MB had accumulated, 612 for a single project.
  * Two project state directories had no config behind them:
    `agent_team_ansible` and `switchyard`, six and nine months untouched. The
    orchestrator had no opinion about them at all, in either direction.

The answer taken here is: report the orphans loudly and remove them only when
an operator types the name. Removing state on its own is not safe -- a config
can go missing without the project being decommissioned, and a reconciliation
that cannot see an existing board creates a duplicate rather than adopting it.

The backups are aged out by the shared retention sweep
(services/data_retention.py) on config/retention.py's single RETENTION_DAYS
window -- not bounded here, and deliberately not by count. See TestBackups.
"""

from pathlib import Path
from unittest.mock import MagicMock

import pytest

yaml = pytest.importorskip("yaml")

from config.state_manager import GitHubStateManager  # noqa: E402


def _manager(tmp_path: Path, configured_projects=(), declared_names=None):
    """A state manager rooted in tmp_path with a stubbed config manager.

    Never touches the real state tree: every test here writes and deletes
    files, and the deployment's state/ is live.
    """
    config_manager = MagicMock()
    config_manager.list_projects.return_value = list(configured_projects)

    declared = declared_names or {}

    def _get_project_config(stem):
        cfg = MagicMock()
        cfg.name = declared.get(stem, stem)
        return cfg

    config_manager.get_project_config.side_effect = _get_project_config
    return GitHubStateManager(state_root=str(tmp_path / "state"),
                              config_manager=config_manager)


def _seed_state(manager: GitHubStateManager, project: str) -> Path:
    state_file = manager._get_project_state_file(project)
    state_file.write_text(yaml.dump({'project_name': project, 'boards': {}}))
    return state_file


def _config_that_raises_for(bad_stem: str):
    """A get_project_config stub that raises for exactly one filename stem.

    Every other stem declares a project.name equal to itself, so the ONLY
    declared name this stub can never produce is the one inside the config that
    raises. A state directory named after that one is therefore claimed by
    nothing if the caller skips the failed config and carries on, and claimed by
    everything if the caller abandons the run -- which is what makes the
    difference between those two behaviours observable at all.
    """
    def _get(stem):
        if stem == bad_stem:
            raise ValueError("bad yaml")
        cfg = MagicMock()
        cfg.name = stem
        return cfg

    return _get


def _seed_backups(manager: GitHubStateManager, project: str, stamps) -> None:
    project_dir = manager.projects_state_dir / project
    project_dir.mkdir(parents=True, exist_ok=True)
    for stamp in stamps:
        (project_dir / f"github_state_backup_{stamp}.yaml").write_text("{}\n")


class TestBackups:
    """Backups accumulate here and are aged out by the shared retention sweep.

    They used to be bounded by COUNT ("keep the 10 newest"), which is not
    aging at all -- ten backups is four days on a busy project and nine months
    on a quiet one, and it would have deleted backups from inside the retention
    window that services/data_retention.py intends to keep. Two mechanisms with
    two answers for the same files is what the single RETENTION_DAYS value
    exists to prevent, so the count rule is gone and only listing remains.
    """

    def test_backup_state_writes_a_copy_and_leaves_the_rest_alone(self, tmp_path):
        manager = _manager(tmp_path, ['proj'])
        _seed_state(manager, 'proj')
        _seed_backups(manager, 'proj', [f"2026010{d}_120000" for d in range(1, 10)])

        manager.backup_state('proj')

        assert len(manager.list_state_backups('proj')) == 10, \
            "nothing is pruned on write -- ageing is the sweep's job"

    def test_backups_are_listed_newest_first_by_NAME_not_mtime(self, tmp_path):
        """backup_state() writes with shutil.copy2, which preserves the source
        file's mtime rather than recording when the copy was taken -- so every
        backup of an unchanged state file shares one mtime and mtime order
        carries no information. Seeded with all mtimes equal on purpose."""
        manager = _manager(tmp_path, ['proj'])
        stamps = [f"2026030{d}_090000" for d in range(1, 6)]
        _seed_backups(manager, 'proj', stamps)

        project_dir = manager.projects_state_dir / 'proj'
        # Stamped in the REVERSE of name order rather than all-equal. Equal
        # mtimes make an mtime-keyed sort fall back to glob order, which is
        # filesystem hash order -- it happens to differ from the expected answer
        # here, but only by luck. Reversing them means an mtime sort produces a
        # provably wrong order every time.
        import os
        for offset, stamp in enumerate(stamps):
            path = project_dir / f"github_state_backup_{stamp}.yaml"
            when = 1_700_000_000 - offset
            os.utime(path, (when, when))

        names = [p.name for p in manager.list_state_backups('proj')]

        assert names == [f"github_state_backup_{s}.yaml" for s in reversed(stamps)]

    def test_listing_an_unknown_project_is_a_no_op(self, tmp_path):
        manager = _manager(tmp_path)
        assert manager.list_state_backups('never-existed') == []

    def test_the_count_based_rule_is_gone(self):
        """Pinned so it cannot come back alongside the age-based sweep."""
        import config.state_manager as sm

        assert not hasattr(sm, 'STATE_BACKUP_RETENTION')
        assert not hasattr(sm.GitHubStateManager, 'prune_state_backups')


class TestOrphanedState:

    def test_state_without_a_config_is_reported(self, tmp_path):
        manager = _manager(tmp_path, ['live-project'])
        _seed_state(manager, 'live-project')
        _seed_state(manager, 'agent_team_ansible')

        assert manager.list_orphaned_project_state() == ['agent_team_ansible']

    def test_a_config_claims_its_state_under_its_declared_name_too(self, tmp_path):
        """Filename stem and project.name are two strings that usually match.

        Claiming under both is the safe direction: a missed orphan costs a line
        of output nobody reads; a false positive invites an operator to delete
        the live board state of a healthy project.
        """
        manager = _manager(
            tmp_path,
            configured_projects=['docs_robotics'],
            declared_names={'docs_robotics': 'documentation_robotics'},
        )
        _seed_state(manager, 'documentation_robotics')

        assert manager.list_orphaned_project_state() == []

    def test_only_directories_count_and_the_report_is_sorted(self, tmp_path):
        """A stray file in state/projects/ is not a project.

        Both halves were previously unpinned because every other assertion here
        is on a list of zero or one name. Without the is_dir() filter a
        `.DS_Store` or a `.tmp` left by an interrupted write is reported as an
        orphaned project, the operator script prints `--remove-orphan .DS_Store`
        under it, and remove_orphan() then calls shutil.rmtree on a file and
        dies with NotADirectoryError.
        """
        manager = _manager(tmp_path, ['live-project'])
        _seed_state(manager, 'live-project')
        _seed_state(manager, 'zeta-orphan')
        _seed_state(manager, 'alpha-orphan')
        (manager.projects_state_dir / '.DS_Store').write_text('')

        assert manager.list_orphaned_project_state() == [
            'alpha-orphan', 'zeta-orphan'
        ]

    def test_nothing_is_orphaned_when_the_config_list_cannot_be_read(self, tmp_path):
        """Every directory looks orphaned without a trustworthy config list."""
        manager = _manager(tmp_path, ['a'])
        _seed_state(manager, 'a')
        _seed_state(manager, 'b')
        manager.config_manager.list_projects.side_effect = RuntimeError("no configs dir")

        assert manager.list_orphaned_project_state() == []

    def test_detection_does_not_remove_anything(self, tmp_path):
        manager = _manager(tmp_path, ['live-project'])
        _seed_state(manager, 'live-project')
        state_file = _seed_state(manager, 'orphan')

        assert manager.list_orphaned_project_state() == ['orphan']
        assert manager.list_orphaned_project_state() == ['orphan']

        assert state_file.exists()

    def test_a_config_with_an_unreadable_body_stops_the_whole_run(self, tmp_path):
        """A broken config is not a decommission -- for ANY project, not just its own.

        Skipping only the config that failed to load looks like the careful
        thing and is the opposite. The loop that reads each config exists
        because a config's filename stem and its declared project.name are
        different strings and the state directory is named after the DECLARED
        one. Drop the declared name for a config that will not parse and that
        project's live state directory is reported orphaned -- under a name the
        failed config was the only thing that could have claimed.

        So the state directory below is named after nothing in the config list:
        it is claimed only by the declared name of the config that raised.
        """
        manager = _manager(
            tmp_path,
            configured_projects=['healthy', 'broken-file-stem'],
        )
        _seed_state(manager, 'healthy')
        _seed_state(manager, 'a-live-project')

        manager.config_manager.get_project_config.side_effect = (
            _config_that_raises_for('broken-file-stem')
        )

        # 'a-live-project' is claimed by NO filename stem. The only thing that
        # could have claimed it is the declared project.name inside
        # broken-file-stem.yaml -- the config that will not parse. Skip that one
        # config and carry on, and this live project's board state is reported
        # orphaned with a removal command under it. A one-config fixture cannot
        # tell `continue` from `return []`; this one can.
        assert manager.list_orphaned_project_state() == []

    def test_an_unreadable_config_is_logged_not_swallowed(self, tmp_path, caplog):
        """The suppression must say why; silence here reads as 'all clear'."""
        import logging

        manager = _manager(tmp_path, ['broken'])
        _seed_state(manager, 'broken')
        manager.config_manager.get_project_config.side_effect = ValueError("bad yaml")

        with caplog.at_level(logging.WARNING, logger='config.state_manager'):
            assert manager.list_orphaned_project_state() == []

        assert any('broken' in r.message and 'bad yaml' in r.message
                   for r in caplog.records), caplog.text

    def test_remove_orphan_cannot_delete_through_an_unreadable_config(
        self, tmp_path, monkeypatch, capsys
    ):
        """The destructive end inherits the suppression, because it re-derives.

        scripts/inspect_project_state.py re-checks orphan status at the point
        of deletion rather than trusting a report -- which is only a guard if
        the function it re-checks with is right. When a config cannot be read,
        it must refuse.
        """
        import scripts.inspect_project_state as script

        manager = _manager(tmp_path, ['healthy', 'broken-file-stem'])
        _seed_state(manager, 'healthy')
        _seed_state(manager, 'a-live-project')

        manager.config_manager.get_project_config.side_effect = (
            _config_that_raises_for('broken-file-stem')
        )
        monkeypatch.setattr(script, 'state_manager', manager)

        rc = script.remove_orphan('a-live-project')

        assert rc == 1
        assert (manager.projects_state_dir / 'a-live-project').exists()


class TestOperatorScript:

    def test_remove_orphan_refuses_a_project_that_still_has_a_config(
        self, tmp_path, monkeypatch, capsys
    ):
        import scripts.inspect_project_state as script

        manager = _manager(tmp_path, ['live-project'])
        _seed_state(manager, 'live-project')
        monkeypatch.setattr(script, 'state_manager', manager)

        rc = script.remove_orphan('live-project')

        assert rc == 1
        assert (manager.projects_state_dir / 'live-project').exists()
        assert 'Refusing to remove' in capsys.readouterr().out

    def test_remove_orphan_removes_one_orphan(self, tmp_path, monkeypatch, capsys):
        import scripts.inspect_project_state as script

        manager = _manager(tmp_path, ['live-project'])
        _seed_state(manager, 'live-project')
        _seed_state(manager, 'gone')
        monkeypatch.setattr(script, 'state_manager', manager)

        rc = script.remove_orphan('gone')

        assert rc == 0
        assert not (manager.projects_state_dir / 'gone').exists()
        assert (manager.projects_state_dir / 'live-project').exists()

    def test_collect_flags_orphans_and_counts_backups(self, tmp_path, monkeypatch):
        import scripts.inspect_project_state as script

        manager = _manager(tmp_path, ['live-project'])
        _seed_state(manager, 'live-project')
        _seed_state(manager, 'gone')
        _seed_backups(manager, 'gone', [f"2026050{d}_080000" for d in range(1, 7)])
        monkeypatch.setattr(script, 'state_manager', manager)

        data = script.collect()

        by_name = {e['project']: e for e in data['projects']}
        assert by_name['gone']['orphaned'] is True
        assert by_name['gone']['backup_count'] == 6
        assert 'prunable_backups' not in by_name['gone']
        assert by_name['live-project']['orphaned'] is False
        assert data['orphaned'] == ['gone']

    def test_an_empty_config_directory_orphans_nothing(self, tmp_path):
        """The worst possible false positive, and the easiest one to hit.

        `config/projects/` is gitignored as a DIRECTORY, so every git worktree
        and every fresh checkout sees zero configs -- and so does a deployment
        whose config volume failed to mount. Read naively, that says all 20
        live projects are orphaned. This was not hypothetical: the first run of
        the operator script against the live state tree printed exactly that,
        with a removal command under each one.
        """
        manager = _manager(tmp_path, configured_projects=[])
        for name in ('context-studio', 'documentation_robotics', 'rounds'):
            _seed_state(manager, name)

        assert manager.list_orphaned_project_state() == []

    def test_remove_orphan_is_refused_when_no_configs_are_visible(
        self, tmp_path, monkeypatch, capsys
    ):
        """The guard has to hold at the destructive end too, not just the report."""
        import scripts.inspect_project_state as script

        manager = _manager(tmp_path, configured_projects=[])
        _seed_state(manager, 'context-studio')
        monkeypatch.setattr(script, 'state_manager', manager)

        rc = script.remove_orphan('context-studio')

        assert rc == 1
        assert (manager.projects_state_dir / 'context-studio').exists()

        # rc == 1 alone does not pin this guard: the fallback branch below it
        # ("not in orphaned") also refuses and also returns 1. What separates
        # them is what the operator is told. Deleting the no-configs check would
        # send a worktree operator the message that their decommissioned project
        # is still configured, which is the opposite of true -- so assert on the
        # message that is only reachable through the guard, and on the absence
        # of the one that is not.
        out = capsys.readouterr().out
        assert 'no project configs are visible' in out
        assert 'not reported as orphaned' not in out

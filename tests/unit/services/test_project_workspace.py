"""
Unit tests for ProjectWorkspaceManager's per-epic worktree mechanism (issue #45).

Covers:
- get_project_dir(project_name) with no epic_id is unchanged (base clone path, no
  git subprocess calls).
- get_project_dir(project_name, epic_id=...) / get_or_create_epic_worktree()
  lazily creates a new, non-detached worktree (both the new-branch and
  existing-branch cases).
- Repeated calls for the same (project_name, epic_id) reuse the existing worktree
  instead of recreating it.
- cleanup_epic_worktree() removes an epic's worktree and its in-flight tracking.
- prune_epic_worktrees() sweeps orphaned worktrees left under the staging
  namespace (e.g. after a crash), mirroring
  DockerAgentRunner.prune_reference_worktrees().

All git operations are mocked (subprocess.run) — no real git commands run.
"""

import pytest
from pathlib import Path
from unittest.mock import patch, Mock

from services.project_workspace import ProjectWorkspaceManager


def _ok(stdout: str = "") -> Mock:
    result = Mock()
    result.returncode = 0
    result.stdout = stdout
    result.stderr = ""
    return result


def _fail(stderr: str = "error") -> Mock:
    result = Mock()
    result.returncode = 1
    result.stdout = ""
    result.stderr = stderr
    return result


@pytest.fixture
def manager(tmp_path):
    """A ProjectWorkspaceManager rooted at an isolated tmp directory."""
    return ProjectWorkspaceManager(workspace_root=tmp_path)


def _make_base_clone(workspace_root: Path, project_name: str) -> Path:
    """Create a fake base clone (just needs a .git dir to pass the existence check)."""
    project_dir = workspace_root / project_name
    (project_dir / '.git').mkdir(parents=True)
    return project_dir


class TestGetProjectDirBaseClone:
    """get_project_dir(project_name) with no epic_id must be behaviorally identical
    to the original single-checkout implementation."""

    def test_no_epic_id_returns_base_clone_path(self, manager, tmp_path):
        result = manager.get_project_dir("my-project")
        assert result == tmp_path / "my-project"

    def test_no_epic_id_makes_no_git_calls(self, manager):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            manager.get_project_dir("my-project")
            mock_run.assert_not_called()

    def test_no_epic_id_is_pure_path_join_not_side_effecting(self, manager, tmp_path):
        # Calling it does not create the directory or any worktree bookkeeping
        manager.get_project_dir("my-project")
        assert not (tmp_path / "my-project").exists()
        assert manager._epic_worktrees == {}


class TestCreateNewEpicWorktree:
    """get_project_dir(project_name, epic_id=X, branch_name=Y) / get_or_create_epic_worktree
    lazily creates an isolated, non-detached worktree."""

    def test_new_branch_case(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        # First fetch (of the target branch) fails -> branch doesn't exist on origin yet.
        # Second fetch (of default_branch) succeeds. worktree add -b succeeds, followed by
        # an immediate `push -u` of the brand-new branch.
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_fail("couldn't find remote ref"), _ok(), _ok(), _ok()]

            result = manager.get_project_dir(
                "my-project", epic_id="100", branch_name="feature/issue-100-epic"
            )

        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '100'
        assert result == expected_path

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ['git', '-C', str(tmp_path / 'my-project'), 'fetch', 'origin',
                             'feature/issue-100-epic:refs/remotes/origin/feature/issue-100-epic',
                             '--quiet']
        assert calls[1] == ['git', '-C', str(tmp_path / 'my-project'), 'fetch', 'origin',
                             'main', '--quiet']
        assert calls[2] == ['git', '-C', str(tmp_path / 'my-project'), 'worktree', 'add',
                             '-b', 'feature/issue-100-epic', str(expected_path),
                             'origin/main']
        assert calls[3] == ['git', '-C', str(expected_path), 'push', '-u', 'origin',
                             'feature/issue-100-epic']

        # Tracked in-flight for reuse
        assert manager._epic_worktrees[("my-project", "100")] == str(expected_path)

    def test_new_branch_case_survives_push_failure(self, manager, tmp_path):
        """The worktree is still usable even if the immediate post-creation push fails
        (e.g. transient network issue) — creation itself must not be rolled back."""
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_fail("couldn't find remote ref"), _ok(), _ok(),
                                     _fail("connection reset")]
            result = manager.get_project_dir(
                "my-project", epic_id="101", branch_name="feature/issue-101-epic"
            )

        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '101'
        assert result == expected_path
        assert manager._epic_worktrees[("my-project", "101")] == str(expected_path)

    def test_existing_branch_case(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]

            result = manager.get_project_dir(
                "my-project", epic_id="200", branch_name="feature/issue-200-existing"
            )

        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '200'
        assert result == expected_path

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ['git', '-C', str(tmp_path / 'my-project'), 'fetch', 'origin',
                             'feature/issue-200-existing:refs/remotes/origin/feature/issue-200-existing',
                             '--quiet']
        assert calls[1] == ['git', '-C', str(tmp_path / 'my-project'), 'worktree', 'add',
                             '-B', 'feature/issue-200-existing', str(expected_path),
                             'origin/feature/issue-200-existing']

    def test_missing_branch_name_on_first_create_raises(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with pytest.raises(ValueError):
            manager.get_project_dir("my-project", epic_id="300")

    def test_missing_base_clone_raises(self, manager):
        with pytest.raises(ValueError):
            manager.get_or_create_epic_worktree("no-such-project", "1", branch_name="feature/x")

    def test_worktree_add_failure_raises_runtime_error(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _fail("fatal: some git error")]

            with pytest.raises(RuntimeError):
                manager.get_project_dir(
                    "my-project", epic_id="400", branch_name="feature/issue-400"
                )


class TestReuseExistingEpicWorktree:
    """Two sequential calls for two different sub-issues of the same epic must
    resolve to the same worktree, without recreating it."""

    def test_second_call_reuses_without_git_calls(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]
            first = manager.get_project_dir(
                "my-project", epic_id="500", branch_name="feature/issue-500"
            )

        with patch('services.project_workspace.subprocess.run') as mock_run:
            # No branch_name needed on reuse, and no git calls should happen
            second = manager.get_project_dir("my-project", epic_id="500")
            mock_run.assert_not_called()

        assert first == second

    def test_reuse_across_different_sub_issue_calls(self, manager, tmp_path):
        """Simulates sub-issue #1 then sub-issue #2 of the same epic #600 both
        resolving to the same worktree path."""
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]
            sub_issue_1_dir = manager.get_project_dir(
                "my-project", epic_id="600", branch_name="feature/issue-600"
            )

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]  # would be used if (wrongly) recreated
            sub_issue_2_dir = manager.get_project_dir(
                "my-project", epic_id="600", branch_name="feature/issue-600"
            )
            assert mock_run.call_count == 0

        assert sub_issue_1_dir == sub_issue_2_dir

    def test_different_epics_get_different_worktrees(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok(), _ok()]
            epic_a = manager.get_project_dir("my-project", epic_id="700", branch_name="feature/issue-700")
            epic_b = manager.get_project_dir("my-project", epic_id="701", branch_name="feature/issue-701")

        assert epic_a != epic_b

    def test_adopts_pre_existing_worktree_on_process_restart(self, manager, tmp_path):
        """A fresh ProjectWorkspaceManager instance (simulating an orchestrator
        restart) has an empty _epic_worktrees cache even though the worktree
        directory (and git's own worktree registration) survived the restart on
        disk. get_or_create_epic_worktree() must adopt it -- reading its real
        on-disk branch -- rather than attempting `git worktree add` again, which
        git unconditionally refuses since the path is already registered (#48
        review: this crashed restart-recovery's auto-commit path outright)."""
        _make_base_clone(tmp_path, "my-project")
        pre_existing = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '900'
        pre_existing.mkdir(parents=True)
        (pre_existing / '.git').write_text("gitdir: /fake/base/.git/worktrees/900\n")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok("feature/issue-900-real\n")  # rev-parse --abbrev-ref HEAD
            result = manager.get_or_create_epic_worktree(
                "my-project", "900", branch_name="feature/issue-900-DIFFERENT"
            )

        assert result == pre_existing
        # No `worktree add`/`fetch` calls -- only the branch-discovery rev-parse
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert all('add' not in c and 'fetch' not in c for c in calls)
        # Adopted the worktree's REAL branch, not the (mismatched) requested one
        assert manager._epic_worktree_branches[("my-project", "900")] == "feature/issue-900-real"
        assert manager._epic_worktrees[("my-project", "900")] == str(pre_existing)


class TestEpicWorktreePathGuard:
    """_epic_worktree_path() must reject an empty/falsy epic_id rather than silently
    collapsing to the shared per-project staging directory."""

    def test_empty_epic_id_raises(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        with pytest.raises(ValueError):
            manager.get_project_dir("my-project", epic_id="", branch_name="feature/x")

    def test_whitespace_only_epic_id_raises(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        with pytest.raises(ValueError):
            manager.get_or_create_epic_worktree("my-project", "   ", branch_name="feature/x")


class TestCleanupEpicWorktree:
    """Cleanup is tied to epic completion, not individual sub-issue/pipeline-run
    completion — it's just a plain callable mechanism here."""

    def test_cleanup_removes_tracked_worktree(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]
            worktree_path = manager.get_project_dir(
                "my-project", epic_id="800", branch_name="feature/issue-800"
            )

        assert ("my-project", "800") in manager._epic_worktrees

        with patch('services.project_workspace.subprocess.run') as mock_run:
            # First call is the push-local-commits-before-remove check (rev-parse
            # --abbrev-ref HEAD); _ok() with empty stdout -> blank branch name -> that
            # helper returns immediately, so the very next call is the real removal.
            mock_run.return_value = _ok()
            removed = manager.cleanup_epic_worktree("my-project", "800")

        assert removed is True
        assert ("my-project", "800") not in manager._epic_worktrees
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert ['git', '-C', str(tmp_path / 'my-project'), 'worktree', 'remove',
                '--force', str(worktree_path)] in calls

    def test_cleanup_falls_back_to_rmtree_on_git_failure(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]
            worktree_path = manager.get_project_dir(
                "my-project", epic_id="810", branch_name="feature/issue-810"
            )
        worktree_path.mkdir(parents=True, exist_ok=True)
        (worktree_path / "somefile.txt").write_text("data")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            # First call is the push-local-commits-before-remove check (rev-parse
            # --abbrev-ref HEAD); failing it short-circuits that helper with no further
            # calls, so the next two are the original remove-fails/prune-ok sequence.
            mock_run.side_effect = [_fail("not a git repo"), _fail("worktree is dirty"), _ok()]
            removed = manager.cleanup_epic_worktree("my-project", "810")

        assert removed is True
        assert not worktree_path.exists()

    def test_cleanup_untracked_epic_returns_false(self, manager):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            removed = manager.cleanup_epic_worktree("my-project", "999")
            mock_run.assert_not_called()
        assert removed is False

    def test_cleanup_returns_false_and_keeps_tracking_when_removal_genuinely_fails(self, manager, tmp_path):
        """If both git-remove and the rmtree fallback fail to actually clear the
        directory, cleanup must report False and keep the epic tracked — not silently
        report success while orphaning a dict entry to a worktree that's still there."""
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]
            worktree_path = manager.get_project_dir(
                "my-project", epic_id="820", branch_name="feature/issue-820"
            )
        worktree_path.mkdir(parents=True, exist_ok=True)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_fail("not a git repo"), _fail("worktree busy"), _fail("prune failed")]
            with patch('shutil.rmtree'):  # simulate rmtree fallback not actually removing it
                removed = manager.cleanup_epic_worktree("my-project", "820")

        assert removed is False
        assert ("my-project", "820") in manager._epic_worktrees


class TestEpicWorktreeConcurrencySafety:
    """get_or_create_epic_worktree()/cleanup_epic_worktree() share a lock so two
    concurrent calls for the same epic can't race each other."""

    def test_branch_mismatch_on_cache_hit_logs_warning_but_returns_existing(self, manager, tmp_path, caplog):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok()]
            first = manager.get_project_dir(
                "my-project", epic_id="900", branch_name="feature/issue-900"
            )

        with patch('services.project_workspace.subprocess.run') as mock_run:
            with caplog.at_level("WARNING"):
                second = manager.get_project_dir(
                    "my-project", epic_id="900", branch_name="feature/issue-900-DIFFERENT"
                )
            mock_run.assert_not_called()

        assert first == second
        assert any("branch_name" in r.message for r in caplog.records)


class TestPruneEpicWorktrees:
    """Startup sweep catches worktrees orphaned by a crashed orchestrator process."""

    def test_prune_removes_orphaned_worktree_dir(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        orphan = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '900'
        orphan.mkdir(parents=True)
        (orphan / "leftover.txt").write_text("stale")

        # Fresh manager instance (simulating orchestrator restart) has no in-memory
        # tracking of this worktree at all.
        assert manager._epic_worktrees == {}

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert not orphan.exists()
        # The now-empty per-project staging dir should also be cleaned up
        assert not (tmp_path / '.orchestrator' / 'worktrees' / 'my-project').exists()

    def test_prune_handles_git_command_failure_gracefully(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        orphan = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '901'
        orphan.mkdir(parents=True)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _fail("git worktree remove failed")
            manager.prune_epic_worktrees()

        # Falls back to removing the directory directly even if git fails
        assert not orphan.exists()

    def test_prune_noop_when_staging_dir_absent(self, manager):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            manager.prune_epic_worktrees()
            mock_run.assert_not_called()

    def test_prune_does_not_touch_ref_worktrees_namespace(self, manager, tmp_path):
        """Sanity check that the epic-worktree prune sweep only ever looks under
        `.orchestrator/worktrees/`, never DockerAgentRunner's sibling
        `.orchestrator/tmp/ref-worktrees/` namespace."""
        _make_base_clone(tmp_path, "my-project")
        ref_worktree = tmp_path / '.orchestrator' / 'tmp' / 'ref-worktrees' / 'my-project' / 'task-1'
        ref_worktree.mkdir(parents=True)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert ref_worktree.exists()

    def test_prune_never_raises_on_unexpected_filesystem_error(self, manager, tmp_path):
        """prune_epic_worktrees() runs unguarded at every orchestrator startup (main.py
        has no try/except around the call site) — an unexpected filesystem error must be
        swallowed and logged, never propagated, or it would take down startup entirely."""
        _make_base_clone(tmp_path, "my-project")
        staging = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '950'
        staging.mkdir(parents=True)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            with patch.object(Path, 'iterdir', side_effect=OSError("permission denied")):
                manager.prune_epic_worktrees()  # must not raise


class TestPushLocalCommitsBeforeRemoval:
    """_push_local_commits_if_any() must never silently no-op when it can't tell
    whether local commits exist (e.g. a brand-new branch whose initial push failed) —
    that's exactly the state most likely to be silently discarding real work."""

    def test_missing_origin_ref_attempts_push_instead_of_silently_returning(self, manager, tmp_path, caplog):
        worktree_path = tmp_path / "some-worktree"
        worktree_path.mkdir(parents=True)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok("feature/issue-999\n"),  # rev-parse --abbrev-ref HEAD
                _fail("unknown revision"),      # rev-list --count origin/<branch>..HEAD -> no such ref
                _ok(),                          # push -u origin <branch> succeeds
            ]
            with caplog.at_level("WARNING"):
                ProjectWorkspaceManager._push_local_commits_if_any(worktree_path)

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ['git', '-C', str(worktree_path), 'push', '-u', 'origin', 'feature/issue-999']
        assert any("No origin/" in r.message for r in caplog.records)

    def test_missing_origin_ref_logs_error_when_push_also_fails(self, manager, tmp_path, caplog):
        worktree_path = tmp_path / "some-worktree"
        worktree_path.mkdir(parents=True)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok("feature/issue-999\n"),
                _fail("unknown revision"),
                _fail("connection reset"),  # push also fails
            ]
            with caplog.at_level("ERROR"):
                ProjectWorkspaceManager._push_local_commits_if_any(worktree_path)

        assert any("lost" in r.message for r in caplog.records)

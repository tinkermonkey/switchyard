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

import sys
import types
import pytest
from contextlib import contextmanager
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


@pytest.fixture(autouse=True)
def _no_op_checkout_lock():
    """Neutralize the project_checkout lock get_or_create_epic_worktree()'s
    creation path (#151/WI-6 item 1) and cleanup_epic_worktree()/
    prune_epic_worktrees()'s teardown paths (#169) now take.

    These tests are about worktree mechanics, not locking -- without this they
    would each build a real ProjectResourceLockManager (Redis / on-disk YAML lock
    state) as a side effect. Worse for the prune sweep specifically: several of
    those tests patch Path.is_dir/iterdir/exists with fixed side-effect
    sequences, and a real acquire's own Path calls would consume them. The
    locks' own behavior on these paths is covered by
    tests/unit/services/test_epic_worktree_checkout_lock.py.
    """
    @contextmanager
    def _noop(*args, **kwargs):
        yield

    with patch('services.project_checkout_lock.project_checkout_lock_sync', _noop):
        yield


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

        # Leading _ok(), _ok() are the new branch-freeing check's `status
        # --porcelain` (clean) and `rev-parse --abbrev-ref HEAD` (empty stdout
        # != the target branch, so it's a no-op). First fetch (of the target
        # branch) fails -> branch doesn't exist on origin yet. Second fetch (of
        # default_branch) succeeds. worktree add -b succeeds, followed by an
        # immediate `push -u` of the brand-new branch.
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _fail("couldn't find remote ref"), _ok(),
                                     _fail("no local ref"), _ok(), _ok()]

            result = manager.get_project_dir(
                "my-project", epic_id="100", branch_name="feature/issue-100-epic"
            )

        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '100'
        assert result == expected_path

        calls = [c.args[0] for c in mock_run.call_args_list]
        # calls[0:2] are the branch-freeing check's status/rev-parse
        assert calls[2] == ['git', '-C', str(tmp_path / 'my-project'), 'fetch', 'origin',
                             'feature/issue-100-epic:refs/remotes/origin/feature/issue-100-epic',
                             '--quiet']
        assert calls[3] == ['git', '-C', str(tmp_path / 'my-project'), 'fetch', 'origin',
                             'main', '--quiet']
        # calls[4] is the stray-branch-with-real-commits guard's rev-parse --verify
        assert calls[5] == ['git', '-C', str(tmp_path / 'my-project'), 'worktree', 'add',
                             '-B', 'feature/issue-100-epic', str(expected_path),
                             'origin/main']
        assert calls[6] == ['git', '-C', str(expected_path), 'push', '-u', 'origin',
                             'feature/issue-100-epic']

        # Tracked in-flight for reuse
        assert manager._epic_worktrees[("my-project", "100")] == str(expected_path)

    def test_new_branch_case_survives_push_failure(self, manager, tmp_path):
        """The worktree is still usable even if the immediate post-creation push fails
        (e.g. transient network issue) — creation itself must not be rolled back."""
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _fail("couldn't find remote ref"), _ok(),
                                     _fail("no local ref"), _ok(),
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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]

            result = manager.get_project_dir(
                "my-project", epic_id="200", branch_name="feature/issue-200-existing"
            )

        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '200'
        assert result == expected_path

        calls = [c.args[0] for c in mock_run.call_args_list]
        # calls[0:2] are the branch-freeing check's status/rev-parse
        assert calls[2] == ['git', '-C', str(tmp_path / 'my-project'), 'fetch', 'origin',
                             'feature/issue-200-existing:refs/remotes/origin/feature/issue-200-existing',
                             '--quiet']
        # calls[3] is the stray-branch-with-real-commits guard's rev-parse --verify
        assert calls[4] == ['git', '-C', str(tmp_path / 'my-project'), 'worktree', 'add',
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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _fail("fatal: some git error")]

            with pytest.raises(RuntimeError):
                manager.get_project_dir(
                    "my-project", epic_id="400", branch_name="feature/issue-400"
                )


class TestFreeBranchFromBaseClone:
    """_free_branch_from_base_clone() -- found live in production (issues #45-51's
    epic-worktree isolation): `git worktree add` unconditionally refuses to check a
    branch out into a new worktree if that branch is already checked out ANYWHERE
    else, including the base clone's own primary checkout. Ordinary ('issues'-
    workspace) dispatch checks an epic's shared branch out directly on the base
    clone, and nothing ever resets it back afterward -- so without this, every
    subsequent epic-worktree creation for that branch is doomed, not just racy
    (confirmed live: one project's repair cycle failed this way every hour for 10+
    consecutive hours). _add_epic_worktree calls this before every `worktree add`
    attempt; these tests cover the helper directly.

    Call order this class asserts against: `git status --porcelain` (dirty-tree
    guard) -> `git rev-parse --abbrev-ref HEAD` (is it even this branch?) ->
    `git checkout --detach <default_branch>` (only if clean AND on this branch)."""

    def test_frees_branch_when_checked_out_on_base_clone_and_clean(self, tmp_path):
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(),                              # status --porcelain -- clean
                _ok("feature/issue-100-epic\n"),  # rev-parse --abbrev-ref HEAD
                _ok(),                              # checkout --detach main
            ]
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ['git', '-C', str(base_repo_dir), 'status', '--porcelain']
        assert calls[1] == ['git', '-C', str(base_repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD']
        assert calls[2] == ['git', '-C', str(base_repo_dir), 'checkout', '--detach', 'main']

    def test_noop_when_base_clone_is_on_a_different_branch(self, tmp_path):
        """The common case for most epics: the base clone is parked on SOME
        other epic's branch, not this one -- nothing to free, no checkout
        attempted."""
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok("feature/some-other-epic\n")]
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )

        assert mock_run.call_count == 2  # status + rev-parse, no checkout
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[1] == ['git', '-C', str(base_repo_dir), 'rev-parse', '--abbrev-ref', 'HEAD']

    def test_dirty_tree_refuses_to_touch_the_base_clone_at_all(self, tmp_path):
        """The real safety guarantee: a plain (non---force) `git checkout` only
        refuses when switching branches would overwrite a file that actually
        DIFFERS between the two commits -- an uncommitted change to a file that's
        IDENTICAL on both branches checks out cleanly and silently carries the
        uncommitted change over onto default_branch (verified against real git;
        this is not hypothetical). Repair cycles steal the pipeline lock from a
        non-retained ordinary holder, so a live agent genuinely can be mid-edit
        in this exact base clone. So: any uncommitted change at all -- not just
        one git's own checkout would refuse over -- must stop this cold before
        even checking which branch it's on."""
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok(" M some_unrelated_file.txt\n")  # dirty
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )

        assert mock_run.call_count == 1  # only the status check -- nothing else
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[0] == ['git', '-C', str(base_repo_dir), 'status', '--porcelain']

    def test_checkout_failure_is_logged_but_never_raises(self, tmp_path):
        """Belt-and-suspenders: even if something slips past the dirty-tree guard
        (e.g. a change made between the status check and the checkout attempt)
        and git's own checkout refuses, this must not raise -- the caller's own
        `worktree add` attempt proceeds and fails with its existing error rather
        than this ever forcing anything through."""
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(),
                _ok("feature/issue-100-epic\n"),
                _fail("error: your local changes would be overwritten by checkout"),
            ]
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )  # must not raise

    def test_status_check_failure_is_logged_but_never_raises(self, tmp_path):
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _fail("not a git repository")
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )  # must not raise

        assert mock_run.call_count == 1  # nothing attempted after a failed status check

    def test_rev_parse_failure_is_logged_but_never_raises(self, tmp_path):
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _fail("not a git repository")]
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )  # must not raise

        assert mock_run.call_count == 2  # no checkout attempted after a failed rev-parse

    def test_subprocess_exception_is_logged_but_never_raises(self, tmp_path):
        base_repo_dir = tmp_path / "my-project"
        with patch('services.project_workspace.subprocess.run', side_effect=OSError("git not found")):
            ProjectWorkspaceManager._free_branch_from_base_clone(
                base_repo_dir, "feature/issue-100-epic", "main"
            )  # must not raise

    def test_add_epic_worktree_calls_this_before_worktree_add(self, tmp_path):
        """Integration point: _add_epic_worktree must actually invoke the
        branch-freeing check, not just have it exist unused."""
        base_repo_dir = tmp_path / "my-project"
        worktree_path = tmp_path / ".orchestrator" / "worktrees" / "my-project" / "100"
        with patch.object(
            ProjectWorkspaceManager, '_free_branch_from_base_clone'
        ) as mock_free, \
             patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _fail("no local ref"), _ok()]
            ProjectWorkspaceManager._add_epic_worktree(
                base_repo_dir, worktree_path, "feature/issue-100-epic", "main"
            )

        mock_free.assert_called_once_with(base_repo_dir, "feature/issue-100-epic", "main")

    def test_freeing_the_branch_lets_a_subsequent_worktree_add_succeed(self, tmp_path):
        """The real end-to-end scenario this whole fix exists for, chained
        through for real (not with _free_branch_from_base_clone mocked out):
        the branch IS checked out on the base clone -> the real freeing logic
        runs and detaches it -> `worktree add` (which would otherwise be
        doomed, per the collision this PR fixes) then succeeds."""
        base_repo_dir = tmp_path / "my-project"
        worktree_path = tmp_path / ".orchestrator" / "worktrees" / "my-project" / "100"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(),                              # status --porcelain -- clean
                _ok("feature/issue-100-epic\n"),  # rev-parse -- checked out here
                _ok(),                              # checkout --detach main -- freed
                _ok(),                              # fetch origin <branch> -- exists
                _fail("no local ref"),              # stray-branch-ahead guard
                _ok(),                              # worktree add -- now succeeds
            ]
            ProjectWorkspaceManager._add_epic_worktree(
                base_repo_dir, worktree_path, "feature/issue-100-epic", "main"
            )  # must not raise -- would raise RuntimeError if worktree add failed

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ['git', '-C', str(base_repo_dir), 'checkout', '--detach', 'main']
        assert calls[5] == ['git', '-C', str(base_repo_dir), 'worktree', 'add',
                             '-B', 'feature/issue-100-epic', str(worktree_path),
                             'origin/feature/issue-100-epic']


class TestReuseExistingEpicWorktree:
    """Two sequential calls for two different sub-issues of the same epic must
    resolve to the same worktree, without recreating it."""

    def test_second_call_reuses_without_git_calls(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
            first = manager.get_project_dir(
                "my-project", epic_id="500", branch_name="feature/issue-500"
            )
        # subprocess is mocked above, so the real `git worktree add` that would
        # create .git on disk never actually runs -- simulate what it would
        # have left behind, since the cache-hit reuse below now also checks
        # for it (issue: dead-code corruption check on cache hits, code review).
        first.mkdir(parents=True, exist_ok=True)
        (first / '.git').mkdir()

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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
            sub_issue_1_dir = manager.get_project_dir(
                "my-project", epic_id="600", branch_name="feature/issue-600"
            )
        # See test_second_call_reuses_without_git_calls above for why.
        sub_issue_1_dir.mkdir(parents=True, exist_ok=True)
        (sub_issue_1_dir / '.git').mkdir()

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]  # would be used if (wrongly) recreated
            sub_issue_2_dir = manager.get_project_dir(
                "my-project", epic_id="600", branch_name="feature/issue-600"
            )
            assert mock_run.call_count == 0

        assert sub_issue_1_dir == sub_issue_2_dir

    def test_different_epics_get_different_worktrees(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok(), _ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
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

    def test_corrupted_worktree_directory_with_no_git_at_all_raises_without_touching_it(
        self, manager, tmp_path
    ):
        """Same failure class found in code-wrapper's agent-entrypoint.sh gap
        (issue investigation, 2026-09-06): the worktree directory exists on
        disk, populated with real files, but .git is completely missing --
        not the pre-existing-worktree-to-adopt case above (that requires .git
        to exist), and NOT a genuinely fresh target either.

        Code review correction on the FIRST version of this fix: it called
        shutil.rmtree() on the directory before retrying `git worktree add`.
        Caught before merging -- (1) it doesn't even work for the realistic
        trigger, since git tracks a worktree by metadata in the base repo's
        OWN .git/worktrees/<id>/, not by the target directory's existence, so
        `git worktree add` still refuses afterward (a DIFFERENT opaque error:
        "is a missing but already registered worktree"); and (2) even a
        correct git-aware cleanup (`git worktree remove --force`) would be
        just as unsafe, since without .git there's no way to tell "empty
        checkout, fine to discard" apart from "an agent's real uncommitted
        work" -- and two other call sites (agent_container_recovery.py's
        restart-recovery flow, agent_executor.py's _failsafe_commit_check())
        reach this exact directory expecting to commit real content from it.
        The fix must raise loudly and leave the directory completely
        untouched instead -- verified explicitly here."""
        _make_base_clone(tmp_path, "my-project")
        corrupted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '901'
        corrupted.mkdir(parents=True)
        (corrupted / 'some_real_file.txt').write_text("leftover project content\n")
        assert not (corrupted / '.git').exists()

        with patch('services.project_workspace.subprocess.run') as mock_run:
            with pytest.raises(RuntimeError, match="no .git at all"):
                manager.get_or_create_epic_worktree(
                    "my-project", "901", branch_name="feature/issue-901"
                )
            # No git subprocess calls at all -- must fail before ever
            # attempting `worktree add` against the corrupted path.
            mock_run.assert_not_called()

        # The directory and its real content must be completely untouched --
        # the whole point of raising instead of cleaning up automatically.
        assert corrupted.exists()
        assert (corrupted / 'some_real_file.txt').read_text() == "leftover project content\n"
        # Not adopted into the in-memory cache either -- a later retry must
        # see the same unresolved state, not a poisoned "already handled" one.
        assert ("my-project", "901") not in manager._epic_worktrees

    def test_corruption_after_being_cached_this_process_is_also_caught(self, manager, tmp_path):
        """Second-pass code review finding: the corruption check above only ran
        on the not-yet-cached path -- dead code for any epic ALREADY tracked in
        self._epic_worktrees this process. A running container's own self-repair
        can remove .git from an already-cached worktree with no orchestrator
        restart in between (the exact trigger this whole check exists for) --
        must be caught on a cache hit too, not just cold resolution."""
        _make_base_clone(tmp_path, "my-project")
        worktree = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '902'
        worktree.mkdir(parents=True)
        (worktree / '.git').write_text("gitdir: /fake/base/.git/worktrees/902\n")
        (worktree / 'real_work.py').write_text("# real work\n")

        # Cache it first, exactly like a real first call would.
        manager._epic_worktrees[("my-project", "902")] = str(worktree)
        manager._epic_worktree_branches[("my-project", "902")] = "feature/issue-902"

        # Simulate a container's self-repair removing .git with no restart --
        # the in-memory cache entry is untouched, but the real state on disk
        # has changed underneath it.
        (worktree / '.git').unlink()

        with patch('services.project_workspace.subprocess.run') as mock_run:
            with pytest.raises(RuntimeError, match="lost its .git"):
                manager.get_or_create_epic_worktree("my-project", "902")
            mock_run.assert_not_called()

        # Untouched, same as the cold-resolution case.
        assert worktree.exists()
        assert (worktree / 'real_work.py').read_text() == "# real work\n"

    def test_genuinely_empty_pre_existing_directory_is_not_treated_as_corrupted(
        self, manager, tmp_path
    ):
        """Second-pass code review correction: the corruption check must only
        fire for a NON-EMPTY directory. A genuinely empty pre-existing
        directory (e.g. a stray leftover from an interrupted worktree
        creation that never got far enough to register with git) has nothing
        to lose -- `git worktree add` succeeds into it exactly as it always
        has (verified empirically) -- so this must fall through to the normal
        creation path instead of raising and demanding manual intervention
        for something with nothing actually at risk."""
        _make_base_clone(tmp_path, "my-project")
        empty_dir = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '903'
        empty_dir.mkdir(parents=True)
        assert list(empty_dir.iterdir()) == []

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
            result = manager.get_or_create_epic_worktree(
                "my-project", "903", branch_name="feature/issue-903"
            )

        assert result == empty_dir
        calls = [c.args[0] for c in mock_run.call_args_list]
        assert any('worktree' in c and 'add' in c for c in calls)
        assert manager._epic_worktrees[("my-project", "903")] == str(empty_dir)


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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
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
            mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
            first = manager.get_project_dir(
                "my-project", epic_id="900", branch_name="feature/issue-900"
            )
        # See TestReuseExistingEpicWorktree.test_second_call_reuses_without_git_calls
        # for why this is needed now that cache hits also check for .git.
        first.mkdir(parents=True, exist_ok=True)
        (first / '.git').mkdir()

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

    def test_prune_skips_a_worktree_already_tracked_this_process(self, manager, tmp_path):
        """(Final whole-PR review, #87) main.py runs repair-cycle container
        recovery BEFORE this prune sweep; recovering an already-completed repair
        cycle can ADOPT an on-disk worktree into _epic_worktrees (#48) before this
        method ever runs. Deleting that just-adopted worktree here would leave the
        cache pointing at a now-missing directory -- prune must skip anything
        already tracked, not just blindly sweep every directory on disk."""
        _make_base_clone(tmp_path, "my-project")
        tracked = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '950'
        tracked.mkdir(parents=True)
        (tracked / "some_committed_file.txt").write_text("real work")

        # Simulate: this epic's worktree was already adopted earlier this process
        # (e.g. by repair-cycle recovery calling get_or_create_epic_worktree()).
        manager._epic_worktrees[("my-project", "950")] = str(tracked)
        manager._epic_worktree_branches[("my-project", "950")] = "feature/issue-950"

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        # Untouched -- still tracked, still on disk, still has its real content.
        assert tracked.exists()
        assert (tracked / "some_committed_file.txt").exists()
        assert manager._epic_worktrees[("my-project", "950")] == str(tracked)

    def test_prune_still_removes_a_genuinely_untracked_worktree_alongside_a_tracked_one(
        self, manager, tmp_path
    ):
        """A tracked worktree being skipped must not accidentally protect its
        untracked siblings -- each is evaluated independently."""
        _make_base_clone(tmp_path, "my-project")
        tracked = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '951'
        tracked.mkdir(parents=True)
        untracked = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '952'
        untracked.mkdir(parents=True)

        manager._epic_worktrees[("my-project", "951")] = str(tracked)

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert tracked.exists()
        assert not untracked.exists()

    def test_prune_skips_a_worktree_a_git_writer_marked_in_use(self, manager, tmp_path):
        """(#154/WI-9 review) main.py runs this sweep the moment startup's
        repair-cycle recovery returns, and since that pass's auto-commit join
        became bounded by a shared budget it can return with commit threads still
        running. Those threads are git WRITERS with no worktree resolution behind
        them, so neither _epic_worktrees nor _epic_worktrees_pending names their
        directory -- the sweep would force-remove it mid-`git commit` and lose the
        repair cycle's fix."""
        _make_base_clone(tmp_path, "my-project")
        in_use = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '953'
        in_use.mkdir(parents=True)
        (in_use / "fix.py").write_text("the repair cycle's fix, mid-commit")

        manager.mark_worktree_path_in_use(str(in_use))

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert in_use.exists()
        assert (in_use / "fix.py").exists()

    def test_prune_removes_the_worktree_once_the_writer_clears_its_mark(self, manager, tmp_path):
        """The mark is held for the writer's life, not forever -- a permanent
        hold would make every later startup's sweep a no-op for that directory."""
        _make_base_clone(tmp_path, "my-project")
        worktree = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '954'
        worktree.mkdir(parents=True)

        manager.mark_worktree_path_in_use(str(worktree))
        manager.clear_worktree_path_in_use(str(worktree))

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert not worktree.exists()

    def test_two_writers_on_one_worktree_do_not_clear_each_other(self, manager, tmp_path):
        """Reference-counted: the first writer finishing must not expose a
        directory the second is still writing."""
        _make_base_clone(tmp_path, "my-project")
        worktree = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '955'
        worktree.mkdir(parents=True)

        manager.mark_worktree_path_in_use(str(worktree))
        manager.mark_worktree_path_in_use(str(worktree))
        manager.clear_worktree_path_in_use(str(worktree))

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert worktree.exists()

    def test_prune_removes_orphaned_worktree_dir(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        orphan = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '900'
        orphan.mkdir(parents=True)
        (orphan / "leftover.txt").write_text("stale")
        # A normal, non-corrupted (just untracked) worktree has .git -- distinct
        # from TestPruneCorruptedWorktreeSkip below, which covers the "no .git
        # at all" case this method must NOT blindly remove.
        (orphan / '.git').write_text("gitdir: /fake/base/.git/worktrees/900\n")

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

        # `git status --porcelain` answers cleanly (this worktree is a removal
        # candidate); every other git command fails. Narrowed from a blanket
        # "every subprocess fails" when #163's uncommitted-work skip landed:
        # this test is about the sweep surviving a failed `git worktree remove`,
        # and an unreadable `git status` is a different case with a deliberately
        # different answer (skip, never remove -- see
        # test_prune_skips_a_worktree_whose_status_cannot_be_read).
        def _git(cmd, **kwargs):
            if 'status' in cmd:
                return _ok("")
            return _fail("git worktree remove failed")

        with patch('services.project_workspace.subprocess.run', side_effect=_git):
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


class TestIsCorruptedNonEmptyWorktree:
    """Third-pass code review finding: the shared corruption-detection helper
    (factored out of get_or_create_epic_worktree()/prune_epic_worktrees() after
    the same condition was hand-written independently at each) must tolerate a
    directory changing out from under it between the .exists() checks and
    any(iterdir()) -- unlike Path.exists(), Path.iterdir() does not swallow
    OSError, and an uncaught one inside prune_epic_worktrees()'s per-worktree
    loop would abort the ENTIRE startup sweep via that method's single
    top-level except, not just skip the one worktree that raced."""

    def test_true_for_a_real_corrupted_worktree(self, tmp_path):
        corrupted = tmp_path / 'corrupted'
        corrupted.mkdir()
        (corrupted / 'real_file.txt').write_text("content")

        assert ProjectWorkspaceManager._is_corrupted_non_empty_worktree(corrupted) is True

    def test_false_for_a_healthy_worktree(self, tmp_path):
        healthy = tmp_path / 'healthy'
        healthy.mkdir()
        (healthy / '.git').mkdir()
        (healthy / 'real_file.txt').write_text("content")

        assert ProjectWorkspaceManager._is_corrupted_non_empty_worktree(healthy) is False

    def test_false_for_a_genuinely_empty_directory(self, tmp_path):
        empty = tmp_path / 'empty'
        empty.mkdir()

        assert ProjectWorkspaceManager._is_corrupted_non_empty_worktree(empty) is False

    def test_false_for_a_path_that_does_not_exist(self, tmp_path):
        assert ProjectWorkspaceManager._is_corrupted_non_empty_worktree(tmp_path / 'nope') is False

    def test_race_where_directory_vanishes_returns_false_not_raise(self, tmp_path):
        """Simulates the exact TOCTOU code review caught: the directory is
        removed (by a concurrent container self-repair, or the same race
        prune_epic_worktrees()'s own docstring already documents against
        concurrent worktree creation/adoption) between this check's own
        .exists() calls and its any(iterdir()) call."""
        vanishing = tmp_path / 'vanishing'
        vanishing.mkdir()
        (vanishing / 'real_file.txt').write_text("content")

        with patch.object(Path, 'iterdir', side_effect=FileNotFoundError("gone")):
            # Must not raise -- treated as "not corrupted" instead.
            assert ProjectWorkspaceManager._is_corrupted_non_empty_worktree(vanishing) is False

    def test_race_where_directory_becomes_a_file_returns_false_not_raise(self, tmp_path):
        target = tmp_path / 'was-a-dir'
        target.mkdir()

        with patch.object(Path, 'iterdir', side_effect=NotADirectoryError("not a directory")):
            assert ProjectWorkspaceManager._is_corrupted_non_empty_worktree(target) is False


class TestPruneUncommittedWorkSkip:
    """#163: the designed half of prune_epic_worktrees()' interaction with
    #149's commit-time branch verification.

    A wrong-branch refusal deliberately leaves the agent's work UNCOMMITTED on
    disk for a human to inspect. This sweep force-removed exactly that work on
    the next restart: _push_local_commits_if_any() saves local COMMITS and has
    no answer at all for a dirty working tree, so for this shape the sweep's
    whole "safe to remove, cheaply recreated" premise is false -- the same
    reason the corrupted-worktree case below is skipped.

    That is what wedged the previous attempt at this fix: a marker written as a
    worktree SIBLING to survive this sweep, while the sweep force-removed the
    worktree the marker's own recovery instructions pointed at.
    """

    def _dirty_git(self, porcelain: str, head: str = ""):
        def _run(cmd, **kwargs):
            if 'status' in cmd:
                return _ok(porcelain)
            if 'rev-parse' in cmd and '--abbrev-ref' in cmd:
                return _ok(f"{head}\n")
            return _ok()
        return _run

    def test_skips_a_worktree_with_uncommitted_changes(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        dirty = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '970'
        dirty.mkdir(parents=True)
        (dirty / '.git').write_text("gitdir: /fake/base/.git/worktrees/970\n")
        (dirty / 'the_agents_work.py').write_text("# uncommitted, refused over\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git(" M the_agents_work.py\n")) as mock_run:
            manager.prune_epic_worktrees()

        assert dirty.exists()
        assert (dirty / 'the_agents_work.py').read_text() == "# uncommitted, refused over\n"
        # Not even attempted -- and _push_local_commits_if_any() must not have run
        # either, since it cannot preserve any of this.
        assert [c for c in mock_run.call_args_list if 'remove' in c.args[0]] == []
        assert [c for c in mock_run.call_args_list if 'push' in c.args[0]] == []

    def test_counts_untracked_files_as_work_worth_keeping(self, manager, tmp_path):
        """A brand-new source file an agent wrote and never got to commit shows up
        only as '??' -- the exact work this rule exists to protect."""
        _make_base_clone(tmp_path, "my-project")
        dirty = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '971'
        dirty.mkdir(parents=True)
        (dirty / '.git').write_text("gitdir: /fake/base/.git/worktrees/971\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git("?? brand_new_module.py\n")):
            manager.prune_epic_worktrees()

        assert dirty.exists()

    def test_skips_a_worktree_whose_status_cannot_be_read(self, manager, tmp_path):
        """Unanswerable resolves to the non-destructive answer: guessing "clean"
        is the only one of the two guesses that can destroy something."""
        _make_base_clone(tmp_path, "my-project")
        unreadable = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '972'
        unreadable.mkdir(parents=True)
        (unreadable / '.git').write_text("gitdir: /fake/base/.git/worktrees/972\n")
        (unreadable / 'maybe_precious.py').write_text("# unknown\n")

        def _run(cmd, **kwargs):
            if 'status' in cmd:
                return _fail("fatal: not a git repository")
            return _ok()

        with patch('services.project_workspace.subprocess.run', side_effect=_run):
            manager.prune_epic_worktrees()

        assert unreadable.exists()
        assert (unreadable / 'maybe_precious.py').exists()

    def test_still_removes_a_clean_worktree(self, manager, tmp_path):
        """The control: this rule must not turn the sweep into a no-op. A clean
        worktree is the case the sweep's whole design is justified by -- cheaply
        recreated on the next resolution, nothing to lose."""
        _make_base_clone(tmp_path, "my-project")
        clean = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '973'
        clean.mkdir(parents=True)
        (clean / '.git').write_text("gitdir: /fake/base/.git/worktrees/973\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git("")):
            manager.prune_epic_worktrees()

        assert not clean.exists()

    def test_a_dirty_worktree_on_the_epics_own_branch_is_still_swept(self, manager, tmp_path):
        """The rule is scoped to the DRIFTED shape, not to "dirty" in general
        (code review on #163).

        A dirty worktree still on the epic's own branch is an ordinary
        interrupted run -- a container SIGKILLed by an orchestrator restart --
        and nothing refuses over it. Keeping it means the next sibling sub-issue
        reconciles to MATCH, runs on top of the stale tree, and auto_commit's
        unscoped `git add -A` commits the dead run's leftovers into THAT issue's
        PR: #143's cross-issue contamination, reached from the other side.
        Removing it (after _push_local_commits_if_any) and letting the next
        resolution recreate it from origin is what makes "each epic gets its own
        fresh worktree" true, and this sweep is the only thing that does it."""
        _make_base_clone(tmp_path, "my-project")
        interrupted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '975'
        interrupted.mkdir(parents=True)
        (interrupted / '.git').write_text("gitdir: /fake/base/.git/worktrees/975\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git(" M work.py\n",
                                               head='feature/issue-975-auth')) as mock_run:
            manager.prune_epic_worktrees()

        assert not interrupted.exists()
        # And its commits still got their push-before-removal chance.
        assert [c for c in mock_run.call_args_list if 'rev-list' in c.args[0]]

    def test_a_dirty_worktree_on_a_branch_belonging_to_no_epic_is_skipped(
        self, manager, tmp_path
    ):
        """The other side of the same rule: this IS what a wrong-branch refusal
        leaves behind, and it is the only shape the skip exists for."""
        _make_base_clone(tmp_path, "my-project")
        drifted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '976'
        drifted.mkdir(parents=True)
        (drifted / '.git').write_text("gitdir: /fake/base/.git/worktrees/976\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git(" M work.py\n", head='scratch')):
            manager.prune_epic_worktrees()

        assert drifted.exists()

    def test_a_dirty_worktree_on_another_epics_branch_is_skipped(self, manager, tmp_path):
        """Epic ownership, not "looks like a feature branch" -- a sibling epic's
        branch in this epic's worktree is exactly #143's contamination."""
        _make_base_clone(tmp_path, "my-project")
        drifted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '977'
        drifted.mkdir(parents=True)
        (drifted / '.git').write_text("gitdir: /fake/base/.git/worktrees/977\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git(" M work.py\n",
                                               head='feature/issue-111-other')):
            manager.prune_epic_worktrees()

        assert drifted.exists()

    def test_the_skip_self_clears_once_the_work_is_committed(self, manager, tmp_path):
        """Nothing durable records the skip -- it is re-derived from the live
        working tree every startup, so committing or discarding the work is the
        whole of the recovery. There is no quarantine to clear."""
        _make_base_clone(tmp_path, "my-project")
        worktree = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '974'
        worktree.mkdir(parents=True)
        (worktree / '.git').write_text("gitdir: /fake/base/.git/worktrees/974\n")

        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git(" M work.py\n")):
            manager.prune_epic_worktrees()
        assert worktree.exists()

        # Same directory, same sweep, nothing cleared by hand -- only the working
        # tree changed.
        with patch('services.project_workspace.subprocess.run',
                   side_effect=self._dirty_git("")):
            manager.prune_epic_worktrees()
        assert not worktree.exists()


class TestPruneCorruptedWorktreeSkip:
    """Second-pass code review finding: prune_epic_worktrees()'s startup sweep was
    a SECOND, unprotected path to the identical destructive operation
    get_or_create_epic_worktree()'s own corruption guard exists to prevent --
    neither the tracked-check nor the liveness check catches a worktree that's
    corrupted (no .git) but currently untracked and unmounted, and
    _push_local_commits_if_any() is a no-op with no .git to run git commands
    against, so the "safe to remove, cheaply recreated" assumption the rest of
    this method's design relies on does not hold for this specific shape."""

    def test_skips_a_non_empty_corrupted_worktree_instead_of_removing_it(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        corrupted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '960'
        corrupted.mkdir(parents=True)
        (corrupted / 'real_uncommitted_work.py').write_text("# precious\n")
        assert not (corrupted / '.git').exists()

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        # Left completely untouched, same guarantee get_or_create_epic_worktree()
        # makes for the identical shape.
        assert corrupted.exists()
        assert (corrupted / 'real_uncommitted_work.py').read_text() == "# precious\n"
        # No `worktree remove`/`rmtree` attempted against it at all.
        remove_calls = [c for c in mock_run.call_args_list if 'remove' in c.args[0]]
        assert remove_calls == []

    def test_still_removes_a_genuinely_empty_corrupted_worktree(self, manager, tmp_path):
        """No .git AND nothing in it either -- has nothing to lose, so this
        stays on the normal (pre-existing) removal path rather than being
        needlessly escalated to manual intervention.

        git is mocked the way REAL git answers for such a directory (code review
        on #163): `git -C <empty non-repo> status --porcelain` exits 128, and
        _worktree_has_uncommitted_work() reports that as None, i.e. "has work".
        Answering rc=0 for every command here is what let the uncommitted-work
        skip silently swallow this carve-out without any test noticing."""
        _make_base_clone(tmp_path, "my-project")
        empty_corrupted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '961'
        empty_corrupted.mkdir(parents=True)
        assert list(empty_corrupted.iterdir()) == []

        def _run(cmd, **kwargs):
            if str(empty_corrupted) in cmd and ('status' in cmd or 'rev-parse' in cmd):
                return _fail("fatal: not a git repository")
            return _ok()

        with patch('services.project_workspace.subprocess.run', side_effect=_run):
            manager.prune_epic_worktrees()

        assert not empty_corrupted.exists()

    def test_still_removes_an_orphaned_gitdir_pointer_that_is_otherwise_empty(
        self, manager, tmp_path
    ):
        """`git worktree prune` against the base clone orphans the pointer, so
        every git command in the directory exits 128 -- which
        _worktree_has_uncommitted_work() reports as None ("has work"). Without
        _is_drift_evidence()'s is-this-still-a-worktree gate that made such a
        directory permanently unprunable, on this startup and every one after
        it."""
        _make_base_clone(tmp_path, "my-project")
        orphaned = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '964'
        orphaned.mkdir(parents=True)
        (orphaned / '.git').write_text("gitdir: /nonexistent/.git/worktrees/964\n")

        def _run(cmd, **kwargs):
            if str(orphaned) in cmd:
                return _fail("fatal: not a git repository: /nonexistent/.git/worktrees/964")
            return _ok()

        with patch('services.project_workspace.subprocess.run', side_effect=_run):
            manager.prune_epic_worktrees()

        assert not orphaned.exists()

    def test_non_empty_corrupted_sibling_does_not_protect_other_worktrees(
        self, manager, tmp_path
    ):
        """A skipped corrupted worktree must not accidentally short-circuit the
        sweep for its siblings -- each is still evaluated independently."""
        _make_base_clone(tmp_path, "my-project")
        corrupted = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '962'
        corrupted.mkdir(parents=True)
        (corrupted / 'real_work.txt').write_text("precious")
        healthy_orphan = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '963'
        healthy_orphan.mkdir(parents=True)
        (healthy_orphan / '.git').write_text("gitdir: /fake/base/.git/worktrees/963\n")

        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok()
            manager.prune_epic_worktrees()

        assert corrupted.exists()
        assert not healthy_orphan.exists()


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


def _fake_dev_container_state_module(verified: bool = True, image_name: str = "my-project-agent:latest"):
    """A stand-in `services.dev_container_state` module for sys.modules patching.

    The real module's singleton (`dev_container_state = DevContainerStateManager()`)
    touches the filesystem at import time (ORCHESTRATOR_ROOT/state/dev_containers),
    which doesn't exist in a plain local test run -- so tests that need to control its
    answers swap the whole module out via `patch.dict(sys.modules, ...)` rather than
    importing the real thing.
    """
    module = types.ModuleType('services.dev_container_state')
    fake_singleton = Mock()
    fake_singleton.is_verified.return_value = verified
    fake_singleton.get_image_name.return_value = image_name if verified else None
    # Live re-check (issue #50 review): defaults to matching `verified`, same as the
    # cached status, so existing tests that don't care about this distinction are
    # unaffected. Tests that DO care override it explicitly (e.g. a hijacked-tag case
    # where cached state says verified but the live check disagrees).
    fake_singleton.verify_image_exists.return_value = verified
    module.dev_container_state = fake_singleton
    return module, fake_singleton


class TestBakedDependencyExtractionIntegration:
    """get_or_create_epic_worktree() (issue #50) triggers baked-dependency extraction
    only on the brand-new-worktree path -- never on cache-hit reuse, and never on
    adopting a pre-existing worktree found on disk after a restart -- and never lets
    an extraction problem block worktree creation itself.

    Extraction runs in a detached background thread (#50 review, 2nd pass) so it
    can never block the caller/event loop -- but that makes its actual invocation
    non-deterministic from a plain test's point of view (a race between the
    background thread and the test's own assertions). run_synchronously below
    patches threading.Thread to invoke its target inline instead of spawning a
    real thread, so every test in this class can assert deterministically."""

    @pytest.fixture(autouse=True)
    def run_synchronously(self):
        """Make the extraction background thread run inline (same thread, same
        call stack) instead of actually spawning one, for deterministic tests."""
        class _ImmediateThread:
            def __init__(self, target=None, args=(), kwargs=None, **_ignored):
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self):
                if self._target:
                    self._target(*self._args, **self._kwargs)

        with patch('services.project_workspace.threading.Thread', _ImmediateThread):
            yield

    def test_new_worktree_triggers_extraction_with_resolved_image(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        fake_module, _ = _fake_dev_container_state_module(image_name="my-project-agent:latest")
        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '300'

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.baked_dependency_extractor.extract_baked_dependencies') as mock_extract:
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]  # existing-branch case
                    result = manager.get_project_dir(
                        "my-project", epic_id="300", branch_name="feature/issue-300"
                    )

        assert result == expected_path
        mock_extract.assert_called_once_with("my-project", "my-project-agent:latest", expected_path)

    def test_unverified_project_skips_extraction_without_calling_docker(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        fake_module, _ = _fake_dev_container_state_module(verified=False)

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.baked_dependency_extractor.extract_baked_dependencies') as mock_extract:
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                    manager.get_project_dir(
                        "my-project", epic_id="301", branch_name="feature/issue-301"
                    )

        mock_extract.assert_not_called()

    def test_verified_but_no_image_name_skips_extraction(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        fake_module, fake_singleton = _fake_dev_container_state_module()
        fake_singleton.get_image_name.return_value = None  # verified=True but no image recorded

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.baked_dependency_extractor.extract_baked_dependencies') as mock_extract:
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                    manager.get_project_dir(
                        "my-project", epic_id="302", branch_name="feature/issue-302"
                    )

        mock_extract.assert_not_called()

    def test_hijacked_image_tag_skips_extraction_despite_cached_verified_status(self, manager, tmp_path):
        """Cached is_verified()=True alone must not be trusted -- a live
        verify_image_exists() re-check (mirroring claude/docker_runner.py's own
        safeguard) catches a project's <project>-agent:latest tag having been
        silently overwritten by an unrelated image while cached state was stale."""
        _make_base_clone(tmp_path, "my-project")
        fake_module, fake_singleton = _fake_dev_container_state_module()
        fake_singleton.verify_image_exists.return_value = False  # live check disagrees

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.baked_dependency_extractor.extract_baked_dependencies') as mock_extract:
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                    manager.get_project_dir(
                        "my-project", epic_id="304", branch_name="feature/issue-304"
                    )

        mock_extract.assert_not_called()

    def test_reuse_on_second_call_does_not_re_trigger_extraction(self, manager, tmp_path):
        _make_base_clone(tmp_path, "my-project")
        fake_module, _ = _fake_dev_container_state_module()

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.baked_dependency_extractor.extract_baked_dependencies') as mock_extract:
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                    created = manager.get_project_dir(
                        "my-project", epic_id="303", branch_name="feature/issue-303"
                    )
                assert mock_extract.call_count == 1
                # See TestReuseExistingEpicWorktree.test_second_call_reuses_without_git_calls
                # for why this is needed now that cache hits also check for .git.
                created.mkdir(parents=True, exist_ok=True)
                (created / '.git').mkdir()

                # Second call for the same epic reuses the in-flight worktree -- no
                # new git calls, and critically no repeat extraction attempt.
                with patch('services.project_workspace.subprocess.run') as mock_run2:
                    manager.get_project_dir("my-project", epic_id="303")
                    mock_run2.assert_not_called()
                assert mock_extract.call_count == 1

    def test_adopted_pre_existing_worktree_does_not_trigger_extraction(self, manager, tmp_path):
        """A worktree found already on disk (surviving an orchestrator restart) is
        adopted, not newly created -- extraction only ever runs on actual creation."""
        _make_base_clone(tmp_path, "my-project")
        pre_existing = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '304'
        pre_existing.mkdir(parents=True)
        (pre_existing / '.git').write_text("gitdir: /fake/base/.git/worktrees/304\n")
        fake_module, _ = _fake_dev_container_state_module()

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.baked_dependency_extractor.extract_baked_dependencies') as mock_extract:
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.return_value = _ok("feature/issue-304\n")
                    manager.get_or_create_epic_worktree(
                        "my-project", "304", branch_name="feature/issue-304"
                    )

        mock_extract.assert_not_called()

    def test_extraction_failure_never_blocks_worktree_creation(self, manager, tmp_path):
        """Even if the extraction call itself raises unexpectedly (it shouldn't --
        extract_baked_dependencies() has its own internal guard -- but this proves the
        integration point has a second, independent safety net), get_or_create_epic_worktree
        must still return the worktree path successfully."""
        _make_base_clone(tmp_path, "my-project")
        fake_module, _ = _fake_dev_container_state_module()
        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '305'

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch(
                'services.baked_dependency_extractor.extract_baked_dependencies',
                side_effect=RuntimeError("boom"),
            ):
                with patch('services.project_workspace.subprocess.run') as mock_run:
                    mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                    result = manager.get_project_dir(
                        "my-project", epic_id="305", branch_name="feature/issue-305"
                    )

        assert result == expected_path
        assert manager._epic_worktrees[("my-project", "305")] == str(expected_path)

    def test_dev_container_state_lookup_failure_never_blocks_worktree_creation(self, manager, tmp_path):
        """Any exception out of the dev_container_state lookup itself (e.g. a state
        file read error, or -- in an environment without ORCHESTRATOR_ROOT such as a
        plain local test run -- the real singleton's own import/construction failing)
        must be swallowed by _extract_baked_dependencies_if_available's outer guard.
        Worktree creation must still succeed."""
        _make_base_clone(tmp_path, "my-project")
        fake_module, fake_singleton = _fake_dev_container_state_module()
        fake_singleton.is_verified.side_effect = RuntimeError("state file corrupt")
        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '306'

        with patch.dict(sys.modules, {'services.dev_container_state': fake_module}):
            with patch('services.project_workspace.subprocess.run') as mock_run:
                mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                result = manager.get_project_dir(
                    "my-project", epic_id="306", branch_name="feature/issue-306"
                )

        assert result == expected_path
        assert manager._epic_worktrees[("my-project", "306")] == str(expected_path)

    def test_dev_container_state_import_failure_never_blocks_worktree_creation(self, manager, tmp_path):
        """In an environment without ORCHESTRATOR_ROOT (e.g. a plain local test run),
        importing the real dev_container_state singleton itself raises. That must be
        swallowed too -- worktree creation still succeeds. Forces the real (unfaked)
        import path by deleting any cached sys.modules entry first, so this is
        deterministic regardless of what earlier tests in the same session imported."""
        _make_base_clone(tmp_path, "my-project")
        expected_path = tmp_path / '.orchestrator' / 'worktrees' / 'my-project' / '307'

        with patch.dict(sys.modules):
            sys.modules.pop('services.dev_container_state', None)
            with patch('services.project_workspace.subprocess.run') as mock_run:
                mock_run.side_effect = [_ok(), _ok(), _ok(), _fail("no local ref"), _ok()]
                result = manager.get_project_dir(
                    "my-project", epic_id="307", branch_name="feature/issue-307"
                )

        assert result == expected_path
        assert manager._epic_worktrees[("my-project", "307")] == str(expected_path)


class TestPushStrayBranchIfAhead:
    """_push_stray_branch_if_ahead() -- final whole-PR review pass 2 on #87.
    `worktree add -B` unconditionally resets an existing local branch ref, which
    is correct for a genuinely stray/stale ref (see _add_epic_worktree's own
    comments) but would silently discard real commits if that ref happens to
    hold unpushed work (e.g. left behind by a force-removed worktree whose own
    push-before-removal attempt failed)."""

    def test_no_local_ref_is_a_noop(self, tmp_path):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _fail("unknown revision")  # rev-parse --verify fails
            ProjectWorkspaceManager._push_stray_branch_if_ahead(tmp_path, "feature/issue-1")

        # Only the rev-parse --verify check -- no push attempted for a ref that
        # doesn't exist.
        assert mock_run.call_count == 1

    def test_local_ref_already_matches_origin_is_a_noop(self, tmp_path):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok("0\n")]  # verify ok, 0 commits ahead
            ProjectWorkspaceManager._push_stray_branch_if_ahead(tmp_path, "feature/issue-1")

        assert mock_run.call_count == 2  # verify + rev-list, no push

    def test_ahead_of_origin_pushes_before_reset_would_discard_it(self, tmp_path):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok("3\n"), _ok()]  # verify ok, 3 ahead, push ok
            ProjectWorkspaceManager._push_stray_branch_if_ahead(tmp_path, "feature/issue-1")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ['git', '-C', str(tmp_path), 'push', 'origin',
                             'feature/issue-1:feature/issue-1']

    def test_no_origin_ref_at_all_still_attempts_push(self, tmp_path):
        """The whole local branch is unpushed (origin/<branch> doesn't exist) --
        can't compute an ahead-count, but there's still something to try to save."""
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _fail("unknown revision"), _ok()]
            ProjectWorkspaceManager._push_stray_branch_if_ahead(tmp_path, "feature/issue-1")

        calls = [c.args[0] for c in mock_run.call_args_list]
        assert calls[2] == ['git', '-C', str(tmp_path), 'push', 'origin',
                             'feature/issue-1:feature/issue-1']

    def test_push_failure_is_logged_but_never_raises(self, tmp_path, caplog):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [_ok(), _ok("2\n"), _fail("non-fast-forward")]
            with caplog.at_level("ERROR"):
                ProjectWorkspaceManager._push_stray_branch_if_ahead(tmp_path, "feature/issue-1")

        assert any("lost" in r.message for r in caplog.records)

    def test_subprocess_exception_never_raises(self, tmp_path):
        with patch('services.project_workspace.subprocess.run', side_effect=OSError("boom")):
            ProjectWorkspaceManager._push_stray_branch_if_ahead(tmp_path, "feature/issue-1")  # must not raise


class TestGetRunningContainerMountSources:
    """_get_running_container_mount_sources() -- host-side bind-mount sources for
    every running switchyard-managed container, used by prune_epic_worktrees() to
    avoid force-removing a worktree a live container still has mounted (final
    whole-PR review pass on #87, directly relevant to #52's pilot rollout, which
    explicitly soak-tests a forced restart mid-epic)."""

    def test_no_running_containers_returns_empty_set(self):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _ok(stdout="")  # `docker ps` -> no names
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()
        mock_run.assert_called_once()  # docker inspect never called -- nothing to inspect

    def test_docker_ps_failure_returns_empty_set(self):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.return_value = _fail("docker daemon not running")
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()

    def test_docker_inspect_failure_returns_empty_set(self):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(stdout="repair-cycle-my-project-100-abc12345\n"),  # docker ps
                _fail("no such container"),  # docker inspect
            ]
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()

    def test_collects_mount_sources_across_multiple_containers(self):
        import json as _json
        mounts_c1 = _json.dumps([
            {"Source": "/host/workspace/.orchestrator/worktrees/my-project/42", "Destination": "/workspace/.orchestrator/worktrees/my-project/42"},
            {"Source": "/host/workspace/switchyard", "Destination": "/app"},
        ])
        mounts_c2 = _json.dumps([
            {"Source": "/host/workspace/my-project", "Destination": "/workspace/my-project"},
        ])
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(stdout="container-1\ncontainer-2\n"),  # docker ps
                _ok(stdout=f"{mounts_c1}\n{mounts_c2}\n"),  # docker inspect, one JSON array per line
            ]
            result = ProjectWorkspaceManager._get_running_container_mount_sources()

        assert result == {
            "/host/workspace/.orchestrator/worktrees/my-project/42",
            "/host/workspace/switchyard",
            "/host/workspace/my-project",
        }

    def test_mount_entry_missing_source_key_is_skipped_not_a_crash(self):
        import json as _json
        mounts = _json.dumps([{"Destination": "/workspace/my-project"}])  # no "Source"
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(stdout="container-1\n"),
                _ok(stdout=mounts),
            ]
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()

    def test_malformed_json_line_is_skipped_not_a_crash(self):
        with patch('services.project_workspace.subprocess.run') as mock_run:
            mock_run.side_effect = [
                _ok(stdout="container-1\n"),
                _ok(stdout="{not valid json"),
            ]
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()

    def test_subprocess_exception_returns_empty_set_not_a_crash(self):
        with patch('services.project_workspace.subprocess.run', side_effect=OSError("docker not found")):
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()

    def test_timeout_returns_empty_set_not_a_crash(self):
        import subprocess as _subprocess
        with patch('services.project_workspace.subprocess.run',
                    side_effect=_subprocess.TimeoutExpired(cmd="docker", timeout=10)):
            result = ProjectWorkspaceManager._get_running_container_mount_sources()
        assert result == set()


class TestPruneEpicWorktreesLivenessCheck:
    """prune_epic_worktrees() must not force-remove a worktree that's currently
    bind-mounted into a live, running switchyard-managed container (e.g. a
    repair-cycle container that survived an orchestrator restart --
    reconnect_repair_cycle_container() resumes monitoring it without ever
    populating _epic_worktrees, so the already-existing tracked-check alone
    doesn't catch this case).

    The container-side -> host-side path translation this checks
    (worktree_path_str.startswith('/workspace/')) only fires for paths actually
    rooted at the in-container /workspace mount -- which the other tests in this
    file deliberately avoid by using `tmp_path` as workspace_root (so they don't
    depend on real filesystem access under /workspace, which isn't writable
    outside the orchestrator container). Exercising that branch here means
    driving prune_epic_worktrees() over a *simulated* /workspace tree instead:
    workspace_root is set to Path('/workspace') and every Path.is_dir()/
    iterdir() call the sweep makes is stubbed in the exact order the method
    calls them, rather than touching a real directory.
    """

    def _manager_with_fake_workspace(self, tmp_path):
        manager = ProjectWorkspaceManager(workspace_root=tmp_path)
        manager.workspace_root = Path('/workspace')
        return manager

    def test_skips_worktree_currently_mounted_into_a_live_container(self, tmp_path):
        manager = self._manager_with_fake_workspace(tmp_path)
        project_staging = Path('/workspace/.orchestrator/worktrees/my-project')
        worktree_path = Path('/workspace/.orchestrator/worktrees/my-project/42')

        # Call order the sweep makes for a single project / single worktree,
        # entirely skipped via the liveness `continue` (no removal-path calls).
        is_dir_calls = [True, True, True, True]
        iterdir_calls = [[project_staging], [worktree_path], [worktree_path]]

        with patch.object(Path, 'is_dir', side_effect=is_dir_calls), \
             patch.object(Path, 'iterdir', side_effect=iterdir_calls), \
             patch.object(ProjectWorkspaceManager, '_get_running_container_mount_sources',
                           return_value={'/host/workspace/.orchestrator/worktrees/my-project/42'}), \
             patch.object(ProjectWorkspaceManager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.shutil.rmtree') as mock_rmtree, \
             patch('claude.docker_runner.DockerAgentRunner') as mock_runner_cls, \
             patch('services.project_workspace.subprocess.run') as mock_subprocess_run:

            mock_runner_cls._detect_host_workspace_path.return_value = '/host/workspace'
            mock_subprocess_run.return_value = _ok()

            manager.prune_epic_worktrees()

        # Neither the push-before-remove step nor the actual removal ran --
        # the worktree was skipped outright because it's still mounted live.
        mock_push.assert_not_called()
        mock_rmtree.assert_not_called()
        remove_calls = [
            c for c in mock_subprocess_run.call_args_list
            if 'remove' in c.args[0]
        ]
        assert remove_calls == []

    def test_still_removes_worktree_not_mounted_into_any_container(self, tmp_path):
        """The liveness check must actually discriminate -- a worktree whose host
        path ISN'T in the running-container mount set gets removed as before,
        not unconditionally protected just because some containers are running."""
        manager = self._manager_with_fake_workspace(tmp_path)
        project_staging = Path('/workspace/.orchestrator/worktrees/my-project')
        worktree_path = Path('/workspace/.orchestrator/worktrees/my-project/42')

        is_dir_calls = [True, True, True, True, True]
        iterdir_calls = [[project_staging], [worktree_path], [worktree_path]]

        # Path.exists patched True (represents a healthy worktree with .git
        # present) -- this class's own new corrupted-worktree skip (code
        # review finding) would otherwise treat every worktree here as "no
        # .git" by default, since .exists() is unpatched by default and
        # always False against this simulated, non-real /workspace path.
        with patch.object(Path, 'is_dir', side_effect=is_dir_calls), \
             patch.object(Path, 'iterdir', side_effect=iterdir_calls), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(ProjectWorkspaceManager, '_get_running_container_mount_sources',
                           return_value={'/host/workspace/.orchestrator/worktrees/some-other-project/99'}), \
             patch.object(ProjectWorkspaceManager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.shutil.rmtree') as mock_rmtree, \
             patch('claude.docker_runner.DockerAgentRunner') as mock_runner_cls, \
             patch('services.project_workspace.subprocess.run') as mock_subprocess_run:

            mock_runner_cls._detect_host_workspace_path.return_value = '/host/workspace'
            mock_subprocess_run.return_value = _ok()

            manager.prune_epic_worktrees()

        mock_push.assert_called_once_with(worktree_path)
        mock_rmtree.assert_called_once_with(worktree_path, ignore_errors=True)
        remove_calls = [
            c for c in mock_subprocess_run.call_args_list
            if 'remove' in c.args[0]
        ]
        assert len(remove_calls) == 1

    def test_liveness_check_failure_falls_back_to_removing(self, tmp_path):
        """If host-path translation itself blows up (e.g. DockerAgentRunner import
        fails), prune must log and fall back to its pre-existing behavior for that
        worktree (remove it), not crash the whole sweep."""
        manager = self._manager_with_fake_workspace(tmp_path)
        project_staging = Path('/workspace/.orchestrator/worktrees/my-project')
        worktree_path = Path('/workspace/.orchestrator/worktrees/my-project/42')

        is_dir_calls = [True, True, True, True, True]
        iterdir_calls = [[project_staging], [worktree_path], [worktree_path]]

        # Path.exists patched True (represents a healthy worktree with .git
        # present) -- this class's own new corrupted-worktree skip (code
        # review finding) would otherwise treat every worktree here as "no
        # .git" by default, since .exists() is unpatched by default and
        # always False against this simulated, non-real /workspace path.
        with patch.object(Path, 'is_dir', side_effect=is_dir_calls), \
             patch.object(Path, 'iterdir', side_effect=iterdir_calls), \
             patch.object(Path, 'exists', return_value=True), \
             patch.object(ProjectWorkspaceManager, '_get_running_container_mount_sources',
                           return_value={'/host/workspace/.orchestrator/worktrees/my-project/42'}), \
             patch.object(ProjectWorkspaceManager, '_push_local_commits_if_any') as mock_push, \
             patch('services.project_workspace.shutil.rmtree') as mock_rmtree, \
             patch('claude.docker_runner.DockerAgentRunner') as mock_runner_cls, \
             patch('services.project_workspace.subprocess.run') as mock_subprocess_run:

            mock_runner_cls._detect_host_workspace_path.side_effect = RuntimeError("boom")
            mock_subprocess_run.return_value = _ok()

            manager.prune_epic_worktrees()

        mock_push.assert_called_once_with(worktree_path)
        mock_rmtree.assert_called_once_with(worktree_path, ignore_errors=True)

"""
Unit tests for ProjectWorkspaceManager.reconcile_worktree_branch() (#163).

#149 hardened the three commit paths so none of them will commit an agent's work
onto a branch that is not this dispatch's own target. They refuse, leave the work
uncommitted on disk, and mark_failed() the run. What none of them did was repair
the drift -- and the drift is on disk and permanent, while the refusal is
per-dispatch.

The next dispatch is a fresh PipelineRun, so resolve_workspace()'s idempotency
guard does not apply, get_or_create_epic_worktree() takes its cache-hit path
without touching git, and resolve_workspace() then read the worktree's ambient
HEAD and persisted the DRIFTED branch as that run's own expectation. Its own
verification compared the drift against itself, passed, and committed both
issues' work onto it -- #143's wrong-branch commit, deferred by exactly one
dispatch.

This method is the line #163 draws instead, and it is deliberately not "does HEAD
equal what this run resolved":

  * a branch that belongs to this EPIC is adopted exactly as before (that is the
    legitimate restart-adoption case resolve_workspace() has always handled),
  * a branch that belongs to no epic is drift: repaired when the working tree is
    clean, and refused when it is not.

Nothing durable is written for either, which is the other half of the design: the
refusal is re-derived from the worktree's live HEAD and working tree on every
resolution, so it cannot outlive the drift and there is no quarantine to clear.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from services.project_workspace import (
    ProjectWorkspaceManager,
    WorktreeBranchStatus,
)


EPIC_BRANCH = 'feature/issue-42-epic'
WORKTREE = '/workspace/.orchestrator/worktrees/my-project/42'


def _result(returncode=0, stdout="", stderr="") -> Mock:
    result = Mock()
    result.returncode = returncode
    result.stdout = stdout
    result.stderr = stderr
    return result


class FakeGit:
    """A subprocess.run stand-in for the handful of git commands this method runs.

    Stateful on purpose: `checkout` moves `head`, so a test can assert the
    post-checkout re-read this method does (it confirms the branch from git rather
    than trusting a zero exit status) sees the right thing.
    """

    def __init__(self, head, status="", status_rc=0, local_branches=(), checkout_ok=True,
                 checkout_moves_head=True):
        self.head = head
        self.status = status
        self.status_rc = status_rc
        self.local_branches = set(local_branches)
        self.checkout_ok = checkout_ok
        self.checkout_moves_head = checkout_moves_head
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if 'rev-parse' in cmd and '--abbrev-ref' in cmd:
            return _result(0, f"{self.head}\n" if self.head else "HEAD\n")
        if 'rev-parse' in cmd and '--verify' in cmd:
            branch = cmd[-1].split('refs/heads/')[-1]
            return _result(0 if branch in self.local_branches else 1)
        if 'status' in cmd:
            if self.status_rc != 0:
                return _result(self.status_rc, "", "fatal: not a git repository")
            return _result(0, self.status)
        if 'checkout' in cmd:
            if not self.checkout_ok:
                return _result(1, "", "error: pathspec did not match")
            if self.checkout_moves_head:
                self.head = cmd[-1]
            return _result(0)
        return _result(0)

    def ran(self, verb: str) -> bool:
        return any(verb in call for call in self.calls)


@pytest.fixture
def manager(tmp_path):
    return ProjectWorkspaceManager(workspace_root=tmp_path)


def _reconcile(manager, git, expected_branch=EPIC_BRANCH, epic_id='42'):
    with patch('services.project_workspace.subprocess.run', side_effect=git):
        return manager.reconcile_worktree_branch(
            'my-project', epic_id, WORKTREE, expected_branch, issue_number=101
        )


class TestTheOrdinaryCases:
    """Nothing about a worktree on its own epic's branch may change."""

    def test_head_on_the_resolved_branch_is_a_match(self, manager):
        git = FakeGit(head=EPIC_BRANCH)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.MATCH
        assert verdict.branch == EPIC_BRANCH
        # Not even asked: there is nothing to decide, so no `git status` cost on
        # the path every healthy dispatch takes.
        assert not git.ran('status')
        assert not git.ran('checkout')

    def test_a_different_branch_of_the_same_epic_is_adopted(self, manager):
        """The legitimate mismatch resolve_workspace() has always handled: an
        adopted worktree whose real branch differs from the name a fresh
        resolution derived. Both are epic #42's own branch, so the worktree's
        real one wins -- unchanged behaviour, and deliberately NOT treated as
        drift."""
        git = FakeGit(head='feature/issue-42-actually-on-disk')
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.EPIC_BRANCH
        assert verdict.branch == 'feature/issue-42-actually-on-disk'
        assert not git.ran('checkout')
        # The tracked-branch map stops naming a branch this worktree is not on.
        assert manager._epic_worktree_branches[('my-project', '42')] == (
            'feature/issue-42-actually-on-disk'
        )

    def test_a_detached_head_is_unknown_not_drift(self, manager):
        """_current_worktree_branch() is documented best-effort and answers None
        for a transient git failure as readily as for a detached HEAD. Turning
        that into a blocked dispatch would be a new failure mode in exchange for
        a check #149 already does at the point it matters."""
        git = FakeGit(head=None)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.UNKNOWN
        assert verdict.branch == EPIC_BRANCH
        assert not git.ran('checkout')


class TestCleanDriftIsRepaired:
    """The case a quarantine would have wedged an epic over for no gain: HEAD is
    off the epic but there is nothing in the worktree to preserve or to decide."""

    def test_head_is_restored_to_the_epics_branch(self, manager):
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.REPAIRED
        assert verdict.branch == EPIC_BRANCH
        assert verdict.found_branch == 'scratch'
        assert git.ran('checkout')
        assert manager._epic_worktree_branches[('my-project', '42')] == EPIC_BRANCH

    def test_a_repair_that_does_not_take_is_refused_not_assumed(self, manager):
        """The confirmation re-read exists because the whole point of this method
        is to stop trusting an unverified branch -- a zero exit status is not the
        same claim as 'HEAD is now on that branch'."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      checkout_moves_head=False)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.branch is None

    def test_a_refused_checkout_is_not_forced(self, manager):
        """Most plausibly: the branch is checked out in another worktree. Reported
        as a failed repair, never forced."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      checkout_ok=False)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert not any('--force' in call for call in git.calls)

    def test_a_branch_that_does_not_exist_locally_is_never_created(self, manager):
        """The realistic reason the expected branch is missing is that it is
        create_feature_branch_name()'s generated fallback for an epic whose real
        branch is named something else -- in which case moving HEAD there is
        wrong, not a repair."""
        git = FakeGit(head='scratch', status="", local_branches=[])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert not git.ran('checkout')


class TestDirtyDriftIsRefused:
    """THE regression this item exists for. The worktree holds an earlier
    dispatch's uncommitted work and HEAD is on a branch belonging to no epic:
    committing that to the drifted branch is #143, committing it to the epic's
    branch without a human confirming whose work it is is worse, and discarding
    it is unrecoverable. So nothing is touched and the caller refuses."""

    def test_nothing_on_disk_is_touched(self, manager):
        git = FakeGit(head='scratch', status=" M the_agents_work.py\n",
                      local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.branch is None, (
            "a drifted verdict must offer no branch at all -- offering one is how "
            "the drift got adopted as the next run's expectation in the first place"
        )
        assert verdict.dirty is True
        assert verdict.found_branch == 'scratch'
        assert verdict.expected_branch == EPIC_BRANCH
        assert not git.ran('checkout')
        assert not git.ran('commit')
        assert not git.ran('stash')

    def test_untracked_files_alone_are_enough_to_refuse(self, manager):
        git = FakeGit(head='scratch', status="?? brand_new_module.py\n",
                      local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert not git.ran('checkout')

    def test_an_unreadable_working_tree_is_refused_too(self, manager):
        git = FakeGit(head='scratch', status_rc=128, local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.dirty is None
        assert not git.ran('checkout')

    def test_the_refusal_clears_itself_once_the_worktree_is_clean(self, manager):
        """Nothing durable records it. A human commits or discards the work and
        the very next resolution repairs the branch and proceeds -- there is no
        marker file, no operator 'clear' step, and nothing for
        prune_epic_worktrees() to destroy."""
        git = FakeGit(head='scratch', status=" M work.py\n", local_branches=[EPIC_BRANCH])
        assert _reconcile(manager, git).status is WorktreeBranchStatus.DRIFTED

        git.status = ""
        assert _reconcile(manager, git).status is WorktreeBranchStatus.REPAIRED

    def test_a_drifted_branch_belonging_to_another_epic_is_still_drift(self, manager):
        """Epic-ownership is the rule, not 'looks like a feature branch': a
        sibling epic's branch in this epic's worktree is exactly the cross-issue
        contamination #143 produced."""
        git = FakeGit(head='feature/issue-77-other-epic', status=" M work.py\n",
                      local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED


class TestBranchOwnership:
    """The single rule separating 'adopt it' from 'never adopt it'. Delegated to
    FeatureBranchManager so it stays tied to the same parse that generates and
    discovers epic branch names."""

    @pytest.mark.parametrize("branch,epic_id,expected", [
        ('feature/issue-42-epic', '42', True),
        ('feature/issue-42', 42, True),
        ('feature/issue-420-other', '42', False),
        ('feature/issue-77-other-epic', '42', False),
        ('scratch', '42', False),
        ('main', '42', False),
        ('', '42', False),
        (None, '42', False),
        ('feature/issue-42-epic', 'not-a-number', False),
    ])
    def test_ownership(self, branch, epic_id, expected):
        from services.feature_branch_manager import feature_branch_manager

        assert feature_branch_manager.branch_belongs_to_epic(branch, epic_id) is expected


class TestSurveyEpicWorktrees:
    """The operator entry point (#163 requirement 1). Neither condition this item
    creates is written down anywhere -- both are derived from the worktree's live
    state -- so there has to be somewhere to SEE them: a refusal that blocks an
    epic, and a worktree the prune sweep is deliberately leaving on disk.

    Backs both `scripts/inspect_epic_worktrees.py` and /api/epic-worktrees.
    """

    def _stage(self, tmp_path, project, epic_id):
        path = tmp_path / '.orchestrator' / 'worktrees' / project / str(epic_id)
        path.mkdir(parents=True)
        (path / '.git').write_text(f"gitdir: /fake/base/.git/worktrees/{epic_id}\n")
        return path

    def test_reports_a_drifted_dirty_worktree_with_what_is_uncommitted_in_it(
        self, manager, tmp_path
    ):
        path = self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head='scratch', status=" M the_agents_work.py\n?? notes.md\n")

        with patch('services.project_workspace.subprocess.run', side_effect=git), \
             patch('services.feature_branch_manager.feature_branch_manager.'
                   'resolve_epic_branch_name', return_value=EPIC_BRANCH):
            rows = manager.survey_epic_worktrees()

        assert len(rows) == 1
        row = rows[0]
        assert row['project'] == 'my-project'
        assert row['epic_id'] == '42'
        assert row['path'] == str(path)
        assert row['current_branch'] == 'scratch'
        assert row['expected_branch'] == EPIC_BRANCH
        assert row['drifted'] is True
        assert row['uncommitted'] is True
        assert row['prune_skipped'] is True
        assert row['uncommitted_files'] == [' M the_agents_work.py', '?? notes.md']

    def test_a_healthy_worktree_reports_nothing_to_act_on(self, manager, tmp_path):
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head=EPIC_BRANCH, status="")

        with patch('services.project_workspace.subprocess.run', side_effect=git), \
             patch('services.feature_branch_manager.feature_branch_manager.'
                   'resolve_epic_branch_name', return_value=EPIC_BRANCH):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['drifted'] is False
        assert rows[0]['prune_skipped'] is False

    def test_a_clean_worktree_on_the_epics_branch_is_not_drift_even_when_dirty_elsewhere(
        self, manager, tmp_path
    ):
        """Uncommitted work on the epic's OWN branch is an ordinary
        interrupted run: prune leaves it alone, but nothing is blocked."""
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head=EPIC_BRANCH, status=" M work.py\n")

        with patch('services.project_workspace.subprocess.run', side_effect=git), \
             patch('services.feature_branch_manager.feature_branch_manager.'
                   'resolve_epic_branch_name', return_value=EPIC_BRANCH):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['drifted'] is False
        assert rows[0]['prune_skipped'] is True

    def test_scoped_to_one_project(self, manager, tmp_path):
        self._stage(tmp_path, 'my-project', 42)
        self._stage(tmp_path, 'other-project', 7)
        git = FakeGit(head=EPIC_BRANCH, status="")

        with patch('services.project_workspace.subprocess.run', side_effect=git), \
             patch('services.feature_branch_manager.feature_branch_manager.'
                   'resolve_epic_branch_name', return_value=EPIC_BRANCH):
            rows = manager.survey_epic_worktrees(project_name='other-project')

        assert [row['project'] for row in rows] == ['other-project']

    def test_empty_when_nothing_is_staged(self, manager):
        assert manager.survey_epic_worktrees() == []

    def test_never_raises_on_an_unreadable_worktree(self, manager, tmp_path):
        """It backs a diagnostic CLI and an HTTP handler; a broken worktree is
        the reason someone is running it."""
        self._stage(tmp_path, 'my-project', 42)

        with patch('services.project_workspace.subprocess.run',
                   side_effect=OSError("git is gone")), \
             patch('services.feature_branch_manager.feature_branch_manager.'
                   'resolve_epic_branch_name', side_effect=RuntimeError("no base clone")):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['current_branch'] is None
        assert rows[0]['expected_branch'] is None
        assert rows[0]['uncommitted'] is None
        # Unreadable is not drift (matching reconcile_worktree_branch()'s UNKNOWN),
        # but it IS skipped by prune, which is the half an operator must see.
        assert rows[0]['drifted'] is False
        assert rows[0]['prune_skipped'] is True

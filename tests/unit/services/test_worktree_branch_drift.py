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
  * a branch that belongs to no epic is drift: repaired only when that directory
    provably holds nothing the epic's branch does not (a clean working tree AND no
    commits of its own), and refused otherwise.

Nothing durable is written for either, which is the other half of the design: the
refusal is re-derived from the worktree's live HEAD and working tree on every
resolution, so it cannot outlive the drift and there is no quarantine to clear.
"""

import json
import subprocess
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
                 checkout_moves_head=True, ahead=0, ahead_rc=0, mounted_sources=(),
                 head_rc=0, docker_rc=0):
        self.head = head
        # `rev-parse --abbrev-ref HEAD` failing outright, as opposed to answering
        # the literal string `HEAD` for a detached one -- the two states this
        # method must tell apart.
        self.head_rc = head_rc
        # `docker ps` failing, i.e. the liveness question is unanswerable rather
        # than answered "nothing is running".
        self.docker_rc = docker_rc
        self.status = status
        self.status_rc = status_rc
        self.local_branches = set(local_branches)
        self.checkout_ok = checkout_ok
        self.checkout_moves_head = checkout_moves_head
        # Commits the drifted branch has that the restore target does not -- the
        # question a clean `git status` says nothing about.
        self.ahead = ahead
        self.ahead_rc = ahead_rc
        # `docker ps` / `docker inspect` answers for the liveness gate.
        self.mounted_sources = list(mounted_sources)
        self.calls = []

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        if cmd[0] == 'docker':
            if self.docker_rc != 0:
                return _result(self.docker_rc, "", "Cannot connect to the Docker daemon")
            if 'ps' in cmd:
                return _result(0, "live-container\n" if self.mounted_sources else "")
            return _result(0, json.dumps(
                [{'Source': src} for src in self.mounted_sources]
            ) + "\n")
        if 'rev-parse' in cmd and '--abbrev-ref' in cmd:
            if self.head_rc != 0:
                return _result(self.head_rc, "", "fatal: not a git repository")
            return _result(0, f"{self.head}\n" if self.head else "HEAD\n")
        if 'rev-parse' in cmd and '--verify' in cmd:
            branch = cmd[-1].split('refs/heads/')[-1]
            return _result(0 if branch in self.local_branches else 1)
        if 'for-each-ref' in cmd:
            return _result(0, "".join(f"{b}\n" for b in sorted(self.local_branches)))
        if 'rev-list' in cmd:
            if self.ahead_rc != 0:
                return _result(self.ahead_rc, "", "fatal: bad revision")
            return _result(0, f"{self.ahead}\n")
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

    def test_an_unreadable_head_is_unknown_not_drift(self, manager):
        """`rev-parse` failing outright is best-effort's transient case: turning
        it into a blocked dispatch would be a new failure mode in exchange for a
        check #149 already does at the point it matters. Deliberately NOT the
        same as a detached HEAD, which git answers definitively -- see
        TestADetachedHeadIsDrift."""
        git = FakeGit(head=None, head_rc=128)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.UNKNOWN
        assert verdict.branch == EPIC_BRANCH
        assert not git.ran('checkout')

    def test_an_unreadable_head_says_so_in_the_log(self, manager, caplog):
        """_read_worktree_head() ended in a bare `except Exception: return None,
        False` with no logging at all, and returned a non-zero exit the same way
        -- alone among the helpers this method uses (code review on #163). It
        resolves to UNKNOWN, the one verdict that neither blocks nor repairs, so
        without a line here the whole gate could abstain and leave no trace of
        why anywhere."""
        import logging

        with caplog.at_level(logging.WARNING, logger='services.project_workspace'):
            _reconcile(manager, FakeGit(head=None, head_rc=128))
            _reconcile(manager, FakeGit(head=None, head_rc=128))

        messages = [r.message for r in caplog.records]
        assert any(
            'Could not read the checked-out branch' in m and 'rev-parse rc=128' in m
            for m in messages
        )

    def test_an_unrunnable_head_read_says_so_in_the_log(self, manager, caplog):
        """The exception half of the same gap -- a `rev-parse` that times out
        under load is the realistic way this happens."""
        import logging

        def boom(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 10)

        with caplog.at_level(logging.WARNING, logger='services.project_workspace'), \
             patch('services.project_workspace.subprocess.run', side_effect=boom):
            verdict = manager.reconcile_worktree_branch(
                'my-project', '42', WORKTREE, EPIC_BRANCH, issue_number=101
            )

        assert verdict.status is WorktreeBranchStatus.UNKNOWN
        assert any(
            'Could not read the checked-out branch' in r.message for r in caplog.records
        )


class TestADetachedHeadIsDrift:
    """A detached HEAD used to reach the same UNKNOWN verdict an unreadable one
    does, because _current_worktree_branch() collapsed both into None (code
    review on #163) -- so the dispatch PROCEEDED with its own resolved branch.

    `git checkout <sha>` / an aborted rebase inside an epic worktree is a shape
    auto_commit.py already documents as observed. The commit path refuses over it
    and leaves the work uncommitted on disk; the next sibling sub-issue then
    reconciled to UNKNOWN, ran an agent on top of that unclaimed work, and only
    the commit-time check caught it -- now with two issues' work mixed in one
    directory, which is exactly what the pre-dispatch refusal exists to prevent.
    """

    def test_a_detached_head_with_dirty_work_is_refused(self, manager):
        git = FakeGit(head=None, status=" M someone_elses_work.py\n",
                      local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.branch is None, (
            "offering a branch here is how the dispatch proceeded onto work "
            "nobody had claimed"
        )
        assert verdict.found_branch is None
        assert verdict.dirty is True
        assert 'detached HEAD' in verdict.detail
        assert not git.ran('checkout')

    def test_a_clean_detached_head_is_repaired_rather_than_wedged(self, manager):
        """Drift is not a synonym for refusal: the same repair every other clean
        drift gets applies here, so an aborted rebase does not wedge the epic."""
        git = FakeGit(head=None, status="", local_branches=[EPIC_BRANCH], ahead=0)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.REPAIRED
        assert verdict.branch == EPIC_BRANCH
        assert git.ran('checkout')

    def test_commits_made_while_detached_are_counted_against_head(self, manager):
        """There is no branch name to compare against, and the commits an agent
        made while detached are reachable from HEAD and from nothing else --
        walking away from them is exactly what the ahead-count exists to stop."""
        git = FakeGit(head=None, status="", local_branches=[EPIC_BRANCH], ahead=2)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.unmerged_commits == 2
        rev_list = [call for call in git.calls if 'rev-list' in call]
        assert rev_list and f'{EPIC_BRANCH}..HEAD' in rev_list[0]
        assert not git.ran('checkout')

    def test_a_detached_head_is_not_adopted_when_no_branch_was_resolved(self, manager):
        """The `not expected_branch` arm adopts whatever HEAD is on. With no
        branch to adopt it must fall through to the drift path, not report an
        EPIC_BRANCH verdict carrying branch=None."""
        git = FakeGit(head=None, status=" M work.py\n", local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git, expected_branch=None)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.branch is None


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

    def test_a_repair_is_logged_not_only_emitted(self, manager, caplog):
        """The observability event is the only other trace of this mutation, and
        ObservabilityManager.emit() returns immediately when observability is
        disabled -- so on such an orchestrator moving HEAD in a shared checkout
        left no evidence anywhere at all."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH])

        with caplog.at_level('WARNING', logger='services.project_workspace'):
            _reconcile(manager, git)

        repaired = [r for r in caplog.records if 'Repaired epic worktree branch drift' in r.message]
        assert len(repaired) == 1
        assert 'scratch' in repaired[0].message
        assert EPIC_BRANCH in repaired[0].message

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

    def test_the_epics_sole_local_branch_is_used_when_the_resolved_name_has_no_ref(
        self, manager
    ):
        """The generated-fallback case, which used to be a PERMANENT block rather
        than a refusal that clears: resolve_epic_branch_name() returns nothing
        (a transient `git fetch --prune` failure is enough), resolve_workspace()
        falls back to create_feature_branch_name(42, "") = 'feature/issue-42-feature',
        and no such ref exists -- so the checkout was doomed on every single
        subsequent dispatch, forever, over a name the epic never used. The epic's
        one real local branch is the answer the EPIC_BRANCH case would have given
        had HEAD happened to be sitting on it."""
        git = FakeGit(head='scratch', status="", local_branches=['feature/issue-42-real'])
        verdict = _reconcile(manager, git, expected_branch='feature/issue-42-feature')

        assert verdict.status is WorktreeBranchStatus.REPAIRED
        assert verdict.branch == 'feature/issue-42-real'
        assert verdict.expected_branch == 'feature/issue-42-feature'
        assert manager._epic_worktree_branches[('my-project', '42')] == 'feature/issue-42-real'

    def test_two_local_branches_for_the_epic_are_an_ambiguity_not_a_guess(self, manager):
        git = FakeGit(head='scratch', status="",
                      local_branches=['feature/issue-42-one', 'feature/issue-42-two'])
        verdict = _reconcile(manager, git, expected_branch='feature/issue-42-feature')

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert not git.ran('checkout')

    def test_the_restore_failure_reason_reaches_the_verdict(self, manager):
        """It used to be logged and dropped, which is the difference between two
        very different operator recoveries."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      checkout_ok=False)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert 'git refused the checkout' in verdict.detail
        assert 'does not clear itself' in verdict.detail


class TestACleanTreeIsNotAnEmptyOne:
    """`git status --porcelain` goes empty the moment the agent container COMMITS
    its own work onto the drifted branch -- the shape #149's _verify_commit_branch()
    refuses over most often, since it checks the branch before it checks whether
    anything is staged.

    Repairing over that moved HEAD off the only ref those commits were reachable
    from: _push_local_commits_if_any() pushes the branch HEAD is ON, which after a
    repair is the epic's, so the prune sweep's push-before-removal safety net never
    covered them and the run reported success with none of that work in the PR.
    """

    def test_a_drifted_branch_that_is_ahead_is_refused_not_repaired(self, manager):
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], ahead=3)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.branch is None
        assert verdict.dirty is False
        assert verdict.unmerged_commits == 3
        assert '3 commit(s)' in verdict.detail
        assert not git.ran('checkout')

    def test_an_uncountable_comparison_is_refused_too(self, manager):
        """Same rule _worktree_has_uncommitted_work() applies to an unreadable
        status: guessing "empty" is the only one of the two guesses that can
        strand work."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], ahead_rc=128)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.unmerged_commits is None
        assert not git.ran('checkout')

    def test_a_drifted_branch_with_nothing_of_its_own_is_still_repaired(self, manager):
        """The control: this check must not turn every clean drift into a block."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], ahead=0)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.REPAIRED
        assert verdict.unmerged_commits == 0

    def test_the_count_is_taken_against_the_branch_head_would_move_to(self, manager):
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], ahead=1)
        _reconcile(manager, git)

        rev_list = [call for call in git.calls if 'rev-list' in call]
        assert rev_list and f'{EPIC_BRANCH}..scratch' in rev_list[0]


class TestALiveContainerIsNeverRepairedUnderneath:
    """A repair rewrites every file in the directory. The per-epic serializer this
    runs under protects against sibling RESOLUTIONS, not against a running agent
    container: nothing holds it for a container's lifetime, and pipeline locks are
    per (project, board), so a planning run for the epic and an sdlc run for one
    of its sub-issues reach this same worktree under different locks."""

    def test_a_bind_mounted_worktree_is_refused_rather_than_repaired(self, manager):
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      mounted_sources=['/host/workspace/.orchestrator/worktrees/my-project/42'])

        with patch('claude.docker_runner.DockerAgentRunner._detect_host_workspace_path',
                   return_value='/host/workspace'):
            verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert 'live, running container' in verdict.detail
        assert not git.ran('checkout')

    def test_the_liveness_refusal_is_distinguishable_from_a_failed_repair(self, manager):
        """It carries dirty=False/unmerged_commits==0, byte-identical to the
        "clean tree, nothing to preserve, but the checkout failed" verdict --
        which is the one whose operator recovery is `git checkout` in that very
        directory (code review on #163). Without its own discriminator the issue
        comment told a human to rewrite a live agent's working tree by hand, and
        claimed a shape that clears itself in minutes never clears at all."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      mounted_sources=['/host/workspace/.orchestrator/worktrees/my-project/42'])

        with patch('claude.docker_runner.DockerAgentRunner._detect_host_workspace_path',
                   return_value='/host/workspace'):
            live = _reconcile(manager, git)

        failed_repair = _reconcile(
            manager,
            FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                    checkout_ok=False),
        )

        assert live.dirty == failed_repair.dirty
        assert live.unmerged_commits == failed_repair.unmerged_commits
        assert live.container_live is True
        assert failed_repair.container_live is False
        assert 'clears itself' in live.detail

    def test_an_unanswerable_liveness_check_refuses_rather_than_repairs(self, manager):
        """The only guard on the only destructive thing this method does used to
        fail OPEN: `docker inspect` timing out under load produced an empty set,
        which read as "nothing is running", and the repair proceeded to rewrite
        every tracked file under a mid-run agent container. Unanswerable resolves
        to the non-destructive answer here, same as an unreadable `git status`
        and an uncountable rev-list."""
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      docker_rc=1)
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.container_live is None
        assert 'could not be established' in verdict.detail
        assert not git.ran('checkout')

    def test_unanswerable_does_not_promise_to_clear_itself(self, manager):
        """`None` used to be reported as container_live=True, i.e. byte-identical
        to a CONFIRMED live container -- and that shape's whole operator story is
        "do nothing, it clears itself when the container exits". With mark_failed()
        retaining the board's pipeline lock there is no next dispatch to clear it,
        and with docker merely slow there may be no container either, so the board
        sat wedged behind advice that said no action was needed (code review on
        #163). Both still refuse; only one self-clears."""
        unanswerable = _reconcile(
            manager,
            FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], docker_rc=1),
        )

        with patch('claude.docker_runner.DockerAgentRunner._detect_host_workspace_path',
                   return_value='/host/workspace'):
            confirmed = _reconcile(
                manager,
                FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                        mounted_sources=[
                            '/host/workspace/.orchestrator/worktrees/my-project/42'
                        ]),
            )

        assert unanswerable.status is confirmed.status is WorktreeBranchStatus.DRIFTED
        assert unanswerable.container_live is None
        assert confirmed.container_live is True
        assert 'clears itself' in confirmed.detail
        assert 'does NOT clear itself' in unanswerable.detail

    def test_every_drift_verdict_carries_the_liveness_answer(self, manager):
        """The probe used to run only on the path that reaches the repair, so the
        dirty, no-restore-target and unmerged-commits verdicts all carried the
        dataclass default container_live=False (code review on #163). A live agent
        container's MOST likely shape is the dirty one -- it is mid-edit, so
        porcelain is non-empty -- and _handle_wrong_branch_refusal() reads
        container_live as authoritative, so that shape told an operator to `git
        reset --hard` / `git clean -fd` a directory a running agent was writing:
        by hand, the exact tree rewrite this gate refuses to perform."""
        mounted = ['/host/workspace/.orchestrator/worktrees/my-project/42']
        shapes = {
            'dirty': FakeGit(head='scratch', status=" M the_agents_work.py\n",
                             local_branches=[EPIC_BRANCH], mounted_sources=mounted),
            'unreadable_status': FakeGit(head='scratch', status_rc=128,
                                         local_branches=[EPIC_BRANCH],
                                         mounted_sources=mounted),
            'no_restore_target': FakeGit(head='scratch', status="",
                                         local_branches=[], mounted_sources=mounted),
            'unmerged_commits': FakeGit(head='scratch', status="",
                                        local_branches=[EPIC_BRANCH], ahead=3,
                                        mounted_sources=mounted),
        }

        for label, git in shapes.items():
            with patch('claude.docker_runner.DockerAgentRunner._detect_host_workspace_path',
                       return_value='/host/workspace'):
                verdict = _reconcile(manager, git)

            assert verdict.status is WorktreeBranchStatus.DRIFTED, label
            assert verdict.container_live is True, label
            assert git.ran('ps'), f"{label} never asked the liveness question"
            assert not git.ran('checkout'), label

    def test_the_dirty_verdicts_detail_says_a_writer_is_in_there(self, manager):
        """`detail` is what gets logged, emitted as the decision event's reason,
        and printed as the issue comment's "Reason" line -- so the verdicts that
        are not themselves about liveness still have to say when one is in there,
        because every recovery they otherwise suggest rewrites that tree."""
        git = FakeGit(head='scratch', status=" M work.py\n", local_branches=[EPIC_BRANCH],
                      mounted_sources=['/host/workspace/.orchestrator/worktrees/my-project/42'])

        with patch('claude.docker_runner.DockerAgentRunner._detect_host_workspace_path',
                   return_value='/host/workspace'):
            verdict = _reconcile(manager, git)

        assert 'holds uncommitted changes' in verdict.detail
        assert 'writer is still live' in verdict.detail

    def test_the_liveness_answer_on_a_dirty_verdict_is_not_invented(self, manager):
        """The control for the above: nothing running means the dirty verdict
        still reports False, so the 'commit it or discard it' recovery -- which is
        correct and safe for that shape -- is not suppressed."""
        git = FakeGit(head='scratch', status=" M work.py\n", local_branches=[EPIC_BRANCH])
        verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.dirty is True
        assert verdict.container_live is False

    def test_a_worktree_marked_in_use_by_a_git_writer_is_refused_too(self, manager):
        """A container is not the only writer that can be mid-run in there
        (code review on #163): startup recovery's auto-commit thread takes
        mark_worktree_path_in_use() for its own lifetime, prune already consults
        that map, and the mutation this gate protects is no less destructive
        than prune's."""
        manager.mark_worktree_path_in_use(WORKTREE)
        try:
            git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH])
            verdict = _reconcile(manager, git)
        finally:
            manager.clear_worktree_path_in_use(WORKTREE)

        assert verdict.status is WorktreeBranchStatus.DRIFTED
        assert verdict.container_live is True
        assert 'marked in use' in verdict.detail
        assert not git.ran('checkout')

    def test_clearing_the_in_use_mark_lets_the_repair_proceed(self, manager):
        """The control: like every other shape of this refusal, it is re-derived
        live and clears itself the moment the condition is gone."""
        manager.mark_worktree_path_in_use(WORKTREE)
        manager.clear_worktree_path_in_use(WORKTREE)
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH])

        assert _reconcile(manager, git).status is WorktreeBranchStatus.REPAIRED

    def test_an_unrelated_running_container_does_not_block_the_repair(self, manager):
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      mounted_sources=['/host/workspace/.orchestrator/worktrees/my-project/99'])

        with patch('claude.docker_runner.DockerAgentRunner._detect_host_workspace_path',
                   return_value='/host/workspace'):
            verdict = _reconcile(manager, git)

        assert verdict.status is WorktreeBranchStatus.REPAIRED


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
        git = FakeGit(head='scratch', status=" M the_agents_work.py\n?? notes.md\n",
                      local_branches=[EPIC_BRANCH], ahead=2)

        with patch('services.project_workspace.subprocess.run', side_effect=git):
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
        assert row['unmerged_commits'] == 2
        assert row['uncommitted_files'] == [' M the_agents_work.py', '?? notes.md']
        # The epic's branch comes from local refs only. resolve_epic_branch_name()
        # would have run an untimed `git fetch --prune` in the SHARED base clone on
        # a cache miss -- an unlocked base-clone writer, from a survey documented as
        # taking no locks and writing nothing (code review on #163).
        assert not git.ran('fetch')

    def test_a_healthy_worktree_reports_nothing_to_act_on(self, manager, tmp_path):
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head=EPIC_BRANCH, status="", local_branches=[EPIC_BRANCH])

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['drifted'] is False
        assert rows[0]['prune_skipped'] is False

    def test_a_dirty_worktree_on_the_epics_own_branch_is_neither_drift_nor_prune_skipped(
        self, manager, tmp_path
    ):
        """Uncommitted work on the epic's OWN branch is an ordinary interrupted
        run -- a container SIGKILLed by a restart -- and nothing refuses over it.
        The startup sweep must still collect it, because the next sibling
        sub-issue reconciles to MATCH and auto_commit's unscoped `git add -A`
        would otherwise land the dead run's leftovers in that issue's PR."""
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head=EPIC_BRANCH, status=" M work.py\n", local_branches=[EPIC_BRANCH])

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['drifted'] is False
        assert rows[0]['uncommitted'] is True
        assert rows[0]['prune_skipped'] is False

    def test_a_clean_drifted_worktree_carrying_its_own_commits_is_prune_skipped(
        self, manager, tmp_path
    ):
        """The shape the sweep's drift rule used to miss entirely (code review on
        #163): reconcile refuses over it because "only a human can decide whether
        those commits belong on the epic's branch", the issue comment prints a
        `git log` against the path -- and then the next restart pushed the drifted
        branch to the shared repo as a stray and deleted the directory the comment
        pointed at, bypassing the adjudication the refusal demanded."""
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], ahead=3)

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['drifted'] is True
        assert rows[0]['uncommitted'] is False
        assert rows[0]['unmerged_commits'] == 3
        assert rows[0]['prune_skipped'] is True

    def test_a_clean_drifted_worktree_with_nothing_of_its_own_stays_prunable(
        self, manager, tmp_path
    ):
        """The control for the rule above: there is genuinely nothing here the
        epic's branch does not already have, so widening the skip must not pin
        every drifted directory on disk forever."""
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH], ahead=0)

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['drifted'] is True
        assert rows[0]['prune_skipped'] is False

    def test_a_detached_head_is_reported_as_drift_not_as_unreadable(
        self, manager, tmp_path
    ):
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head=None, status=" M work.py\n", local_branches=[EPIC_BRANCH])

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['current_branch'] is None
        assert rows[0]['head_detached'] is True
        assert rows[0]['drifted'] is True

    def test_a_live_container_is_reported_so_the_script_can_suppress_its_advice(
        self, manager, tmp_path
    ):
        """survey_epic_worktrees() consulted no container liveness at all, so
        scripts/inspect_epic_worktrees.py printed "nothing to preserve — move HEAD
        back" for a worktree an agent was mid-run inside (code review on #163).

        The container-side -> host-side translation only fires for paths actually
        rooted at /workspace/, which these tmp_path-rooted worktrees are not, so
        the check itself is stubbed here and exercised directly in
        TestALiveContainerIsNeverRepairedUnderneath."""
        path = self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH])

        with patch.object(ProjectWorkspaceManager, '_worktree_is_bind_mounted',
                          return_value=True) as mounted, \
             patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees(project_name='my-project')

        assert rows[0]['container_live'] is True
        assert str(mounted.call_args[0][0]) == str(path)

    def test_an_unanswerable_liveness_check_is_reported_as_unknown_not_as_idle(
        self, manager, tmp_path
    ):
        self._stage(tmp_path, 'my-project', 42)
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH],
                      docker_rc=1)

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['container_live'] is None

    def test_liveness_is_asked_once_for_the_whole_survey(self, manager, tmp_path):
        """It backs an HTTP handler; two docker round-trips per staged worktree
        is not a diagnostic cost anyone signed up for."""
        self._stage(tmp_path, 'my-project', 42)
        self._stage(tmp_path, 'my-project', 43)
        git = FakeGit(head='scratch', status="", local_branches=[EPIC_BRANCH])

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            manager.survey_epic_worktrees()

        assert len([call for call in git.calls if call[0] == 'docker']) <= 1

    def test_scoped_to_one_project(self, manager, tmp_path):
        self._stage(tmp_path, 'my-project', 42)
        self._stage(tmp_path, 'other-project', 7)
        git = FakeGit(head=EPIC_BRANCH, status="")

        with patch('services.project_workspace.subprocess.run', side_effect=git):
            rows = manager.survey_epic_worktrees(project_name='other-project')

        assert [row['project'] for row in rows] == ['other-project']

    def test_empty_when_nothing_is_staged(self, manager):
        assert manager.survey_epic_worktrees() == []

    def test_never_raises_on_an_unreadable_worktree(self, manager, tmp_path):
        """It backs a diagnostic CLI and an HTTP handler; a broken worktree is
        the reason someone is running it."""
        self._stage(tmp_path, 'my-project', 42)

        with patch('services.project_workspace.subprocess.run',
                   side_effect=OSError("git is gone")):
            rows = manager.survey_epic_worktrees()

        assert rows[0]['current_branch'] is None
        assert rows[0]['expected_branch'] is None
        assert rows[0]['uncommitted'] is None
        # Unreadable is not drift (matching reconcile_worktree_branch()'s UNKNOWN),
        # but it IS skipped by prune, which is the half an operator must see.
        assert rows[0]['drifted'] is False
        assert rows[0]['prune_skipped'] is True

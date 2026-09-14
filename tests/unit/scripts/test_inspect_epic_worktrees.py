"""
Tests for scripts/inspect_epic_worktrees.py — the operator entry point a
wrong-branch refusal's issue comment sends people to (#163).

What it prints is the recovery an operator actually runs, so the shapes it must
tell apart are the shapes whose recoveries differ. The one that matters most is
a drifted worktree an agent container is still live inside: every other clean
drift is fixed with `git checkout <epic branch>` in that directory, and running
that here means a human rewriting the working tree underneath a mid-run agent —
the exact mutation reconcile_worktree_branch()'s own liveness gate refuses to
perform (code review on #163).
"""

import pytest

from scripts.inspect_epic_worktrees import _describe_prune, _print_row


def _row(**overrides) -> dict:
    row = {
        'project': 'my-project',
        'epic_id': '42',
        'path': '/workspace/.orchestrator/worktrees/my-project/42',
        'current_branch': 'scratch',
        'head_detached': False,
        'expected_branch': 'feature/issue-42-epic',
        'epic_branches': ['feature/issue-42-epic'],
        'belongs_to_epic': False,
        'drifted': True,
        'container_live': False,
        'unmerged_commits': 0,
        'uncommitted': False,
        'uncommitted_files': [],
        'prune_skipped': False,
        'active_run_protected': False,
    }
    row.update(overrides)
    return row


class TestALiveContainerSuppressesTheMutatingAdvice:

    def test_the_move_head_hint_is_printed_when_nothing_is_running(self, capsys):
        """The control: this is the shape the hint exists for -- nothing to
        commit or discard, so the block really does persist until HEAD moves."""
        _print_row(_row())

        out = capsys.readouterr().out
        assert 'move HEAD back' in out
        assert 'checkout feature/issue-42-epic' in out

    def test_the_move_head_hint_is_suppressed_while_a_container_is_live(self, capsys):
        _print_row(_row(container_live=True))

        out = capsys.readouterr().out
        assert 'move HEAD back' not in out
        assert 'checkout feature/issue-42-epic' not in out
        assert 'LIVE CONTAINER' in out
        assert 'resolves on its own' in out

    def test_an_unanswerable_liveness_check_suppresses_it_too(self, capsys):
        """None is "docker could not be asked", which is not the same claim as
        "nothing is running" -- and the destructive reading is the one that
        costs an agent its working tree."""
        _print_row(_row(container_live=None))

        out = capsys.readouterr().out
        assert 'move HEAD back' not in out
        assert 'may still be running' in out

    def test_the_stash_and_discard_pair_is_suppressed_too(self, capsys):
        """`git stash` / `git reset --hard` / `git clean -fd` rewrite the tree
        just as thoroughly as a checkout does."""
        _print_row(_row(
            container_live=True, uncommitted=True,
            uncommitted_files=[' M work.py'], prune_skipped=True,
        ))

        out = capsys.readouterr().out
        assert 'reset --hard' not in out
        assert 'stash' not in out
        # The read-only commands stay -- seeing what is in there is always fine.
        assert 'git -C /workspace/.orchestrator/worktrees/my-project/42 status' in out


class TestDetachedHeadIsNamedAsSuch:

    def test_a_detached_head_reads_as_detached_not_unreadable(self, capsys):
        _print_row(_row(current_branch=None, head_detached=True))

        out = capsys.readouterr().out
        assert '<detached HEAD>' in out

    def test_an_unreadable_head_still_reads_as_unreadable(self, capsys):
        _print_row(_row(current_branch=None, head_detached=False, drifted=False,
                        prune_skipped=True))

        out = capsys.readouterr().out
        assert '<unreadable>' in out


class TestTheCommitCarryingDriftIsGivenAWorkingRecovery:
    """`branch -D <current_branch>` is a command git always refuses here: that
    branch is by construction the one checked out in this very worktree
    ("Cannot delete branch 'X' checked out at ..."). And the count is "commits
    the drifted branch has that the epic's does not", which for `main` or a
    sibling epic's branch never reaches zero -- so the cherry-pick/merge advice
    never terminates either, and the one instruction that does clear the block
    was printed only for the OTHER clean shape (code review on #163)."""

    def test_moving_head_is_offered_before_deleting_the_branch(self, capsys):
        _print_row(_row(unmerged_commits=3, prune_skipped=True))

        out = capsys.readouterr().out
        assert out.index('checkout feature/issue-42-epic') < out.index('branch -D')

    def test_it_is_suppressed_while_a_writer_may_be_live_in_there(self, capsys):
        _print_row(_row(unmerged_commits=3, container_live=None, prune_skipped=True))

        out = capsys.readouterr().out
        assert 'unblock it' not in out
        # The read-only `git log` of what is on that branch still prints.
        assert 'feature/issue-42-epic..scratch' in out

    def test_it_is_suppressed_while_the_tree_is_dirty(self, capsys):
        """A checkout over uncommitted work is refused by git anyway; that shape
        gets the stash/discard pair instead."""
        _print_row(_row(unmerged_commits=3, uncommitted=True,
                        uncommitted_files=[' M work.py'], prune_skipped=True))

        out = capsys.readouterr().out
        assert 'unblock it' not in out


class TestAnUnanswerableLivenessCheckIsNotPromisedToSelfResolve:
    """None used to be printed with the same "this resolves on its own once that
    container exits" as a CONFIRMED live container. mark_failed() retains the
    board's pipeline lock, so there is no next dispatch to resolve it, and with
    docker merely slow there may be no container either (code review on #163)."""

    def test_a_confirmed_live_container_keeps_its_self_resolving_promise(self, capsys):
        _print_row(_row(container_live=True))

        assert 'resolves on its own' in capsys.readouterr().out

    def test_an_unanswerable_one_does_not(self, capsys):
        _print_row(_row(container_live=None))

        out = capsys.readouterr().out
        assert 'does NOT resolve on its own' in out
        assert 'docker ps' in out


class TestALiveContainerOverWorkIsNotPromisedToSelfResolve:
    """Liveness says the directory must not be TOUCHED; it does not say the block
    lifts when that container exits. Only a live container over a directory
    holding nothing of its own is cleared by its own exit -- over a dirty tree or
    over commits the epic's branch does not have, the work is still there
    afterwards and nothing commits it on the way out (code review on #163). The
    row printed "unmerged: 3 commit(s) not on the epic's branch" and then, two
    lines later, "this resolves on its own once that container exits"."""

    def test_commits_of_its_own_are_not_reported_as_self_resolving(self, capsys):
        _print_row(_row(unmerged_commits=3, container_live=True, prune_skipped=True))

        out = capsys.readouterr().out
        assert 'this resolves on its own' not in out
        assert 'does NOT resolve on its own' in out
        assert '3 commit(s) not on the epic' in out
        # ...and still no mutating command while something is writing in there.
        assert 'unblock it' not in out
        assert 'reset --hard' not in out

    def test_a_dirty_tree_under_a_live_container_is_not_either(self, capsys):
        _print_row(_row(
            container_live=True, uncommitted=True,
            uncommitted_files=[' M work.py'], prune_skipped=True,
        ))

        out = capsys.readouterr().out
        assert 'this resolves on its own' not in out
        assert 'nothing else to run' not in out
        assert 're-run this script' in out

    def test_an_unreadable_tree_under_a_live_container_is_not_either(self, capsys):
        """`uncommitted` is None -- the working tree could not be read -- which
        reconcile treats as "there is something here", not as clean."""
        _print_row(_row(container_live=True, uncommitted=None, prune_skipped=True))

        out = capsys.readouterr().out
        assert 'this resolves on its own' not in out

    def test_an_empty_worktree_under_a_live_container_still_self_resolves(self, capsys):
        """The control: this is the one shape that genuinely needs no human."""
        _print_row(_row(container_live=True))

        out = capsys.readouterr().out
        assert 'this resolves on its own once that container exits' in out
        assert 'nothing else to run' in out


class TestThePruneVerdictCoversEveryRuleItCanSee:
    """#231: the verdict was built from the drift rule alone, so a worktree the
    sweep would skip for any other reason was printed as "eligible".

    Observed in production on 2026-09-14: documentation_robotics epic #767 was
    reported eligible while it held an active pipeline run. The failure
    direction is the dangerous one -- an operator acting on "eligible" removes
    the workspace of a mid-pipeline run by hand, which is the incident #229's
    fifth rule exists to prevent, re-entered through the diagnostic.
    """

    def test_an_active_run_is_reported_as_skipped(self):
        verdict = _describe_prune(_row(drifted=False, active_run_protected=True))
        assert verdict.startswith("SKIPPED")
        assert "in flight" in verdict

    def test_the_production_row_that_was_wrong_is_no_longer_eligible(self):
        """The exact shape observed: clean, undrifted, no container, active run."""
        row = _row(
            project='documentation_robotics', epic_id='767',
            current_branch='feature/issue-767-feature',
            expected_branch='feature/issue-767-feature',
            epic_branches=['feature/issue-767-feature'],
            belongs_to_epic=True, drifted=False, unmerged_commits=None,
            container_live=False, prune_skipped=False,
            active_run_protected=True,
        )
        assert _describe_prune(row) != "eligible"

    def test_a_live_container_is_reported_as_skipped(self):
        assert _describe_prune(
            _row(drifted=False, container_live=True)
        ).startswith("SKIPPED")

    def test_the_drift_rule_still_reports(self):
        assert _describe_prune(
            _row(drifted=True, prune_skipped=True)
        ).startswith("SKIPPED")

    def test_nothing_holding_it_reads_as_eligible(self):
        """Control: the verdict must not collapse into always-skipped."""
        assert _describe_prune(
            _row(drifted=False, prune_skipped=False,
                 container_live=False, active_run_protected=False)
        ) == "eligible"

    def test_an_unanswerable_lookup_is_not_eligible(self):
        """None is "could not ask". The sweep aborts in full rather than prune
        without that answer, so reporting "eligible" would be doubly wrong."""
        verdict = _describe_prune(_row(drifted=False, active_run_protected=None))
        assert verdict.startswith("UNKNOWN")
        assert "eligible" not in verdict

    def test_an_active_run_outranks_a_drift_skip_in_the_wording(self):
        """Both skip; the transient reason is the one an operator can act on."""
        assert "in flight" in _describe_prune(
            _row(drifted=True, prune_skipped=True, active_run_protected=True)
        )

    def test_the_printed_row_carries_the_verdict(self, capsys):
        _print_row(_row(drifted=False, active_run_protected=True))
        assert "in flight" in capsys.readouterr().out


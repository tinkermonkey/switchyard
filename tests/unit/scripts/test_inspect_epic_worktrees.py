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

from scripts.inspect_epic_worktrees import _print_row


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

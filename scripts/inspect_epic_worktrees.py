#!/usr/bin/env python3
"""
Inspect Epic Worktrees

The operator entry point for a pipeline blocked on a drifted epic worktree
(#163), and the discovery tool for worktrees the startup prune sweep is
deliberately leaving on disk.

Two on-disk conditions are worth an operator's attention, and both are reported
here:

  * DRIFTED — the worktree's HEAD is on a branch that belongs to no epic, i.e.
    an agent container's own git moved it (`git switch -c scratch`). The
    orchestrator repairs this by itself on the next dispatch ONLY when that
    directory provably holds nothing the epic's branch does not: a clean working
    tree AND no commits of its own on the drifted branch. Otherwise the dispatch
    is refused, because committing that work to the drifted branch is the
    wrong-branch commit #149 exists to prevent, and committing it to the epic's
    branch, or discarding it, are calls only a human can make.
  * PRUNE-SKIPPED — the worktree is drifted AND holds something the startup
    sweep cannot preserve: uncommitted changes, an unreadable working tree, or
    commits on the drifted branch that no branch of this epic already has. So
    prune_epic_worktrees() leaves it alone rather than force-removing work it
    has no way to save (`_push_local_commits_if_any()` publishes the drifted
    branch to the shared repo and nothing else). That skip is what keeps a
    refusal's evidence alive across a restart; the cost is that such a directory
    stays until someone deals with it, and this is where to see which ones those
    are. A dirty worktree still on its own epic's branch is NOT skipped — that is
    an ordinary interrupted run, and leaving it would contaminate the next
    sibling issue's commit.

Deliberately READ-ONLY. There is no `--clear`, and nothing to clear: neither
condition is recorded anywhere and both are re-derived from the worktree's live
HEAD and working tree on every dispatch. Most of them stop applying the moment
the work in that directory is committed, discarded or merged; a clean, empty
worktree whose HEAD simply could not be moved back needs a `git checkout` by
hand. The one shape that needs NO action at all is a drifted worktree an agent
container is still running inside AND holding nothing of its own — it is reported
as LIVE CONTAINER, it resolves by itself when that container exits, and the `git
checkout` the other clean shapes want would rewrite the tree underneath the
running agent. A live container over uncommitted changes or over commits the
epic's branch does not have is reported as LIVE CONTAINER too, for the same
do-not-touch reason, but that one does NOT resolve on its exit: the work is still
there afterwards and still needs a human. The recovery is
therefore ordinary git, run against the path this script prints, followed by
scripts/release_lock.py for the board's retained pipeline lock — the commands are
printed alongside each finding.

Usage:
    python scripts/inspect_epic_worktrees.py
    python scripts/inspect_epic_worktrees.py --project PROJECT_NAME
    python scripts/inspect_epic_worktrees.py --problems-only
    python scripts/inspect_epic_worktrees.py --json
"""

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.project_workspace import workspace_manager

logging.basicConfig(
    level=logging.WARNING,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def _describe_uncommitted(row: dict) -> str:
    if row['uncommitted'] is None:
        return "unreadable"
    if not row['uncommitted']:
        return "clean"
    return f"{len(row['uncommitted_files'])} file(s)"


def _is_problem(row: dict) -> bool:
    """A row worth an operator's time: drifted, or kept alive by the prune skip."""
    return bool(row['drifted']) or bool(row['prune_skipped'])


def _holds_work(row: dict) -> bool:
    """Whether this directory holds something that outlives a live writer.

    A clean tree with no commits of its own is the only shape a container's exit
    clears by itself. Anything else -- uncommitted changes, an unreadable working
    tree, commits the epic's branch does not have -- is still sitting there when
    that container is gone, and nothing commits it on the way out: the container's
    own auto-commit is refused for the very reason the dispatch was (code review
    on #163). unmerged_commits is only computed for a drifted row, so it decides
    nothing for the rest.
    """
    if row['uncommitted'] is not False:
        return True
    return bool(row['drifted']) and row['unmerged_commits'] != 0


def _describe_unmerged(row: dict) -> str:
    if row['unmerged_commits'] is None:
        return "unknown"
    return str(row['unmerged_commits'])


def _describe_branch(row: dict) -> str:
    if row['current_branch']:
        return row['current_branch']
    return '<detached HEAD>' if row.get('head_detached') else '<unreadable>'


def _print_row(row: dict) -> None:
    print(f"  epic #{row['epic_id']}  {row['path']}")
    print(f"    branch:       {_describe_branch(row)}")
    print(f"    epic branch:  {row['expected_branch'] or '<none found>'}")
    if len(row['epic_branches']) > 1:
        print(f"    epic has:     {', '.join(row['epic_branches'])}")
    print(f"    uncommitted:  {_describe_uncommitted(row)}")
    if row['drifted']:
        # The question a clean working tree does NOT answer: an agent that
        # committed its own work onto the drifted branch leaves an empty
        # porcelain and commits that exist nowhere else.
        print(f"    unmerged:     {_describe_unmerged(row)} commit(s) not on the epic's branch")
    print(f"    prune:        {'SKIPPED (drifted + work this sweep cannot preserve)' if row['prune_skipped'] else 'eligible'}")

    if row['drifted']:
        print(
            f"    ⚠️  DRIFTED — {_describe_branch(row)} belongs to no epic. "
            "Dispatches for this epic are refused unless this directory holds "
            "nothing the epic's branch does not (clean tree, no commits of its own)."
        )
    if row['drifted'] and row.get('container_live') is not False:
        # The drift shape where the move-HEAD advice below would be actively
        # destructive: a repair (by the orchestrator OR by hand) rewrites every
        # tracked file in a directory an agent is live inside. None is "docker
        # could not be asked", which is not the same claim as "nothing is
        # running" — so it gets the same treatment (code review on #163).
        #
        # Liveness does not decide whether it needs an operator, though: only the
        # row that holds nothing of its own is cleared by that container's exit.
        # Printing "resolves on its own" two lines under "unmerged: 3 commit(s)
        # not on the epic's branch" is how those three commits get deleted (code
        # review on #163).
        if not _holds_work(row):
            resolution = (
                "this resolves on its own once that container exits."
                if row.get('container_live')
                # None is "docker could not be asked", not "something is running":
                # there may be nothing to wait for, so promising self-resolution
                # leaves the board's retained lock waiting forever (code review on
                # #163).
                else "confirm with `docker ps` that nothing is in there — if "
                     "nothing is, this does NOT resolve on its own."
            )
        else:
            resolution = (
                "this does NOT resolve on its own — what is in this directory "
                "is still here when that container exits, and nothing commits it "
                "on the way out. Deal with it once `docker ps` shows nothing "
                "running against this path."
            )
        print(
            "    ⏳ LIVE CONTAINER — an agent container "
            + ("is still running" if row.get('container_live')
               else "may still be running (docker could not be asked)")
            + " against this worktree. Do NOT move HEAD or remove the directory; "
            + resolution
        )
    for line in row['uncommitted_files']:
        print(f"      {line}")

    if _is_problem(row):
        print("    to recover:")
        print(f"      git -C {row['path']} status")
        print(f"      git -C {row['path']} diff")
        if row.get('container_live') is not False:
            # Read-only commands stay; every mutating suggestion below is gated on
            # the liveness answer, because all of them rewrite a working tree an
            # agent may still be editing.
            if not _holds_work(row):
                print(
                    "      # a container is live in there — nothing else to "
                    "run; it resolves when that container exits"
                    if row.get('container_live') else
                    "      # docker could not be asked whether anything is live in "
                    "there — check `docker ps`; if nothing is, this needs HEAD moved "
                    "by hand"
                )
            else:
                # The container's exit is the START of this row's recovery, not the
                # whole of it. The commands that deal with the work are suppressed
                # only while something may still be writing, so the instruction is
                # to come back rather than "nothing else to run" (code review on
                # #163).
                print(
                    "      # a container is live in there — nothing safe to run "
                    "yet, and the work above outlives its exit"
                    if row.get('container_live') else
                    "      # docker could not be asked whether anything is live in "
                    "there — check `docker ps`; the work above needs dealing with "
                    "either way"
                )
                print(
                    "      # once `docker ps` shows nothing against this path, "
                    "re-run this script for the recovery that applies"
                )
        # The stash/discard pair only applies when there IS something uncommitted.
        # Printing it for a clean worktree sends an operator to run a no-op and
        # conclude the directory is now fine, which is the wrong conclusion for
        # every clean shape of this block.
        if row['uncommitted'] is not False and row.get('container_live') is False:
            if row['expected_branch']:
                print(
                    f"      # keep it:    git -C {row['path']} stash && "
                    f"git -C {row['path']} checkout {row['expected_branch']} && "
                    f"git -C {row['path']} stash pop"
                )
            print(
                f"      # discard it: git -C {row['path']} reset --hard && "
                f"git -C {row['path']} clean -fd"
            )
        if row['drifted'] and row['unmerged_commits'] != 0 and row['expected_branch']:
            print(
                f"      # commits on {row['current_branch']}: git -C {row['path']} log "
                f"{row['expected_branch']}..{row['current_branch']}"
            )
            if row['uncommitted'] is False and row.get('container_live') is False:
                # Moving HEAD is what unblocks this, and `branch -D` is named only
                # after it: git refuses to delete the branch checked out in this
                # very worktree, and the count never reaches zero on its own when
                # the drifted branch legitimately carries commits of its own
                # (`main`, a sibling epic's branch) -- so "merge them and it
                # clears" is advice that never terminates (code review on #163).
                print(
                    f"      # unblock it:  git -C {row['path']} checkout "
                    f"{row['expected_branch']}  "
                    "# the ref survives; cherry-pick/merge or `branch -D` after"
                )
        if (
            row['drifted']
            and not row['uncommitted']
            and row['unmerged_commits'] == 0
            and row.get('container_live') is False
        ):
            # Nothing to commit or discard, so this one does not clear itself:
            # the block persists until HEAD is moved by hand. Suppressed when a
            # container is (or may be) live in there — telling an operator to
            # check out over a running agent's working tree is the one thing the
            # orchestrator's own liveness gate exists to prevent.
            print(
                f"      # nothing to preserve — move HEAD back: git -C {row['path']} "
                f"checkout {row['expected_branch'] or '<epic branch>'}"
            )
            print(
                f"      # if git refuses, find who holds it: git -C {row['path']} worktree list"
            )
        print(
            "      # then release the board's retained lock: "
            "python scripts/release_lock.py --project "
            f"{row['project']} --board BOARD --issue ISSUE"
        )
    print()


def main():
    parser = argparse.ArgumentParser(
        description="Inspect staged epic worktrees for branch drift and uncommitted work"
    )
    parser.add_argument('--project', type=str, help='Limit to one project')
    parser.add_argument(
        '--problems-only', action='store_true',
        help='Only show worktrees that have drifted or that prune is skipping'
    )
    parser.add_argument('--json', action='store_true', help='Emit raw JSON')
    args = parser.parse_args()

    rows = workspace_manager.survey_epic_worktrees(project_name=args.project)
    if args.problems_only:
        rows = [row for row in rows if _is_problem(row)]

    if args.json:
        print(json.dumps(rows, indent=2))
        return 0

    if not rows:
        scope = f" for project {args.project}" if args.project else ""
        print(
            f"No epic worktrees{scope}"
            + (" with drift or uncommitted work." if args.problems_only else " staged on disk.")
        )
        return 0

    current_project = None
    for row in rows:
        if row['project'] != current_project:
            current_project = row['project']
            print(f"\n{current_project}")
        _print_row(row)

    drifted = sum(1 for row in rows if row['drifted'])
    skipped = sum(1 for row in rows if row['prune_skipped'])
    live = sum(1 for row in rows if row['drifted'] and row.get('container_live') is not False)
    print(
        f"{len(rows)} worktree(s): {drifted} drifted, "
        f"{skipped} holding work the startup sweep cannot preserve (prune skips "
        f"these), {live} with a container still (or possibly) live inside — those "
        "need no action."
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())

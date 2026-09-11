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
  * PRUNE-SKIPPED — the worktree is drifted AND holds uncommitted changes, so
    prune_epic_worktrees() leaves it alone at startup rather than force-removing
    work it has no way to preserve (`_push_local_commits_if_any()` saves commits,
    not a dirty tree). That skip is what keeps a refusal's evidence alive across
    a restart; the cost is that such a directory stays until someone deals with
    it, and this is where to see which ones those are. A dirty worktree still on
    its own epic's branch is NOT skipped — that is an ordinary interrupted run,
    and leaving it would contaminate the next sibling issue's commit.

Deliberately READ-ONLY. There is no `--clear`, and nothing to clear: neither
condition is recorded anywhere and both are re-derived from the worktree's live
HEAD and working tree on every dispatch. Most of them stop applying the moment
the work in that directory is committed, discarded or merged; the one that does
not is a clean, empty worktree whose HEAD simply could not be moved back, which
needs a `git checkout` by hand. The recovery is therefore ordinary git, run
against the path this script prints, followed by scripts/release_lock.py for the
board's retained pipeline lock — the commands are printed alongside each
finding.

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


def _describe_unmerged(row: dict) -> str:
    if row['unmerged_commits'] is None:
        return "unknown"
    return str(row['unmerged_commits'])


def _print_row(row: dict) -> None:
    print(f"  epic #{row['epic_id']}  {row['path']}")
    print(f"    branch:       {row['current_branch'] or '<unreadable/detached>'}")
    print(f"    epic branch:  {row['expected_branch'] or '<none found>'}")
    if len(row['epic_branches']) > 1:
        print(f"    epic has:     {', '.join(row['epic_branches'])}")
    print(f"    uncommitted:  {_describe_uncommitted(row)}")
    if row['drifted']:
        # The question a clean working tree does NOT answer: an agent that
        # committed its own work onto the drifted branch leaves an empty
        # porcelain and commits that exist nowhere else.
        print(f"    unmerged:     {_describe_unmerged(row)} commit(s) not on the epic's branch")
    print(f"    prune:        {'SKIPPED (drifted + uncommitted work)' if row['prune_skipped'] else 'eligible'}")

    if row['drifted']:
        print(
            f"    ⚠️  DRIFTED — {row['current_branch']!r} belongs to no epic. "
            "Dispatches for this epic are refused unless this directory holds "
            "nothing the epic's branch does not (clean tree, no commits of its own)."
        )
    for line in row['uncommitted_files']:
        print(f"      {line}")

    if _is_problem(row):
        print("    to recover:")
        print(f"      git -C {row['path']} status")
        print(f"      git -C {row['path']} diff")
        # The stash/discard pair only applies when there IS something uncommitted.
        # Printing it for a clean worktree sends an operator to run a no-op and
        # conclude the directory is now fine, which is the wrong conclusion for
        # every clean shape of this block.
        if row['uncommitted'] is not False:
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
                f"{row['expected_branch']}..{row['current_branch']}  "
                "# cherry-pick/merge them, or `branch -D` if unwanted"
            )
        if row['drifted'] and not row['uncommitted'] and row['unmerged_commits'] == 0:
            # Nothing to commit or discard, so this one does not clear itself:
            # the block persists until HEAD is moved by hand.
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
    print(
        f"{len(rows)} worktree(s): {drifted} drifted, "
        f"{skipped} drifted and holding uncommitted work (prune skips these)."
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())

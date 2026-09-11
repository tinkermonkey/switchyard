#!/usr/bin/env python3
"""
Inspect Epic Worktrees

The operator entry point for a pipeline blocked on a drifted epic worktree
(#163), and the discovery tool for worktrees the startup prune sweep is
deliberately leaving on disk.

Two on-disk conditions are worth an operator's attention, and both are reported
here:

  * DRIFTED — the worktree's HEAD is on a branch that belongs to no epic, i.e.
    an agent container's own git moved it (`git switch -c scratch`). When the
    working tree is CLEAN the orchestrator repairs this by itself on the next
    dispatch, so it shows up here only in passing. When it is DIRTY the dispatch
    is refused instead: committing that work to the drifted branch is the
    wrong-branch commit #149 exists to prevent, and committing it to the epic's
    branch, or discarding it, are calls only a human can make.
  * PRUNE-SKIPPED — the worktree holds uncommitted changes, so
    prune_epic_worktrees() leaves it alone at startup rather than force-removing
    work it has no way to preserve (`_push_local_commits_if_any()` saves commits,
    not a dirty tree). That skip is what keeps a refusal's evidence alive across
    a restart; the cost is that such a directory stays until someone deals with
    it, and this is where to see which ones those are.

Deliberately READ-ONLY. There is no `--clear`, and nothing to clear: neither
condition is recorded anywhere, both are re-derived from the worktree's live
HEAD and working tree on every dispatch, and both stop applying the moment the
work in that directory is committed or discarded. The recovery is therefore
ordinary git, run against the path this script prints, followed by
scripts/release_lock.py for the board's retained pipeline lock — the commands
are printed alongside each finding.

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


def _print_row(row: dict) -> None:
    print(f"  epic #{row['epic_id']}  {row['path']}")
    print(f"    branch:       {row['current_branch'] or '<unreadable/detached>'}")
    print(f"    epic branch:  {row['expected_branch'] or '<none found>'}")
    print(f"    uncommitted:  {_describe_uncommitted(row)}")
    print(f"    prune:        {'SKIPPED (uncommitted work)' if row['prune_skipped'] else 'eligible'}")

    if row['drifted']:
        print(
            f"    ⚠️  DRIFTED — {row['current_branch']!r} belongs to no epic. "
            "Dispatches for this epic are refused while it also holds "
            "uncommitted work."
        )
    for line in row['uncommitted_files']:
        print(f"      {line}")

    if _is_problem(row):
        print("    to recover:")
        print(f"      git -C {row['path']} status")
        print(f"      git -C {row['path']} diff")
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
        f"{skipped} holding uncommitted work (prune skips these)."
    )
    return 0


if __name__ == '__main__':
    sys.exit(main())

#!/usr/bin/env python3
"""
List Failed Pipeline Runs

Scans every pipeline lock for one with retained_reason set — i.e. an issue whose
pipeline run failed and is durably blocked pending human action (see
PipelineLockManager.mark_lock_failed). This is the list/scan capability the old
halt-marker mechanism never had: scripts/clear_halt_marker.py required already
knowing the exact --project/--issue to check, so discovering "what's currently
blocked" required hand-scanning state/execution_history/*.yaml directly.

Locks are the durable source of truth (Redis + non-expiring YAML) — unlike the
PipelineRun record in Elasticsearch, which rolls off after 7 days, or its Redis
cache, which expires in hours. So this script finds every currently-blocked issue
even if its PipelineRun history has long since aged out.

Usage:
    python scripts/list_failed_pipeline_runs.py [--project PROJECT_NAME] [--json]
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.pipeline_lock_manager import get_pipeline_lock_manager


def main():
    parser = argparse.ArgumentParser(
        description="List all pipeline locks currently retained due to a failed run"
    )
    parser.add_argument('--project', type=str, default=None, help='Filter to a single project')
    parser.add_argument('--json', action='store_true', help='Output as JSON instead of a table')
    args = parser.parse_args()

    lock_manager = get_pipeline_lock_manager()
    failed_locks = [
        lock for lock in lock_manager.get_all_locks()
        if lock.retained_reason and (args.project is None or lock.project == args.project)
    ]

    if args.json:
        print(json.dumps([
            {
                'project': lock.project,
                'board': lock.board,
                'issue_number': lock.locked_by_issue,
                'retained_at': lock.retained_at,
                'reason': lock.retained_reason,
            }
            for lock in failed_locks
        ], indent=2))
        return

    if not failed_locks:
        print("No pipeline runs are currently in a failed/retained state.")
        return

    print(f"{len(failed_locks)} pipeline run(s) currently failed and blocking their board:\n")
    for lock in sorted(failed_locks, key=lambda l: l.retained_at or ''):
        print(f"  {lock.project}/{lock.board} — issue #{lock.locked_by_issue}")
        print(f"    Retained at: {lock.retained_at}")
        print(f"    Reason: {lock.retained_reason}")
        print(
            f"    Recover: python scripts/release_lock.py --project {lock.project} "
            f"--board \"{lock.board}\" --issue {lock.locked_by_issue}"
        )
        print()


if __name__ == '__main__':
    main()

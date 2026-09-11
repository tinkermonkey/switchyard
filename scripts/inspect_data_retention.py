#!/usr/bin/env python3
"""
Inspect Data Retention

What the orchestrator has written to disk and never cleaned up, and what the
retention rules would remove.

Reports by default and deletes only with `--apply`, so the first run is always
safe. The same sweep runs automatically each night (services/scheduled_tasks.py,
4:30 AM); this exists to see what it will do, to run it early after a deploy
that has a large backlog to clear, and to answer "where did the disk go".

Every rule uses the SAME window: config/retention.py's RETENTION_DAYS (30 days
by default), which is also what every Elasticsearch ILM policy uses. There is
no per-location number to get wrong.

The rules and their reasoning live in services/data_retention.py. What it does
NOT sweep, and why, is documented there too -- in short: live state
(`state/pipeline_locks/`, `state/pipeline_queues/`, `state/dev_containers/`,
`github_state.yaml`) is cleaned by orphan detection rather than by age (see
scripts/inspect_project_state.py), lock sidecars are never deleted, and
`orchestrator_data/logs/` plus each checkout's `.repair_cycle.log` are bounded
by monitoring/log_rotation.py instead.

Usage:
    python scripts/inspect_data_retention.py
    python scripts/inspect_data_retention.py --json
    python scripts/inspect_data_retention.py --apply
    python scripts/inspect_data_retention.py --root /some/other/orchestrator
    RETENTION_DAYS=7 python scripts/inspect_data_retention.py
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.retention import RETENTION_DAYS  # noqa: E402
from services.data_retention import (  # noqa: E402
    RETENTION_RULES,
    WORKSPACE_ROOT,
    sweep,
)


def _human(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if value < 1024 or unit == 'GB':
            return f"{value:.0f}{unit}" if unit == 'B' else f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}GB"


def report(outcomes, applied: bool) -> None:
    verb = 'Removed' if applied else 'Would remove'
    total_bytes = 0
    total_entries = 0

    for outcome in outcomes:
        rule = outcome.rule
        print(f"{rule.name}  [{rule.root_kind}]")
        print(f"    {rule.description}")
        print(f"    {outcome.path}")

        if outcome.missing:
            print("    directory does not exist -- nothing to do")
            print()
            continue

        count = len(outcome.removed) if applied else len(outcome.expired)
        total_entries += count
        total_bytes += outcome.bytes_removed
        print(f"    {outcome.examined} entr{'y' if outcome.examined == 1 else 'ies'}, "
              f"{verb.lower()} {count} ({_human(outcome.bytes_removed)})")

        for error in outcome.errors:
            print(f"    ! {error}")
        print()

    print(f"{verb} {total_entries} entr{'y' if total_entries == 1 else 'ies'} "
          f"totalling {_human(total_bytes)}.")
    if not applied and total_entries:
        print("Re-run with --apply to delete them.")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Report (and optionally apply) data retention",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument('--apply', action='store_true',
                        help='Actually delete. Without this, nothing is removed.')
    parser.add_argument('--json', action='store_true',
                        help='Machine-readable output')
    parser.add_argument('--root', default=None,
                        help='Orchestrator root (default: $ORCHESTRATOR_ROOT or /app)')
    parser.add_argument('--workspace-root', default=None,
                        help=f'Workspace root for the rules that live beside the '
                             f'checkout (default: $WORKSPACE_ROOT or {WORKSPACE_ROOT})')
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format='%(message)s')

    root = Path(args.root) if args.root else Path(
        os.environ.get('ORCHESTRATOR_ROOT', '/app')
    )
    workspace_root = Path(args.workspace_root) if args.workspace_root else None
    outcomes = sweep(root=root, apply=args.apply, workspace_root=workspace_root)

    if args.json:
        print(json.dumps({
            'root': str(root),
            'workspace_root': str(workspace_root or WORKSPACE_ROOT),
            'retention_days': RETENTION_DAYS,
            'applied': args.apply,
            'rules': [
                {
                    'name': o.rule.name,
                    'path': o.path,
                    'root_kind': o.rule.root_kind,
                    'missing': o.missing,
                    'examined': o.examined,
                    'expired': len(o.expired),
                    'removed': len(o.removed),
                    'bytes': o.bytes_removed,
                    'errors': o.errors,
                }
                for o in outcomes
            ],
        }, indent=2))
    else:
        print(f"Orchestrator root: {root}")
        print(f"Workspace root:    {workspace_root or WORKSPACE_ROOT}")
        print(f"Retention window:  {RETENTION_DAYS} days (RETENTION_DAYS) -- the "
              f"same value every Elasticsearch ILM policy uses")
        print(f"Rules: {len(RETENTION_RULES)}")
        print()
        report(outcomes, applied=args.apply)

    return 0


if __name__ == '__main__':
    sys.exit(main())

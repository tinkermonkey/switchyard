#!/usr/bin/env python3
"""
Clear Halt Marker

Explicitly clears a "halted_for_review" marker set by the consecutive-dispatch-failure
guard (services/project_monitor.py) or a crashed review-cycle thread. These halts are
deliberately NOT auto-cleared by board activity (see
services/work_execution_state.py's set_halt_marker() docstring) — this script is the
intended recovery path once a human has investigated and fixed the underlying issue.

Usage:
    python scripts/clear_halt_marker.py --project PROJECT_NAME --issue ISSUE_NUMBER
"""

import argparse
import logging
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.work_execution_state import work_execution_tracker

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Clear a halted_for_review marker so auto-dispatch can resume"
    )
    parser.add_argument('--project', type=str, required=True, help='Project name')
    parser.add_argument('--issue', type=int, required=True, help='Issue number')

    args = parser.parse_args()

    halt_marker = work_execution_tracker.get_halt_marker(args.project, args.issue)
    if not halt_marker:
        print(f"Issue #{args.issue} in {args.project} is not currently halted.")
        return

    print(
        f"Clearing halt for {args.project}/#{args.issue} "
        f"(halted at {halt_marker['halted_at']}, reason: {halt_marker['reason']})"
    )
    work_execution_tracker.clear_halt_marker(args.project, args.issue)
    print("Halt marker cleared. Auto-dispatch will resume on the next poll.")


if __name__ == '__main__':
    main()

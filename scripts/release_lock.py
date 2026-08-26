#!/usr/bin/env python3
"""
Release Pipeline Lock

The deliberate human recovery action for a pipeline that has failed and been
durably marked as retained (see PipelineLockManager.mark_lock_failed / the
services.pipeline_run.PipelineRunManager.mark_failed / end_pipeline_run design).

By default this REFUSES to release a lock that is not currently marked
retained-due-to-failure (retained_reason is None) — releasing an actively-in-
-progress lock out from under a running pipeline would be destructive, so that
requires explicit --force. This mirrors the intent the old halt-marker system
had (clearing required deliberate, explicit action) without halt markers' other
problems: no expiry games, and this script itself doubles as the discovery tool
paired with scripts/list_failed_pipeline_runs.py.

After releasing, the issue's queue entry is reset to 'waiting' so it becomes
eligible for a fresh dispatch attempt (a fresh PipelineRun, fresh retry counter)
on the next natural trigger (poll cycle or board move) — no other action needed.

Usage:
    python scripts/release_lock.py --project PROJECT_NAME --board BOARD_NAME --issue ISSUE_NUMBER
    python scripts/release_lock.py --project PROJECT_NAME --board BOARD_NAME --issue ISSUE_NUMBER --force
"""

import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from services.pipeline_lock_manager import get_pipeline_lock_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description="Release a pipeline lock (the deliberate recovery action for a failed run)"
    )
    parser.add_argument('--project', type=str, required=True, help='Project name')
    parser.add_argument('--board', type=str, required=True, help='Board name')
    parser.add_argument('--issue', type=int, required=True, help='Issue number holding the lock')
    parser.add_argument(
        '--force', action='store_true',
        help='Release even if the lock is not marked as retained-due-to-failure '
             '(DANGEROUS: can pull the lock out from under an actively-running pipeline)'
    )
    args = parser.parse_args()

    lock_manager = get_pipeline_lock_manager()
    # get_lock_fail_closed(), not the plain get_lock() — this is the tool a
    # human runs specifically to recover from a stuck board, so it's exactly
    # the wrong place to fold "couldn't determine lock state" into "nothing to
    # do": an outage severe enough to make Redis AND the YAML file both
    # unreadable is precisely when an operator most needs an honest "unknown"
    # rather than false reassurance that there's nothing to release.
    lock, reads_healthy = lock_manager.get_lock_fail_closed(args.project, args.board)

    if not reads_healthy:
        print(
            f"Could not determine lock state for {args.project}/{args.board} — "
            f"both Redis and the YAML state file failed to read. This does NOT "
            f"mean the board is unlocked; it means lock state is currently "
            f"unknown. Check Redis connectivity and the state file at "
            f"state/pipeline_locks/ before assuming anything, then retry."
        )
        sys.exit(1)

    if not lock or lock.lock_status != 'locked':
        print(f"{args.project}/{args.board} is not currently locked. Nothing to do.")
        return

    if lock.locked_by_issue != args.issue:
        print(
            f"Lock for {args.project}/{args.board} is held by issue "
            f"#{lock.locked_by_issue}, not #{args.issue}. Refusing to release — "
            f"pass the correct --issue if you're sure."
        )
        sys.exit(1)

    if not lock.retained_reason and not args.force:
        print(
            f"Lock for {args.project}/{args.board} (issue #{args.issue}) is not marked "
            f"as retained-due-to-failure — it looks like an actively-in-progress "
            f"pipeline run, not a failed/blocked one. Releasing it now would likely "
            f"disrupt a running agent.\n\n"
            f"If you're certain this should be released anyway, re-run with --force."
        )
        sys.exit(1)

    if lock.retained_reason:
        print(f"Releasing failed lock for {args.project}/{args.board} issue #{args.issue}")
        print(f"  Retained at: {lock.retained_at}")
        print(f"  Reason: {lock.retained_reason}")
    else:
        print(
            f"--force: releasing lock for {args.project}/{args.board} issue #{args.issue} "
            f"(not marked as failed)"
        )

    # force=True: this script IS the deliberate human recovery action —
    # release_lock() itself now refuses to release a retained lock unless
    # explicitly told to (see PipelineLockManager.release_lock), specifically
    # so that no other, automatic call site can silently release one. We've
    # already confirmed above (retained + printed detail, or --force for a
    # non-retained lock) that releasing is intended here.
    released = lock_manager.release_lock(args.project, args.board, args.issue, force=True)
    if not released:
        print("Failed to release lock (see logs).")
        sys.exit(1)

    print("Lock released.")

    # Clear the cancellation signal that mark_failed()/end_pipeline_run() set when
    # this run was originally marked failed (services/pipeline_run.py). It has a
    # 1-hour TTL, so a human recovering promptly would otherwise leave it active —
    # and the queue-processing failsafe treats an active cancellation signal as a
    # reason to immediately remove the issue from the queue and release its lock
    # again, undoing this recovery. Must run before reset_issue_to_waiting below.
    try:
        from services.cancellation import get_cancellation_signal
        get_cancellation_signal().clear(args.project, args.issue)
    except Exception as e:
        logger.warning(f"Could not clear cancellation signal: {e}")

    # Reset the queue entry so the issue is eligible for a fresh dispatch attempt.
    try:
        from services.pipeline_queue_manager import get_pipeline_queue_manager
        queue = get_pipeline_queue_manager(args.project, args.board)
        queue.reset_issue_to_waiting(args.issue)
        print(f"Issue #{args.issue} reset to 'waiting' — it will be picked up on the next poll.")
    except Exception as e:
        logger.warning(f"Could not reset queue entry to waiting: {e}")
        print(
            "Warning: could not reset the queue entry automatically. The issue may "
            "not be picked up until its board card is moved or the queue is force-synced."
        )


if __name__ == '__main__':
    main()

"""Registry of dispatches that are waiting on a busy (project, board) pipeline lock.

Why this exists
---------------
#58 (Phase 2 of #34) replaced repair-cycle lock *stealing* with wait-and-retry.
Waiting is the correct behaviour and this module does not change it. What it
adds is the two properties that rework left open (#214):

1. **A release-driven wake.** Both release-time backfill sites --
   ``PipelineRunManager.end_pipeline_run()`` and
   ``ProjectMonitor._release_pipeline_lock_and_process_next()`` -- ask
   ``PipelineQueueManager.get_next_n_waiting_issues()`` what to dispatch next,
   and that queue is scoped exclusively to the pipeline *trigger* column
   ("Development" for ``sdlc_execution_workflow``). A repair cycle waits in
   "Testing", so it is not in that queue and the release path cannot see it: on
   release the board is handed straight to the next Development issue, inline,
   and the waiting repair cycle only gets another chance on the next poll tick
   -- where ``_check_and_process_waiting_issues_failsafe()`` then finds the
   board locked again. This registry is the cheap, GitHub-free channel that
   lets the release paths see the waiter, applying the same "mid-pipeline beats
   Development" preference the poll-time failsafe already documents.

   The two sites use it differently, and deliberately.
   ``_release_pipeline_lock_and_process_next()`` already dispatches inline (via
   ``_trigger_next_issue_with_rollback``), so it dispatches the waiter inline
   too -- ``ProjectMonitor.dispatch_waiting_board_lock_waiter()``.
   ``end_pipeline_run()`` only ever enqueued a Task, and is called from the
   watchdog's self-heal sweep, ``review_cycle`` and the human feedback loop; it
   therefore only *yields* the board (skips its Development backfill) and lets
   the poll-tick failsafe do the dispatch on the monitor's own thread, rather
   than blocking those callers on a worktree resolution and a container launch.

2. **Observability.** A wait previously emitted nothing at all on the cheap
   lock-probe path (``_start_repair_cycle_for_issue``'s early-out) -- no log
   line, no event, no escalation -- so a board that stalled this way was
   invisible. Entries here carry ``waiting_since``, which is what lets the
   wait's start, its acquisition and its duration be logged and indexed.

Scope and lifetime
------------------
Process-global and in-memory, deliberately. The recorder (the dispatch path)
and the readers (the two release paths and the poll-tick refresher) all run
inside the single orchestrator process, so shared memory is sufficient and
avoids adding a Redis dependency -- and therefore a new failure mode -- to the
release path, which is on the critical path of every stage completion.

Losing the registry (a restart) costs nothing but the wake: the wait itself
lives in GitHub board state, and ``_find_stalled_issues_for_pipeline()`` +
the poll-tick failsafe remain the failsafe they always were. Every entry is
therefore a *hint*, never an authority. The dispatch the wake performs
re-validates the column, the cancellation signal and the lock itself, exactly
as the failsafe's own stalled-issue path does.

Staleness is bounded by ``last_seen_at`` rather than by trying to clear the
entry on every one of ``_start_repair_cycle_for_issue``'s many return paths.

What refreshes ``last_seen_at`` is ``refresh_waiters()``, called from
``ProjectMonitor._refresh_board_lock_waits()`` at the poll-tick failsafe's
busy-board abort. That is deliberate and it is the only refresher, because the
dispatch path does NOT re-record: while the board is locked the failsafe aborts
that board before it reaches the stalled-issue scan (``break  # Pipeline busy,
skip``), and the only other routes into ``_start_repair_cycle_for_issue`` are a
board status change or ``item_added`` -- and a card parked in "Testing" waiting
for the lock is not moving. So ``record_wait()`` runs once, at the moment of the
refusal, and an entry that nothing refreshed would age out mid-wait, taking the
wake, the waited-duration report and the escalation with it.
"""

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# Per-thread set of (project, board) pairs whose wake is currently on the stack.
_wake_in_progress = threading.local()


@contextmanager
def wake_reentrancy_guard(project: str, board: str):
    """Refuse to start a wake for a board whose wake is already on this stack.

    Not defensive padding -- there is a concrete recursion here. The wake
    dispatches through ProjectMonitor.trigger_agent_for_status(), and that
    method calls _release_pipeline_lock_and_process_next() itself on two of its
    own branches: when the issue is sitting in a pipeline EXIT column, and when
    the issue turns out to be CLOSED while still holding the lock. That release
    then calls the wake again, which re-selects the same still-registered
    waiter -- one Python frame deeper each time, until the interpreter's
    recursion limit fires inside a lock release.

    The wake now drops the registry entry for both of those outcomes (an exit
    column is checked before dispatch; a closed issue is recognised from the
    DispatchDecline it returns), so neither one loops ACROSS releases any more.
    This guard is still what stops the recursion WITHIN a single release, and
    the closed-issue branch still reaches the release path before the decline
    that clears the entry gets back to the wake -- so it is load-bearing, not
    historical.

    Yields True when the caller may proceed, False when it must not.
    """
    active: Set[Tuple[str, str]] = getattr(_wake_in_progress, 'boards', None)
    if active is None:
        active = set()
        _wake_in_progress.boards = active

    key = (project, board)
    if key in active:
        yield False
        return

    active.add(key)
    try:
        yield True
    finally:
        active.discard(key)


# An entry not refreshed within this many seconds is treated as gone and is
# neither woken nor reported. Sized against the cadence of the one thing that
# actually refreshes it -- ProjectMonitor._refresh_board_lock_waits(), driven by
# the poll-tick failsafe, which runs once per monitor cycle. That cycle's sleep
# is _next_cycle_sleep_seconds(), the minimum time-until-due across tracked
# boards, and every per-board interval is capped at `_max_poll_interval = 60`
# (services/project_monitor.py), so the slowest refresh interval the monitor
# will take is 60s plus the cycle's own work. 300s is five of those: a live
# waiter cannot age out over several slow or skipped ticks, while a waiter that
# genuinely stopped waiting stops being woken within a few minutes.
#
# The one window where nothing refreshes is a cycle that never reaches the
# failsafe at all -- monitor_projects() `continue`s above it while a circuit
# breaker is open. A wait that spans such a window ages out, and the board falls
# back to exactly the pre-#214 behaviour: the poll-tick failsafe picks the
# stalled issue up once the board is free. Degraded, not broken, and not worth
# a second refresher on the breaker path, where no dispatch is happening anyway.
WAIT_ENTRY_STALE_SECONDS = 300.0


@dataclass
class BoardWaitEntry:
    """One dispatch waiting on one (project, board) pipeline lock."""

    project: str
    board: str
    issue_number: int
    kind: str
    waiting_since: float
    last_seen_at: float
    escalated: bool = False

    def waited_seconds(self, now: Optional[float] = None) -> float:
        return (time.monotonic() if now is None else now) - self.waiting_since


class BoardWaitRegistry:
    """Thread-safe map of (project, board, issue) -> BoardWaitEntry.

    Threaded because dispatch runs on worker threads while the release paths run
    on the monitor thread; the lock only ever guards dict mutation, never I/O.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._entries: Dict[Tuple[str, str, int], BoardWaitEntry] = {}

    def record_wait(
        self,
        project: str,
        board: str,
        issue_number: int,
        kind: str = "repair_cycle",
    ) -> Tuple[float, bool]:
        """Note that ``issue_number`` is waiting for ``project``/``board``.

        Idempotent: the first call starts the clock, any later call only
        refreshes ``last_seen_at``, so ``waited_seconds`` keeps measuring the
        whole wait rather than the gap since the last call. In practice the
        dispatch path reaches this exactly once per wait (see the module
        docstring); keeping it idempotent costs nothing and means a wait that a
        board move does re-enter is not silently restarted.

        Returns:
            ``(waited_seconds, is_new)`` -- ``is_new`` is True only for the call
            that started the wait, which is what lets the caller log "started
            waiting" once rather than on every call.
        """
        now = time.monotonic()
        key = (project, board, issue_number)
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or (now - entry.last_seen_at) > WAIT_ENTRY_STALE_SECONDS:
                # No entry, or one so old it describes a previous, finished wait
                # -- either way this is the start of a new wait, not a refresh.
                entry = BoardWaitEntry(
                    project=project,
                    board=board,
                    issue_number=issue_number,
                    kind=kind,
                    waiting_since=now,
                    last_seen_at=now,
                )
                self._entries[key] = entry
                return 0.0, True

            entry.last_seen_at = now
            return entry.waited_seconds(now), False

    def refresh_waiters(
        self,
        project: str,
        board: str,
        escalate_after_seconds: Optional[float] = None,
    ) -> List[BoardWaitEntry]:
        """Mark every live waiter on this board as still waiting.

        This is what keeps an entry alive, and the only thing that does -- the
        dispatch path writes an entry once, at the moment of the refusal, and
        never returns to it (module docstring). Without this call every wait
        longer than ``WAIT_ENTRY_STALE_SECONDS`` would age out mid-wait: the
        release-driven wake would stop firing, ``clear_wait()`` would stop
        returning a duration to report, and no wait could ever grow long enough
        to escalate.

        ``escalate_after_seconds`` is passed in rather than defined here because
        the threshold is the caller's to choose -- ProjectMonitor reads it from
        config/foundations/agents.yaml rather than restating a number.

        Returns:
            The entries that crossed ``escalate_after_seconds`` on this call, so
            the caller reports each long wait exactly once. Empty when the
            threshold is None or nothing crossed it.
        """
        now = time.monotonic()
        newly_escalated: List[BoardWaitEntry] = []
        with self._lock:
            for key, entry in list(self._entries.items()):
                if (now - entry.last_seen_at) > WAIT_ENTRY_STALE_SECONDS:
                    del self._entries[key]
                    continue
                if entry.project != project or entry.board != board:
                    continue
                entry.last_seen_at = now
                if (
                    escalate_after_seconds is not None
                    and not entry.escalated
                    and entry.waited_seconds(now) >= escalate_after_seconds
                ):
                    entry.escalated = True
                    newly_escalated.append(entry)
        return newly_escalated

    def clear_wait(
        self, project: str, board: str, issue_number: int
    ) -> Optional[float]:
        """Drop the entry for this dispatch.

        Returns the total seconds waited if there was a live wait to clear, else
        None. Callers use the None case to stay silent: a repair cycle that
        acquired the lock on its first attempt never waited, and must not log a
        wait that did not happen.
        """
        key = (project, board, issue_number)
        now = time.monotonic()
        with self._lock:
            entry = self._entries.pop(key, None)
        if entry is None:
            return None
        if (now - entry.last_seen_at) > WAIT_ENTRY_STALE_SECONDS:
            # Removed, but reported as "no wait": the entry is old enough that
            # its duration would be a fabricated number rather than a measured
            # one, and a fabricated duration in a log is worse than no log.
            return None
        return entry.waited_seconds(now)

    def get_waiters_for_board(
        self, project: str, board: str
    ) -> List[BoardWaitEntry]:
        """Live waiters for one board, longest-waiting first.

        Longest-first is the anti-starvation ordering: with several waiters on
        one board, whoever has waited longest is handed the next release, so no
        waiter can be indefinitely overtaken by a newer one.

        Stale entries are dropped here rather than by a sweeper -- this is the
        only read path, so pruning on read is sufficient and needs no timer.
        """
        now = time.monotonic()
        with self._lock:
            fresh: List[BoardWaitEntry] = []
            for key, entry in list(self._entries.items()):
                if (now - entry.last_seen_at) > WAIT_ENTRY_STALE_SECONDS:
                    del self._entries[key]
                    continue
                if entry.project == project and entry.board == board:
                    fresh.append(entry)
        fresh.sort(key=lambda e: e.waiting_since)
        return fresh

    def clear_all(self) -> None:
        """Drop every entry. For tests and for a clean restart path only."""
        with self._lock:
            self._entries.clear()


_board_wait_registry = BoardWaitRegistry()


def get_board_wait_registry() -> BoardWaitRegistry:
    """The process-global registry."""
    return _board_wait_registry

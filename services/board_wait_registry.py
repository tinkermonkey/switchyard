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
   lets the release path see the waiter and give it the board first, applying
   the same "mid-pipeline beats Development" preference the poll-time failsafe
   already documents.

2. **Observability.** A wait previously emitted nothing at all on the cheap
   lock-probe path (``_start_repair_cycle_for_issue``'s early-out) -- no log
   line, no event, no escalation -- so a board that stalled this way was
   invisible. Entries here carry ``waiting_since``, which is what lets the
   wait's start, its acquisition and its duration be logged and indexed.

Scope and lifetime
------------------
Process-global and in-memory, deliberately. Both the recorder (the dispatch
path) and both readers (the two release paths) run inside the single
orchestrator process, so shared memory is sufficient and avoids adding a Redis
dependency -- and therefore a new failure mode -- to the release path, which is
on the critical path of every stage completion.

Losing the registry (a restart) costs nothing but the wake: the wait itself
lives in GitHub board state, and ``_find_stalled_issues_for_pipeline()`` +
the poll-tick failsafe remain the failsafe they always were. Every entry is
therefore a *hint*, never an authority. The dispatch the wake performs
re-validates the column, the cancellation signal and the lock itself, exactly
as the failsafe's own stalled-issue path does.

Staleness is bounded by ``last_seen_at`` rather than by trying to clear the
entry on every one of ``_start_repair_cycle_for_issue``'s many return paths. A
genuinely-still-waiting dispatch re-records on every poll tick (15s on an
active board, up to the 60s ``_max_poll_interval`` on a fully idle one -- both
literals read from ``services/project_monitor.py``), so a refresh window
several times the slowest tick separates "still waiting" from "gone" without
needing the dispatch path to be exhaustive about cleanup.
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
    method calls _release_pipeline_lock_and_process_next() itself when the issue
    it is handed turns out to be sitting in a pipeline EXIT column
    (services/project_monitor.py, the exit-column branches). That release then
    calls the wake again. The woken issue's registry entry is only cleared by
    _start_repair_cycle_for_issue on acquisition, and the exit-column branch
    returns long before reaching it -- so without this guard the same issue is
    selected, dispatched and re-released forever, one Python frame deeper each
    time, until the interpreter's recursion limit fires inside a lock release.

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
# neither woken nor reported. Sized against the poll cadence that does the
# refreshing, not guessed: a waiting repair cycle re-records once per poll tick,
# and the slowest tick the monitor will ever take is `_max_poll_interval = 60`
# (services/project_monitor.py, the fully-idle backoff ceiling). 300s is five of
# those, so a live waiter cannot age out even if several consecutive ticks are
# slow or skipped, while a waiter that genuinely stopped waiting stops being
# woken within a few minutes.
WAIT_ENTRY_STALE_SECONDS = 300.0

# A wait longer than this is reported once, at WARNING, as a potential stall.
# This is an escalation threshold, not a timeout: nothing gives up, nothing is
# evicted, and the wait continues exactly as before -- the only effect is that
# the board becomes alertable instead of silent. 1800s (30 min) is above the
# longest ordinary holder this would legitimately queue behind except
# senior_software_engineer, whose agent timeout is 10800s
# (config/foundations/agents.yaml); waits behind that agent are expected to trip
# this, and a single WARNING naming the holder is the correct outcome there too.
WAIT_ESCALATION_SECONDS = 1800.0


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
        holder_issue: Optional[int] = None,
    ) -> Tuple[float, bool]:
        """Note that ``issue_number`` is waiting for ``project``/``board``.

        Idempotent across poll ticks: the first call starts the clock, every
        later call only refreshes ``last_seen_at``, so ``waited_seconds`` keeps
        measuring the whole wait rather than the gap since the last tick.

        Returns:
            ``(waited_seconds, is_new)`` -- ``is_new`` is True only for the call
            that started the wait, which is what lets the caller log "started
            waiting" once instead of on every tick.
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
            waited = entry.waited_seconds(now)
            should_escalate = (not entry.escalated) and waited >= WAIT_ESCALATION_SECONDS
            if should_escalate:
                entry.escalated = True

        if should_escalate:
            holder = f"issue #{holder_issue}" if holder_issue else "another issue"
            logger.warning(
                f"{kind} for issue #{issue_number} has been waiting "
                f"{waited:.0f}s for the pipeline lock on {project}/{board} "
                f"(held by {holder}). Not a timeout — the wait continues and the "
                f"board will be handed over on release — but a wait this long "
                f"means the holder is long-running or stuck; check the holder "
                f"before assuming the waiter is at fault."
            )
        return waited, False

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

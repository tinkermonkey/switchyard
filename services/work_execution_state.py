"""
Work Execution State Tracker

Tracks execution history, outcomes, and status changes to enable:
- Intelligent work restart on status changes
- Prevention of double-triggering on auto-progression
- Retry on failure
- Complete audit trail
"""

import yaml
import logging
from pathlib import Path
from typing import Optional, Dict, List, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# pipeline_progression writes a pre-enqueue probe (in_progress, no task_id) before
# enqueuing the task to Redis so project_monitor can't start a review cycle race.
# In normal operation the task_manager stamps a task_id within seconds of pickup.
# If this threshold elapses with no task_id stamp, the Redis task was swept or lost
# before being consumed and the probe is now a permanent blocker — clear it.
_STALE_ENQUEUE_PROBE_SECS = 60  # 1 minute — a probe without a task_id stamp after 60s means the Redis task was lost

# How far back detect_and_retry_empty_successful_executions() will look (#150).
# The sweep globs EVERY state file ever written, every 15 minutes -- 4700+ of them
# on the live orchestrator, ~97% months old and terminal -- and each one that gets
# past the cheap checks costs lock reads, queue reads and a GitHub query. Until
# PROTECTION 1's re-entrant flock was fixed the sweep wedged on the first 'success'
# record it found, so none of that cost was ever paid and none of it was visible.
# Overridable with WATCHDOG_MAX_RECORD_AGE_HOURS; <= 0 disables the gate.
_WATCHDOG_MAX_RECORD_AGE_HOURS = 24


def _execution_anchor_time(execution: dict) -> Optional[str]:
    """The "after what?" timestamp for an execution record.

    record_execution_outcome() stamps completed_at on every record it finalises
    (#150), but nothing did before that, so every record already on disk has only
    the start time record_execution_start() wrote. Falling back to it is safe in
    both places this is used: PROTECTION 5's recency window only widens, and
    _has_github_output() counts a comment posted mid-execution as output, which
    defers rather than redispatching. Returns None when the record carries
    neither, which callers must treat as unverifiable.
    """
    return execution.get('completed_at') or execution.get('timestamp')


def _parse_iso_timestamp(value: str) -> datetime:
    """Parse a recorded ISO timestamp as an aware UTC datetime.

    Records written by this module are always UTC-aware, but a few older ones
    (and anything hand-edited) are naive; comparing one of those against an aware
    datetime raises TypeError, which the watchdog's handlers turn into a silent
    "cannot verify". Assume UTC rather than letting that happen.
    """
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


@dataclass
class ExecutionRecord:
    """Record of a single work execution attempt"""
    column: str
    agent: str
    timestamp: str
    # 'lock_contention' (#148): the dispatch never ran because a project resource
    # lock (project_checkout / dev_container_build) was held for the whole of its
    # timeout. Deliberately NOT 'failure' — count_consecutive_failures() counts
    # only 'failure', and project_monitor's MAX_CONSECUTIVE_DISPATCH_FAILURES turns
    # three of those into a durably-retained board lock. Same role 'frozen' plays
    # for a Claude Code token-limit rejection.
    outcome: str  # 'success', 'failure', 'frozen', 'lock_contention', 'cancelled', 'in_progress'
    trigger_source: str  # 'manual_move', 'pipeline_progression', 'webhook'
    error: Optional[str] = None
    # The board this execution ran on (#144). Optional because records written
    # before this field existed don't have it, and because a couple of dispatch
    # paths genuinely have no board in scope. Consumers must handle None -- see
    # detect_and_retry_empty_successful_executions()'s PROTECTION 2, which falls
    # back to checking every board of the project when it's missing.
    board_name: Optional[str] = None


@dataclass
class StatusChange:
    """Record of a status change"""
    from_status: Optional[str]
    to_status: str
    timestamp: str
    trigger: str  # 'manual', 'auto'


class WorkExecutionStateTracker:
    """Tracks work execution state and determines when to execute/skip work"""

    def __init__(self, state_dir: Path = None):
        """Initialize work execution state tracker"""
        if state_dir is None:
            # CRITICAL: Use absolute path to orchestrator's state directory
            # This prevents state from being created inside project directories when
            # agents execute with project working directory
            import os
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "execution_history"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        logger.info(f"WorkExecutionStateTracker initialized with state_dir: {state_dir}")

    def get_state_file(self, project_name: str, issue_number: int) -> Path:
        """Get the state file path for an issue"""
        return self.state_dir / f"{project_name}_issue_{issue_number}.yaml"

    def _empty_state(self, project_name: str, issue_number: int) -> Dict:
        """The state an issue with nothing recorded yet gets -- also the fallback
        every load_state() failure path returns, so they can't drift apart."""
        return {
            'issue_number': issue_number,
            'project_name': project_name,
            'execution_history': [],
            'status_changes': [],
            'current_status': None,
            'last_updated': None
        }

    def load_state(self, project_name: str, issue_number: int) -> Dict:
        """Load execution state for an issue"""
        state_file = self.get_state_file(project_name, issue_number)

        if not state_file.exists():
            return self._empty_state(project_name, issue_number)

        from utils.file_lock import file_lock, ReentrantFileLockError

        try:
            # Use file lock when reading to prevent reading partial writes
            lock_file = state_file.with_suffix(state_file.suffix + '.lock')
            with file_lock(lock_file):
                if state_file.exists():  # Check again inside lock
                    with open(state_file, 'r') as f:
                        try:
                            state = yaml.safe_load(f)
                        except yaml.YAMLError as parse_error:
                            # Same corrupted-state-file condition as the non-mapping
                            # case below, just detected one step earlier -- report it
                            # the same way rather than as an opaque load failure.
                            self._log_corrupted_state_file(
                                state_file, str(parse_error),
                                owner=f"{project_name}/#{issue_number}"
                            )
                            return self._empty_state(project_name, issue_number)

                    # A truncated or empty state file parses to None, and anything
                    # that isn't a mapping (e.g. a stray scalar) parses to a
                    # non-dict -- either way the setdefault() calls below raise
                    # AttributeError/TypeError into the generic handler, which is
                    # how a single empty file on disk produced
                    # "'NoneType' object has no attribute 'setdefault'" at ERROR on
                    # every load of that issue, forever, without ever naming the
                    # file as the thing needing repair. Report it as its own
                    # condition and fall back to the same empty state a file that
                    # doesn't exist yet gets.
                    if not isinstance(state, dict):
                        self._log_corrupted_state_file(
                            state_file,
                            f"YAML parsed as {type(state).__name__}, expected a mapping",
                            owner=f"{project_name}/#{issue_number}"
                        )
                        return self._empty_state(project_name, issue_number)

                    # Ensure all expected keys exist
                    state.setdefault('execution_history', [])
                    state.setdefault('status_changes', [])
                    return state
            # File doesn't exist inside lock, return default
            return self._empty_state(project_name, issue_number)
        except ReentrantFileLockError:
            # A caller that already holds this issue's lock, i.e. a programming
            # error -- the exact shape PROTECTION 1 had until #150. Swallowing it
            # here hands that caller execution_history: [], which
            # has_active_execution() reads as "nothing is running" and dispatches
            # on: a double execution of a live issue, strictly worse than the
            # deadlock the guard replaced. Let it surface as a traceback.
            raise
        except Exception as e:
            logger.error(f"Failed to load state for {project_name}/#{issue_number}: {e}")
            return self._empty_state(project_name, issue_number)

    @staticmethod
    def _log_corrupted_state_file(
        state_file: Path, detail: str, owner: Optional[str] = None
    ) -> None:
        """
        Report an execution state file that exists but holds nothing loadable.

        Deliberately NOT repaired automatically. Readers and writers here are
        strictly serialised -- utils.file_lock.file_lock() is always LOCK_EX, and
        every reader in this module takes it on the same `<state>.yaml.lock` path
        save_state() writes under -- so a zero-byte file is never a write caught
        in flight. It means one of two things this code cannot tell apart:
        save_state() truncates in place (open(..., 'w')) and the process was
        killed between the truncate and the yaml.dump, or something outside this
        module wrote the file. Deleting it would silently discard execution
        history an operator may want to inspect in either case, so naming the path
        is what they get; every caller falls back to empty state anyway, which
        makes this a visibility fix, not a fatal condition.

        Args:
            state_file: Path to the unreadable file -- the point of this message.
            detail: Why it isn't loadable (parse error, or what it parsed as).
            owner: "project/#issue" when the caller already knows it; omitted by
                callers that were about to learn it FROM the file.
        """
        logger.warning(
            f"Corrupted execution state file {state_file}"
            f"{f' for {owner}' if owner else ''}: {detail} -- treating it as having "
            f"no recorded execution history. Delete or repair the file to clear this."
        )

    def save_state(self, project_name: str, issue_number: int, state: Dict):
        """Save execution state for an issue with thread-safe file locking"""
        from utils.file_lock import safe_yaml_write, ReentrantFileLockError

        state_file = self.get_state_file(project_name, issue_number)

        try:
            state['last_updated'] = datetime.now(timezone.utc).isoformat()
            with safe_yaml_write(state_file):
                with open(state_file, 'w') as f:
                    yaml.dump(state, f, default_flow_style=False, sort_keys=False)

            logger.debug(f"Saved execution state for {project_name}/#{issue_number}")
        except ReentrantFileLockError:
            # See load_state()'s matching handler. A re-entrant save is a caller
            # bug, and logging it here would silently DROP the write -- the
            # execution outcome or probe cleanup this call was persisting is
            # simply lost, with the in-memory dict still claiming it was saved.
            raise
        except Exception as e:
            logger.error(f"Failed to save state for {project_name}/#{issue_number}: {e}")

    def record_execution_start(
        self,
        issue_number: int,
        column: str,
        agent: str,
        trigger_source: str,
        project_name: str,
        board_name: Optional[str] = None
    ):
        """
        Record the start of work execution.

        CRITICAL: This MUST be called BEFORE enqueuing the task to prevent
        race conditions where the task completes before the in_progress
        state is recorded.

        Correct order:
        1. record_execution_start()  <- Creates in_progress state
        2. task_queue.enqueue()      <- Worker can now find in_progress state

        Args:
            issue_number: Issue number for the execution
            column: Workflow column/status
            agent: Agent name
            trigger_source: Source of the trigger (e.g., 'manual', 'pipeline_progression')
            project_name: Project name
            board_name: Board this execution runs on (#144). Recorded so the
                watchdog can scope its pipeline-lock check to THIS execution's
                own board instead of every board configured for the project.
                Every dispatch call site that has a board in scope passes it;
                omitting it is not an error, it just leaves the watchdog with
                the older, deliberately conservative every-board behavior for
                this record.
        """
        state = self.load_state(project_name, issue_number)

        execution = {
            'column': column,
            'agent': agent,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'outcome': 'in_progress',
            'trigger_source': trigger_source
        }

        # Only written when actually known -- an explicit key holding None would
        # be indistinguishable from a board that was recorded as empty, and the
        # consumer's fallback keys off "absent or falsy" either way.
        if board_name:
            execution['board_name'] = board_name

        state['execution_history'].append(execution)
        state['current_status'] = column

        self.save_state(project_name, issue_number, state)

        logger.info(
            f"Recorded execution start: {project_name}/#{issue_number} "
            f"{agent} in {column} (trigger: {trigger_source}"
            f"{f', board: {board_name}' if board_name else ''})"
        )

    def stamp_execution_task_id(self, project_name, issue_number, agent, column, task_id):
        """Stamp the execution-level task_id onto the current in_progress execution.

        Called by agent_executor after generating the task_id, which happens
        after record_execution_start() has already created the in_progress entry.
        This task_id matches the Redis key suffix in agent_result:{project}:{issue}:{task_id}.
        """
        state = self.load_state(project_name, issue_number)
        for execution in reversed(state['execution_history']):
            if (execution.get('outcome') == 'in_progress' and
                execution.get('agent') == agent and
                execution.get('column') == column):
                execution['task_id'] = task_id
                self.save_state(project_name, issue_number, state)
                logger.debug(
                    f"Stamped task_id={task_id} on execution for "
                    f"{project_name}/#{issue_number} {agent} in {column}"
                )
                return
        logger.warning(
            f"No matching in_progress execution found to stamp task_id={task_id} "
            f"for {project_name}/#{issue_number} {agent} in {column}"
        )

    def record_execution_outcome(
        self,
        issue_number: int,
        column: str,
        agent: str,
        outcome: str,
        project_name: str,
        error: Optional[str] = None,
        claude_session_id: Optional[str] = None
    ):
        """Record the outcome of work execution.

        claude_session_id: piggybacked onto the same write (not a separate
        persistence mechanism) when outcome=='frozen' and the rejected call had
        already established a Claude Code session — see docker_runner.py's
        _rate_limit_signal capture. Used by the active-resume step to decide
        whether a captured session is worth --resume-ing.

        board_name (#144) is deliberately NOT a parameter here: the normal path
        mutates the in_progress entry record_execution_start() already wrote, so
        the board it recorded is carried through to the 'success'/'failure'
        record the watchdog inspects. Only the crash-recovery path below
        (no matching in_progress entry) appends a fresh record, and it has no
        board to record for the same reason it has no trigger_source -- nothing
        in scope knows what the lost execution was dispatched onto. Those records
        get PROTECTION 2's every-board fallback; see the comment there.
        """
        state = self.load_state(project_name, issue_number)

        # Find the in_progress entry for this agent/column and update it.
        # Normally there is exactly one (the pre-enqueue probe created by project_monitor).
        # The loop handles any historical duplicates defensively.
        found_primary = False
        phantom_count = 0
        for execution in reversed(state['execution_history']):
            if (execution['column'] == column and
                execution['agent'] == agent and
                execution['outcome'] == 'in_progress'):

                execution['outcome'] = outcome
                if not found_primary:
                    # Most recent in_progress: the real execution entry.
                    #
                    # completed_at is written HERE and nowhere else on the normal
                    # path (#150). Both watchdog gates that ask "did anything
                    # happen after this execution finished?" -- PROTECTION 5's
                    # recency window and _has_github_output() -- key off it, and
                    # until now nothing in production ever wrote it: 0 of the 4721
                    # state files on the live orchestrator carry the field, so both
                    # gates silently degraded to "cannot verify" on every record.
                    execution['completed_at'] = datetime.now(timezone.utc).isoformat()
                    if error:
                        execution['error'] = error
                    if claude_session_id:
                        execution['claude_session_id'] = claude_session_id
                    found_primary = True
                else:
                    # Older in_progress entries are phantom probes (pre-enqueue).
                    # Mark them with the same outcome so they don't linger as in_progress.
                    execution['error'] = (
                        'Superseded by a later execution. This was a pre-enqueue probe '
                        'entry; the actual outcome was recorded on the newer tracking entry.'
                    )
                    phantom_count += 1

        if found_primary:
            self.save_state(project_name, issue_number, state)
            suffix = f" (cleaned up {phantom_count} phantom probe entries)" if phantom_count else ""
            logger.info(
                f"Recorded execution outcome: {project_name}/#{issue_number} "
                f"{agent} in {column} → {outcome}{suffix}"
            )
            return

        # If we get here, no in_progress execution was found.
        # Distinguish between a benign cleanup race and a genuine missing start.
        # When cleanup_stuck_in_progress_states() beats the monitoring thread's
        # finally: block, it already finalized the record with the same outcome —
        # a duplicate write would be wrong and the ERROR log is misleading.
        already_finalized = any(
            e.get('column') == column and e.get('agent') == agent and e.get('outcome') == outcome
            for e in state['execution_history']
        )
        if already_finalized:
            logger.debug(
                f"record_execution_outcome race (benign): {project_name}/#{issue_number} "
                f"{agent} in {column} already finalized with outcome={outcome} — "
                f"cleanup_stuck_in_progress_states won the race"
            )
            return

        # Genuine missing start (restart/crash scenario)
        logger.error(
            f"No in_progress execution found for {agent} in {column}, "
            f"creating new record with outcome {outcome}. "
            f"This should only happen after orchestrator restart/crash."
        )

        # No board_name and no trigger_source: this record is synthesised from
        # what the caller knows now, not from the lost dispatch. See the docstring.
        # timestamp and completed_at are the same instant for the same reason --
        # the real start time went with the lost dispatch, and a record with no
        # completed_at is one the watchdog cannot verify at all.
        now_iso = datetime.now(timezone.utc).isoformat()
        execution = {
            'column': column,
            'agent': agent,
            'timestamp': now_iso,
            'outcome': outcome,
            'trigger_source': 'unknown',
            'completed_at': now_iso
        }

        if error:
            execution['error'] = error
        if claude_session_id:
            execution['claude_session_id'] = claude_session_id

        state['execution_history'].append(execution)
        self.save_state(project_name, issue_number, state)

    def record_status_change(
        self,
        issue_number: int,
        from_status: Optional[str],
        to_status: str,
        trigger: str,
        project_name: str
    ):
        """Record a status change"""
        state = self.load_state(project_name, issue_number)

        status_change = {
            'from_status': from_status,
            'to_status': to_status,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'trigger': trigger
        }

        state['status_changes'].append(status_change)
        state['current_status'] = to_status

        self.save_state(project_name, issue_number, state)

        logger.info(
            f"Recorded status change: {project_name}/#{issue_number} "
            f"{from_status} → {to_status} (trigger: {trigger})"
        )

    def should_execute_work(
        self,
        issue_number: int,
        column: str,
        agent: str,
        trigger_source: str,
        project_name: str
    ) -> Tuple[bool, str]:
        """
        Determine if agent should execute work in this column

        Returns:
            Tuple[bool, str]: (should_execute, reason)
        """
        state = self.load_state(project_name, issue_number)

        # Get executions for this column/agent
        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column and e['agent'] == agent
        ]

        last_execution = column_executions[-1] if column_executions else None

        # Get status changes to this column
        status_changes_to_column = [
            sc for sc in state['status_changes']
            if sc['to_status'] == column
        ]

        last_status_change = status_changes_to_column[-1] if status_changes_to_column else None

        # Case 1: First time in this column
        if not last_execution:
            logger.debug(
                f"Should execute {agent} on {project_name}/#{issue_number}: "
                f"first_execution"
            )
            return True, "first_execution"

        # Case 2: Status changed back to this column after previous execution
        # (indicates manual rework needed)
        if last_status_change:
            last_exec_time = datetime.fromisoformat(last_execution['timestamp'])
            status_change_time = datetime.fromisoformat(last_status_change['timestamp'])

            if status_change_time > last_exec_time:
                logger.debug(
                    f"Should execute {agent} on {project_name}/#{issue_number}: "
                    f"manual_rework_detected (status changed at {status_change_time}, "
                    f"last execution at {last_exec_time})"
                )
                return True, "manual_rework_detected"

        # Case 3: Previous execution failed, was frozen, blocked by lock contention,
        # cancelled, or abandoned. 'lock_contention' belongs here for the same reason
        # 'frozen' does: the agent never ran, so the next poll is the retry point
        # (#148) — it just doesn't count toward count_consecutive_failures().
        if last_execution['outcome'] in ['failure', 'frozen', 'lock_contention', 'cancelled', 'abandoned']:
            logger.debug(
                f"Should execute {agent} on {project_name}/#{issue_number}: "
                f"retry_after_{last_execution['outcome']}"
            )
            return True, f"retry_after_{last_execution['outcome']}"

        # Case 4: Automatic progression triggering after successful execution
        # (prevent double-triggering)
        if trigger_source == 'pipeline_progression':
            if last_execution['outcome'] == 'success':
                # Check if there was a status change after the execution
                if last_status_change:
                    last_exec_time = datetime.fromisoformat(last_execution['timestamp'])
                    status_change_time = datetime.fromisoformat(last_status_change['timestamp'])

                    if status_change_time <= last_exec_time:
                        logger.debug(
                            f"Should skip {agent} on {project_name}/#{issue_number}: "
                            f"skip_auto_progression_after_success"
                        )
                        return False, "skip_auto_progression_after_success"
                else:
                    logger.debug(
                        f"Should skip {agent} on {project_name}/#{issue_number}: "
                        f"skip_auto_progression_after_success (no status change)"
                    )
                    return False, "skip_auto_progression_after_success"

        # Case 5: Work is already in progress
        if last_execution['outcome'] == 'in_progress':
            logger.debug(
                f"Should skip {agent} on {project_name}/#{issue_number}: "
                f"work_already_in_progress"
            )
            return False, "work_already_in_progress"

        # Case 6: Successful execution, no status change, manual trigger
        # (allow explicit retry)
        if trigger_source in ['manual_move', 'webhook', 'manual']:
            logger.debug(
                f"Should execute {agent} on {project_name}/#{issue_number}: "
                f"explicit_manual_trigger"
            )
            return True, "explicit_manual_trigger"

        # Default: skip (already processed successfully)
        logger.debug(
            f"Should skip {agent} on {project_name}/#{issue_number}: "
            f"already_processed_successfully"
        )
        return False, "already_processed_successfully"

    def get_last_execution(
        self,
        project_name: str,
        issue_number: int,
        column: str,
        agent: str
    ) -> Optional[Dict]:
        """Get the last execution record for a column/agent"""
        state = self.load_state(project_name, issue_number)

        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column and e['agent'] == agent
        ]

        return column_executions[-1] if column_executions else None

    def get_last_execution_for_column(
        self,
        project_name: str,
        issue_number: int,
        column: str
    ) -> Optional[Dict]:
        """Get the last execution record for a column, whichever agent recorded it.

        The agent-scoped get_last_execution() above is right when the caller knows
        which agent it is asking about. project_monitor's sustained-contention check
        (#148) does not: the agent that records an outcome for a column is often NOT
        that column's configured agent -- a review column's maker dispatch records
        under the maker's own name, and PR review records under the synthetic
        'pr_review_stage' wrapper. Keyed on the column's configured agent, those
        contention entries are invisible, which is most of what the escalation is
        for. Returns the whole record, so the caller can read `agent` off it and
        stay agent-scoped from there (count_consecutive_lock_contentions() below
        deliberately counts a run for one agent, not a mixture).
        """
        state = self.load_state(project_name, issue_number)

        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column
        ]

        return column_executions[-1] if column_executions else None

    def get_resumable_frozen_session(
        self,
        project_name: str,
        issue_number: int,
        column: str,
        agent: str
    ) -> Optional[str]:
        """
        Return the captured Claude Code session_id from the most recent frozen
        execution for this (project, issue, column, agent), if one exists.

        Used by agent_executor.py's frozen-session resume fork to decide whether
        to --resume a prior session (with a short continuation prompt) instead of
        rebuilding the stage's normal prompt from scratch. Only returns a value
        when the frozen execution actually captured one: agent_executor.py only
        piggybacks claude_session_id onto the 'frozen' outcome write when the
        rejected call had positive evidence of prior progress (see
        ClaudeCodeRateLimitError.prior_progress in docker_runner.py) — a session
        with zero prior turns has nothing to continue, so it's never stored.
        """
        last_execution = self.get_last_execution(project_name, issue_number, column, agent)
        if not last_execution or last_execution.get('outcome') != 'frozen':
            return None
        return last_execution.get('claude_session_id')

    # NOTE: the old set_halt_marker/get_halt_marker/clear_halt_marker mechanism
    # (a per-issue YAML flag, invisible to any dashboard, with no relationship to
    # the pipeline lock) was removed in favor of a durable failure signal carried
    # on the pipeline lock itself — see PipelineLockManager.mark_lock_failed /
    # get_retained_reason and PipelineRunManager.mark_failed. Recovery is
    # scripts/release_lock.py; discovery is scripts/list_failed_pipeline_runs.py.

    def count_consecutive_failures(
        self,
        project_name: str,
        issue_number: int,
        column: str,
        agent: str
    ) -> int:
        """Count trailing consecutive 'failure' outcomes for this column/agent —
        same filter idiom as get_last_execution().

        Only 'failure' increments, so a 'lock_contention' dispatch (#148) — where
        the agent never ran because another holder owned a project resource lock —
        never accumulates toward project_monitor's
        MAX_CONSECUTIVE_DISPATCH_FAILURES, which would durably retain the whole
        board's pipeline lock over pure contention.

        But 'lock_contention' is SKIPPED rather than treated as the end of the run.
        Ending the run on it would let contention erase real failure history: on a
        busy shared base clone, a genuinely broken agent that loses the lock race
        every third dispatch produces failure, failure, lock_contention, failure,
        failure, ... — a trailing count that never reaches 3, so the budget never
        fires and the broken agent is re-dispatched forever. Contention is
        transparent to this count in both directions: it neither accumulates
        toward the budget nor resets it. Every other outcome still ends the run —
        a success, a frozen pause or a cancellation genuinely does mean the
        preceding failures are no longer consecutive.
        """
        state = self.load_state(project_name, issue_number)
        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column and e['agent'] == agent
        ]
        count = 0
        for execution in reversed(column_executions):
            outcome = execution.get('outcome')
            if outcome == 'failure':
                count += 1
            elif outcome == 'lock_contention':
                continue
            else:
                break
        return count

    def count_consecutive_lock_contentions(
        self,
        project_name: str,
        issue_number: int,
        column: str,
        agent: str
    ) -> int:
        """Count trailing consecutive 'lock_contention' outcomes for this
        column/agent — the contention counterpart of
        count_consecutive_failures(), same filter idiom.

        Exists so repeated contention is bounded and visible rather than silent
        (#148). 'lock_contention' is deliberately excluded from the dispatch
        failure budget, and should_execute_work() re-dispatches after it, so
        without a counter of its own a permanently-held project resource lock
        (e.g. an orchestrator-side coroutine wedged while its heartbeat thread
        keeps refreshing the lock's TTL) produces an unbounded wait/re-dispatch
        loop with nothing but a per-occurrence log line to show for it.
        project_monitor escalates on this count; it deliberately does NOT
        mark_failed(), which would reintroduce exactly the durable board-lock
        retention the contention exemption exists to prevent.

        Unlike count_consecutive_failures() above this one ends its run on ANY
        other outcome, 'failure' included: a real dispatch that got far enough to
        fail is proof the lock was obtainable in between, so the contention was
        not continuous.
        """
        state = self.load_state(project_name, issue_number)
        column_executions = [
            e for e in state['execution_history']
            if e['column'] == column and e['agent'] == agent
        ]
        count = 0
        for execution in reversed(column_executions):
            if execution.get('outcome') == 'lock_contention':
                count += 1
            else:
                break
        return count

    def get_execution_history(
        self,
        project_name: str,
        issue_number: int
    ) -> List[Dict]:
        """Get full execution history for an issue"""
        state = self.load_state(project_name, issue_number)
        return state['execution_history']

    def has_active_execution(
        self,
        project_name: str,
        issue_number: int
    ) -> bool:
        """
        Check if there's an active (in_progress) execution for this issue.

        This method checks ALL types of work that can be active on an issue:
        1. Regular agent execution (execution_history with outcome='in_progress')
        2. Active review cycles (maker-checker loops)
        3. Repair cycle containers (long-running test containers)
        4. Conversational feedback loops (human-in-the-loop)

        Returns True if ANY type of work is currently active for this issue,
        preventing duplicate work from being scheduled.

        This is critical for preventing race conditions between:
        - Project Monitor and Review Cycle Manager
        - Project Monitor and Repair Cycle containers
        - Pipeline Orchestrator and Project Monitor
        - Any concurrent work trigger sources

        Callers that ALREADY hold this issue's state-file lock must call
        has_active_execution_for_state() instead -- see its docstring.
        """
        state = self.load_state(project_name, issue_number)
        return self.has_active_execution_for_state(state, project_name, issue_number)

    def has_active_execution_for_state(
        self,
        state: Dict,
        project_name: str,
        issue_number: int,
        persist_probe_cleanup: bool = True
    ) -> bool:
        """
        has_active_execution()'s body, operating on an ALREADY-LOADED state dict
        instead of reading the state file itself.

        Exists because fcntl.flock() locks are per open-file-description, not per
        process or per thread: a caller holding <issue>.yaml.lock that then called
        has_active_execution() re-entered load_state(), which opens the SAME lock
        path on a fresh fd and blocks forever on its own lock. That is exactly what
        detect_and_retry_empty_successful_executions()'s PROTECTION 1 did -- it took
        the state file's lock for the whole per-file body and then called
        has_active_execution() -- wedging the sweep's scheduler thread on the first
        'success' record it found and leaving every later protection, including
        #144's board scoping, permanently unreachable.

        Args:
            state: state dict the caller already read (under its own lock)
            persist_probe_cleanup: whether a cleared stale pre-enqueue probe may be
                written back via save_state(). Callers holding the state file's lock
                MUST pass False -- save_state() takes that same lock and would
                deadlock the same way. The mutation still happens on the passed-in
                dict, so a caller that writes `state` back itself persists it; if
                nobody does, the next unlocked has_active_execution() call re-derives
                and persists it, since the decision is a pure function of the probe's
                age.
        """
        # Check 1: Regular agent execution in execution_history
        for execution in state.get('execution_history', []):
            if execution.get('outcome') == 'in_progress':
                # Detect stale pre-enqueue probes written by pipeline_progression.
                #
                # pipeline_progression writes in_progress BEFORE enqueuing the Redis
                # task so that concurrent project_monitor polls can't start a review
                # cycle while the task is queued.  When the task_manager actually picks
                # up the task, agent_executor calls stamp_execution_task_id(), which
                # adds 'task_id' to this entry.  If the task was swept from Redis
                # before pickup (e.g. by the 10-minute queue-sync during a restart),
                # no task_id is ever stamped and the probe becomes a permanent blocker.
                #
                # Guard: only applies to pipeline_progression probes with no task_id.
                # Entries written by other sources, or probes that were stamped, are
                # left alone — they represent legitimately running containers.
                if (execution.get('trigger_source') == 'pipeline_progression'
                        and 'task_id' not in execution):
                    try:
                        probe_time = datetime.fromisoformat(execution['timestamp'])
                        if probe_time.tzinfo is None:
                            probe_time = probe_time.replace(tzinfo=timezone.utc)
                        age_secs = (datetime.now(timezone.utc) - probe_time).total_seconds()
                        if age_secs > _STALE_ENQUEUE_PROBE_SECS:
                            logger.warning(
                                f"Clearing stale pipeline_progression probe for "
                                f"{project_name}/#{issue_number}: "
                                f"{execution.get('agent')} in {execution.get('column')} "
                                f"(age={age_secs:.0f}s, no task_id stamp — "
                                f"Redis task was swept before consumption)"
                            )
                            # Mutate this specific entry directly in the already-loaded
                            # state dict, then persist once.  Using record_execution_outcome()
                            # here would be unsafe: that method matches by agent+column and
                            # would also mark any legitimately-running entry for the same
                            # agent/column as abandoned.
                            execution['outcome'] = 'abandoned'
                            execution['error'] = (
                                f'Pre-enqueue probe stale after {age_secs:.0f}s '
                                f'with no task_id stamp; Redis task was swept '
                                f'from queue before being consumed'
                            )
                            if persist_probe_cleanup:
                                self.save_state(project_name, issue_number, state)
                            continue  # not blocking
                    except Exception as e:
                        logger.warning(
                            f"Failed to check probe staleness for "
                            f"{project_name}/#{issue_number}: {e}"
                        )
                        # Fall through: treat as active (safe default)

                logger.debug(
                    f"Active execution found in history for {project_name}/#{issue_number}: "
                    f"{execution.get('agent')} in {execution.get('column')}"
                )
                return True

        # Track if any service checks fail (indicates potential initialization issue)
        check_failures = []

        # Check 2: Active review cycles
        try:
            from services.review_cycle import review_cycle_executor
            ck = review_cycle_executor._cycle_key(project_name, issue_number)
            if ck in review_cycle_executor.active_cycles:
                cycle = review_cycle_executor.active_cycles[ck]
                logger.debug(
                    f"Active review cycle found for {project_name}/#{issue_number}: "
                    f"iteration {cycle.current_iteration}, status={cycle.status}"
                )
                return True
        except (ImportError, AttributeError) as e:
            # Review cycle module may not be available or initialized
            logger.warning(
                f"Could not check review cycles for {project_name}/#{issue_number}: {e}. "
                f"This may indicate an initialization issue."
            )
            check_failures.append('review_cycle')

        # Check 3: Repair cycle containers
        try:
            if self._check_redis_repair_cycle_tracking(project_name, issue_number):
                logger.debug(
                    f"Active repair cycle container found for {project_name}/#{issue_number}"
                )
                return True
        except Exception as e:
            logger.warning(
                f"Could not check repair cycles for {project_name}/#{issue_number}: {e}"
            )
            check_failures.append('repair_cycle')

        # Check 4: Conversational feedback loops
        try:
            from services.human_feedback_loop import human_feedback_loop_executor
            lk = human_feedback_loop_executor._loop_key(project_name, issue_number)
            if lk in human_feedback_loop_executor.active_loops:
                logger.debug(
                    f"Active feedback loop found for {project_name}/#{issue_number}"
                )
                return True
        except (ImportError, AttributeError) as e:
            # Feedback loop module may not be available or initialized
            logger.warning(
                f"Could not check feedback loops for {project_name}/#{issue_number}: {e}. "
                f"This may indicate an initialization issue."
            )
            check_failures.append('feedback_loop')

        # Fail-safe: If multiple service checks failed, assume work might be active
        # to prevent duplicate executions during degraded state
        if len(check_failures) >= 2:
            logger.error(
                f"Multiple service checks failed for {project_name}/#{issue_number}: {check_failures}. "
                f"Failing safe by assuming active execution to prevent duplicates."
            )
            return True

        # No active work found
        return False

    def is_frozen_by_circuit_breaker(
        self,
        project_name: str,
        issue_number: int
    ) -> bool:
        """
        Check if the last execution was frozen by the Claude Code circuit breaker.

        This allows the project monitor to detect when a previously frozen
        execution can be retried (after circuit breaker closes).

        The 'frozen' outcome value is itself the unambiguous marker for this
        condition, unlike the old 'blocked' value it replaced. Written by
        agent_executor.py's is_claude_breaker_failure branch (the normal
        per-agent execution path) and by project_monitor.py's
        _monitor_repair_cycle_container (the standalone repair-cycle container
        path, which has its own ClaudeCodeRateLimitError handling since it
        doesn't route through agent_executor). Deliberately does NOT also
        substring-match
        the human-readable error text: that text varies by which of several
        detection paths fired (e.g. the new structural detector's "Claude Code
        rate limit confirmed (source=...)" never contains the literal phrase
        "circuit breaker" that an older fallback message happened to use), so
        matching on it would silently miss real frozen runs.

        Returns:
            True if last execution was frozen by circuit breaker, False otherwise
        """
        state = self.load_state(project_name, issue_number)
        history = state.get('execution_history', [])

        if not history:
            return False

        # Check the most recent execution
        last_execution = history[-1]
        is_frozen = last_execution.get('outcome') == 'frozen'

        if is_frozen:
            logger.debug(
                f"Issue #{issue_number} was frozen by circuit breaker: "
                f"{last_execution.get('error', '')}"
            )

        return is_frozen

    def was_recent_programmatic_change(
        self,
        project_name: str,
        issue_number: int,
        to_status: str,
        time_window_seconds: Optional[int] = None
    ) -> bool:
        """
        Check if a status change to the given status was recently made programmatically.

        This helps avoid duplicate event emission when the project monitor detects
        a status change that was already emitted by the pipeline progression service.

        Args:
            project_name: Project name
            issue_number: Issue number
            to_status: Target status to check
            time_window_seconds: Time window in seconds to consider "recent"
                                If None, reads from env var PROGRAMMATIC_CHANGE_WINDOW_SECONDS
                                with fallback to 60 seconds

        Returns:
            True if a programmatic status change to this status was made within the time window
        """
        # Allow configurable time window via environment variable
        if time_window_seconds is None:
            import os
            time_window_seconds = int(os.environ.get('PROGRAMMATIC_CHANGE_WINDOW_SECONDS', '60'))
            logger.debug(f"Using programmatic change window: {time_window_seconds}s")
        state = self.load_state(project_name, issue_number)
        
        # Check status_changes for recent programmatic changes
        for status_change in reversed(state.get('status_changes', [])):
            if status_change['to_status'] != to_status:
                continue
            
            # Check if trigger indicates programmatic change
            trigger = status_change.get('trigger', '')
            if trigger in ['agent_auto_advance', 'pipeline_progression', 'review_cycle',
                          'review_cycle_completion', 'repair_cycle_completion',
                          'agent_completion', 'auto', 'all_subtasks_completed']:
                # Check if it's recent
                try:
                    change_time = datetime.fromisoformat(status_change['timestamp'])
                    now = datetime.now(timezone.utc)
                    time_diff = (now - change_time).total_seconds()
                    
                    if time_diff <= time_window_seconds:
                        logger.debug(
                            f"Found recent programmatic status change for {project_name}/#{issue_number} "
                            f"to {to_status} (trigger: {trigger}, {time_diff:.1f}s ago)"
                        )
                        return True
                except Exception as e:
                    logger.warning(f"Error parsing timestamp for status change: {e}")
                    continue
        
        return False

    def _check_redis_tracking_for_agent(self, project: str, agent: str, issue_number: int) -> bool:
        """
        Check if there's a Redis tracking key for an active agent container.
        
        Args:
            project: Project name
            agent: Agent name
            issue_number: Issue number
            
        Returns:
            True if Redis tracking exists for this agent, False otherwise
        """
        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            
            # Cursor-based scan (non-blocking, unlike keys())
            for key in redis_client.scan_iter(match='agent:container:*', count=100):
                try:
                    container_info = redis_client.hgetall(key)
                    if (container_info.get('project') == project and
                        container_info.get('agent') == agent and
                        container_info.get('issue_number') == str(issue_number)):
                        return True
                except Exception as e:
                    logger.warning(f"Error checking Redis key {key}: {e}")
                    continue
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking Redis tracking: {e}")
            return False

    def _discover_containers_for_execution(self, project: str, execution: dict, provided_container_names: list) -> list:
        """
        Discover containers for an execution using hierarchical discovery methods.

        Priority:
        1. Use provided container names (from caller's docker ps)
        2. Docker label filter by task_id (if available) - MOST PRECISE
        3. Docker label filter by project + issue_number (fallback)
        4. Name pattern matching (legacy fallback)

        Returns: List of container names matching this execution
        """
        import subprocess

        # If caller already found containers, use those
        if provided_container_names:
            logger.debug(
                f"Using {len(provided_container_names)} containers from caller for "
                f"{project}/#{execution.get('issue_number')}"
            )
            return provided_container_names

        task_id = execution.get('task_id')
        issue_number = execution.get('issue_number')

        # Method 1: Docker label filter by task_id (most precise)
        if task_id:
            try:
                result = subprocess.run(
                    ['docker', 'ps', '--filter', f'label=org.switchyard.task_id={task_id}',
                     '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    containers = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
                    if containers:
                        logger.info(
                            f"Discovered {len(containers)} container(s) via task_id label "
                            f"({task_id}) for {project}/#{issue_number}"
                        )
                        return containers
            except Exception as e:
                logger.warning(f"Failed to discover containers by task_id label: {e}")

        # Method 2: Docker label filter by project + issue_number (medium precision)
        if issue_number:
            try:
                result = subprocess.run(
                    ['docker', 'ps',
                     '--filter', f'label=org.switchyard.project={project}',
                     '--filter', f'label=org.switchyard.issue_number={issue_number}',
                     '--format', '{{.Names}}'],
                    capture_output=True,
                    text=True,
                    timeout=5
                )
                if result.returncode == 0 and result.stdout.strip():
                    containers = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
                    if containers:
                        logger.info(
                            f"Discovered {len(containers)} container(s) via project+issue labels "
                            f"for {project}/#{issue_number} (task_id unavailable)"
                        )
                        return containers
            except Exception as e:
                logger.warning(f"Failed to discover containers by project+issue labels: {e}")

        # Method 3: Name pattern matching (legacy fallback - least precise)
        logger.info(
            f"Falling back to name pattern matching for {project}/#{issue_number} "
            f"(labels not available)"
        )
        try:
            result = subprocess.run(
                ['docker', 'ps', '--filter', f'name=claude-agent-{project}-',
                 '--format', '{{.Names}}'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0 and result.stdout.strip():
                containers = [n.strip() for n in result.stdout.strip().split('\n') if n.strip()]
                if containers:
                    logger.warning(
                        f"Discovered {len(containers)} container(s) via name pattern for {project} - "
                        f"may include false positives, will validate with labels"
                    )
                    return containers
        except Exception as e:
            logger.warning(f"Failed to discover containers by name pattern: {e}")

        logger.info(f"No containers discovered for {project}/#{issue_number} task_id={task_id}")
        return []

    def _repair_missing_redis_tracking(self, execution: dict, project: str, container_names: list = None):
        """
        Repair missing Redis tracking keys by reading Docker container labels.

        When a container exists in Docker but has no Redis tracking key, this method
        inspects the container to extract metadata from labels and re-registers it.

        Args:
            execution: Execution record dict with task_id, issue_number, agent, etc.
            project: Project name
            container_names: Optional list of container names to repair (discovered if not provided)
        """
        import subprocess

        # Extract metadata from execution record
        task_id = execution.get('task_id')
        issue_number = execution.get('issue_number')
        agent = execution.get('agent')
        column = execution.get('column', 'unknown')
        timestamp = execution.get('timestamp')

        if not issue_number:
            logger.error(f"Execution record missing issue_number, cannot repair: {execution}")
            return

        if not agent:
            logger.error(f"Execution record missing agent field, cannot repair: {execution}")
            return

        logger.debug(
            f"Attempting repair for {project}/#{issue_number}: "
            f"agent={agent}, task_id={task_id}, column={column}, timestamp={timestamp}"
        )

        # Use provided containers or discover via hierarchical search
        if container_names is None:
            container_names = []

        if not container_names:
            container_names = self._discover_containers_for_execution(project, execution, [])

        if not container_names:
            logger.info(
                f"No containers found for {project}/#{issue_number} task_id={task_id} - "
                f"nothing to repair"
            )
            return

        logger.debug(
            f"Repairing Redis tracking for {project}/#{issue_number}. "
            f"Containers to inspect: {container_names}"
        )

        for container_name in container_names:
            try:
                # Extract container ID and labels via docker inspect
                inspect_result = subprocess.run(
                    ['docker', 'inspect', '--format',
                     '{{.Id}}|{{index .Config.Labels "org.switchyard.agent"}}|'
                     '{{index .Config.Labels "org.switchyard.project"}}|'
                     '{{index .Config.Labels "org.switchyard.task_id"}}|'
                     '{{index .Config.Labels "org.switchyard.issue_number"}}|'
                     '{{index .Config.Labels "org.switchyard.pipeline_run_id"}}',
                     container_name],
                    capture_output=True,
                    text=True,
                    timeout=5
                )

                if inspect_result.returncode != 0:
                    logger.warning(f"Failed to inspect container {container_name}: {inspect_result.stderr}")
                    continue

                parts = inspect_result.stdout.strip().split('|')
                container_id = parts[0] if len(parts) > 0 else ''
                label_agent = parts[1] if len(parts) > 1 and parts[1] else agent
                label_project = parts[2] if len(parts) > 2 and parts[2] else project
                label_task_id = parts[3] if len(parts) > 3 and parts[3] else 'unknown'
                label_issue = parts[4] if len(parts) > 4 and parts[4] else str(issue_number)
                label_pipeline_run_id = parts[5] if len(parts) > 5 and parts[5] else ''

                # Validation hierarchy: task_id (primary) > issue_number (secondary) > agent (tertiary)

                # Level 1: Task ID validation (most reliable)
                if task_id and label_task_id and label_task_id != 'unknown':
                    if label_task_id != task_id:
                        logger.warning(
                            f"VALIDATION FAILED (task_id mismatch): Container {container_name} "
                            f"has task_id={label_task_id}, expected={task_id}. Skipping repair."
                        )
                        continue
                    logger.info(
                        f"VALIDATION PASSED (task_id): {container_name} matches task_id={task_id}"
                    )

                # Level 2: Issue number validation (consistency check)
                if label_issue != str(issue_number):
                    if task_id and label_task_id == task_id:
                        # Task ID matched but issue doesn't - data inconsistency
                        logger.error(
                            f"DATA INCONSISTENCY: Container {container_name} has matching task_id "
                            f"but mismatched issue (container: #{label_issue}, execution: #{issue_number}). "
                            f"Proceeding with repair."
                        )
                    else:
                        # Issue mismatch and no task_id confirmation - skip
                        logger.warning(
                            f"VALIDATION FAILED (issue_number): Container {container_name} "
                            f"is for issue #{label_issue}, not #{issue_number}. Skipping repair."
                        )
                        continue

                # Level 3: Agent consistency check (informational)
                if label_agent and agent and label_agent != agent:
                    logger.warning(
                        f"Agent mismatch for {container_name}: "
                        f"container={label_agent}, execution={agent}"
                    )

                # Level 4: Project validation (sanity check)
                if label_project != project:
                    logger.error(
                        f"VALIDATION FAILED (project): Container {container_name} "
                        f"project={label_project}, expected={project}. Skipping."
                    )
                    continue

                from datetime import datetime
                container_info = {
                    'container_name': container_name,
                    'container_id': container_id,
                    'agent': label_agent,
                    'project': label_project,
                    'task_id': label_task_id,
                    'started_at': datetime.now().isoformat(),
                    'issue_number': label_issue,
                    'pipeline_run_id': label_pipeline_run_id,
                    'repaired': 'true'
                }

                import redis
                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                redis_client.hset(f'agent:container:{container_name}', mapping=container_info)
                redis_client.expire(f'agent:container:{container_name}', 7200)

                logger.info(
                    f"REPAIRED Redis tracking for container {container_name} "
                    f"(agent={label_agent}, project={label_project}, issue=#{label_issue}, "
                    f"task_id={label_task_id}, validation={'task_id' if (task_id and label_task_id and label_task_id != 'unknown') else 'issue_number'})"
                )

            except Exception as e:
                logger.warning(f"Failed to repair Redis tracking for container {container_name}: {e}")

    def _check_redis_repair_cycle_tracking(self, project: str, issue_number: int) -> bool:
        """
        Check if there's a Redis tracking key for an active repair cycle container.
        
        Args:
            project: Project name
            issue_number: Issue number
            
        Returns:
            True if Redis tracking exists for this repair cycle, False otherwise
        """
        try:
            import redis
            redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
            
            # Check for repair cycle tracking key (format: repair_cycle:container:{project}:{issue})
            redis_key = f"repair_cycle:container:{project}:{issue_number}"
            exists = redis_client.exists(redis_key)
            
            if exists:
                logger.debug(f"Found repair cycle tracking in Redis: {redis_key}")
                return True
            
            return False

        except Exception as e:
            logger.error(f"Error checking repair cycle Redis tracking: {e}")
            return False

    def _should_retry_failed_execution(
        self,
        project_name: str,
        issue_number: int,
        agent: str,
        column: str,
        execution: dict,
        project_config=None
    ) -> tuple:
        """
        Determine if a failed execution should be retried by watchdog.

        Performs comprehensive eligibility checks:
        - Retry limit not exceeded
        - Pipeline run still active
        - Issue still in same active column
        - Column still requires agent
        - Issue still open
        - Circuit breakers not open

        Args:
            project_name: Project name
            issue_number: Issue number
            agent: Agent name
            column: Column name
            execution: Execution record dict
            project_config: Already-loaded ProjectConfig, when the caller has one.
                get_project_config() re-reads and re-parses the project's YAML from
                disk on every call, and the sweep already caches it per project --
                passing it through stops this method re-reading the same file once
                per state file. Omitted (None) means fetch it here, as before.

        Returns:
            (should_retry, reason) tuple
        """
        import os

        # Check 1: Retry limit
        max_retries = int(os.environ.get('WATCHDOG_MAX_RETRIES', '3'))
        retry_count = execution.get('watchdog_retry_count', 0)

        if retry_count >= max_retries:
            return False, f"max_retries_exceeded (count={retry_count}, max={max_retries})"

        # Check 2: Pipeline run active.
        #
        # Deliberately ahead of the GitHub query below (#150), which used to run
        # first and unconditionally. This check is a Redis hash lookup and it is
        # the one that rejects the overwhelming majority of records -- an issue
        # with no active pipeline run is not retryable no matter what GitHub says.
        # Asking GitHub first meant one GraphQL query for EVERY 'success' record
        # the sweep examined; on the live orchestrator that is 4570 records every
        # 15 minutes, ~18k queries an hour against a 5000/hour budget. That cost
        # was invisible only because the sweep wedged at PROTECTION 1 and never
        # got here.
        try:
            from services.pipeline_run import get_pipeline_run_manager

            pipeline_run_mgr = get_pipeline_run_manager()
            active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

            if not active_run:
                return False, "no_active_pipeline_run"

        except Exception as e:
            logger.error(f"Error checking pipeline run: {e}")
            return False, f"error_checking_pipeline_run: {str(e)}"

        # Check 3 & 5: Issue state and column (combined GitHub query)
        try:
            from config.manager import config_manager
            from services.github_api_client import get_github_client

            if project_config is None:
                project_config = config_manager.get_project_config(project_name)
            if not project_config:
                return False, "project_config_not_found"

            github_client = get_github_client()

            # Get issue details (state and current column)
            query = """
            query($owner: String!, $repo: String!, $number: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $number) {
                  state
                  projectItems(first: 10) {
                    nodes {
                      fieldValueByName(name: "Status") {
                        ... on ProjectV2ItemFieldSingleSelectValue {
                          name
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            success, data = github_client.graphql(query, {
                'owner': project_config.github['org'],
                'repo': project_config.github['repo'],
                'number': issue_number
            })

            if not success:
                logger.warning(f"Failed to query issue state for {project_name}/#{issue_number}: {data}")
                return False, "github_query_failed"

            issue_data = data.get('repository', {}).get('issue', {})

            # Check 5: Issue state
            if issue_data.get('state', '').upper() == 'CLOSED':
                return False, "issue_closed"

            # Check 2: Current column
            current_column = None
            for item in issue_data.get('projectItems', {}).get('nodes', []):
                field_value = item.get('fieldValueByName')
                if field_value:
                    current_column = field_value.get('name')
                    break

            if current_column != column:
                return False, f"issue_moved_to_different_column (was={column}, now={current_column})"

        except Exception as e:
            logger.error(f"Error checking issue state: {e}")
            return False, f"error_checking_issue_state: {str(e)}"

        # Check 4: Column requires agent (uses active_run.board from Check 2)
        try:
            workflow_template = config_manager.get_project_workflow(project_name, active_run.board)
            if not workflow_template:
                return False, "workflow_template_not_found"

            column_config = None
            for col in workflow_template.columns:
                if col.name == column:
                    column_config = col
                    break

            if not column_config or not column_config.agent or column_config.agent == 'null':
                return False, "column_no_longer_requires_agent"

        except Exception as e:
            logger.error(f"Error checking workflow template: {e}")
            return False, f"error_checking_workflow: {str(e)}"

        # Check 6: Claude Code circuit breaker
        try:
            from monitoring.claude_code_breaker import get_claude_code_breaker

            claude_breaker = get_claude_code_breaker()
            if claude_breaker.is_open():
                return False, "claude_code_breaker_open"

        except Exception as e:
            logger.warning(f"Error checking Claude Code breaker: {e}")
            # Continue - don't block on breaker check failure

        # Check 7: Agent-specific circuit breaker
        # Note: Agent circuit breakers are checked in agent_executor, not globally accessible
        # Skip this check for now - agent_executor will handle it on retry

        # All checks passed
        return True, "eligible_for_retry"

    def should_retry_execution(
        self,
        project_name: str,
        issue_number: int,
        max_retries: int = None
    ) -> tuple:
        """
        Check if execution should be retried (works for both failed and successful-but-empty executions).

        This is the public API for checking retry eligibility. It wraps _should_retry_failed_execution
        with additional context loading.

        Args:
            project_name: Project name
            issue_number: Issue number
            max_retries: Optional max retry limit (defaults to WATCHDOG_MAX_RETRIES env var)

        Returns:
            (should_retry: bool, reason: str)
        """
        state = self.load_state(project_name, issue_number)
        if not state or not state['execution_history']:
            return False, "No execution state found"

        # Get last execution
        last_execution = state['execution_history'][-1]
        agent = last_execution.get('agent')
        column = last_execution.get('column')

        if not agent or not column:
            return False, "Missing agent or column in execution state"

        # Use comprehensive eligibility checks
        return self._should_retry_failed_execution(
            project_name, issue_number, agent, column, last_execution
        )

    def detect_and_retry_empty_successful_executions(self) -> int:
        """
        Detect executions marked as 'success' but with no GitHub output.
        Mark them as 'failure' to trigger retry.

        This watchdog runs as a scheduled task and uses comprehensive race condition
        protections to prevent duplicate work launches:
        0. Age gate - records older than _WATCHDOG_MAX_RECORD_AGE_HOURS are skipped
           before any I/O is spent on them
        1. has_active_execution() - Checks ALL 4 types of active work
        2. Pipeline lock verification
        3. Queue status check
        4. Execution eligibility via _should_retry_failed_execution
        5. 5-minute recency check

        CRITICAL: This method only marks executions as 'failure' - it does NOT
        directly trigger work. The project_monitor picks up failed executions and
        handles retry with its own race protections.

        Returns:
            Number of executions marked for retry
        """
        import os
        from pathlib import Path
        from datetime import datetime, timedelta
        import re

        if not self.state_dir.exists():
            logger.debug("No execution state directory found, skipping empty output detection")
            return 0

        retried_count = 0
        state_files = list(self.state_dir.glob("*.yaml"))

        max_record_age_hours = float(
            os.environ.get('WATCHDOG_MAX_RECORD_AGE_HOURS', _WATCHDOG_MAX_RECORD_AGE_HOURS)
        )

        logger.info(f"Watchdog: Checking {len(state_files)} execution state files for empty outputs")

        # Cache project_config across every state file in this sweep, keyed
        # by project name (found in #58 review round 3: get_project_config()
        # re-reads and re-parses the project's YAML from disk on every call,
        # no caching of its own -- state files for the same project are
        # common in one sweep, so fetching it once per state file instead of
        # once per project multiplies disk I/O by issue count rather than
        # project count on this periodic maintenance path).
        project_config_cache = {}

        # Cache PipelineQueueManager instances across the whole sweep, keyed by
        # (project, board) (#140 item 17): get_pipeline_queue_manager() constructs
        # a brand-new manager -- state_dir mkdir included -- on every call, and
        # PROTECTION 3 below calls it once per board for EVERY stuck state file it
        # examines, so an N-file x M-board sweep built N*M throwaway managers for
        # the same M boards. Only the manager is cached, deliberately not the
        # queue contents: get_issue_status() re-reads the queue file under its own
        # lock on each call, and PROTECTION 3 is a race guard -- acting on a
        # snapshot taken at the top of a long sweep is exactly the staleness it
        # exists to avoid.
        queue_manager_cache = {}

        for state_file in state_files:
            try:
                from utils.file_lock import file_lock

                # Use file lock for entire read-modify-write cycle
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(lock_file):
                    # Load state
                    if not state_file.exists():  # Check inside lock
                        continue
                    with open(state_file, 'r') as f:
                        state = yaml.safe_load(f)

                    # Same corrupted-state-file condition load_state() reports --
                    # see _log_corrupted_state_file(). Without this an empty or
                    # non-mapping file reached state['execution_history'] below and
                    # surfaced as a generic TypeError/KeyError from this loop's
                    # outer handler, naming the exception rather than the file.
                    if state is not None and not isinstance(state, dict):
                        self._log_corrupted_state_file(
                            state_file,
                            f"YAML parsed as {type(state).__name__}, expected a mapping"
                        )
                        continue

                    if not state or not state.get('execution_history'):
                        continue

                    # Get last execution
                    last_exec = state['execution_history'][-1]

                    # Only check successful executions
                    if last_exec.get('outcome') != 'success':
                        continue

                    # Parse project and issue from state
                    project_name = state.get('project_name')
                    issue_number = state.get('issue_number')

                    if not project_name or not issue_number:
                        logger.warning(f"Malformed state file {state_file}: missing project or issue")
                        continue

                    # PROTECTION 0: Age gate -- see _WATCHDOG_MAX_RECORD_AGE_HOURS.
                    #
                    # Placed ahead of every other protection because it is the only
                    # one that costs nothing: PROTECTION 2 and 3 read lock and queue
                    # state per configured board, and PROTECTION 4 issues a GitHub
                    # GraphQL query. On the live orchestrator 4570 of 4721 state
                    # files end in 'success', so an ungated sweep every 15 minutes
                    # is ~18k GraphQL queries an hour against a 5k/hour budget --
                    # the watchdog would exhaust the budget for the whole
                    # orchestrator in the first minutes of every hour. A record this
                    # old is not something a retry can un-stick anyway; nothing is
                    # waiting on it.
                    #
                    # A record with no parseable timestamp is NOT skipped: this gate
                    # exists to bound cost, and silently dropping records it cannot
                    # date would be the same class of quiet no-op #150 is undoing.
                    age_anchor = _execution_anchor_time(last_exec)
                    if age_anchor and max_record_age_hours > 0:
                        try:
                            age_hours = (
                                datetime.now(timezone.utc) - _parse_iso_timestamp(age_anchor)
                            ).total_seconds() / 3600
                            if age_hours > max_record_age_hours:
                                logger.debug(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                    f"last execution is {age_hours:.1f}h old "
                                    f"(cutoff {max_record_age_hours}h)"
                                )
                                continue
                        except (ValueError, TypeError, AttributeError) as e:
                            # An unquoted timestamp in a hand-edited file parses as
                            # a datetime, not a str, and anything else parses as
                            # whatever it looks like -- none of which this gate
                            # needs to be fatal about.
                            logger.debug(
                                f"Watchdog: Could not date {project_name}/#{issue_number} "
                                f"({age_anchor!r}) -- age gate skipped: {e}"
                            )

                    # PROTECTION 1: Check for active execution (ANY type of work)
                    #
                    # Uses the already-loaded `state` rather than has_active_execution()
                    # (#150): this loop holds `state_file`'s flock for the whole body,
                    # and has_active_execution() re-reads the file through load_state(),
                    # which opens the SAME lock path on a fresh fd. flock locks are per
                    # open-file-description, so that second acquire blocked forever --
                    # in the same thread, with no timeout. Every line below this one,
                    # #144's board scoping included, was therefore unreachable, and the
                    # wedged thread never released this issue's lock either, so all
                    # later record_execution_start()/record_execution_outcome() calls
                    # for it blocked too. persist_probe_cleanup=False for the same
                    # reason: save_state() takes this lock as well.
                    if self.has_active_execution_for_state(
                        state, project_name, issue_number, persist_probe_cleanup=False
                    ):
                        logger.debug(
                            f"Watchdog: Skipping {project_name}/#{issue_number}: work already in progress"
                        )
                        continue

                    # PROTECTION 2: Check pipeline lock
                    #
                    # Found in #57 review: this previously did
                    # project_config.get('pipelines', {}).get('enabled', [])
                    # on a ProjectConfig dataclass (which has no .get() at
                    # all -- `pipelines` is a plain `List[ProjectPipeline]`
                    # attribute) and called lock_manager.get_lock_status(...),
                    # a method that doesn't exist on PipelineLockManager --
                    # both raised AttributeError on every single invocation,
                    # silently swallowed by the except below exactly like
                    # PROTECTION 3's own dead get_pipeline_queue() import
                    # (fixed above in this same commit), making this
                    # protection a permanent no-op too. Separately, the inner
                    # `continue` only continued the `for pipeline_config`
                    # loop, not the outer per-state-file loop -- even with a
                    # real API call, it would not actually have skipped this
                    # execution. Fixed to use the real
                    # ProjectPipeline.board_name attribute and
                    # PipelineLockManager.get_lock_holder(), and to use the
                    # same locked-flag + break + outer-continue shape
                    # PROTECTION 3 already gets right.
                    #
                    # Fetches project_config once, shared with PROTECTION 3
                    # below (found in #58 review: each protection previously
                    # called config_manager.get_project_config(project_name)
                    # separately for the same project in the same loop
                    # iteration -- get_project_config() re-reads and
                    # re-parses the project's YAML from disk on every call,
                    # no caching, so this was a redundant disk read + parse
                    # every single state-file iteration).
                    from services.pipeline_lock_manager import get_pipeline_lock_manager
                    from config.manager import config_manager

                    if project_name in project_config_cache:
                        project_config = project_config_cache[project_name]
                    else:
                        # Only cache a SUCCESSFUL lookup, not a failure (found
                        # in final whole-PR review): caching None on the
                        # first exception would silently degrade PROTECTION
                        # 2/3 to no-ops for every remaining state file of
                        # this project in the same sweep, with no retry --
                        # a transient error on file #1 shouldn't poison
                        # files #2..N when the underlying config read might
                        # well succeed on a later attempt.
                        try:
                            project_config = config_manager.get_project_config(project_name)
                            project_config_cache[project_name] = project_config
                        except Exception as e:
                            project_config = None
                            # Warning, not debug (#140 item 31): a failure here
                            # silently degrades BOTH PROTECTION 2 and PROTECTION 3
                            # to no-ops for this state file, which is exactly the
                            # kind of quiet degradation that hid two real bugs in
                            # this code.
                            logger.warning(
                                f"Watchdog: Could not load project config for {project_name} "
                                f"-- PROTECTION 2/3 degraded for this state file: {e}"
                            )

                    try:
                        lock_manager = get_pipeline_lock_manager()

                        # Scope the lock check to the board this stuck execution
                        # actually ran on (#144). A lock held on some OTHER board of
                        # the same project says nothing about whether THIS execution
                        # is safe to retry: issue #10 stuck on a completely idle
                        # sdlc_execution board was being skipped every sweep because
                        # issue #20 was legitimately working on planning_design.
                        #
                        # Two cases fall back to the original every-board behavior,
                        # and both are deliberate:
                        #   - no board recorded at all (every record written before
                        #     record_execution_start() started carrying board_name,
                        #     plus the crash-recovery record record_execution_outcome()
                        #     synthesises when it finds no matching in_progress
                        #     entry, plus the dispatch paths with no board in scope);
                        #   - a board recorded that is no longer one of this
                        #     project's configured boards (a board rename, or the
                        #     'system' pseudo-board some task contexts carry).
                        # The second case MUST NOT be trusted as-is: get_lock_holder
                        # on an unknown board name is not an error, both stores
                        # simply have no entry and it returns None, which would turn
                        # this protection into a guaranteed no-op -- strictly weaker
                        # than the pre-#144 behavior it replaced, rather than more
                        # conservative than it.
                        #
                        # The fallback is genuinely conservative, not free: a board
                        # lock is held for the life of a pipeline run (up to
                        # LOCK_TTL_SECONDS, 2h), so a record without a usable board
                        # reproduces #144 -- skipped every sweep while ANY other
                        # board of the project stays busy -- for as long as that lock
                        # lives, not just until the next sweep. That is accepted
                        # because it only defers: no retry budget is consumed, the
                        # record stays 'success' and is re-examined on every sweep,
                        # and the issue is picked up as soon as the other board frees
                        # up. Everything already on disk before this change lands is
                        # in exactly that state.
                        configured_boards = [
                            board for board in (
                                getattr(pipeline_config, 'board_name', None)
                                for pipeline_config in getattr(project_config, 'pipelines', None) or []
                            ) if board
                        ]
                        recorded_board = last_exec.get('board_name')
                        if recorded_board and recorded_board in configured_boards:
                            boards_to_check = [recorded_board]
                        else:
                            if recorded_board:
                                logger.warning(
                                    f"Watchdog: {project_name}/#{issue_number} recorded board "
                                    f"'{recorded_board}', which is not one of this project's "
                                    f"configured boards ({configured_boards}) -- falling back to "
                                    f"checking every board rather than trusting a name no lock "
                                    f"is ever keyed on"
                                )
                            boards_to_check = configured_boards

                        locked_by_another_issue = False
                        for board_name in boards_to_check:
                            # Fail-closed read (#150): get_lock_holder() goes through
                            # get_lock(), which drops the health flag both stores
                            # return, and those stores swallow their own exceptions --
                            # so Redis down + an unreadable YAML lock file surfaced
                            # here as "no holder", i.e. exactly the same answer as an
                            # idle board, and this protection cheerfully marked the
                            # execution for retry onto a board another issue was
                            # actively holding.
                            holder_issue, reads_healthy = lock_manager.get_lock_holder_fail_closed(
                                project_name, board_name
                            )
                            if not reads_healthy:
                                logger.warning(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: lock state "
                                    f"for board '{board_name}' could not be read from either store "
                                    f"-- assuming locked rather than deciding on unverified data"
                                )
                                locked_by_another_issue = True
                                break
                            # CRITICAL fix (found in #58 review): this must only
                            # skip when the lock is held by a DIFFERENT issue.
                            # The original version fired for ANY holder,
                            # including this exact issue holding its own
                            # lock -- which is the common case right after an
                            # issue finishes a stage (locks release only at
                            # specific exit columns, not after every stage),
                            # so this protection was skipping almost every
                            # retry check, not just the ones actually racing
                            # a different issue's in-progress work.
                            if holder_issue and holder_issue != issue_number:
                                logger.debug(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                    f"board '{board_name}' locked by issue #{holder_issue}"
                                )
                                locked_by_another_issue = True
                                break

                        if locked_by_another_issue:
                            continue
                    except (AttributeError, TypeError) as e:
                        # A coding bug, not a transient outage -- and precisely the
                        # shape (.get() on a dataclass, a method that doesn't exist)
                        # that kept this protection a silent permanent no-op until
                        # #57/#58 (#140 item 31). Surfaced distinctly from the
                        # transient case below, and loudly, so the next one can't
                        # hide the same way.
                        #
                        # Both handlers fall THROUGH to PROTECTION 3 rather than
                        # skipping this issue -- the opposite posture to
                        # services/pipeline_watchdog.py, which bails out on a check
                        # it can't verify, and deliberately so. That watchdog ends
                        # the run and releases its board lock, so acting on a bad
                        # answer there produces a genuinely concurrent second
                        # container; this sweep only rewrites a state record, and the
                        # redispatch it invites still goes through project_monitor,
                        # which takes the board's pipeline lock and consults the
                        # queue itself. Failing closed here would instead let one
                        # permanent coding bug silently freeze the un-sticking
                        # watchdog for every issue, which is the failure mode #57/#58
                        # already cost us twice. The narrower "lock state is
                        # unreadable" case above IS failed closed, because there the
                        # check itself worked and told us it doesn't know.
                        logger.error(
                            f"Watchdog: PROTECTION 2 (pipeline lock) failed for "
                            f"{project_name}/#{issue_number} with a programming error "
                            f"-- this protection is not working, continuing without it: {e}",
                            exc_info=True
                        )
                    except Exception as e:
                        logger.warning(
                            f"Watchdog: Could not check pipeline lock for "
                            f"{project_name}/#{issue_number} -- PROTECTION 2 skipped: {e}"
                        )

                    # PROTECTION 3: Check queue state
                    #
                    # Issue #57: this used to import a nonexistent
                    # get_pipeline_queue() (only get_pipeline_queue_manager
                    # (project, board) / PipelineQueueManager actually exist in
                    # services/pipeline_queue_manager.py), so this protection
                    # was a silent no-op -- the ImportError was swallowed by
                    # the broad except below and only ever logged at debug
                    # level. Implemented properly now that the queue manager
                    # exposes get_issue_status(): skip retry-marking if the
                    # issue is already 'waiting' or 'active' in the queue for
                    # any of its pipelines' boards -- it's already about to be
                    # (or currently being) legitimately processed, so marking
                    # it 'failure' here to force a retry would race with that.
                    try:
                        from services.pipeline_queue_manager import get_pipeline_queue_manager

                        already_queued_or_active = False
                        # Reuses project_config fetched once above PROTECTION 2 --
                        # see the comment there.
                        #
                        # Deliberately still checks EVERY board, unlike PROTECTION 2
                        # above: this asks "is this issue already queued anywhere",
                        # and an issue very often sits in a different board's queue
                        # from the one its last execution ran on (that's what a
                        # board-to-board handoff looks like). Narrowing this one to
                        # the recorded board would make the watchdog mark an issue
                        # for retry while it is legitimately queued elsewhere.
                        for pipeline_cfg in getattr(project_config, 'pipelines', None) or []:
                            board_name = getattr(pipeline_cfg, 'board_name', None)
                            if not board_name:
                                continue
                            queue_manager = queue_manager_cache.get((project_name, board_name))
                            if queue_manager is None:
                                queue_manager = get_pipeline_queue_manager(project_name, board_name)
                                queue_manager_cache[(project_name, board_name)] = queue_manager
                            queue_status = queue_manager.get_issue_status(issue_number)
                            if queue_status in ('waiting', 'active'):
                                logger.debug(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                    f"already '{queue_status}' in pipeline queue for board '{board_name}'"
                                )
                                already_queued_or_active = True
                                break

                        if already_queued_or_active:
                            continue
                    except (AttributeError, TypeError, ImportError) as e:
                        # See PROTECTION 2's matching handler (#140 item 31) --
                        # including why both of these log and fall through rather
                        # than skipping the issue. ImportError is in the list here
                        # because that is literally how this protection was dead
                        # before #57 -- an import of a function that never existed,
                        # logged at debug and never noticed.
                        logger.error(
                            f"Watchdog: PROTECTION 3 (queue status) failed for "
                            f"{project_name}/#{issue_number} with a programming error "
                            f"-- this protection is not working, continuing without it: {e}",
                            exc_info=True
                        )
                    except Exception as e:
                        logger.warning(
                            f"Watchdog: Could not check queue status for "
                            f"{project_name}/#{issue_number} -- PROTECTION 3 skipped: {e}"
                        )

                    # PROTECTION 4: Check execution eligibility
                    agent = last_exec.get('agent')
                    column = last_exec.get('column')

                    if not agent or not column:
                        logger.warning(f"Watchdog: Missing agent or column for {project_name}/#{issue_number}")
                        continue

                    # project_config is the sweep's per-project cached copy (may be
                    # None if the lookup above failed, in which case the callee
                    # fetches it itself) -- see the cache comment above PROTECTION 2.
                    should_retry, reason = self._should_retry_failed_execution(
                        project_name, issue_number, agent, column, last_exec,
                        project_config=project_config
                    )

                    if not should_retry:
                        logger.debug(
                            f"Watchdog: Not eligible for retry {project_name}/#{issue_number}: {reason}"
                        )
                        continue

                    # PROTECTION 5: Verify no recent execution started
                    # Check if execution completed within last 5 minutes
                    # (could be starting but not yet marked as in_progress)
                    #
                    # Anchored on completed_at OR the record's start timestamp
                    # (#150): this gated on completed_at alone, which nothing wrote
                    # until record_execution_outcome() started stamping it above, so
                    # the whole block was skipped for every record on disk and this
                    # window was never enforced for any issue. Newly load-bearing,
                    # too -- on main the sweep wedged at PROTECTION 1 and never
                    # reached here.
                    recency_anchor = _execution_anchor_time(last_exec)
                    if recency_anchor:
                        try:
                            anchor_dt = _parse_iso_timestamp(recency_anchor)
                            if datetime.now(timezone.utc) - anchor_dt < timedelta(minutes=5):
                                logger.debug(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                    f"execution too recent ({anchor_dt})"
                                )
                                continue
                        except Exception as e:
                            logger.debug(f"Could not parse execution timestamp: {e}")

                    # Check if GitHub output exists (fails closed - see the method's
                    # docstring: True also means "could not verify", which defers
                    # rather than redispatching)
                    if self._has_github_output(project_name, issue_number, last_exec):
                        logger.debug(
                            f"Watchdog: {project_name}/#{issue_number} has GitHub output "
                            f"(or it could not be verified) - leaving the record alone"
                        )
                        continue

                    # ALL PROTECTIONS PASSED - Safe to mark for retry
                    logger.warning(
                        f"Watchdog: Detected successful execution with no output for "
                        f"{project_name}/#{issue_number} - marking as failure to trigger retry"
                    )

                    # Mark as failure to trigger retry
                    last_exec['outcome'] = 'failure'
                    last_exec['error'] = 'Execution marked as success but produced no visible GitHub output'
                    last_exec['watchdog_retry_triggered'] = True
                    last_exec['watchdog_retry_count'] = last_exec.get('watchdog_retry_count', 0) + 1
                    last_exec['watchdog_last_retry_at'] = datetime.now().isoformat() + 'Z'

                    # Write updated state
                    with open(state_file, 'w') as f:
                        yaml.dump(state, f, default_flow_style=False, sort_keys=False)

                    retried_count += 1

                    # Emit observability event
                    try:
                        from monitoring.observability import get_observability_manager, EventType
                        obs = get_observability_manager()
                        obs.emit(
                            EventType.RETRY_ATTEMPTED,
                            agent='watchdog',
                            project=project_name,
                            data={
                                'issue_number': issue_number,
                                'reason': 'empty_output_on_success',
                                'retry_count': last_exec['watchdog_retry_count']
                            }
                        )
                    except Exception as e:
                        logger.debug(f"Could not emit observability event: {e}")

            except Exception as e:
                logger.error(f"Watchdog: Error processing {state_file}: {e}", exc_info=True)

        if retried_count > 0:
            logger.info(f"Watchdog: Marked {retried_count} executions for retry (empty output)")

        return retried_count

    def _has_github_output(self, project_name: str, issue_number: int, execution: dict) -> bool:
        """
        Check if execution resulted in GitHub output (comment/discussion post).

        This is the LAST gate before an execution is rewritten to 'failure' and
        redispatched, so every "can't verify" path deliberately fails CLOSED
        (reports output, i.e. leaves the record alone) rather than open (#150). The
        two directions are not symmetric: a wrong "no output" answer redispatches a
        real agent container onto an issue that already has its comment, while a
        wrong "has output" answer only defers -- the record stays 'success', no
        retry budget is consumed, and the next sweep re-examines it. That is the
        same posture PROTECTION 2's fail-closed lock read takes.

        Args:
            project_name: Project name
            issue_number: Issue number
            execution: Execution record dict

        Returns:
            True if GitHub output exists (or could not be verified), False if the
            execution demonstrably produced none
        """
        from urllib.parse import quote

        try:
            from services.github_api_client import get_github_client
            from config.manager import config_manager

            gh = get_github_client()
            # get_project_config() raises rather than returning None for an unknown
            # project, so there is no falsy-config case to test for here -- and a
            # ProjectConfig dataclass instance is always truthy anyway.
            project_config = config_manager.get_project_config(project_name)

            agent = execution.get('agent')
            # completed_at when the record has one, else the start timestamp
            # (#150): gating on completed_at alone made this return True for every
            # record in production, because nothing wrote the field -- so the sweep
            # bailed at this gate on every issue, on every pass, forever, which is
            # the same permanent no-op #150 set out to remove, just one gate later.
            # See _execution_anchor_time() for why the start time is a safe
            # substitute here.
            completed_at = _execution_anchor_time(execution)

            if not agent or not completed_at:
                # "After what?" has no answer without a timestamp of any kind, so
                # there is no comparison to make -- unverifiable, not verified-empty.
                logger.warning(
                    f"Watchdog: Missing agent or execution timestamp for "
                    f"{project_name}/#{issue_number} -- cannot verify GitHub output, "
                    f"leaving the record alone"
                )
                return True

            # Parse completion timestamp
            completed_dt = _parse_iso_timestamp(completed_at)

            # Check for comments after completion time.
            #
            # Attribute access, not subscription (#150): ProjectConfig is a plain
            # dataclass with no __getitem__, so project_config['github'] raised
            # TypeError on EVERY call, was swallowed by the broad handler below and
            # returned False -- making this gate unconditionally "no output" for
            # every project. Same defect class as the .get()-on-a-dataclass bugs
            # #57/#58 fixed in PROTECTION 2/3; _should_retry_failed_execution()
            # above has always used the correct form.
            org = project_config.github['org']
            repo = project_config.github['repo']

            # Ask GitHub only for the window that matters (#150). rest() shells out
            # to `gh api` with no --paginate and no per_page, and the endpoint
            # defaults to per_page=30 sorted created/asc -- so the bare path returns
            # the OLDEST 30 comments. On a managed-repo issue with 178 comments
            # (context-studio has several) every one of them predates the execution,
            # the loop below finds nothing after completed_dt, and this returns a
            # confident False: the one wrong answer that redispatches a real agent
            # container onto an issue whose comment is already posted. `since` is
            # server-side and inclusive, and truncating it to whole seconds only
            # widens the window, so the created_at > completed_dt loop still filters
            # exactly. One page of 100 is far more than a post-completion window
            # ever holds, and it costs the same single call --paginate would have
            # turned into six.
            since_param = completed_dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            endpoint = (
                f'repos/{org}/{repo}/issues/{issue_number}/comments'
                f'?since={quote(since_param)}&per_page=100'
            )

            success, comments = gh.rest('GET', endpoint)

            if not success:
                logger.warning(
                    f"Watchdog: Failed to fetch comments for {project_name}/#{issue_number} "
                    f"-- cannot verify GitHub output, leaving the record alone"
                )
                return True  # Can't verify - defer rather than redispatch blind

            # Check if any comment was created after completion
            for comment in comments:
                try:
                    created_at = _parse_iso_timestamp(comment['created_at'])
                    if created_at > completed_dt:
                        # Found a comment after execution - assume it's the output
                        logger.debug(
                            f"Found GitHub comment after execution completion for "
                            f"{project_name}/#{issue_number}"
                        )
                        return True
                except Exception as e:
                    logger.debug(f"Error parsing comment timestamp: {e}")
                    continue

            logger.debug(f"No GitHub output found for {project_name}/#{issue_number} after {completed_dt}")
            return False

        except (AttributeError, TypeError, KeyError) as e:
            # A coding bug, not a transient outage -- and the exact shape (dataclass
            # vs dict mixup) that made this gate a permanent "no output" until #150.
            # Surfaced at ERROR with a traceback and distinctly from the transient
            # case below, the same way PROTECTION 2/3's handlers were narrowed, so
            # the next one can't hide as another quiet return value.
            logger.error(
                f"Watchdog: GitHub output check failed for {project_name}/#{issue_number} "
                f"with a programming error -- this gate is not working, leaving the "
                f"record alone: {e}",
                exc_info=True
            )
            return True
        except Exception as e:
            logger.warning(
                f"Watchdog: Error checking GitHub output for {project_name}/#{issue_number} "
                f"-- cannot verify, leaving the record alone: {e}"
            )
            return True  # Can't verify - defer rather than redispatch blind

    def _try_recover_result_from_redis(self, project_name, issue_number, agent, column, execution):
        """
        Check Redis for a persisted agent result before marking execution as failed.

        The docker-claude-wrapper.py writes final results to Redis
        (agent_result:{project}:{issue_number}:{task_id}) with a 2-hour TTL
        before the container exits. When the orchestrator restarts and the
        container is gone (due to --rm), the result may still be in Redis.

        Two recovery strategies:
        1. Primary: Exact key lookup when execution has a stamped task_id (O(1), no ambiguity)
        2. Fallback: Wildcard scan with timestamp validation for old records without task_id

        Returns True if a result was recovered (execution dict is updated in place),
        False otherwise.
        """
        try:
            import redis
            import json

            redis_client = redis.Redis(
                host='redis', port=6379, decode_responses=True,
                socket_timeout=5, socket_connect_timeout=5
            )

            # Primary: exact key lookup when task_id is stamped on the execution
            execution_task_id = execution.get('task_id')
            if execution_task_id:
                exact_key = f"agent_result:{project_name}:{issue_number}:{execution_task_id}"
                result_json = redis_client.get(exact_key)
                if result_json:
                    try:
                        result_data = json.loads(result_json)
                    except (json.JSONDecodeError, TypeError) as e:
                        logger.warning(f"Failed to parse Redis result at {exact_key}: {e}")
                        return False

                    if result_data.get('agent') != agent:
                        logger.warning(
                            f"Redis result at {exact_key} has agent={result_data.get('agent')}, "
                            f"expected {agent} — skipping recovery"
                        )
                        return False

                    recovered = self._apply_redis_result(
                        execution, result_data, exact_key, project_name,
                        issue_number, agent, column, redis_client
                    )
                    return recovered

                # No result for this specific execution — it never produced output
                logger.debug(
                    f"No Redis result for exact key {exact_key} — "
                    f"execution never completed or result already expired"
                )
                return False

            # Fallback: wildcard scan for old execution records without task_id
            # Add timestamp validation to prevent cross-execution contamination
            pattern = f"agent_result:{project_name}:{issue_number}:*"
            result_keys = list(redis_client.scan_iter(match=pattern, count=100))

            if not result_keys:
                return False

            for redis_key in result_keys:
                try:
                    result_json = redis_client.get(redis_key)
                    if not result_json:
                        continue

                    result_data = json.loads(result_json)

                    # Strict agent match
                    if result_data.get('agent') != agent:
                        continue

                    # Timestamp validation: reject results from earlier executions
                    execution_timestamp = execution.get('timestamp')
                    completed_at = result_data.get('completed_at')
                    if execution_timestamp and completed_at:
                        try:
                            exec_dt = datetime.fromisoformat(
                                execution_timestamp.replace('Z', '+00:00')
                            )
                            completed_dt = datetime.fromisoformat(
                                completed_at.replace('Z', '+00:00')
                            )
                            if completed_dt < exec_dt:
                                logger.info(
                                    f"Skipping stale Redis result at {redis_key}: "
                                    f"completed_at={completed_at} < execution_timestamp={execution_timestamp}"
                                )
                                continue
                        except (ValueError, TypeError) as e:
                            logger.warning(
                                f"Unparseable timestamps for {redis_key}: "
                                f"execution_timestamp={execution_timestamp!r}, "
                                f"completed_at={completed_at!r}: {e}. "
                                f"Skipping result to prevent cross-execution contamination."
                            )
                            continue

                    recovered = self._apply_redis_result(
                        execution, result_data, redis_key, project_name,
                        issue_number, agent, column, redis_client
                    )
                    if recovered:
                        return True

                except (json.JSONDecodeError, TypeError) as e:
                    logger.warning(f"Failed to parse Redis result at {redis_key}: {e}")
                    continue

            return False

        except ImportError:
            logger.debug("Redis not available for result recovery")
            return False
        except Exception as e:
            # Distinguish expected connection failures from unexpected bugs
            if isinstance(e, OSError):
                logger.warning(f"Redis unavailable for result recovery: {e}")
            else:
                logger.error(
                    f"Unexpected error during Redis result recovery for "
                    f"{project_name}/#{issue_number} {agent}: {e}",
                    exc_info=True
                )
            return False

    def _apply_redis_result(self, execution, result_data, redis_key, project_name,
                            issue_number, agent, column, redis_client):
        """
        Apply a recovered Redis result to an execution record.

        Shared by both exact-key and wildcard-scan recovery paths.
        Returns True if result was applied, False if it was skipped.
        """
        exit_code = result_data.get('exit_code')
        if exit_code is None:
            logger.warning(
                f"Redis result at {redis_key} has no exit_code field, "
                f"cannot determine outcome — skipping recovery"
            )
            return False

        # The Redis blob carries the container's own completion time
        # (docker_runner._persist_agent_result). Copy it onto the record so this
        # recovery path leaves the same completed_at anchor the normal
        # record_execution_outcome() path writes (#150) -- without it a recovered
        # record is one the watchdog can never verify. Falls back to now for a
        # blob written before the wrapper started stamping the field.
        execution['completed_at'] = (
            result_data.get('completed_at') or datetime.now(timezone.utc).isoformat()
        )

        if exit_code == 0:
            execution['outcome'] = 'success'
            logger.info(
                f"Recovered successful result from Redis for "
                f"{project_name}/#{issue_number} {agent} in {column} "
                f"(key: {redis_key})"
            )
        else:
            execution['outcome'] = 'failure'
            output = result_data.get('output', '') or ''
            # Truncate output for error message
            error_snippet = output[-500:] if len(output) > 500 else output
            execution['error'] = (
                f"Agent exited with code {exit_code}. "
                f"Result recovered from Redis after container exited. "
                f"Output tail: {error_snippet}"
            )
            logger.warning(
                f"Recovered failed result from Redis for "
                f"{project_name}/#{issue_number} {agent} in {column} "
                f"(exit_code={exit_code}, key: {redis_key})"
            )

        # Delete the key after processing to prevent reprocessing.
        try:
            redis_client.delete(redis_key)
        except Exception as del_err:
            logger.warning(
                f"Failed to delete recovered Redis key {redis_key}: {del_err}. "
                f"Key will expire via TTL or be reprocessed next cycle."
            )

        return True

    def cleanup_stuck_in_progress_states(self):
        """
        Clean up execution states that are stuck as 'in_progress'.

        This handles cases where:
        - Orchestrator was restarted while agents were running
        - Agents completed but record_execution_outcome() was never called
        - Execution states remain permanently stuck as 'in_progress'

        Strategy:
        - Find all state files with in_progress executions
        - Check if corresponding agent container still exists
        - If container is gone, mark execution as 'failure' with reason 'orchestrator_restart'
        """
        import subprocess

        if not self.state_dir.exists():
            logger.info("No execution state directory found, skipping cleanup")
            return

        state_files = list(self.state_dir.glob("*.yaml"))

        if not state_files:
            logger.info("No execution state files found, skipping cleanup")
            return

        logger.info(f"Checking {len(state_files)} execution state files for stuck in_progress states")

        cleaned_count = 0
        for state_file in state_files:
            try:
                from utils.file_lock import file_lock

                # Use file lock for entire read-modify-write cycle
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(lock_file):
                    # Load state
                    if not state_file.exists():  # Check inside lock
                        continue
                    with open(state_file, 'r') as f:
                        state = yaml.safe_load(f)

                    # Third of this module's three state-file readers -- same
                    # corrupted-state-file condition, same report. The membership
                    # test below happens to survive a string or a list, but not a
                    # scalar (`'x' not in 42` raises TypeError), so this site was
                    # unguarded too.
                    if state is not None and not isinstance(state, dict):
                        self._log_corrupted_state_file(
                            state_file,
                            f"YAML parsed as {type(state).__name__}, expected a mapping"
                        )
                        continue

                    if not state or 'execution_history' not in state:
                        continue

                    project_name = state.get('project_name')
                    issue_number = state.get('issue_number')

                    # Find in_progress executions
                    modified = False
                    for execution in state['execution_history']:
                        if execution.get('outcome') == 'in_progress':
                            agent = execution.get('agent')
                            column = execution.get('column')
                            timestamp = execution.get('timestamp')

                            logger.info(
                                f"Found stuck in_progress execution: {project_name}/#{issue_number} "
                                f"{agent} in {column} from {timestamp}"
                            )

                            # Coordination guard: prevent double-processing with other mechanisms
                            try:
                                from services.cleanup_guard import try_claim_cleanup
                                if not try_claim_cleanup(project_name, issue_number, "stuck_state_cleanup"):
                                    continue
                            except Exception as e:
                                logger.warning(f"Cleanup guard unavailable, proceeding without coordination: {e}")

                            # Check if agent/repair cycle container still exists using two methods:
                            # 1. Docker ps (checks if container actually exists)
                            # 2. Redis tracking keys (checks orchestrator's view of active agents)

                            # Check if this issue is in the pipeline queue
                            # If it is, it's waiting to start, not stuck
                            try:
                                import redis
                                import json
                                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                                
                                # Scan for all board queues for this project
                                queue_keys = redis_client.keys(f"orchestrator:pipeline_queue:{project_name}:*")
                                is_in_queue = False
                                
                                for key in queue_keys:
                                    # Get all items in the queue
                                    items = redis_client.lrange(key, 0, -1)
                                    for item_json in items:
                                        try:
                                            item = json.loads(item_json)
                                            if str(item.get('issue_number')) == str(issue_number):
                                                is_in_queue = True
                                                break
                                        except:
                                            pass
                                    if is_in_queue:
                                        break
                                
                                if is_in_queue:
                                    logger.info(
                                        f"Issue {project_name}/#{issue_number} is in pipeline queue, "
                                        f"skipping stuck state cleanup"
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"Failed to check pipeline queue: {e}")

                            # Method 1: Check if Docker container is running.
                            # Use label filters scoped to this project + issue so agent
                            # containers, repair cycle containers, and PR review containers
                            # are all found with one query and without false positives from
                            # other issues or other projects.
                            ps_result = subprocess.run(
                                [
                                    'docker', 'ps',
                                    '--filter', f'label=org.switchyard.project={project_name}',
                                    '--filter', f'label=org.switchyard.issue_number={issue_number}',
                                    '--format', '{{.Names}}',
                                ],
                                capture_output=True,
                                text=True,
                                timeout=5,
                            )
                            container_names = (
                                [n for n in ps_result.stdout.strip().split('\n') if n]
                                if ps_result.returncode == 0 else []
                            )

                            has_docker_container = bool(container_names)

                            # Method 2: Check Redis tracking keys for agents
                            has_redis_tracking = self._check_redis_tracking_for_agent(project_name, agent, issue_number)

                            # Also check for repair cycle Redis tracking
                            has_repair_cycle_tracking = self._check_redis_repair_cycle_tracking(project_name, issue_number)

                            has_redis_tracking = has_redis_tracking or has_repair_cycle_tracking

                            # IMPORTANT: Docker is the source of truth for running containers.
                            # Redis tracking keys can persist after containers die (orphaned keys).
                            # Only trust Docker to determine if a container is actually running.
                            has_running_container = has_docker_container

                            if has_docker_container and not has_redis_tracking:
                                all_container_names = container_names
                                logger.warning(
                                    f"Container exists in Docker but not in Redis tracking for "
                                    f"{project_name}/#{issue_number} {agent} - attempting repair"
                                )
                                # Enrich execution dict with state-level metadata
                                # (issue_number and project are stored at state level, not in execution record)
                                execution_with_metadata = {
                                    **execution,
                                    'issue_number': issue_number,
                                    'project': project_name
                                }
                                self._repair_missing_redis_tracking(
                                    execution=execution_with_metadata,
                                    project=project_name,
                                    container_names=all_container_names
                                )
                            elif has_redis_tracking and not has_docker_container:
                                logger.warning(
                                    f"Redis tracking exists but container not found in Docker for "
                                    f"{project_name}/#{issue_number} {agent} (orphaned tracking key)"
                                )
                                # Clean up orphaned Redis tracking keys
                                try:
                                    import redis
                                    redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)

                                    # Clean up agent tracking key if exists
                                    agent_key = f"agent:container:claude-agent-{project_name}-*"
                                    # For repair cycle tracking
                                    repair_cycle_key = f"repair_cycle:container:{project_name}:{issue_number}"
                                    deleted = redis_client.delete(repair_cycle_key)
                                    if deleted:
                                        logger.info(f"Cleaned up orphaned repair cycle Redis key: {repair_cycle_key}")
                                except Exception as e:
                                    logger.warning(f"Failed to clean up orphaned Redis keys: {e}")

                            # If this issue holds the pipeline lock on any board,
                            # work is in progress or pending (review cycle iterating,
                            # repair cycle, status progression, etc.). Do not touch it.
                            try:
                                from services.pipeline_lock_manager import get_pipeline_lock_manager
                                from config.manager import config_manager as _cfg_mgr
                                _lm = get_pipeline_lock_manager()
                                _pcfg = _cfg_mgr.get_project_config(project_name)
                                _holds_lock = any(
                                    (_lk := _lm.get_lock(project_name, p.board_name))
                                    and _lk.lock_status == 'locked'
                                    and _lk.locked_by_issue == issue_number
                                    for p in _pcfg.pipelines
                                )
                                if _holds_lock:
                                    logger.info(
                                        f"Issue {project_name}/#{issue_number} holds pipeline lock "
                                        f"- skipping stuck state cleanup"
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"Failed to check pipeline lock for {project_name}/#{issue_number}: {e}")
                                continue  # Fail-safe: don't clean up a run we can't verify

                            # Check if this issue has an active review cycle (any non-completed
                            # status). Between iterations the container exits normally but the
                            # cycle is still orchestrating the next iteration.
                            try:
                                from services.review_cycle import review_cycle_executor
                                ck = review_cycle_executor._cycle_key(project_name, issue_number)
                                rc = review_cycle_executor.active_cycles.get(ck)
                                if rc and rc.status != 'completed':
                                    logger.info(
                                        f"Issue {project_name}/#{issue_number} has active review cycle "
                                        f"(status: {rc.status}) - skipping stuck state cleanup"
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"Failed to check review cycle state for {project_name}/#{issue_number}: {e}")
                                continue  # Fail-safe: don't clean up a run we can't verify

                            # Check if this issue has an active human feedback loop.
                            try:
                                from services.human_feedback_loop import human_feedback_loop_executor
                                lk = human_feedback_loop_executor._loop_key(project_name, issue_number)
                                if lk in human_feedback_loop_executor.active_loops:
                                    logger.info(
                                        f"Issue {project_name}/#{issue_number} has active feedback "
                                        f"loop - skipping stuck state cleanup"
                                    )
                                    continue
                            except Exception as e:
                                logger.warning(f"Failed to check feedback loop state for {project_name}/#{issue_number}: {e}")
                                continue  # Fail-safe: don't clean up a run we can't verify

                            if not has_running_container:
                                # Before marking as failure, check if the container completed
                                # and persisted its result to Redis (written by docker-claude-wrapper.py)
                                recovered = self._try_recover_result_from_redis(
                                    project_name, issue_number, agent, column, execution
                                )

                                if not recovered:
                                    # No container AND no Redis result — truly lost execution
                                    execution['outcome'] = 'failure'
                                    execution['error'] = (
                                        'Agent execution interrupted. Container no longer exists and execution '
                                        'state was not updated. This may indicate the agent crashed, was killed, '
                                        'or the orchestrator was restarted before outcome could be recorded.'
                                    )

                                    # Special handling for dev_environment_verifier agent
                                    # Only reset to UNVERIFIED if the current state is NOT already VERIFIED.
                                    # A later verifier execution may have already succeeded, in which case
                                    # this stuck record is from a superseded execution and should not clobber
                                    # the verified state.
                                    if agent == 'dev_environment_verifier':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            current_status = dev_container_state.get_status(project_name)
                                            if current_status == DevContainerStatus.VERIFIED:
                                                logger.info(
                                                    f"Stuck dev_environment_verifier detected for {project_name}, "
                                                    f"but dev container is already VERIFIED — skipping reset"
                                                )
                                            else:
                                                logger.info(
                                                    f"Stuck dev_environment_verifier detected for {project_name}, "
                                                    f"resetting dev container state to UNVERIFIED"
                                                )
                                                dev_container_state.set_status(
                                                    project_name=project_name,
                                                    status=DevContainerStatus.UNVERIFIED,
                                                    error_message="Verification container died before completion"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    # Special handling for dev_environment_setup agent
                                    # Only reset if not already VERIFIED (a verifier may have already confirmed).
                                    if agent == 'dev_environment_setup':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            current_status = dev_container_state.get_status(project_name)
                                            if current_status == DevContainerStatus.VERIFIED:
                                                logger.info(
                                                    f"Stuck dev_environment_setup detected for {project_name}, "
                                                    f"but dev container is already VERIFIED — skipping reset"
                                                )
                                            else:
                                                logger.info(
                                                    f"Stuck dev_environment_setup detected for {project_name}, "
                                                    f"resetting dev container state to UNVERIFIED"
                                                )
                                                dev_container_state.set_status(
                                                    project_name=project_name,
                                                    status=DevContainerStatus.UNVERIFIED,
                                                    error_message="Setup container died before completion"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                modified = True
                                cleaned_count += 1

                                # Emit events based on recovered outcome for UX visibility
                                if execution['outcome'] == 'success':
                                    # Agent completed successfully but result was only recovered
                                    # from Redis after restart. The normal result-processing chain
                                    # (posting output to GitHub, triggering progression) was skipped.
                                    # Monitoring loop will re-detect card position and continue pipeline.
                                    logger.info(
                                        f"Reconciled successful execution from Redis: "
                                        f"{project_name}/#{issue_number} {agent} in {column}. "
                                        f"Monitoring loop will re-detect card position and continue pipeline."
                                    )

                                    # Verify dev container state consistency for dev_environment_verifier
                                    # The agent should have already set state to VERIFIED before exiting
                                    if agent == 'dev_environment_verifier':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            current_status = dev_container_state.get_status(project_name)
                                            if current_status != DevContainerStatus.VERIFIED:
                                                logger.warning(
                                                    f"Dev environment verifier succeeded for {project_name} but "
                                                    f"state is {current_status.value}, expected VERIFIED. "
                                                    f"Correcting state now."
                                                )
                                                dev_container_state.set_status(
                                                    project_name=project_name,
                                                    status=DevContainerStatus.VERIFIED,
                                                    image_name=f"{project_name}-agent:latest"
                                                )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to verify dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    try:
                                        from monitoring.decision_events import DecisionEventEmitter
                                        from monitoring.observability import get_observability_manager
                                        from services.pipeline_run import get_pipeline_run_manager

                                        obs = get_observability_manager()
                                        decision_events = DecisionEventEmitter(obs)
                                        pipeline_run_mgr = get_pipeline_run_manager()
                                        active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

                                        decision_events.emit_execution_state_reconciled(
                                            agent=agent,
                                            project=project_name,
                                            issue_number=issue_number,
                                            column=column,
                                            recovered_outcome='success',
                                            context={
                                                'timestamp': timestamp,
                                                'recovered_from_redis': True,
                                            },
                                            pipeline_run_id=active_run.id if active_run else None
                                        )
                                    except Exception as e:
                                        logger.error(
                                            f"Failed to emit reconciliation event for "
                                            f"{project_name}/#{issue_number} ({agent}): {e}",
                                            exc_info=True
                                        )
                                else:
                                    # Failure path — applies to both recovered-from-Redis failures
                                    # and truly lost executions where no result was found
                                    logger.warning(
                                        f"Marked stuck execution as failed: {project_name}/#{issue_number} "
                                        f"{agent} in {column} "
                                        f"({'recovered from Redis' if recovered else 'no container found, outcome not recorded'}). "
                                        f"Pipeline is now blocked - manual intervention required."
                                    )

                                    # Special handling for dev_environment_verifier agent failures
                                    # BUG FIX: Synchronize dev container state when verification fails
                                    # This handles both: (1) recovered-from-Redis failures, (2) non-recovered failures
                                    # Note: Non-recovered failures already handled above at line ~1460, but
                                    # this ensures recovered failures also update the state
                                    if agent == 'dev_environment_verifier' and recovered:
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            logger.info(
                                                f"Dev environment verification failed for {project_name} "
                                                f"(recovered from Redis with failure), marking as BLOCKED"
                                            )
                                            # Extract error from execution for context
                                            error_msg = execution.get('error', 'Verification failed')[:200]
                                            dev_container_state.set_status(
                                                project_name=project_name,
                                                status=DevContainerStatus.BLOCKED,
                                                error_message=error_msg
                                            )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    # Special handling for dev_environment_setup agent failures
                                    # Reset to UNVERIFIED so setup can be retried automatically
                                    if agent == 'dev_environment_setup':
                                        try:
                                            from services.dev_container_state import dev_container_state, DevContainerStatus
                                            logger.info(
                                                f"Dev environment setup failed for {project_name}, "
                                                f"resetting dev container state to UNVERIFIED"
                                            )
                                            error_msg = execution.get('error', 'Setup failed')[:200]
                                            dev_container_state.set_status(
                                                project_name=project_name,
                                                status=DevContainerStatus.UNVERIFIED,
                                                error_message=error_msg
                                            )
                                        except Exception as e:
                                            logger.error(
                                                f"Failed to update dev container state for {project_name}: {e}",
                                                exc_info=True
                                            )

                                    # CRITICAL CHANGE: DO NOT call end_pipeline_run()
                                    #
                                    # Previous behavior (REMOVED): Called end_pipeline_run() which:
                                    # - Released the pipeline lock
                                    # - Processed the next waiting issue in queue
                                    # - This caused issues to be skipped on failure (violated FIFO ordering)
                                    #
                                    # New behavior: DO NOT end the pipeline run when agent fails:
                                    # - Pipeline run remains active (keeps the lock)
                                    # - Failed issue blocks the queue (enforces FIFO ordering)
                                    # - Requires manual intervention to unblock:
                                    #   * Move issue to Backlog (releases lock, allows retry)
                                    #   * Move to non-trigger column (releases lock, skips issue)
                                    #   * Close issue (releases lock, abandons work)
                                    #
                                    # Emit comprehensive failure events for UX visibility
                                    try:
                                        from monitoring.decision_events import DecisionEventEmitter
                                        from monitoring.observability import get_observability_manager, EventType
                                        from services.pipeline_run import get_pipeline_run_manager

                                        obs = get_observability_manager()
                                        decision_events = DecisionEventEmitter(obs)
                                        pipeline_run_mgr = get_pipeline_run_manager()
                                        active_run = pipeline_run_mgr.get_active_pipeline_run(project_name, issue_number)

                                        # Emit error decision event with blocking context
                                        decision_events.emit_error_decision(
                                            error_type='ExecutionContainerLost',
                                            error_message=execution['error'],
                                            context={
                                                'project': project_name,
                                                'issue_number': issue_number,
                                                'agent': agent,
                                                'column': column,
                                                'timestamp': timestamp,
                                                'recovered_from_redis': recovered,
                                                'blocking_pipeline': True,
                                                'lock_held': True,
                                            },
                                            recovery_action='manual_intervention_required',
                                            success=False,
                                            project=project_name,
                                            pipeline_run_id=active_run.id if active_run else None
                                        )

                                        # Emit specific pipeline blocked event
                                        if active_run:
                                            obs.emit(
                                                EventType.PIPELINE_RUN_FAILED,
                                                "pipeline_lifecycle",
                                                active_run.id,
                                                project_name,
                                                {
                                                    "pipeline_run_id": active_run.id,
                                                    "issue_number": issue_number,
                                                    "board": active_run.board,
                                                    "reason": "agent_execution_failed",
                                                    "error": execution['error'],
                                                    "blocking_pipeline": True,
                                                    "requires_manual_intervention": True,
                                                    "recovered_from_redis": recovered,
                                                },
                                                pipeline_run_id=active_run.id
                                            )

                                        logger.info(
                                            f"Emitted failure events for {project_name}/#{issue_number}. "
                                            f"UX should display: 'Pipeline blocked - manual intervention required'"
                                        )

                                    except Exception as e:
                                        logger.error(f"Failed to emit execution failure events: {e}", exc_info=True)
                                        # Continue anyway - the execution state is still marked as failed
                            else:
                                logger.info(
                                    f"Agent container still running for {project_name}/#{issue_number}, "
                                    f"keeping in_progress state"
                                )

                    # Save if modified (still inside the lock)
                    if modified:
                        state['last_updated'] = datetime.now(timezone.utc).isoformat()
                        with open(state_file, 'w') as f:
                            yaml.dump(state, f, default_flow_style=False, sort_keys=False)

                        logger.info(f"Updated state file: {state_file}")

            except Exception as e:
                logger.error(f"Error processing state file {state_file}: {e}")

        if cleaned_count > 0:
            logger.info(f"Cleaned up {cleaned_count} stuck in_progress execution states")
        else:
            logger.info("No stuck in_progress execution states found")

    def abandon_stale_in_progress_entries(
        self,
        project_name: str,
        issue_number: int,
        active_task_ids: set,
        reason: str = 'Orchestrator restarted without completing this execution.'
    ) -> int:
        """
        Mark orphaned in_progress entries as 'abandoned'.

        Originally written for the orchestrator-restart recovery case (see
        agent_container_recovery.py, this method's original and still most
        common caller) — hence the default `reason` text. Also called by
        pipeline_watchdog.py's zombie/frozen-run self-heal
        (_redispatch_same_issue), where no restart occurred; that caller
        passes its own `reason` so this durable, human-readable forensic
        record (persisted execution history an operator reads when
        debugging a repeatedly-zombie'd issue) doesn't claim a restart that
        didn't happen.

        An entry is considered stale (and safe to abandon) when:
        - It has no task_id (was never assigned to a container — a pure probe entry), OR
        - Its task_id is not in active_task_ids (the container is no longer running)

        Entries whose task_id IS in active_task_ids are left untouched because
        their container was successfully recovered.

        Args:
            project_name: Project name
            issue_number: Issue number
            active_task_ids: Set of task_ids belonging to currently running/recovered containers
            reason: Human-readable explanation stored on each abandoned entry's
                `error` field. Defaults to the restart-recovery wording for
                backward compatibility with the original caller.

        Returns:
            Number of entries marked as abandoned
        """
        state = self.load_state(project_name, issue_number)
        abandoned = 0

        for execution in state['execution_history']:
            if execution.get('outcome') != 'in_progress':
                continue

            task_id = execution.get('task_id')
            if task_id and task_id in active_task_ids:
                # Container is still running — leave this entry alone
                continue

            execution['outcome'] = 'abandoned'
            execution['error'] = reason
            abandoned += 1

        if abandoned:
            self.save_state(project_name, issue_number, state)
            logger.info(
                f"Abandoned {abandoned} stale in_progress entries for "
                f"{project_name}/#{issue_number}"
            )

        return abandoned


# Global instance
work_execution_tracker = WorkExecutionStateTracker()

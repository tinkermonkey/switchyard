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
import re
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

# The empty-output watchdog's own retry budget, and why it is held strictly BELOW
# project_monitor's MAX_CONSECUTIVE_DISPATCH_FAILURES (#166 review).
#
# Each watchdog rewrite turns THIS record's trailing 'success' into a trailing
# 'failure' for the same (column, agent) -- it rewrites in place rather than
# appending -- and a trailing 'failure' for a (column, agent) is exactly what
# count_consecutive_failures() accumulates. So N watchdog rewrites in a row produce
# N consecutive dispatch failures with nothing between them. Both budgets defaulted
# to 3, which meant the last redispatch the watchdog was allowed to invite was also
# the one that tripped project_monitor's mark_failed() -- NOT a plain release: the
# board's pipeline lock stays held, durably marked retained-due-to-failure, so every
# sibling issue on that board is blocked until a human runs scripts/release_lock.py.
# One issue the gate was systematically wrong about therefore took the whole board
# offline in about three sweeps (~45 minutes). Strictly below means the budget stops
# the loop first and the blast radius stays the one record.
#
# Overridable with WATCHDOG_MAX_RETRIES, but the override is clamped to the same
# ceiling -- see _watchdog_max_retries(); a bigger number is not a configuration
# choice, it is the escalation path reopening.
_WATCHDOG_MAX_RETRIES = 2


def _dispatch_failure_budget() -> int:
    """project_monitor's MAX_CONSECUTIVE_DISPATCH_FAILURES.

    Read rather than restated so the two cannot drift apart again; the import is
    local because project_monitor imports this module back (lazily, from inside its
    own functions) and pulls in most of the orchestrator with it. An unreadable
    constant falls back to one above the watchdog's own default budget, i.e. to the
    relationship the pair is supposed to hold.
    """
    try:
        from services.project_monitor import MAX_CONSECUTIVE_DISPATCH_FAILURES
        return MAX_CONSECUTIVE_DISPATCH_FAILURES
    except Exception as e:
        logger.debug(
            f"Could not read MAX_CONSECUTIVE_DISPATCH_FAILURES ({e}) -- "
            f"assuming {_WATCHDOG_MAX_RETRIES + 1}"
        )
        return _WATCHDOG_MAX_RETRIES + 1


def _watchdog_max_retries() -> int:
    """WATCHDOG_MAX_RETRIES, clamped strictly below the dispatch-failure budget.

    Only the ceiling is enforced. A value BELOW the default is an operator turning
    the retry down, including all the way off: WATCHDOG_MAX_RETRIES=0 leaves a fresh
    record's watchdog_retry_count of 0 already at the limit, so Check 1 refuses
    every rewrite and the watchdog is a pure detector again. That is the kill switch
    an operator reaches for when the gate starts rewriting records it shouldn't, and
    clamping it up to 1 -- which this did (#166 review) -- silently re-enabled one
    rewrite and one redispatch per eligible record. Zero satisfies the relationship
    with MAX_CONSECUTIVE_DISPATCH_FAILURES a fortiori; it is only the other
    direction that is not a configuration choice.
    """
    import os

    raw = os.environ.get('WATCHDOG_MAX_RETRIES')
    try:
        configured = int(raw) if raw is not None else _WATCHDOG_MAX_RETRIES
    except (TypeError, ValueError):
        logger.warning(
            f"WATCHDOG_MAX_RETRIES={raw!r} is not an integer -- "
            f"using {_WATCHDOG_MAX_RETRIES}"
        )
        configured = _WATCHDOG_MAX_RETRIES

    return max(0, min(configured, _dispatch_failure_budget() - 1))


# How much of a workspace the empty-output gate reads before it gives up and
# declines the record (#166). `gh api` is called with no --paginate, so a page is
# all this gate ever sees: the issue endpoint is bounded with per_page + since,
# and the Discussion query with last:. A page that could have been clipped inside
# the execution's window is reported as unverifiable, never as "no output".
_WATCHDOG_COMMENT_PAGE_SIZE = 100
_WATCHDOG_DISCUSSION_REPLY_PAGE_SIZE = 50

# Every agent comment this orchestrator posts ends with this marker -- see
# AgentCommentFormatter.format_agent_completion() in services/github_integration.py,
# which both real completion paths (docker_runner._complete_agent_execution and
# agent_executor._post_agent_output_to_github) format their output with.
# project_monitor, review_cycle, pr_review_stage and human_feedback_loop already
# attribute comments to agents with exactly this string; the watchdog reuses it
# rather than inventing a second spelling of the same convention.
_ANY_AGENT_OUTPUT_SIGNATURE_RE = re.compile(r'_Processed by the \S[^\n]*? agent_')

# What one scan of one workspace concluded.
_OUTPUT_EVIDENCE_NONE = 'none'                    # nothing agentic in the window
_OUTPUT_EVIDENCE_OTHER_AGENT = 'other_agent_output'  # agent output, but not this agent's
_OUTPUT_EVIDENCE_AGENT = 'agent_output'           # this agent's own signed output
_OUTPUT_EVIDENCE_UNVERIFIABLE = 'unverifiable'    # the scan could not answer

# Increasing order of "leave the record alone": _NONE is the only one that lets
# the sweep rewrite anything.
_OUTPUT_EVIDENCE_RANK = {
    _OUTPUT_EVIDENCE_NONE: 0,
    _OUTPUT_EVIDENCE_OTHER_AGENT: 1,
    _OUTPUT_EVIDENCE_AGENT: 2,
    _OUTPUT_EVIDENCE_UNVERIFIABLE: 3,
}


# The dispatch paths whose output the empty-output gate can actually attribute --
# i.e. the ones that finish through AgentExecutor._post_agent_output_to_github or
# docker_runner._complete_agent_execution and therefore post a comment signed
# "_Processed by the {agent} agent_". An allowlist rather than a denylist on
# purpose: a dispatch path added later defaults to "cannot verify", which defers,
# instead of to "verified empty", which redispatches.
#
# Two exclusions are load-bearing and both were measured, not reasoned:
#
#   * The repair cycle ('repair_cycle_test' / '_fix' / '_warning_review'). Its
#     agent calls run inside the repair-cycle container and post nothing
#     individually -- documentation_robotics #909 recorded 23 agent calls and not
#     one signed comment; the only output is the stage's own summary, signed
#     "_Repair cycle executed by Switchyard (containerized)_". Evaluating the
#     activated gate over seven days of live records answered "no output" for 83
#     of them, every one a repair-cycle record whose cycle had demonstrably
#     posted its summary. That is the 29-of-30 failure mode this gate was split
#     out of #150 to avoid, and it is 2,053 of 4,566 last-record successes -- so
#     this exclusion is also the watchdog's largest blind spot, tracked in #188.
#
#     #188 IS NOW ROOT-CAUSED AND FIXED, AND THIS EXCLUSION STILL HAS TO STAY.
#     Read that carefully before deleting the line below. The silence was never
#     structural: GitHubIntegration resolved its repo owner from the GITHUB_ORG
#     environment variable alone, ignoring the owner every caller passes from
#     project config, and services/project_monitor.py does not set that variable
#     on the repair-cycle container it launches. So every GitHub call made from
#     inside a repair cycle addressed `/repos/None/<repo>/...`, 404'd, and logged
#     it to a --rm container's discarded stdout. Both halves are fixed now, and
#     a repair cycle's agent runs post signed comments like any other dispatch.
#
#     But every record ALREADY ON DISK was written while they did not. Adding
#     'repair_cycle_test' to this set today would hand the watchdog 2,053
#     historical successes that genuinely have no comment to find and let it
#     redispatch all of them -- the exact outcome the exclusion exists to
#     prevent, arrived at from the opposite direction. The gate anchors on each
#     record's own start time, so records written after the fix will verify on
#     their own merits; this becomes safe once the corpus is post-fix, and the
#     way to establish that is the dry-run harness (scripts/dry_run_state_sweep.py),
#     not this comment. Until then it stays declined, which costs nothing that
#     was not already being paid.
#   * 'manual'. project_monitor keeps it for its two WRAPPER stages only
#     (pr_review_stage at project_monitor.py:~7992, the repair cycle at ~8623),
#     which record an outcome under a name their sub-run does not post under.
#     The ordinary board dispatch used to share that name, and the exclusion cost
#     6,104 attributable 'success' records -- the largest single population the
#     gate could otherwise verify, 5,914 of them senior_software_engineer -- for
#     no verifiable gain, because the task-queue worker does NOT write a second
#     'task_queue' start behind project_monitor's probe (its
#     record_execution_start is guarded on there being no in_progress entry, see
#     agents/orchestrator_integration.py) and record_execution_outcome() finalizes
#     the probe in place. That dispatch now records 'board_dispatch' instead, so
#     the two are told apart by name rather than declined together (#166).
#     Records already on disk carry 'manual' and stay declined, which is the
#     conservative direction.
_WATCHDOG_ATTRIBUTABLE_TRIGGER_SOURCES = frozenset({
    'board_dispatch',
    'task_queue',
    'pipeline_progression',
    'review_cycle',
    'pr_review_phase2',
    'pr_review_phase4',
    'human_feedback_loop_initial',
    'human_feedback_loop_response',
})

# Agents that own their own GitHub posting and therefore never emit the signed
# comment the gate looks for -- a per-AGENT exclusion, because whether a signed
# comment gets posted is decided by the agent, not by the dispatch path that
# started it (#166).
#
# work_breakdown_agent sets task_context['suppress_github_post'] = True (which
# makes docker_runner skip _complete_agent_execution) and context['output_posted']
# = True (which makes agent_executor skip _post_agent_output_to_github), so both
# AgentCommentFormatter.format_agent_completion call sites are dead for it. Its
# only output is _post_creation_summary()/_post_error_comment(), whose bodies
# carry no "_Processed by the ... agent_" marker at all -- and in question mode
# it posts nothing. Its 50 live 'success' records arrive under allowlisted
# trigger sources ('human_feedback_loop_initial', 'task_queue'), so without this
# the gate reads every one of them as "demonstrably produced no output" and
# redispatches an agent whose redispatch runs in initial mode and creates the
# sub-issues a second time.
#
# test_every_agent_that_owns_its_own_posting_is_declined pins this set against
# the suppress_github_post literals actually in agents/, so an agent added later
# that opts out of the signed post cannot quietly become verifiable.
_WATCHDOG_UNATTRIBUTABLE_AGENTS = frozenset({
    'work_breakdown_agent',
})


def _agent_output_signature(agent: str) -> str:
    """The marker `agent`'s own output comments carry."""
    return f"_Processed by the {agent} agent_"


def _stronger_output_evidence(left: str, right: str) -> str:
    """Combine two workspace scans into the answer that leaves the record alone."""
    return left if _OUTPUT_EVIDENCE_RANK[left] >= _OUTPUT_EVIDENCE_RANK[right] else right


def _unquoted_body(body: str) -> str:
    """`body` with GitHub quote-reply lines removed.

    "Quote reply" copies the quoted comment verbatim behind '> ' prefixes, so a
    human follow-up that quotes an agent's earlier signed comment carries that
    agent's signature -- and a bare substring test reads it as the agent's own
    fresh output, which permanently spares a genuinely empty execution from the
    retry it needs. services/human_feedback_loop.py already skips signature lines
    whose stripped form starts with '>' for exactly this reason (four sites); this
    is the same idiom, applied once so both the this-agent and any-agent checks
    below get it.
    """
    return '\n'.join(
        line for line in body.split('\n') if not line.strip().startswith('>')
    )


def _classify_output_evidence(entries, agent: str, anchor: datetime) -> str:
    """Classify (created_at, body) pairs against one execution's start time.

    entries may be a generator; it is consumed once. A pair this cannot date is
    fatal to the whole scan rather than skipped -- "assume it fell outside the
    window" is the assumption that redispatches an agent whose comment is sitting
    right there.
    """
    signature = _agent_output_signature(agent)
    evidence = _OUTPUT_EVIDENCE_NONE

    for created_at, body in entries:
        try:
            created = _parse_iso_timestamp(created_at)
        except (ValueError, TypeError, AttributeError) as e:
            logger.warning(
                f"Watchdog: Could not date a comment ({created_at!r}) while looking for "
                f"{agent} output -- cannot verify, leaving the record alone: {e}"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        if created < anchor:
            continue

        body = _unquoted_body(body or '')
        if signature in body:
            return _OUTPUT_EVIDENCE_AGENT
        if _ANY_AGENT_OUTPUT_SIGNATURE_RE.search(body):
            evidence = _OUTPUT_EVIDENCE_OTHER_AGENT

    return evidence


def _page_holds_every_node(total_count, nodes) -> bool:
    """Did a `last: N` GraphQL page return its connection in FULL?

    Stricter than _newest_page_covers_window() below, and the only honest test
    wherever the nodes carry children the same query only fetched for the nodes it
    got back (#166 review). A dropped Discussion comment takes its whole reply
    thread with it, and a reply posted today can hang off a comment created months
    ago -- so the comment-level createdAt test says nothing about whether the
    window is covered. An absent or unparseable totalCount answers False, which the
    caller turns into "leaving the record alone".
    """
    try:
        return int(total_count) <= len(nodes)
    except (TypeError, ValueError):
        return False


def _newest_page_covers_window(total_count, nodes, anchor: datetime) -> bool:
    """Does a `last: N` GraphQL page provably hold every node created since `anchor`?

    Only sound for LEAF connections -- see _page_holds_every_node() for why a
    connection whose nodes carry unfetched children needs the stricter test.

    GitHub orders connections oldest-first, so `last` returns the tail: anything
    dropped is older than nodes[0]. The page therefore covers the window when
    nothing was dropped at all, or when its own oldest node already predates the
    anchor. Anything this cannot establish -- an absent totalCount, an undatable
    first node -- is answered False, which the caller turns into "leaving the
    record alone".
    """
    try:
        dropped = int(total_count) > len(nodes)
    except (TypeError, ValueError):
        return False

    if not dropped:
        return True
    if not nodes:
        return False

    try:
        return _parse_iso_timestamp(nodes[0].get('createdAt')) <= anchor
    except (ValueError, TypeError, AttributeError):
        return False



def _transition_dev_container_state(
    project_name: str,
    status,
    *,
    reason: str,
    skip_when: Tuple = (),
    image_name: Optional[str] = None,
    error_message: Optional[str] = None,
) -> bool:
    """
    Reconcile a project's dev container state under the dev_container_build lock.

    cleanup_stuck_in_progress_states() writes dev_container_state at five points
    when a dev_environment_setup/verifier execution is found stuck, and every one
    of them used to be an unlocked read-then-write (#152 item A) -- running right
    after a crash/restart, which is exactly when a build started by the previous
    process may still be in flight and holding this lock. Each site re-read
    get_status() outside the lock and then wrote on the strength of that read, so
    a status the live holder changed in between was silently clobbered.

    Uses the NON-BLOCKING variant of the lock: this runs from main.py's startup
    and from inside the per-project execution state file lock, so it must never
    stall holding that file lock. On a busy lock it does NOT write, and returns
    False so the caller can leave its execution record alone for the next sweep.

    Whether a skip is safe was the subject of two #152 review rounds, and the
    honest answer is that it is only safe when there really is a live holder:

      - Right after a restart, the lock of the process that died is still in
        Redis under its own TTL, and a dead holder never writes a fresher status.
        That case is now removed at the source -- main.py releases the locks left
        by its OWN dead predecessor BEFORE this sweep runs (see
        ProjectResourceLockManager.recover_orphaned_resource_locks, which leaves
        a live cross-process holder's lock alone) -- and, for whatever is left,
        by the caller retrying rather than consuming the record.
      - "Not acquired" is not always contention: try_acquire_lock() also fails
        closed on unknown/degraded lock state. dev_container_build_lock's
        _log_skipped() reports those at ERROR with the real reason instead of
        claiming a holder exists.

    The one recovery that does exist without any of this is narrow and worth
    naming precisely: validate_task_can_run()'s staleness check on
    get_status_updated_at() covers IN_PROGRESS only. CHANGES_NEEDED gets its own
    (see STALE_CHANGES_NEEDED_MINUTES); the terminal statuses get none. See
    services/dev_container_build_lock.py's module docstring ("Bookkeeping
    writers").

    Args:
        project_name: project whose dev container state to reconcile.
        status: DevContainerStatus to write.
        reason: human-readable description of why, for the log line.
        skip_when: statuses that mean this write is already superseded -- checked
            against a re-read taken INSIDE the lock, which is what makes the
            check-then-act atomic rather than merely serialized.
        image_name / error_message: passed through to set_status().

    Returns:
        True when the reconciliation reached a conclusion (wrote, or deliberately
        skipped because the status inside the lock said the write was superseded).
        False when the lock could not be taken, so nothing was decided and the
        caller should arrange for this to be retried.
    """
    try:
        from services.dev_container_build_lock import dev_container_build_lock_if_free_sync
        from services.dev_container_state import dev_container_state

        with dev_container_build_lock_if_free_sync(project_name) as acquired:
            if not acquired:
                # _log_skipped() has already reported the acquire's real reason
                # (WARNING for genuine contention, ERROR for a degraded outcome).
                logger.warning(
                    f"Skipped dev container state reconciliation for {project_name} "
                    f"({reason}): the dev_container_build lock could not be taken"
                )
                return False

            current_status = dev_container_state.get_status(project_name)
            if current_status in skip_when:
                logger.info(
                    f"{reason} for {project_name}, but dev container is already "
                    f"{current_status.value} — skipping reset"
                )
                return True

            logger.info(
                f"{reason} for {project_name}, setting dev container state to {status.value}"
            )
            dev_container_state.set_status(
                project_name=project_name,
                status=status,
                image_name=image_name,
                error_message=error_message,
            )
            return True
    except Exception as e:
        logger.error(
            f"Failed to update dev container state for {project_name}: {e}",
            exc_info=True
        )
        # An exception here is not lock contention -- retrying it every sweep
        # would loop on the same failure. Treated as concluded; the ERROR above
        # is the signal.
        return True


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
    # True only on the record record_execution_outcome() synthesises when it finds
    # no matching in_progress entry (#166): its `timestamp` is stamped at
    # outcome-recording time, so it is a finish time wearing a start time's name.
    # The empty-output gate declines any record carrying this.
    start_time_unknown: bool = False


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

        history = state['execution_history']

        # Carry the empty-output watchdog's retry budget across the redispatch it
        # just invited (#166). detect_and_retry_empty_successful_executions() writes
        # watchdog_retry_count onto the record it rewrites to 'failure' -- and that
        # record is then never looked at again, because the sweep only ever examines
        # a state file whose LAST record is 'success'. Without this the counter on
        # any record is only ever 0 or 1 and _should_retry_failed_execution()'s
        # `>= _watchdog_max_retries()` can never bind, so a systematic false "no
        # output" for one issue loops sweep -> rewrite -> redispatch -> success ->
        # rewrite every 15 minutes until project_monitor's
        # MAX_CONSECUTIVE_DISPATCH_FAILURES fires mark_failed() and durably retains
        # the board lock -- exactly the blast radius the budget exists to bound, and
        # the reason _watchdog_max_retries() is clamped strictly below it.
        #
        # Scoped to the last record for this same (column, agent), and cleared by a
        # 'success': a redispatch that then genuinely posts ends the run, so the next
        # unrelated start begins at zero again rather than inheriting a budget spent
        # months ago.
        #
        # What does NOT clear it is any other outcome, watchdog-written or not (#166
        # review). Conditioning the carry on the previous record's
        # watchdog_retry_triggered -- which only ever lands on the record the sweep
        # itself rewrote -- meant one genuine dispatch failure landing between two
        # rewrites reset the budget to zero while count_consecutive_failures() kept
        # climbing, so the pair the budget is supposed to stay below could still be
        # reached. The run terminator is the same one count_consecutive_failures()
        # uses, for the same reason: these two counters have to agree about where a
        # run of failures begins or the budget bounds nothing.
        #
        # The lookback searches BACKWARD for this (column, agent) rather than testing
        # history[-1] (#166 review). A state file is per (project, issue) and holds
        # records for every column, agent and board that issue has ever touched, so
        # any interleaved record -- the other board of an issue live on two of them,
        # pipeline_progression.record_execution_start(), review_cycle's direct
        # dispatch -- landed between the rewrite and the redispatch and reset the
        # carried budget to zero, which is precisely the condition this block exists
        # to prevent.
        previous = next(
            (
                entry for entry in reversed(history)
                if entry.get('column') == column and entry.get('agent') == agent
            ),
            None
        )
        if previous and previous.get('outcome') != 'success':
            carried = previous.get('watchdog_retry_count', 0)
            if carried:
                execution['watchdog_retry_count'] = carried

        history.append(execution)
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
        claude_session_id: Optional[str] = None,
        github_post_attempted: bool = True
    ):
        """Record the outcome of work execution.

        claude_session_id: piggybacked onto the same write (not a separate
        persistence mechanism) when outcome=='frozen' and the rejected call had
        already established a Claude Code session — see docker_runner.py's
        _rate_limit_signal capture. Used by the active-resume step to decide
        whether a captured session is worth --resume-ing.

        github_post_attempted=False says this caller finalized the record on a path
        that never posts the agent's comment at all (#166 review). It is stamped onto
        the record as outcome_recovered_without_post and the empty-output gate
        declines it, for the same reason it declines _apply_redis_result()'s records:
        "no signed agent comment" is only evidence of "produced nothing" when
        something tried to write one. The default is True because every other caller
        reaches here through a completion path that posts first — see
        docker_runner._complete_agent_execution and
        agent_executor._post_agent_output_to_github.

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

                # Stamped before the outcome, the same way _apply_redis_result()
                # stamps its own, so the two can never be written apart.
                if not github_post_attempted:
                    execution['outcome_recovered_without_post'] = True

                execution['outcome'] = outcome
                if not found_primary:
                    # Most recent in_progress: the real execution entry.
                    #
                    # Still deliberately does NOT stamp completed_at, now for the
                    # opposite reason to #150's. The empty-output gate is live
                    # (#166), but it anchors on `timestamp` -- the START, written by
                    # record_execution_start() before the task is even enqueued --
                    # precisely because both completion paths post the agent's
                    # comment BEFORE reaching this call. A completion anchor
                    # post-dates the comment that proves output, which is what a
                    # dry run measured as 29 wrong answers in 30 real successes.
                    # The one remaining consumer of the field, PROTECTION 5's
                    # 5-minute recency window, therefore stays inert; giving it an
                    # anchor is its own change, not a side effect of this one.
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
        # `timestamp` is therefore a stand-in -- the real start time went with the
        # lost dispatch, and this one is stamped AFTER the agent has already
        # posted -- so `start_time_unknown` says so outright rather than leaving
        # the empty-output gate to infer it (#166). Without the flag the gate would
        # ask "has anything been posted since?" of an instant that is really the
        # finish, and answer "no output" for an execution that posted perfectly
        # well: 8,692 of 54,594 live 'success' records have this shape.
        # _output_anchor_for_record() also declines trigger_source 'unknown', which
        # is what covers every record of this shape already on disk.
        execution = {
            'column': column,
            'agent': agent,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'outcome': outcome,
            'trigger_source': 'unknown',
            'start_time_unknown': True
        }

        # Already declined by start_time_unknown and trigger_source 'unknown', but
        # stamped anyway so the record says why in the caller's own terms rather than
        # relying on two other flags to happen to cover it.
        if not github_post_attempted:
            execution['outcome_recovered_without_post'] = True

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
        # 'commit_in_flight' (#154/WI-9) belongs here for a third reason: startup
        # recovery stopped waiting on a repair cycle's auto-commit thread before it
        # finished, so that pass never learned the outcome. It marks the run failed
        # and retains the board's lock while the thread is still writing that
        # directory (agent_container_recovery's commit_in_flight branch), so this
        # predicate only starts to matter once an operator has released the lock —
        # at which point the commit has long since resolved and the repair cycle is
        # re-runnable and idempotent (a commit that did land leaves nothing to
        # commit).
        if last_execution['outcome'] in ['failure', 'frozen', 'lock_contention',
                                         'cancelled', 'abandoned', 'commit_in_flight']:
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
        return self._count_consecutive_failures_in(
            state['execution_history'], column, agent
        )

    @staticmethod
    def _count_consecutive_failures_in(
        executions: List[dict],
        column: str,
        agent: str
    ) -> int:
        """count_consecutive_failures() over an already-loaded history.

        Split out for the empty-output sweep (#166 review), which has to ask this
        question with the state file's flock in hand: load_state() takes that same
        lock on a fresh fd, and flock locks are per open-file-description, so the
        public method would block forever there -- the same re-entrancy that wedged
        PROTECTION 1 until #150. It also lets the sweep ask about history MINUS the
        record it is about to rewrite; see _rewrite_verified_empty_execution().
        """
        column_executions = [
            e for e in executions
            if e.get('column') == column and e.get('agent') == agent
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
                # Same TTL invariant as the original registration (#160 review):
                # this hash is what gives POST /agents/kill/<c> a project and an
                # issue number, so it MUST outlast the longest agent a container
                # can be running. A hardcoded 7200 was the defect this whole
                # repair path then had to clean up after, restated -- the key
                # expired again two hours later, under the same live container.
                from claude.docker_runner import ACTIVE_CONTAINER_TRACKING_TTL_SECONDS
                redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
                redis_client.hset(f'agent:container:{container_name}', mapping=container_info)
                redis_client.expire(
                    f'agent:container:{container_name}',
                    ACTIVE_CONTAINER_TRACKING_TTL_SECONDS,
                )

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
        - Issue still in same active column
        - Column still requires agent
        - Pipeline run still active
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
        # Check 1: Retry limit -- see _watchdog_max_retries() for why the
        # configured value is capped below MAX_CONSECUTIVE_DISPATCH_FAILURES.
        max_retries = _watchdog_max_retries()
        retry_count = execution.get('watchdog_retry_count', 0)

        if retry_count >= max_retries:
            return False, f"max_retries_exceeded (count={retry_count}, max={max_retries})"

        # Check 2 & 3 & 5: Issue state and column (combined GitHub query)
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

        # Check 3: Pipeline run active (moved before workflow check to get board name)
        #
        # restore_to_redis=False because this is NOT the plain Redis hash lookup it
        # looks like: on a mapping miss -- the normal case once end_pipeline_run()
        # has deleted the mapping -- get_active_pipeline_run() falls through to an
        # Elasticsearch search and, on a hit, writes the run back with a fresh TTL
        # under the board-less legacy issue key. A periodic sweep must not do that:
        # a crashed run whose ES doc still reads 'active' would be resurrected on
        # every pass, and the legacy key it lands under can shadow a later
        # board-scoped lookup. The watchdog only needs to know whether a run is
        # active, not to repair Redis.
        try:
            from services.pipeline_run import get_pipeline_run_manager

            pipeline_run_mgr = get_pipeline_run_manager()
            active_run = pipeline_run_mgr.get_active_pipeline_run(
                project_name, issue_number, restore_to_redis=False
            )

            if not active_run:
                return False, "no_active_pipeline_run"

        except Exception as e:
            logger.error(f"Error checking pipeline run: {e}")
            return False, f"error_checking_pipeline_run: {str(e)}"

        # Check 4: Column requires agent
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

    def _watchdog_board_lock_blocks_retry(
        self, project_name: str, issue_number: int, project_config, execution: dict
    ) -> bool:
        """PROTECTION 2: is this execution's board locked by a DIFFERENT issue?

        Returns True when the record must be left alone.

        Extracted from the sweep so both of its passes run the identical check
        (#166 review). The collection pass answers it, and
        _rewrite_verified_empty_execution() answers it AGAIN immediately before
        the rewrite -- the GitHub verification between the two holds no lock and
        is serial over every candidate, so a phase-1 answer can be many minutes
        stale by the time it is acted on.

        Found in #57 review: this previously did
        project_config.get('pipelines', {}).get('enabled', []) on a ProjectConfig
        dataclass (which has no .get() at all -- `pipelines` is a plain
        `List[ProjectPipeline]` attribute) and called lock_manager.get_lock_status(...),
        a method that doesn't exist on PipelineLockManager -- both raised
        AttributeError on every single invocation, silently swallowed by the except
        below exactly like PROTECTION 3's own dead get_pipeline_queue() import,
        making this protection a permanent no-op too. Separately, the inner
        `continue` only continued the `for pipeline_config` loop, not the outer
        per-state-file loop -- even with a real API call, it would not actually have
        skipped this execution. Fixed to use the real ProjectPipeline.board_name
        attribute and PipelineLockManager.get_lock_holder(), and to use the same
        locked-flag + break + early-return shape PROTECTION 3 already gets right.
        """
        from services.pipeline_lock_manager import get_pipeline_lock_manager

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
            recorded_board = execution.get('board_name')
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
                return True
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

        return False

    def _watchdog_queue_blocks_retry(
        self, project_name: str, issue_number: int, project_config,
        queue_manager_cache: dict
    ) -> bool:
        """PROTECTION 3: is this issue already waiting or active in a pipeline queue?

        Returns True when the record must be left alone.

        Extracted alongside _watchdog_board_lock_blocks_retry() and re-run for the
        same reason (#166 review) -- and this one PROTECTION 1 cannot stand in for:
        an issue sitting 'waiting' in a PipelineQueueManager queue has no execution
        record at all, because record_execution_start() runs at dispatch time, after
        the enqueue. A rewrite decided minutes earlier would land on a record whose
        dispatch is already queued.
        """
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
            # Uses the project_config the caller fetched once -- see the sweep's
            # call site for why it is not re-read here.
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
                return True
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

        return False

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
        5. 5-minute recency check
        4. Execution eligibility via _should_retry_failed_execution
        6. GitHub-output verification via _has_github_output

        4 and 6 are listed last because they run last: they are the two protections
        that talk to GitHub, and they run in a second pass with no state-file lock
        held. See the comment at the collection site.

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

        # Records that survived PROTECTIONS 0-5 and still need the GitHub-output
        # gate. Collected under each state file's lock and verified after the loop
        # with no lock held -- see the collection site for why.
        candidates = []

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
                    # Anchored on the record's start timestamp, which is the only
                    # time any production record carries -- nothing writes
                    # completed_at. A start anchor makes a record look OLDER than a
                    # completion anchor would, i.e. more likely to be skipped before
                    # any I/O is spent on it, which is the conservative direction
                    # for a gate that exists to bound cost.
                    #
                    # A record with no parseable timestamp is NOT skipped: this gate
                    # exists to bound cost, and silently dropping records it cannot
                    # date would be the same class of quiet no-op #150 is undoing.
                    age_anchor = last_exec.get('timestamp')
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

                    # PROTECTION 2 (pipeline lock) and PROTECTION 3 (queue status)
                    # both live in helpers now, because the rewrite pass below re-runs
                    # them -- see _watchdog_board_lock_blocks_retry().
                    #
                    # project_config is fetched once here and shared with both of them
                    # and with PROTECTION 4 (found in #58 review: each protection
                    # previously called config_manager.get_project_config(project_name)
                    # separately for the same project in the same loop iteration --
                    # get_project_config() re-reads and re-parses the project's YAML
                    # from disk on every call, no caching, so this was a redundant disk
                    # read + parse every single state-file iteration).
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

                    if self._watchdog_board_lock_blocks_retry(
                        project_name, issue_number, project_config, last_exec
                    ):
                        continue

                    if self._watchdog_queue_blocks_retry(
                        project_name, issue_number, project_config, queue_manager_cache
                    ):
                        continue

                    # PROTECTION 4 is NOT run here either -- it joins the gate in the
                    # unlocked pass below. Its Checks 2/3/5 are one `gh api graphql`
                    # query for the issue's state and column, and Check 3's
                    # get_active_pipeline_run() falls through to an Elasticsearch
                    # search on a Redis mapping miss, which is the normal case; both
                    # are exactly the blocking remote work the paragraph below refuses
                    # to hold this issue's flock across (#166 review). All this pass
                    # keeps is the local sanity check that there is an agent and a
                    # column to ask about at all.
                    agent = last_exec.get('agent')
                    column = last_exec.get('column')

                    if not agent or not column:
                        logger.warning(f"Watchdog: Missing agent or column for {project_name}/#{issue_number}")
                        continue

                    # PROTECTION 5: Verify no recent execution started
                    # Check if execution completed within last 5 minutes
                    # (could be starting but not yet marked as in_progress)
                    #
                    # Still gated on completed_at, which no production code path
                    # writes, so this stays a no-op -- and #166 deliberately left
                    # it one. The gate below came alive on the START timestamp
                    # instead, because the agent's comment is posted before the
                    # outcome is recorded; stamping a completion here to wake this
                    # window is a separate decision with its own blast radius, not
                    # a free side effect of activating the gate.
                    if last_exec.get('completed_at'):
                        try:
                            completed_at = _parse_iso_timestamp(last_exec['completed_at'])
                            if datetime.now(timezone.utc) - completed_at < timedelta(minutes=5):
                                logger.debug(
                                    f"Watchdog: Skipping {project_name}/#{issue_number}: "
                                    f"execution too recent ({completed_at})"
                                )
                                continue
                        except Exception as e:
                            logger.debug(f"Could not parse completed_at timestamp: {e}")

                    # PROTECTION 4 and PROTECTION 6, the two remote protections, are
                    # deliberately NOT run here -- they are the second pass below,
                    # with no lock held.
                    #
                    # Everything above this point is local file/lock/queue work, and
                    # that is the invariant this loop is built on rather than a
                    # description of where the code happens to sit. Both remote
                    # protections make blocking `gh` subprocess calls -- PROTECTION 4
                    # a GraphQL query for the issue's state and column, the gate a
                    # REST call for the issue's comments and, in a discussion
                    # workspace, a GraphQL call as well -- each of which sleeps up to
                    # 30s for rate-limit throttling, uses a 30s subprocess timeout and
                    # retries a transient failure three times on a 2/4/8s ladder, so a
                    # single record can spend minutes inside them. This loop holds the
                    # issue's flock for its whole body, taken with file_lock()'s
                    # default enforce_timeout=False, i.e. blocking with no timeout;
                    # every record_execution_start()/record_execution_outcome() for the
                    # same issue goes through that lock, several of them from async
                    # callers on the event loop (review_cycle, human_feedback_loop,
                    # pr_review_stage). Verifying under the lock therefore parks the
                    # monitoring thread -- and the event loop -- behind a GitHub call,
                    # and surfaces as an unexplained polling stall rather than as an
                    # error. It cost nothing before #166 only because the gate returned
                    # before its first network call on every production record;
                    # PROTECTION 4's query was made under the lock on every single one
                    # of them (#166 review).
                    #
                    # project_config/agent/column ride along so the second pass can run
                    # PROTECTION 4 -- and the rewrite re-run it, plus 2/3 -- without
                    # re-reading the project's YAML or re-deriving them from a record
                    # it re-reads anyway.
                    candidates.append({
                        'state_file': state_file,
                        'project_name': project_name,
                        'issue_number': issue_number,
                        'execution': last_exec,
                        'project_config': project_config,
                        'agent': agent,
                        'column': column,
                    })

            except Exception as e:
                logger.error(f"Watchdog: Error processing {state_file}: {e}", exc_info=True)

        # PROTECTIONS 4 and 6: ask GitHub whether each surviving candidate is still
        # eligible and whether it actually produced output, with no lock held, then
        # re-take the lock to rewrite -- see _rewrite_verified_empty_execution() for
        # what the re-read has to re-establish.
        for candidate in candidates:
            state_file = candidate['state_file']
            project_name = candidate['project_name']
            issue_number = candidate['issue_number']

            try:
                # PROTECTION 4, ahead of the gate because it is the cheaper of the
                # two: its Check 1 (the retry budget) answers with no network at all,
                # and a record it refuses never pays for the comment scan. Given the
                # sweep's per-project cached config (may be None if that lookup
                # failed, in which case the callee fetches it itself) -- see the cache
                # comment above PROTECTION 2.
                should_retry, reason = self._should_retry_failed_execution(
                    project_name, issue_number, candidate['agent'], candidate['column'],
                    candidate['execution'], project_config=candidate['project_config']
                )
                if not should_retry:
                    logger.debug(
                        f"Watchdog: Not eligible for retry {project_name}/#{issue_number}: {reason}"
                    )
                    continue

                # Fails closed - see the method's docstring: True also means "could
                # not verify", which defers rather than redispatching.
                if self._has_github_output(
                    project_name, issue_number, candidate['execution']
                ):
                    logger.debug(
                        f"Watchdog: {project_name}/#{issue_number} has GitHub output "
                        f"(or it could not be verified) - leaving the record alone"
                    )
                    continue

                if self._rewrite_verified_empty_execution(
                    state_file, candidate, queue_manager_cache
                ):
                    retried_count += 1

            except Exception as e:
                logger.error(f"Watchdog: Error processing {state_file}: {e}", exc_info=True)

        if retried_count > 0:
            logger.info(f"Watchdog: Marked {retried_count} executions for retry (empty output)")

        return retried_count

    def _rewrite_verified_empty_execution(
        self, state_file, candidate: dict, queue_manager_cache: dict = None
    ) -> bool:
        """Rewrite one verified-empty 'success' record to 'failure', under the lock.

        The second half of the sweep's two-phase shape. The GitHub verification that
        produced this decision ran with NO lock held (see the collection site), so
        the record it was about may have moved on: an outcome recorded, a fresh
        dispatch appended, another sweep's rewrite. The re-read here closes that
        window -- it re-establishes that the last record is still the same 'success'
        entry (timestamp + agent + column identify it). Anything else means the
        verification answered a question about a state that no longer exists, and the
        record is left for the next sweep rather than rewritten on a stale answer.

        EVERY protection is re-run here, not just PROTECTION 1 (#166 review). The
        first version re-ran PROTECTION 1 alone, on the theory that a state worth
        skipping would show up as an in_progress entry -- and none of the other three
        does. PROTECTION 3's whole subject is an issue sitting 'waiting' in a queue,
        which has no execution record at all until dispatch time; PROTECTION 4 refuses
        on a closed issue, a card a human moved, and a pipeline run that ended, none
        of which touch this file either. The window is not small: phase 2 is serial
        over every candidate and each one can spend minutes inside `gh` (30s
        rate-limit sleeps, a 30s subprocess timeout, a 2/4/8s retry ladder), so the
        last candidate's rewrite can land 15-20 minutes after its eligibility check.
        Acting on a stale answer there writes a spurious 'failure', and
        count_consecutive_failures() accumulates those straight toward
        project_monitor's MAX_CONSECUTIVE_DISPATCH_FAILURES -- whose terminal state is
        mark_failed() with the board's lock durably retained, i.e. exactly the blast
        radius #166 set out to bound.

        PROTECTION 4 runs BEFORE the lock is taken, because it makes its own GraphQL
        call and holding this issue's flock across a GitHub call is what the two-phase
        split exists to avoid. PROTECTIONS 1/2/3 are local/Redis reads and run inside
        it, as tight to the write as they can be.

        The last check before the write is not a protection at all but the budget
        itself, read off the counter that actually drives the escalation (#166
        review). _should_retry_failed_execution()'s Check 1 counts only the watchdog's
        OWN rewrites, while count_consecutive_failures() counts every trailing
        'failure' for the (column, agent) whoever wrote it -- so a genuine dispatch
        failure already sitting in the trailing run is added to the watchdog's, and
        the private budget bounds a different number from the one project_monitor
        escalates on. Asked here, with the history in hand and the record still
        unwritten, the question is exact: the count over everything BEFORE this record
        plus the one this rewrite is about to add.

        Returns True only when the record was actually rewritten.
        """
        from utils.file_lock import file_lock

        project_name = candidate['project_name']
        issue_number = candidate['issue_number']
        verified = candidate['execution']
        agent = candidate.get('agent') or verified.get('agent')
        column = candidate.get('column') or verified.get('column')
        project_config = candidate.get('project_config')
        if queue_manager_cache is None:
            queue_manager_cache = {}

        # PROTECTION 4 again, unlocked -- see the docstring. Its Check 1 (the retry
        # budget) short-circuits before it touches GitHub, so a record that has
        # already spent its budget costs nothing here.
        should_retry, reason = self._should_retry_failed_execution(
            project_name, issue_number, agent, column, verified,
            project_config=project_config
        )
        if not should_retry:
            logger.debug(
                f"Watchdog: {project_name}/#{issue_number} stopped being eligible while "
                f"its GitHub output was being verified: {reason}"
            )
            return False

        lock_file = state_file.with_suffix(state_file.suffix + '.lock')
        with file_lock(lock_file):
            if not state_file.exists():  # Check inside lock
                return False
            with open(state_file, 'r') as f:
                state = yaml.safe_load(f)

            if not isinstance(state, dict) or not state.get('execution_history'):
                return False

            last_exec = state['execution_history'][-1]

            if (last_exec.get('outcome') != 'success'
                    or last_exec.get('timestamp') != verified.get('timestamp')
                    or last_exec.get('agent') != verified.get('agent')
                    or last_exec.get('column') != verified.get('column')):
                logger.debug(
                    f"Watchdog: {project_name}/#{issue_number} changed while its GitHub "
                    f"output was being verified -- leaving it for the next sweep"
                )
                return False

            # PROTECTION 1 again, and for the same reason it exists: a dispatch may
            # have started in the window this sweep spent talking to GitHub.
            if self.has_active_execution_for_state(
                state, project_name, issue_number, persist_probe_cleanup=False
            ):
                logger.debug(
                    f"Watchdog: Skipping {project_name}/#{issue_number}: work started "
                    f"while its GitHub output was being verified"
                )
                return False

            # PROTECTIONS 2 and 3 again, for the same reason -- see the docstring.
            # Both read local/Redis state, so unlike PROTECTION 4 above they are cheap
            # enough to answer with the lock in hand.
            if self._watchdog_board_lock_blocks_retry(
                project_name, issue_number, project_config, last_exec
            ):
                return False

            if self._watchdog_queue_blocks_retry(
                project_name, issue_number, project_config, queue_manager_cache
            ):
                return False

            # The dispatch-failure budget, read off project_monitor's own counter --
            # see the docstring. history[:-1] is everything before the record being
            # rewritten (the trailing 'success' this method exists to turn over, which
            # would otherwise end the run at zero every time), and +1 is the failure
            # about to be appended to that run. Refusing at the budget rather than one
            # short of it is deliberate: >= is the same comparison project_monitor
            # makes, so this declines exactly the rewrite that would trip mark_failed()
            # -- NOT a plain release, the board's pipeline lock stays held and durably
            # marked retained-due-to-failure until a human runs scripts/release_lock.py.
            consecutive_failures = self._count_consecutive_failures_in(
                state['execution_history'][:-1], column, agent
            )
            dispatch_budget = _dispatch_failure_budget()
            if consecutive_failures + 1 >= dispatch_budget:
                logger.warning(
                    f"Watchdog: {project_name}/#{issue_number} produced no output for "
                    f"'{agent}' in '{column}', but rewriting it would be dispatch "
                    f"failure {consecutive_failures + 1} of {dispatch_budget} for that "
                    f"pair -- declining, so the retry loop stops here instead of "
                    f"retaining the board's pipeline lock"
                )
                return False

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

            retry_count = last_exec['watchdog_retry_count']

        # Emit observability event -- outside the lock, because an Elasticsearch
        # write is not something to hold this issue's flock for.
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
                    'retry_count': retry_count
                }
            )
        except Exception as e:
            logger.debug(f"Could not emit observability event: {e}")

        return True

    def _has_github_output(self, project_name: str, issue_number: int, execution: dict) -> bool:
        """
        Check if this execution produced GitHub output -- an agent comment on the
        issue, or on the issue's Discussion.

        This is the LAST gate before an execution is rewritten to 'failure' and
        redispatched, so every "can't verify" path deliberately fails CLOSED
        (reports output, i.e. leaves the record alone) rather than open (#150). The
        two directions are not symmetric: a wrong "no output" answer redispatches a
        real agent container onto an issue that already has its comment, while a
        wrong "has output" answer only defers -- the record stays 'success', no
        retry budget is consumed, and the next sweep re-examines it. That is the
        same posture PROTECTION 2's fail-closed lock read takes.

        Live as of #166. Four things had to be true first, and every one of them
        was a confirmed false "no output" against real production records:

          * It has to look where the output actually goes. `planning_design`
            carries workspace: "discussions", so Requirements / Research / Design
            / Work Breakdown / In Development / In Review post to a Discussion, in
            all 17 projects -- 3,266 of 54,594 'success' records. Querying only
            repos/{org}/{repo}/issues/{n}/comments answered "demonstrably produced
            no output" for every one of them; phone-home #72's idea_researcher
            report went to Discussion #191.
          * It has to anchor on a real START time. Both completion paths post the
            comment BEFORE recording the outcome (docker_runner's
            _complete_agent_execution, agent_executor's finalization), so any
            completion anchor post-dates the very comment that proves output --
            the inversion that would have rewritten 29 of 30 genuine successes.
            The anchor is `timestamp`, written by record_execution_start() before
            the task is even enqueued; the crash-recovery record has no such start
            and is declined (see _output_anchor_for_record).
          * It has to tell THIS agent's output from anyone else's. Every agent
            comment the orchestrator posts carries
            "_Processed by the {agent} agent_" (AgentCommentFormatter.
            format_agent_completion), which is the marker project_monitor,
            review_cycle and pr_review_stage already attribute comments with.
            Counting any comment in the window made a human reply, a review-cycle
            banner or the pipeline watchdog's own "Pipeline Stuck" notice read as
            the agent's work.
          * ...and it has to decline the executions whose output is not a signed
            agent comment at all. That is three separate axes, not one, and the
            class is not closed on any of them -- each has its own guard:
              - the DISPATCH PATH. The repair cycle reports through a stage summary
                of its own, so a signature check reads every repair-cycle record as
                empty; measured over seven days of live records that was 83 wrong
                answers out of 96. See _WATCHDOG_ATTRIBUTABLE_TRIGGER_SOURCES.
              - the AGENT. work_breakdown_agent suppresses the orchestrator's post
                and does its own, unsigned; its 50 live 'success' records arrive
                under allowlisted trigger sources, and a redispatch of it re-creates
                the sub-issues. See _WATCHDOG_UNATTRIBUTABLE_AGENTS.
              - the WRITER of the outcome. Two writers mark a record 'success' from
                an exit code alone, on paths that never post to GitHub at all:
                _apply_redis_result(), from a payload recovered out of Redis, and
                docker_runner._process_recovered_pr_review_phase_completion(), which
                checkpoints a recovered PR-review phase and re-triggers the stage.
                Those are stamped outcome_recovered_from_redis and
                outcome_recovered_without_post respectively, and both are declined
                here. Any future writer that finalizes a record without posting
                belongs on this axis too: pass github_post_attempted=False to
                record_execution_outcome() rather than relying on one of the other
                two axes to happen to cover it.

        Args:
            project_name: Project name
            issue_number: Issue number
            execution: Execution record dict

        Returns:
            True if GitHub output exists (or could not be verified), False if the
            execution demonstrably produced none
        """
        try:
            from services.github_api_client import get_github_client
            from config.manager import config_manager

            agent = execution.get('agent')
            if not agent:
                logger.debug(
                    f"Watchdog: No agent on the record for {project_name}/#{issue_number} "
                    f"-- cannot attribute output, leaving the record alone"
                )
                return True

            anchor = self._output_anchor_for_record(execution)
            if anchor is None:
                logger.debug(
                    f"Watchdog: No usable start time for {project_name}/#{issue_number} "
                    f"-- cannot verify GitHub output, leaving the record alone"
                )
                return True

            trigger_source = execution.get('trigger_source')
            if trigger_source not in _WATCHDOG_ATTRIBUTABLE_TRIGGER_SOURCES:
                # This execution's output does not arrive as a comment signed by
                # this agent, so "no signed comment" says nothing about whether it
                # produced anything. See the allowlist for what was measured.
                logger.debug(
                    f"Watchdog: {project_name}/#{issue_number} was dispatched by "
                    f"'{trigger_source}', whose output this gate cannot attribute "
                    f"-- leaving the record alone"
                )
                return True

            if agent in _WATCHDOG_UNATTRIBUTABLE_AGENTS:
                # The dispatch path posts a signed comment; THIS agent opted out of
                # it and does its own posting. The allowlist above cannot see that,
                # because it is a property of the agent -- see the denylist.
                logger.debug(
                    f"Watchdog: {project_name}/#{issue_number} ran '{agent}', which owns "
                    f"its own GitHub posting and emits no signed comment "
                    f"-- leaving the record alone"
                )
                return True

            if (execution.get('outcome_recovered_from_redis')
                    or execution.get('outcome_recovered_without_post')):
                # cleanup_stuck_in_progress_states() -> _apply_redis_result() turned
                # this in_progress entry into a 'success' from an exit_code it found
                # in Redis, on a record that keeps its real start timestamp and its
                # real trigger_source. docker_runner persists that payload BEFORE
                # _complete_agent_execution posts, so a recovered success is exactly
                # the window in which the comment may never have been written -- the
                # gate would answer "no output" correctly and redispatch a
                # code-writing agent onto a branch it has already pushed commits to.
                # Declined for the same reason every other "can't tell" is (#166).
                #
                # outcome_recovered_without_post is the same shape reached by a
                # different writer: record_execution_outcome(github_post_attempted=
                # False), used by docker_runner._process_recovered_pr_review_phase_
                # completion, which finalizes a recovered phase container's record as
                # 'success' under an allowlisted trigger_source ('pr_review_phase2' /
                # 'pr_review_phase4'), with a real start timestamp and an agent that
                # is not on the denylist -- and posts nothing, because it checkpoints
                # the phase output and re-triggers the stage instead (#166 review).
                logger.debug(
                    f"Watchdog: {project_name}/#{issue_number}'s outcome was recorded on a "
                    f"path that never attempts a GitHub post "
                    f"-- leaving the record alone"
                )
                return True

            gh = get_github_client()
            # get_project_config() raises rather than returning None for an unknown
            # project, so there is no falsy-config case to test for here -- and a
            # ProjectConfig dataclass instance is always truthy anyway.
            #
            # Attribute access, not subscription (#150): ProjectConfig is a plain
            # dataclass with no __getitem__, so project_config['github'] raised
            # TypeError on EVERY call, was swallowed by the broad handler below and
            # returned False -- making this gate unconditionally "no output" for
            # every project. Same defect class as the .get()-on-a-dataclass bugs
            # #57/#58 fixed in PROTECTION 2/3.
            project_config = config_manager.get_project_config(project_name)
            org = project_config.github['org']
            repo = project_config.github['repo']

            # Where this column's agent posts. Resolved from the pipeline config by
            # the same helper the poster itself uses (claude/docker_runner.py), so
            # the gate and the writer cannot drift apart -- the _strict sibling,
            # which reports "could not resolve" instead of defaulting to the
            # poster's 'issues'.
            from claude.docker_runner import resolve_workspace_type_for_column_strict
            from config.state_manager import state_manager

            column = execution.get('column') or 'unknown'
            workspace_type = resolve_workspace_type_for_column_strict(project_name, column)

            if workspace_type is None:
                # The _strict variant, not the poster's resolve_workspace_type_for_
                # column(), which answers 'issues' for an 'unknown' column, for a
                # column no configured workflow names any more, and for any
                # exception during resolution alike. 'issues' is the one answer
                # that lets the Discussion scan below be skipped entirely, so that
                # default reads a failed resolution as a positive claim about where
                # the agent posted: measured against live state, 45 of 165
                # attributable discussion-workspace records flip from "defer" to
                # "rewrite and redispatch" on that single value (#166).
                logger.debug(
                    f"Watchdog: could not resolve which workspace {project_name} column "
                    f"'{column}' posts to -- cannot verify GitHub output, leaving the "
                    f"record alone"
                )
                return True

            discussion_id, link_store_readable = (
                state_manager.get_discussion_for_issue_checked(project_name, issue_number)
            )
            if not link_store_readable:
                # load_project_state() answers None for "no link" and for "the state
                # file is there and failed to parse" alike, and save_project_state()
                # is a non-atomic truncate-and-rewrite called from the
                # project-monitor thread while this sweep reads from its executor
                # thread -- so a concurrent read genuinely lands on half a file.
                # Reading that as "this issue has no discussion" would scan only the
                # issue for output that is in a Discussion.
                logger.warning(
                    f"Watchdog: {project_name}'s issue/discussion link store could not be "
                    f"read while checking #{issue_number} -- cannot verify GitHub output, "
                    f"leaving the record alone"
                )
                return True

            if workspace_type in ('discussions', 'hybrid') and not discussion_id:
                # post_agent_output() falls back to an issue comment when it has no
                # discussion id, so the issue scan below would be the right place to
                # look -- but only if the link was ALSO missing when the agent
                # posted. unlink_issue_discussion() exists, and a link removed since
                # would leave the output in a Discussion this gate can no longer
                # find. Not worth a redispatch to find out.
                logger.debug(
                    f"Watchdog: {project_name}/#{issue_number} is in a '{workspace_type}' "
                    f"workspace column ('{column}') with no recorded discussion "
                    f"-- cannot verify GitHub output, leaving the record alone"
                )
                return True

            # Both workspaces are scanned whenever the issue has a discussion at
            # all, not just the one workspace_type names. The two are independent
            # (an issue can carry a discussion for context in an 'issues' column),
            # and post_agent_output() itself routes on discussion_id presence
            # before it consults workspace_type.
            evidence = self._scan_issue_comments_for_agent_output(
                gh, org, repo, project_name, issue_number, agent, anchor
            )
            if evidence == _OUTPUT_EVIDENCE_UNVERIFIABLE:
                return True

            if discussion_id and evidence != _OUTPUT_EVIDENCE_AGENT:
                discussion_evidence = self._scan_discussion_comments_for_agent_output(
                    gh, discussion_id, project_name, issue_number, agent, anchor
                )
                if discussion_evidence == _OUTPUT_EVIDENCE_UNVERIFIABLE:
                    return True
                evidence = _stronger_output_evidence(evidence, discussion_evidence)

            if evidence == _OUTPUT_EVIDENCE_AGENT:
                logger.debug(
                    f"Watchdog: Found {agent} output posted after {anchor.isoformat()} "
                    f"for {project_name}/#{issue_number}"
                )
                return True

            if evidence == _OUTPUT_EVIDENCE_OTHER_AGENT:
                # Some agent posted here inside this execution's window, just not
                # under this record's agent name. The wrapper stages record an
                # outcome under a name their sub-run does not post under (the
                # repair cycle's summary is signed "Repair cycle executed by
                # Switchyard", not by an agent), so an unattributed agent comment
                # is as likely to be this execution's output under another name as
                # it is to be an unrelated stage's. Reported at INFO because it is
                # the one answer here that is a genuine "don't know" rather than a
                # measurement.
                logger.info(
                    f"Watchdog: {project_name}/#{issue_number} has agent output posted "
                    f"after {anchor.isoformat()} but none signed by '{agent}' "
                    f"-- ambiguous, leaving the record alone"
                )
                return True

            logger.debug(
                f"No GitHub output found for {project_name}/#{issue_number} from "
                f"'{agent}' after {anchor.isoformat()} "
                f"(workspace: {workspace_type}, discussion: {discussion_id or 'none'})"
            )
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

    def _output_anchor_for_record(self, execution: dict) -> Optional[datetime]:
        """The instant this execution's output must have been posted after, or None.

        `timestamp` is that instant for every record record_execution_start()
        wrote: it is stamped before the task is even enqueued, so anything the
        agent posts necessarily follows it. It is NOT that instant for the record
        record_execution_outcome() synthesises when it finds no matching
        in_progress entry (an orchestrator restart lost the dispatch) -- there
        `timestamp` is stamped at outcome-recording time, i.e. AFTER the agent
        already posted, so anchoring on it reports "nothing posted since" for an
        execution that posted perfectly well. 8,692 of 54,594 live 'success'
        records have that shape.

        Those records are marked `start_time_unknown` at the point they are
        written; the trigger_source fallback covers the ones already on disk,
        which carry no board and no real trigger either ('unknown' is written
        nowhere else).

        There is a THIRD writer of outcome='success', and its anchor is fine while
        its attribution is not: _apply_redis_result() mutates the live in_progress
        entry when cleanup_stuck_in_progress_states() recovers an exit_code 0
        payload from Redis, so the record keeps a genuine start `timestamp` and a
        genuine (allowlisted) trigger_source. Nothing on that path posts to GitHub
        -- docker_runner persists the payload BEFORE _complete_agent_execution
        posts -- so a signature scan is answering a question the path never gave
        GitHub a chance to answer. Deliberately NOT handled here, because the
        anchor really is usable; those records are stamped
        `outcome_recovered_from_redis` where they are written and declined by
        _has_github_output() alongside the other unattributable shapes.
        """
        if execution.get('start_time_unknown'):
            return None

        trigger_source = execution.get('trigger_source')
        if not trigger_source or trigger_source == 'unknown':
            return None

        started_at = execution.get('timestamp')
        if not started_at:
            return None

        try:
            return _parse_iso_timestamp(started_at)
        except (ValueError, TypeError, AttributeError) as e:
            logger.debug(f"Watchdog: Could not parse execution start {started_at!r}: {e}")
            return None

    def _scan_issue_comments_for_agent_output(
        self, gh, org: str, repo: str, project_name: str, issue_number: int,
        agent: str, anchor: datetime
    ) -> str:
        """Classify the issue's comments posted at or after `anchor`.

        Bounded server-side (#166). `gh api` is called with no --paginate, and the
        list-comments endpoint defaults to per_page=30 sorted created/ascending --
        so the unbounded form returned the OLDEST 30 comments, every one of which
        predates the execution on any issue with a history (context-studio has
        several past 170). `since` filters on updated_at, which a comment created
        after the anchor always satisfies, so the window is never clipped by it.
        """
        since = anchor.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        endpoint = (
            f'repos/{org}/{repo}/issues/{issue_number}/comments'
            f'?per_page={_WATCHDOG_COMMENT_PAGE_SIZE}&since={since}'
        )

        success, comments = gh.rest('GET', endpoint)

        if not success:
            logger.warning(
                f"Watchdog: Failed to fetch comments for {project_name}/#{issue_number} "
                f"-- cannot verify GitHub output, leaving the record alone"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        if not isinstance(comments, list):
            # rest() hands back whatever the response body decoded to. A dict
            # (an error envelope) or a string iterates into something the scan
            # below silently skips, ending in a confident "no output" derived
            # from a body that was never a comment list.
            logger.warning(
                f"Watchdog: Comments response for {project_name}/#{issue_number} was "
                f"{type(comments).__name__}, not a list -- cannot verify GitHub "
                f"output, leaving the record alone"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        if len(comments) >= _WATCHDOG_COMMENT_PAGE_SIZE:
            # A full page is indistinguishable from a clipped one, and the page is
            # the OLDEST comments in the window -- the agent's could be past it.
            logger.warning(
                f"Watchdog: {project_name}/#{issue_number} returned a full page of "
                f"{len(comments)} comments since {since} -- the window may be clipped, "
                f"leaving the record alone"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        return _classify_output_evidence(
            ((c.get('created_at'), c.get('body')) for c in comments if isinstance(c, dict)),
            agent, anchor
        )

    def _scan_discussion_comments_for_agent_output(
        self, gh, discussion_id: str, project_name: str, issue_number: int,
        agent: str, anchor: datetime
    ) -> str:
        """Classify a Discussion's comments and threaded replies posted at or after `anchor`.

        Uses the REST/GraphQL client directly rather than
        GitHubDiscussions.get_discussion_comments(), which returns [] for both "no
        comments" and "the query failed" -- the one distinction this gate cannot
        afford to lose.

        `last:` rather than `first:` because the question is about a recent window;
        GitHub orders connections oldest-first, so `last` returns the tail and
        anything dropped is older than the page's own first node.
        """
        query = """
        query($discussionId: ID!, $comments: Int!, $replies: Int!) {
          node(id: $discussionId) {
            ... on Discussion {
              comments(last: $comments) {
                totalCount
                nodes {
                  createdAt
                  body
                  replies(last: $replies) {
                    totalCount
                    nodes {
                      createdAt
                      body
                    }
                  }
                }
              }
            }
          }
        }
        """

        success, data = gh.graphql(query, {
            'discussionId': discussion_id,
            'comments': _WATCHDOG_COMMENT_PAGE_SIZE,
            'replies': _WATCHDOG_DISCUSSION_REPLY_PAGE_SIZE,
        })

        if not success:
            logger.warning(
                f"Watchdog: Failed to fetch discussion {discussion_id} for "
                f"{project_name}/#{issue_number} -- cannot verify GitHub output, "
                f"leaving the record alone"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        comments = ((data or {}).get('node') or {}).get('comments') or {}
        nodes = comments.get('nodes')
        if not isinstance(nodes, list):
            logger.warning(
                f"Watchdog: Discussion {discussion_id} for {project_name}/#{issue_number} "
                f"returned no comment list -- cannot verify GitHub output, leaving "
                f"the record alone"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        # The strict test, not _newest_page_covers_window() (#166 review). A clipped
        # comments page cannot vouch for the window no matter how old its own oldest
        # node is, because the replies on the comments it dropped were never
        # requested -- and a threaded reply is exactly where a human_feedback_loop
        # response lands, on a thread whose root comment may be months old. A long
        # epic Discussion that crosses 100 top-level comments would otherwise report
        # a confident "no output" on every single sweep.
        if not _page_holds_every_node(comments.get('totalCount'), nodes):
            logger.warning(
                f"Watchdog: Discussion {discussion_id} for {project_name}/#{issue_number} "
                f"has more comments than one page, so the replies on the ones it dropped "
                f"were never read -- leaving the record alone"
            )
            return _OUTPUT_EVIDENCE_UNVERIFIABLE

        entries = []
        for node in nodes:
            if not isinstance(node, dict):
                logger.warning(
                    f"Watchdog: Discussion {discussion_id} for {project_name}/#{issue_number} "
                    f"returned a {type(node).__name__} where a comment was expected "
                    f"-- leaving the record alone"
                )
                return _OUTPUT_EVIDENCE_UNVERIFIABLE
            entries.append((node.get('createdAt'), node.get('body')))

            # A threaded reply is where a human_feedback_loop response lands
            # (post_agent_output passes reply_to_comment_id through to
            # addDiscussionComment), so replies are agent output too and are
            # bounded the same way as the comments carrying them.
            replies = node.get('replies') or {}
            reply_nodes = replies.get('nodes')
            if not isinstance(reply_nodes, list) or not _newest_page_covers_window(
                replies.get('totalCount'), reply_nodes, anchor
            ):
                logger.warning(
                    f"Watchdog: A comment thread on discussion {discussion_id} for "
                    f"{project_name}/#{issue_number} could not be read back to "
                    f"{anchor.isoformat()} -- leaving the record alone"
                )
                return _OUTPUT_EVIDENCE_UNVERIFIABLE
            for reply in reply_nodes:
                if not isinstance(reply, dict):
                    logger.warning(
                        f"Watchdog: Discussion {discussion_id} for {project_name}/#{issue_number} "
                        f"returned a {type(reply).__name__} where a reply was expected "
                        f"-- leaving the record alone"
                    )
                    return _OUTPUT_EVIDENCE_UNVERIFIABLE
                entries.append((reply.get('createdAt'), reply.get('body')))

        return _classify_output_evidence(entries, agent, anchor)

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

        # Stamped on both outcomes: this record's terminal state came out of Redis,
        # not out of a completion path. docker_runner persists the payload BEFORE
        # _complete_agent_execution posts the agent's comment, so a record recovered
        # here is precisely one whose GitHub post may never have been attempted --
        # the empty-output gate declines it rather than reading "no signed comment"
        # as "produced nothing" and redispatching an agent that has already run
        # (#166). Set before the outcome so the two can never be written apart.
        execution['outcome_recovered_from_redis'] = True

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
                                    # Reconcile the dev container state BEFORE writing this
                                    # record off. Marking the record 'failure' is what makes it
                                    # invisible to every later sweep (this loop only looks at
                                    # 'in_progress'), so consuming it while the reset was skipped
                                    # would strand the project at whatever the dead build left
                                    # behind, with nothing scheduled to revisit it (#152 review).
                                    # Leaving the record alone instead costs one more sweep and
                                    # is self-healing: the 15-minute pass retries until the lock
                                    # is free.
                                    #
                                    # Only reset to UNVERIFIED if the current state is NOT already
                                    # VERIFIED. A later setup/verifier execution may have already
                                    # succeeded, in which case this stuck record is from a
                                    # superseded execution and should not clobber the verified
                                    # state. (The re-read that decides this happens inside the
                                    # lock — see _transition_dev_container_state.)
                                    if agent in ('dev_environment_verifier', 'dev_environment_setup'):
                                        from services.dev_container_state import DevContainerStatus
                                        _reconciled = _transition_dev_container_state(
                                            project_name,
                                            DevContainerStatus.UNVERIFIED,
                                            reason=f"Stuck {agent} detected",
                                            skip_when=(DevContainerStatus.VERIFIED,),
                                            error_message=(
                                                "Verification container died before completion"
                                                if agent == 'dev_environment_verifier'
                                                else "Setup container died before completion"
                                            ),
                                        )
                                        if not _reconciled:
                                            logger.warning(
                                                f"Leaving stuck {agent} execution "
                                                f"{project_name}/#{issue_number} as in_progress — "
                                                f"its dev container state could not be reconciled "
                                                f"yet; the next cleanup pass will retry"
                                            )
                                            try:
                                                from services.cleanup_guard import release_cleanup
                                                release_cleanup(project_name, issue_number)
                                            except Exception:
                                                pass  # TTL expires the claim well before the next sweep
                                            continue

                                    # No container AND no Redis result — truly lost execution
                                    execution['outcome'] = 'failure'
                                    execution['error'] = (
                                        'Agent execution interrupted. Container no longer exists and execution '
                                        'state was not updated. This may indicate the agent crashed, was killed, '
                                        'or the orchestrator was restarted before outcome could be recorded.'
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
                                    # Unlike the not-recovered path above, this one cannot
                                    # defer the record: the Redis result it recovered has
                                    # already been consumed, so a retry next sweep would find
                                    # nothing. A skipped write here is reported by
                                    # _log_skipped() (ERROR when the acquire was degraded
                                    # rather than contended) and left for the operator.
                                    if agent == 'dev_environment_verifier':
                                        from services.dev_container_state import DevContainerStatus
                                        _transition_dev_container_state(
                                            project_name,
                                            DevContainerStatus.VERIFIED,
                                            reason=(
                                                "Dev environment verifier succeeded (recovered from Redis) "
                                                "but state was not VERIFIED"
                                            ),
                                            skip_when=(DevContainerStatus.VERIFIED,),
                                            image_name=f"{project_name}-agent:latest",
                                        )

                                    try:
                                        from monitoring.decision_events import DecisionEventEmitter
                                        from monitoring.observability import get_observability_manager
                                        from services.pipeline_run import get_pipeline_run_manager

                                        obs = get_observability_manager()
                                        decision_events = DecisionEventEmitter(obs)
                                        pipeline_run_mgr = get_pipeline_run_manager()
                                        # read-only: this lookup only wants an id
                                        # to stamp on an event, and the ES fallback
                                        # would otherwise write a possibly-dead run
                                        # back into Redis (#150)
                                        active_run = pipeline_run_mgr.get_active_pipeline_run(
                                            project_name, issue_number, restore_to_redis=False
                                        )

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
                                        from services.dev_container_state import DevContainerStatus
                                        # No skip_when: a recovered verifier FAILURE is this
                                        # record's own verdict on the image, and BLOCKED is
                                        # deliberately terminal. The lock is still what keeps
                                        # it from landing on top of a build that is running
                                        # right now (that acquire fails, and this is skipped).
                                        _transition_dev_container_state(
                                            project_name,
                                            DevContainerStatus.BLOCKED,
                                            reason=(
                                                "Dev environment verification failed "
                                                "(recovered from Redis with failure)"
                                            ),
                                            error_message=execution.get('error', 'Verification failed')[:200],
                                        )

                                    # Special handling for dev_environment_setup agent failures
                                    # Reset to UNVERIFIED so setup can be retried automatically
                                    #
                                    # Gated on `recovered`, exactly like the verifier block
                                    # above it. The non-recovered case is ALREADY handled, ~100
                                    # lines earlier and correctly: that transition carries
                                    # skip_when=(VERIFIED,) because a stuck record says nothing
                                    # about an image a LATER setup+verify has since verified.
                                    # Without this gate the same loop iteration wrote the state
                                    # twice, and this second, unguarded write silently undid the
                                    # guard -- the lock cannot help, both writes being the same
                                    # process, the same sweep, sequential acquisitions. A stale
                                    # in_progress record therefore reset a healthy VERIFIED
                                    # project to UNVERIFIED, refusing every task for it until a
                                    # redundant setup ran (#152 review). A RECOVERED failure is
                                    # different: it is this record's own verdict, read back from
                                    # the run's real result, so it gets no skip_when.
                                    if agent == 'dev_environment_setup' and recovered:
                                        from services.dev_container_state import DevContainerStatus
                                        _transition_dev_container_state(
                                            project_name,
                                            DevContainerStatus.UNVERIFIED,
                                            reason="Dev environment setup failed",
                                            error_message=execution.get('error', 'Setup failed')[:200],
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
                                        # read-only: this lookup only wants an id
                                        # to stamp on an event, and the ES fallback
                                        # would otherwise write a possibly-dead run
                                        # back into Redis (#150)
                                        active_run = pipeline_run_mgr.get_active_pipeline_run(
                                            project_name, issue_number, restore_to_redis=False
                                        )

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

    def _entry_is_in_board_scope(
        self,
        execution: Dict,
        board_name: str,
        cutoff: Optional[datetime],
        project_name: str,
        issue_number: int
    ) -> bool:
        """
        Whether a board-scoped abandon sweep may touch this execution record.

        Three cases, and only the first is an outright match:
        - recorded on this board: in scope;
        - recorded on a DIFFERENT board: out of scope, always. This is the case
          the scoping exists for — the other board's run is somebody else's
          decision, and it may be live;
        - no board recorded: in scope only if it predates `cutoff` (the calling
          run's own start). An unattributed record older than the dead run
          cannot be a dispatch that started after it, and leaving those behind
          is what keeps has_active_execution() stuck True on the very records
          this sweep exists to clear.
        """
        recorded_board = execution.get('board_name')
        if recorded_board:
            return recorded_board == board_name

        if cutoff is None:
            return False

        try:
            return _parse_iso_timestamp(execution['timestamp']) < cutoff
        except Exception as e:
            # An unparsable timestamp on an unattributed record leaves no basis
            # for the decision at all -- skip it rather than guess, same
            # posture as a missing cutoff.
            logger.warning(
                f"Could not parse timestamp on an in_progress entry for "
                f"{project_name}/#{issue_number} with no recorded board — "
                f"leaving it in_progress rather than abandoning it "
                f"unattributed: {e}"
            )
            return False

    def abandon_stale_in_progress_entries(
        self,
        project_name: str,
        issue_number: int,
        active_task_ids: set,
        reason: str = 'Orchestrator restarted without completing this execution.',
        board_name: Optional[str] = None,
        started_before: Optional[str] = None
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
            board_name: Restrict the sweep to executions recorded on THIS board
                (#144's board_name stamp). Omitted by the restart-recovery
                caller, whose authority genuinely is project-wide: the
                orchestrator just came up, so no execution of this issue on any
                board can still be live. A caller whose decision was made about
                ONE board must pass it — see the pipeline watchdog's
                _clear_non_holder_reentry_blocks. An issue can sit on more than
                one Projects v2 board at once, and `active_task_ids` cannot
                protect the other board's entry: it is empty for those callers,
                and a just-dispatched entry has no task_id stamped yet anyway
                (record_execution_start deliberately runs BEFORE the enqueue).
            started_before: Only meaningful alongside board_name, and only for
                entries carrying no recorded board at all (pre-#144 records,
                record_execution_outcome()'s synthesised crash-recovery record,
                and the dispatch paths with no board in scope). Those cannot be
                attributed to a board, so they are abandoned only when they
                predate this ISO timestamp — the dead run's started_at, which
                every such caller has in hand. Without it they would be swept
                unconditionally, which is the project-wide write the board
                filter exists to stop.

        Returns:
            Number of entries marked as abandoned
        """
        state = self.load_state(project_name, issue_number)
        abandoned = 0
        cutoff = None
        if board_name and started_before:
            try:
                cutoff = _parse_iso_timestamp(started_before)
            except Exception as e:
                # Not fatal: without a usable cutoff the unattributed entries
                # are simply left alone, which is the conservative half of the
                # scoping rather than a fall back to the project-wide sweep.
                logger.warning(
                    f"Could not parse started_before='{started_before}' while "
                    f"abandoning stale entries for {project_name}/#{issue_number} "
                    f"— entries with no recorded board will be left in_progress: {e}"
                )

        for execution in state['execution_history']:
            if execution.get('outcome') != 'in_progress':
                continue

            task_id = execution.get('task_id')
            if task_id and task_id in active_task_ids:
                # Container is still running — leave this entry alone
                continue

            if board_name and not self._entry_is_in_board_scope(
                execution, board_name, cutoff, project_name, issue_number
            ):
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

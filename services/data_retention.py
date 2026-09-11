"""Age-based retention for everything the orchestrator writes and never reads.

The window comes from config/retention.py -- one RETENTION_DAYS value shared
with every Elasticsearch ILM policy, so the two halves of this system cannot
drift apart. Before that they had: eight hand-written ILM windows (7d/14d/30d/
180d), three hand-picked file windows (14d/30d/90d), one count-based rule, ten
indices with no policy at all, and nine filesystem locations with no sweep.
The metrics JSONL "backup" was kept 90 days against an Elasticsearch original
deleted after 7, so for 83 of those days it was backing up nothing.

HISTORY AGES. LIVE STATE DOES NOT.
----------------------------------
Every rule below covers an ARTIFACT: something written once, describing a
moment that has passed. Those age out.

Four directories deliberately have no rule, because they describe what is true
*now*, and a lock file is no less valid for being three months old:

    state/pipeline_locks/          who holds which board lock
    state/pipeline_queues/         what is waiting to run
    state/dev_containers/          which image is verified for which project
    state/projects/*/github_state.yaml
                                   board and column node IDs

Deleting any of those by age would be destructive, not tidy. What they
accumulate is ORPHANS -- entries for projects whose config is gone -- and that
is scripts/inspect_project_state.py's job, keyed on the config list rather than
on a clock.

Two more exclusions, for their own reasons:

  * state/execution_history/*.yaml.lock -- 0 bytes each, so they cost inodes
    and not space, and deleting one a process currently holds breaks the mutual
    exclusion it exists to provide. Same for the .lock sidecars under
    state/pipeline_locks/ and state/pipeline_queues/.
  * <checkout>/.repair_cycle.log -- a live append target. Bounded by
    monitoring/log_rotation.py's per-checkout cap instead, since aging out a
    file something is writing to just truncates it at an arbitrary moment.
"""


import logging
import os
import shutil
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

logger = logging.getLogger(__name__)


from config.retention import RETENTION_DAYS  # noqa: E402


# Where the workspace root is, for the locations that live beside the
# orchestrator checkout rather than inside it (`/workspace/.orchestrator`,
# `/workspace/<project>/`). In the container /app IS /workspace/switchyard, so
# this cannot be derived from ORCHESTRATOR_ROOT alone.
WORKSPACE_ROOT = os.environ.get('WORKSPACE_ROOT', '/workspace')


@dataclass(frozen=True)
class RetentionRule:
    """One directory and what counts as an entry in it.

    There is no per-rule window: every rule ages at config/retention.py's
    RETENTION_DAYS. That is the whole point -- a `retention_days` field here
    would be an invitation to give one location a different number, which is
    how the eleven disagreeing windows this replaced came about.

    `entries` returns the things to age out, which are files for most rules and
    directories for two of them -- hence the explicit `_remove()` rather than
    assuming unlink().

    `root_kind` selects which root `relative_path` hangs off: 'orchestrator'
    for ORCHESTRATOR_ROOT (/app) and 'workspace' for WORKSPACE_ROOT
    (/workspace). In the container /app IS /workspace/switchyard, so the two
    cannot be derived from one another.

    `keep` is an optional last-chance veto, consulted ONLY for entries that
    have already been found to be past the window. That ordering is the point:
    it keeps a per-entry cost (parsing a YAML file, say) proportional to what
    is about to be deleted rather than to what is on disk, and it means a
    directory that is already bounded costs exactly one readdir.
    """
    name: str
    relative_path: str
    entries: Callable[[Path], Iterable[Path]]
    description: str
    root_kind: str = 'orchestrator'
    keep: Optional[Callable[[Path], bool]] = None

    @property
    def retention_days(self) -> int:
        return RETENTION_DAYS

    def age_cutoff(self, now: float) -> float:
        return now - (RETENTION_DAYS * 86400)


def _files_matching(pattern: str) -> Callable[[Path], Iterable[Path]]:
    def _entries(root: Path) -> Iterable[Path]:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    return _entries


def _dirs_one_level_down(root: Path) -> Iterable[Path]:
    """`<root>/<a>/` -- for the scratch trees keyed directly by run.

    Used for .orchestrator/tmp/pipeline_context/<issue>_<run-prefix>/, whose
    contents are files (initial_request.md, <agent>_output.md, ...) rather than
    a further level of directories. Getting this depth wrong is silent: the
    wrong selector simply returns nothing and the rule reports a clean
    directory forever. See services/pipeline_context_writer.py:setup().
    """
    return sorted(child for child in root.iterdir() if child.is_dir())


def _dirs_two_levels_down(root: Path) -> Iterable[Path]:
    """`<root>/<a>/<b>/` -- the per-run unit, not the per-project container.

    Used for repair_cycles/<project>/<issue>/. Removing a whole project
    directory would take the scratch for cycles that are still recent with it.
    """
    return sorted(
        leaf
        for parent in root.iterdir() if parent.is_dir()
        for leaf in parent.iterdir() if leaf.is_dir()
    )


# Kept as an alias: the name says what it is for at the one call site, and
# tests reference it.
_repair_cycle_issue_dirs = _dirs_two_levels_down


def _files_matching_nested(pattern: str) -> Callable[[Path], Iterable[Path]]:
    """Like _files_matching but recursive, for per-project subdirectories."""
    def _entries(root: Path) -> Iterable[Path]:
        return sorted(p for p in root.rglob(pattern) if p.is_file())
    return _entries


def _execution_history_records(root: Path) -> Iterable[Path]:
    """Execution-history YAML only -- never the .yaml.lock sidecars.

    The sidecars are excluded by construction rather than by a later filter:
    glob('*.yaml') would otherwise also need a name check, and getting that
    wrong deletes a lock some process is holding.

    WHY 30 DAYS IS SAFE HERE, given that some readers have no time bound at all
    ---------------------------------------------------------------------------
    The file is per-ISSUE and is rewritten whole on every execution start,
    outcome, task-id stamp and status change (WorkExecutionStateTracker
    .save_state), so its mtime is the last time anything happened to that
    issue. An issue still being worked cannot expire, however old its first
    record is. Expiry means "this issue has been untouched for a month".

    The short-horizon readers are genuinely short-horizon: the empty-output
    watchdog gates on _WATCHDOG_MAX_RECORD_AGE_HOURS (24h) before it does
    anything but parse, and was_recent_programmatic_change() uses a 60-second
    window over status_changes.

    The readers with NO time bound are the ones worth naming, because the
    argument has to be made for them rather than against their existence:

      * should_execute_work() -- the dedup gate. A missing file reads as
        "first_execution", so deleting a record makes an untouched issue
        dispatchable again. Backstopped by _rescan_boards_for_stalled_items()'s
        has_existing_output check, which scans GitHub for the agent's own
        completion comment and does not expire; and by the fact that the normal
        poll loop only dispatches on a board diff, which is a fresh reason to
        run regardless of history.
      * ProjectMonitor's startup last_state seeding, and get_last_execution()
        on the lock-holding path -- both degrade to "no prior run", which is
        the conservative direction for everything except a feedback_listening
        conversation that has idled longer than the window with no writes. That
        one is a real if narrow exposure; it is bounded by the same
        RETENTION_DAYS on its pipeline-run document either way.

    What is NOT safe to delete is a record whose last execution is still
    in_progress: that is live state by this module's own rule, and removing it
    flips has_active_execution() from True to False under a container that may
    still be running. Hence the `keep` predicate on the rule -- see
    _execution_still_running().
    """
    return sorted(p for p in root.glob('*.yaml') if p.is_file())


def _execution_still_running(path: Path) -> bool:
    """True if this execution record's last entry has not finished.

    Applied only to records already past the window, so the YAML parse costs
    nothing on a directory that is already bounded. Anything unreadable counts
    as still running: "I could not tell" must resolve to keeping the file, the
    same way an unstattable entry does.

    A genuinely stuck in_progress entry is not kept forever -- it is resolved
    by cleanup_stuck_in_progress_states() / abandon_stale_in_progress_entries(),
    and resolving it rewrites the file, after which it ages normally.
    """
    try:
        import yaml
        with open(path) as handle:
            state = yaml.safe_load(handle)
    except Exception as e:
        logger.warning(
            f"Retention: could not read {path} to check for a running "
            f"execution; keeping it: {e}"
        )
        return True
    if not isinstance(state, dict):
        # Parsed fine and is not a record -- that is a definite answer, unlike
        # a parse failure, so it does not get the benefit of the doubt. (Its
        # own callers already treat an unreadable record as empty state.)
        return False
    history = state.get('execution_history') or []
    if not isinstance(history, list) or not history:
        return False
    last = history[-1]
    return isinstance(last, dict) and last.get('outcome') == 'in_progress'


RETENTION_RULES = (
    RetentionRule(
        name='container_failure_logs',
        relative_path='orchestrator_data/logs/container-failures',
        root_kind='orchestrator',
        entries=_files_matching('*.log'),
        description='per-container diagnostic logs from failed agent runs',
    ),
    RetentionRule(
        name='repair_cycle_scratch',
        relative_path='orchestrator_data/repair_cycles',
        root_kind='orchestrator',
        entries=_dirs_two_levels_down,
        description='per-run repair-cycle context/result files',
    ),
    RetentionRule(
        name='metrics_backup',
        relative_path='orchestrator_data/metrics',
        root_kind='orchestrator',
        entries=_files_matching('*.jsonl'),
        description='local JSONL mirror of metrics already in Elasticsearch',
    ),
    RetentionRule(
        name='medic_advisor_reports',
        relative_path='orchestrator_data/medic/advisor_reports',
        root_kind='orchestrator',
        entries=_files_matching_nested('*.md'),
        description='per-project advisor reports',
    ),
    RetentionRule(
        name='conversational_sessions',
        relative_path='state/conversational_sessions',
        root_kind='orchestrator',
        entries=_files_matching('*.yaml'),
        description='threaded-conversation state for a single issue',
    ),
    RetentionRule(
        name='execution_history',
        relative_path='state/execution_history',
        root_kind='orchestrator',
        entries=_execution_history_records,
        keep=_execution_still_running,
        description='per-issue execution records (NOT their .lock sidecars)',
    ),
    RetentionRule(
        name='state_backups',
        relative_path='state/projects',
        root_kind='orchestrator',
        entries=_files_matching_nested('github_state_backup_*.yaml'),
        description='point-in-time copies of a project\'s github_state.yaml',
    ),
    RetentionRule(
        name='agent_launch_scratch',
        relative_path='.orchestrator/tmp',
        root_kind='workspace',
        entries=_files_matching('mcp_config_*.json'),
        description='per-agent-launch MCP config files',
    ),
    RetentionRule(
        name='pipeline_context_scratch',
        relative_path='.orchestrator/tmp/pipeline_context',
        root_kind='workspace',
        entries=_dirs_one_level_down,
        description='per-run pipeline stage-output fallback copies',
    ),
)


@dataclass
class RuleOutcome:
    rule: RetentionRule
    path: str
    examined: int = 0
    expired: List[Path] = field(default_factory=list)
    removed: List[Path] = field(default_factory=list)
    kept_live: int = 0
    bytes_removed: int = 0
    errors: List[str] = field(default_factory=list)
    missing: bool = False


def _entry_size(path: Path) -> int:
    try:
        if path.is_dir():
            return sum(p.stat().st_size for p in path.rglob('*') if p.is_file())
        return path.stat().st_size
    except OSError:
        return 0


def _entry_mtime(path: Path) -> Optional[float]:
    """Newest mtime under `path`, so a directory ages from its last write.

    A repair-cycle directory gets its context file at launch and its result
    file at the end; aging it from the older of the two would expire a run
    while it is arguably still interesting.

    A child that cannot be stat'd makes the whole directory UNDATABLE (None),
    rather than being quietly dropped from the max. Path.is_file() swallows
    OSError and answers False, so the obvious spelling of this loop silently
    excludes exactly the file most likely to matter -- the newest one, written
    by an agent container under a different uid into mounted scratch. The
    caller's "could not date it, leave it alone" branch only protects anything
    if this function is willing to say it could not tell.
    """
    try:
        if not path.is_dir():
            return path.stat().st_mtime
        mtimes = []
        for child in path.rglob('*'):
            try:
                info = child.stat()
            except OSError:
                return None
            if stat.S_ISREG(info.st_mode):
                mtimes.append(info.st_mtime)
        return max(mtimes) if mtimes else path.stat().st_mtime
    except OSError:
        return None


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def sweep_rule(
    rule: RetentionRule,
    root: Path,
    apply: bool,
    now: Optional[float] = None,
) -> RuleOutcome:
    """Age out one rule's directory. `apply=False` reports without deleting."""
    now = time.time() if now is None else now
    directory = root / rule.relative_path
    outcome = RuleOutcome(rule=rule, path=str(directory))

    if not directory.is_dir():
        outcome.missing = True
        return outcome

    cutoff = rule.age_cutoff(now)

    try:
        entries = list(rule.entries(directory))
    except Exception as e:
        # Deliberately not just OSError. sweep() promises never to raise, and
        # the caller is an unattended nightly job -- a rule whose selector has
        # a bug in it must cost that one rule, not every rule after it in the
        # tuple.
        outcome.errors.append(f"could not list {directory}: {e}")
        return outcome

    for entry in entries:
        outcome.examined += 1
        mtime = _entry_mtime(entry)
        if mtime is None:
            # Unreadable, so undatable. Left alone: "assume it is old" is the
            # assumption that deletes something still in use.
            outcome.errors.append(f"could not stat {entry}, leaving it alone")
            continue
        if mtime >= cutoff:
            continue

        if rule.keep is not None:
            try:
                if rule.keep(entry):
                    outcome.kept_live += 1
                    continue
            except Exception as e:
                outcome.errors.append(
                    f"could not decide whether {entry} is still live, "
                    f"leaving it alone: {e}"
                )
                outcome.kept_live += 1
                continue

        outcome.expired.append(entry)
        size = _entry_size(entry)
        if not apply:
            outcome.bytes_removed += size
            continue
        try:
            _remove(entry)
        except Exception as e:
            # Per entry, so one undeletable file does not abandon the sweep.
            outcome.errors.append(f"could not remove {entry}: {e}")
            continue
        outcome.removed.append(entry)
        outcome.bytes_removed += size

    return outcome


def resolve_roots(
    root: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> dict:
    """The two roots rules hang off, with overrides for tests and the script.

    An ORCHESTRATOR_ROOT env override carries the workspace rules with it for
    exactly the same reason an explicit `root=` does. The test suite is
    required to run with ORCHESTRATOR_ROOT pointed at scratch (issue #181), and
    without this a test that called sweep() with no arguments would sweep its
    own scratch tree for the orchestrator rules and the REAL /workspace -- which
    holds every managed project checkout -- for the other two. Only a
    deployment that has set neither gets the production defaults.
    """
    env_root = os.environ.get('ORCHESTRATOR_ROOT')
    env_workspace = os.environ.get('WORKSPACE_ROOT')

    if root is not None:
        orchestrator = Path(root)
    elif env_root:
        orchestrator = Path(env_root)
    else:
        orchestrator = Path('/app')

    if workspace_root is not None:
        workspace = Path(workspace_root)
    elif env_workspace:
        workspace = Path(env_workspace)
    elif root is not None or env_root:
        # Pointed at a scratch orchestrator root and given no workspace of its
        # own: land the workspace-rooted rules under it too, rather than
        # reaching out to the real /workspace.
        workspace = orchestrator
    else:
        workspace = Path(WORKSPACE_ROOT)
    return {'orchestrator': orchestrator, 'workspace': workspace}


# Roots a sweep must never delete from while running under pytest. Any test
# that resolves to one of these has lost its isolation, and the right outcome
# is a loud test failure rather than a production directory being emptied.
_PROTECTED_ROOTS = (Path('/app'), Path('/workspace'), Path('/'))


def _refuse_unisolated_apply(roots: dict) -> None:
    if not os.environ.get('PYTEST_CURRENT_TEST'):
        return
    for kind, resolved in roots.items():
        if Path(resolved) in _PROTECTED_ROOTS:
            raise RuntimeError(
                f"Refusing to apply retention to the {kind} root {resolved} "
                f"from inside a test. Pass root=/workspace_root= explicitly, or "
                f"set ORCHESTRATOR_ROOT/WORKSPACE_ROOT to a scratch directory. "
                f"This deletes real data on the live deployment."
            )


def sweep(
    root: Optional[Path] = None,
    apply: bool = False,
    rules: Iterable[RetentionRule] = RETENTION_RULES,
    now: Optional[float] = None,
    workspace_root: Optional[Path] = None,
) -> List[RuleOutcome]:
    """Run every retention rule. Never raises; failures land in the outcomes."""
    roots = resolve_roots(root, workspace_root)
    if apply:
        _refuse_unisolated_apply(roots)
    return [
        sweep_rule(rule, roots[rule.root_kind], apply=apply, now=now)
        for rule in rules
    ]


def run_scheduled_sweep(
    root: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> List[RuleOutcome]:
    """Entry point for the daily job in services/scheduled_tasks.py.

    Logs a closing summary at INFO unconditionally. That is the point of it: a
    sweep that finds none of its directories -- a wrong root, a changed layout,
    an unmounted volume -- otherwise produces exactly the same log output as a
    healthy night with nothing to delete, which means retention can be entirely
    broken for months and read as working.
    """
    roots = resolve_roots(root, workspace_root)
    logger.info(
        f"Data retention sweep starting -- {RETENTION_DAYS}-day window, "
        f"orchestrator root {roots['orchestrator']}, "
        f"workspace root {roots['workspace']}"
    )
    outcomes = sweep(root=root, apply=True, workspace_root=workspace_root)
    for outcome in outcomes:
        if outcome.missing:
            logger.debug(f"Retention: {outcome.rule.name}: {outcome.path} absent, nothing to do")
            continue
        if outcome.removed:
            logger.info(
                f"Retention: {outcome.rule.name}: removed {len(outcome.removed)} of "
                f"{outcome.examined} entr{'y' if outcome.examined == 1 else 'ies'} "
                f"older than {outcome.rule.retention_days}d "
                f"({outcome.bytes_removed / 1024 / 1024:.1f}MB) from {outcome.path}"
            )
        else:
            logger.debug(
                f"Retention: {outcome.rule.name}: nothing older than "
                f"{outcome.rule.retention_days}d among {outcome.examined} entries"
            )
        for error in outcome.errors:
            logger.warning(f"Retention: {outcome.rule.name}: {error}")

    present = [o for o in outcomes if not o.missing]
    if not present:
        logger.error(
            f"Data retention sweep found NONE of its {len(outcomes)} directories "
            f"under {roots['orchestrator']} / {roots['workspace']}. Retention is "
            f"not running. Checked: {[o.path for o in outcomes]}"
        )

    # One undeletable file is noise; a rule where EVERY expired entry failed is
    # a mount or permissions problem, and reporting it as N warnings buries the
    # one fact that matters.
    for outcome in present:
        attempted = len(outcome.expired)
        if attempted and not outcome.removed:
            logger.error(
                f"Retention: {outcome.rule.name}: all {attempted} expired entries "
                f"failed to delete under {outcome.path} -- this is systemic "
                f"(permissions or mount), not per-file. First: "
                f"{outcome.errors[0] if outcome.errors else 'no error recorded'}"
            )

    logger.info(
        f"Data retention sweep complete: {len(present)}/{len(outcomes)} directories "
        f"present, {sum(len(o.removed) for o in outcomes)} entries removed "
        f"({sum(o.bytes_removed for o in outcomes) / 1024 / 1024:.1f}MB), "
        f"{sum(o.kept_live for o in outcomes)} kept as still live, "
        f"{sum(len(o.errors) for o in outcomes)} errors"
    )
    return outcomes

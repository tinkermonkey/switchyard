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
    """
    name: str
    relative_path: str
    entries: Callable[[Path], Iterable[Path]]
    description: str
    root_kind: str = 'orchestrator'

    @property
    def retention_days(self) -> int:
        return RETENTION_DAYS

    def age_cutoff(self, now: float) -> float:
        return now - (RETENTION_DAYS * 86400)


def _files_matching(pattern: str) -> Callable[[Path], Iterable[Path]]:
    def _entries(root: Path) -> Iterable[Path]:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    return _entries


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

    Safe to age at RETENTION_DAYS because every consumer of these records works
    on a far shorter horizon: the empty-output watchdog skips anything older
    than _WATCHDOG_MAX_RECORD_AGE_HOURS (24h), was_recent_programmatic_change()
    uses a 60-second window, and record_execution_start()'s in-progress guard
    only cares about entries that have not finished. Nothing reads a record
    from last month.

    The sidecars are excluded by construction rather than by a later filter:
    glob('*.yaml') would otherwise also need a name check, and getting that
    wrong deletes a lock some process is holding.
    """
    return sorted(p for p in root.glob('*.yaml') if p.is_file())


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
        entries=_dirs_two_levels_down,
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
    """
    try:
        if path.is_dir():
            mtimes = [p.stat().st_mtime for p in path.rglob('*') if p.is_file()]
            return max(mtimes) if mtimes else path.stat().st_mtime
        return path.stat().st_mtime
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
    except OSError as e:
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

        outcome.expired.append(entry)
        size = _entry_size(entry)
        if not apply:
            outcome.bytes_removed += size
            continue
        try:
            _remove(entry)
        except OSError as e:
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
    """The two roots rules hang off, with overrides for tests and the script."""
    orchestrator = Path(root) if root is not None else Path(
        os.environ.get('ORCHESTRATOR_ROOT', '/app')
    )
    if workspace_root is not None:
        workspace = Path(workspace_root)
    elif root is not None:
        # A caller that pointed us at a scratch orchestrator root means the
        # workspace-rooted rules to land under it too -- otherwise a test or a
        # --root run would sweep the REAL /workspace.
        workspace = Path(root)
    else:
        workspace = Path(WORKSPACE_ROOT)
    return {'orchestrator': orchestrator, 'workspace': workspace}


def sweep(
    root: Optional[Path] = None,
    apply: bool = False,
    rules: Iterable[RetentionRule] = RETENTION_RULES,
    now: Optional[float] = None,
    workspace_root: Optional[Path] = None,
) -> List[RuleOutcome]:
    """Run every retention rule. Never raises; failures land in the outcomes."""
    roots = resolve_roots(root, workspace_root)
    return [
        sweep_rule(rule, roots[rule.root_kind], apply=apply, now=now)
        for rule in rules
    ]


def run_scheduled_sweep(
    root: Optional[Path] = None,
    workspace_root: Optional[Path] = None,
) -> List[RuleOutcome]:
    """Entry point for the daily job in services/scheduled_tasks.py."""
    logger.info(f"Data retention sweep starting -- {RETENTION_DAYS}-day window")
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
    return outcomes

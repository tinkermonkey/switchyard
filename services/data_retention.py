"""Age-based retention for the orchestrator's write-only data directories.

Several directories were written to on every run and never read back or
cleaned. Measured on the live deployment when this was added:

    orchestrator_data/logs/            9.2 GB   two unrotated log files
    state/projects/                     31 MB   3,196 state backups, 3,006 stale
    state/execution_history/            23 MB   4,703 records, oldest Oct 2025
    orchestrator_data/metrics/         9.5 MB   191 daily files, oldest Nov 2025
    orchestrator_data/logs/container-
      failures/                        9.6 MB   76 logs, oldest July
    orchestrator_data/repair_cycles/   5.0 MB   69 per-run scratch directories

The logs are handled by monitoring/log_rotation.py and the state backups by
config/state_manager.py's STATE_BACKUP_RETENTION. This module covers the rest
-- the three whose contents are transient by nature.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
`state/execution_history/` is the empty-output watchdog's corpus and the record
that an issue was worked at all, so an age sweep over it is a behaviour change,
not housekeeping, and it belongs behind its own decision. Its 4,703 sidecar
`.yaml.lock` files are not swept either: they are 0 bytes (inodes, not space)
and deleting one that a process currently holds breaks the mutual exclusion it
exists to provide.

Everything here is age-based rather than count-based, because these are
diagnostic artifacts whose usefulness is a function of how long ago the
incident was, not of how many happened.
"""

import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, List, Optional

logger = logging.getLogger(__name__)


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(f"Ignoring unparseable {name}={raw!r}, using {default}")
        return default
    if value <= 0:
        logger.warning(f"Ignoring non-positive {name}={raw!r}, using {default}")
        return default
    return value


# Per-container diagnostic logs, written only when an agent container fails.
# Long enough to cover "what happened last week", which is the only question
# they answer.
CONTAINER_FAILURE_LOG_RETENTION_DAYS = _positive_int(
    'CONTAINER_FAILURE_LOG_RETENTION_DAYS', 14
)

# Per-run scratch for a repair-cycle container: the context file it is handed at
# launch and the result it writes back. Read once, by that container, during
# that run. Kept a month so a recent cycle can still be reconstructed by hand.
REPAIR_CYCLE_SCRATCH_RETENTION_DAYS = _positive_int(
    'REPAIR_CYCLE_SCRATCH_RETENTION_DAYS', 30
)

# Local JSONL mirror of the task/quality metrics that also go to Elasticsearch,
# where the same data is already under ILM. This is the backup copy, so it
# outlives the ES retention on purpose, but not by nine months.
METRICS_BACKUP_RETENTION_DAYS = _positive_int('METRICS_BACKUP_RETENTION_DAYS', 90)


@dataclass(frozen=True)
class RetentionRule:
    """One directory, what counts as an entry in it, and how long entries live.

    `entries` returns the things to age out, which are files in two of the
    three cases and directories in the third -- hence the explicit `remove`
    rather than assuming unlink().
    """
    name: str
    relative_path: str
    retention_days: int
    entries: Callable[[Path], Iterable[Path]]
    description: str

    def age_cutoff(self, now: float) -> float:
        return now - (self.retention_days * 86400)


def _files_matching(pattern: str) -> Callable[[Path], Iterable[Path]]:
    def _entries(root: Path) -> Iterable[Path]:
        return sorted(p for p in root.glob(pattern) if p.is_file())
    return _entries


def _repair_cycle_issue_dirs(root: Path) -> Iterable[Path]:
    """`repair_cycles/<project>/<issue>/` -- the per-run unit, not the project.

    Removing a whole project directory would delete the scratch for cycles that
    are still recent; the issue directory is the thing a single run owns.
    """
    return sorted(
        issue_dir
        for project_dir in root.iterdir() if project_dir.is_dir()
        for issue_dir in project_dir.iterdir() if issue_dir.is_dir()
    )


RETENTION_RULES = (
    RetentionRule(
        name='container_failure_logs',
        relative_path='orchestrator_data/logs/container-failures',
        retention_days=CONTAINER_FAILURE_LOG_RETENTION_DAYS,
        entries=_files_matching('*.log'),
        description='per-container diagnostic logs from failed agent runs',
    ),
    RetentionRule(
        name='repair_cycle_scratch',
        relative_path='orchestrator_data/repair_cycles',
        retention_days=REPAIR_CYCLE_SCRATCH_RETENTION_DAYS,
        entries=_repair_cycle_issue_dirs,
        description='per-run repair-cycle context/result files',
    ),
    RetentionRule(
        name='metrics_backup',
        relative_path='orchestrator_data/metrics',
        retention_days=METRICS_BACKUP_RETENTION_DAYS,
        entries=_files_matching('*.jsonl'),
        description='local JSONL mirror of metrics already in Elasticsearch',
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


def sweep(
    root: Optional[Path] = None,
    apply: bool = False,
    rules: Iterable[RetentionRule] = RETENTION_RULES,
    now: Optional[float] = None,
) -> List[RuleOutcome]:
    """Run every retention rule. Never raises; failures land in the outcomes."""
    if root is None:
        root = Path(os.environ.get('ORCHESTRATOR_ROOT', '/app'))
    return [sweep_rule(rule, Path(root), apply=apply, now=now) for rule in rules]


def run_scheduled_sweep(root: Optional[Path] = None) -> List[RuleOutcome]:
    """Entry point for the daily job in services/scheduled_tasks.py."""
    outcomes = sweep(root=root, apply=True)
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

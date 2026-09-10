#!/usr/bin/env python3
"""
Dry-Run Harness for Sweeps That Mutate Execution Records

The deliberate pre-activation check for any periodic sweep that rewrites
`state/execution_history/*.yaml` — the empty-output watchdog (#166), the stuck-
in_progress cleanup, and anything of that shape added later.

Why this exists as a script rather than as something someone does by hand: the
ad-hoc version of this check, run once during the #140 burn-down, is what caught
a watchdog gate that would have rewritten 29 of 30 genuine production successes
to 'failure' and redispatched their agents. Unit tests did not catch it and
could not have — the activated path had no coverage worth the name, and the
defect was not "the fix is wrong" but "the fix is right and the code it
activates is wrong". That distinction is only visible against real state.

What a run does, in order:

  1. Snapshots the live `state/` tree (relpath -> size + sha256).
  2. Copies it to a scratch orchestrator root and VERIFIES the copy against that
     snapshot — count and per-file content hash — before anything else runs.
  3. Repoints every state-owning singleton at the copy (see "Repointing" below)
     and ASSERTS each one's resolved directory is under the scratch root. A
     mistake here fails loudly instead of running the sweep against production.
  4. Runs the REAL sweep method — not a reimplementation of it — with its log
     output captured.
  5. Reports, per gate: records examined, how many each protection skipped and
     why, how many reached the terminal decision, and exactly which records
     would be mutated, named by project/issue/agent.
  6. Diffs the copy before/after and reports every changed file.
  7. Re-snapshots the live tree and asserts it is byte-identical, printing the
     proof (file count + aggregate manifest digest, before and after).

Repointing — there are two independent path mechanisms and ORCHESTRATOR_ROOT
alone does NOT cover both:

  * `services/work_execution_state.py`, `services/dev_container_state.py`,
    `services/pipeline_queue_manager.py` and `services/pipeline_lock_manager.py`
    all derive their state directory from ORCHESTRATOR_ROOT (default '/app') at
    singleton-construction / import time. This script sets that env var before
    importing anything from `services/`, which is why there are no orchestrator
    imports at module scope.
  * `config/state_manager.py` derives its root from
    `Path(__file__).parent.parent` with NO environment override (#181), so it
    follows the checkout the code is running from, not ORCHESTRATOR_ROOT. It is
    repointed explicitly, in place.
  * `config/manager.py` resolves `projects_dir` from `Path(__file__).parent`
    too, so running from a worktree examines the worktree's project set. A
    worktree usually has NO `config/projects/` at all (it is gitignored), which
    silently reduces a 17-project sweep to zero configured projects and makes
    every lock/queue protection degrade to "could not load project config".
    --config-root repoints it at the deployment's config by default.

Usage:
    python scripts/dry_run_state_sweep.py --list
    python scripts/dry_run_state_sweep.py --sweep empty_output_watchdog
    python scripts/dry_run_state_sweep.py --sweep empty_output_watchdog \\
        --deployment-root /app --keep-scratch --json /tmp/report.json

Exit codes:
    0  the sweep ran and the live tree is byte-identical
    1  a hard failure: copy verification failed, a manager resolved to a live
       path, or a file the sweep mutated in the copy ALSO changed live (a leak)
    2  usage error
    3  the live tree drifted in ways not attributable to this run (the
       orchestrator is running and writing its own state). Pass
       --allow-concurrent-writes to treat that as success; it is still printed.
"""

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# DELIBERATELY no orchestrator imports at module scope. Every state-owning
# singleton in this codebase resolves its directory at IMPORT time from
# ORCHESTRATOR_ROOT, so importing any of them before _repoint_runtime() has set
# that variable binds them to the LIVE production state tree -- which is exactly
# the outcome this harness exists to make impossible. Every such import happens
# inside a function, after the repoint.

_HASH_CHUNK = 1024 * 1024

logger = logging.getLogger('dry_run_state_sweep')


# ---------------------------------------------------------------------------
# Sweep registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """One protection in a sweep, and how to recognise it skipping a record.

    `patterns` are matched (re.search) against captured log messages. They are
    the sweep's OWN messages -- the point of this harness is to run the real
    method, so its accounting is read out of what it actually logged rather than
    re-derived by a parallel implementation that could disagree with it.
    """

    name: str
    why: str
    patterns: Tuple[str, ...]
    #  Some lines report that a protection did not run at all (a degraded
    #  lock/queue read). Those are counted separately: they are not skips, they
    #  are missing safety.
    degradation: bool = False


@dataclass(frozen=True)
class SweepSpec:
    """A sweep this harness knows how to dry-run."""

    name: str
    description: str
    # (scratch_state_root) -> the manager under test
    build: Callable[[Path], Any]
    # (manager) -> {label: resolved path}, every one of which MUST be under the
    # scratch root before the sweep is allowed to run.
    resolved_dirs: Callable[[Any], Dict[str, Path]]
    # (manager) -> whatever the sweep returns (a count, usually)
    run: Callable[[Any], Any]
    gates: Tuple[Gate, ...]
    # Regexes with one numeric group naming how many records the sweep examined.
    examined_patterns: Tuple[str, ...]
    # Regexes marking a record reaching the sweep's terminal decision.
    terminal_patterns: Tuple[str, ...]
    loggers: Tuple[str, ...] = ('services.work_execution_state',)
    # Effects outside state/ that a run has. Reads are informational; writes
    # require --allow-external-side-effects, because a harness whose whole
    # purpose is "prove nothing was touched" must not quietly touch something
    # it does not checksum.
    external_reads: Tuple[str, ...] = ()
    external_writes: Tuple[str, ...] = ()


def _build_execution_tracker(scratch_state_root: Path):
    """Construct WorkExecutionStateTracker against the COPY.

    state_dir is passed explicitly rather than relying on ORCHESTRATOR_ROOT: the
    env var is already set by the time this runs, but an explicit argument is
    what makes the assertion in _assert_manager_is_isolated() meaningful -- it
    checks the value the manager actually resolved, not the value we hoped it
    would resolve.
    """
    from services.work_execution_state import WorkExecutionStateTracker

    return WorkExecutionStateTracker(state_dir=scratch_state_root / 'execution_history')


def _execution_tracker_dirs(manager) -> Dict[str, Path]:
    return {'WorkExecutionStateTracker.state_dir': Path(manager.state_dir)}


_EMPTY_OUTPUT_GATES: Tuple[Gate, ...] = (
    Gate(
        name='PROTECTION 0 (age gate)',
        why='last execution older than WATCHDOG_MAX_RECORD_AGE_HOURS',
        patterns=(r'Skipping \S+/#\d+: last execution is [\d.]+h old',),
    ),
    Gate(
        name='PROTECTION 1 (active execution)',
        why='some execution for the issue is still in progress',
        patterns=(r'Skipping \S+/#\d+: work already in progress',),
    ),
    Gate(
        name='PROTECTION 2 (pipeline lock)',
        why='the board is locked, or its lock state could not be read (fails closed)',
        patterns=(
            r"Skipping \S+/#\d+: board '.*' locked by issue #\d+",
            r'Skipping \S+/#\d+: lock state for board .* could not be read',
        ),
    ),
    Gate(
        name='PROTECTION 3 (queue status)',
        why='the issue is already waiting/active in the pipeline queue',
        patterns=(r"Skipping \S+/#\d+: already '\w+' in pipeline queue",),
    ),
    Gate(
        name='PROTECTION 4 (retry eligibility)',
        why='_should_retry_failed_execution() refused, or the record names no agent/column',
        patterns=(
            r'Not eligible for retry \S+/#\d+',
            r'Missing agent or column for \S+/#\d+',
        ),
    ),
    Gate(
        name='PROTECTION 5 (recency)',
        why='the execution completed inside the 5-minute window',
        patterns=(r'Skipping \S+/#\d+: execution too recent',),
    ),
    Gate(
        name='PROTECTION 6 (GitHub output, fail-closed)',
        why='output was found, OR could not be verified -- both leave the record alone',
        patterns=(r'has GitHub output \(or it could not be verified\)',),
    ),
    Gate(
        name='DEGRADED: project config unavailable',
        why='PROTECTION 2 and 3 both ran without project config for this record',
        patterns=(r'Could not load project config for .* PROTECTION 2/3 degraded',),
        degradation=True,
    ),
    Gate(
        name='DEGRADED: protection skipped by an error',
        why='a protection raised and the sweep continued WITHOUT it',
        patterns=(
            r'PROTECTION \d+ \([^)]+\) failed for',
            r'Could not check (pipeline lock|queue status) for .* PROTECTION \d+ skipped',
        ),
        degradation=True,
    ),
    Gate(
        name='INFO: record could not be dated',
        why='the age gate was skipped for this record (deliberately not a skip)',
        patterns=(r'Could not date \S+/#\d+ .* age gate skipped',),
        degradation=True,
    ),
)


_STUCK_IN_PROGRESS_GATES: Tuple[Gate, ...] = (
    Gate(
        name='GUARD: already in pipeline queue',
        why='the issue is queued, so the in_progress entry is not orphaned',
        patterns=(r'is in pipeline queue, skipping stuck state cleanup',),
    ),
    Gate(
        name='GUARD: holds the pipeline lock',
        why='an issue holding the board lock is live work, not a stuck record',
        patterns=(r'holds pipeline lock - skipping stuck state cleanup',),
    ),
    Gate(
        name='GUARD: active review cycle',
        why='a running review cycle owns this execution',
        patterns=(r'has active review cycle .* skipping stuck state cleanup',),
    ),
    Gate(
        name='DEGRADED: a guard could not be evaluated',
        why='the guard raised and the sweep continued WITHOUT it',
        patterns=(
            r'Cleanup guard unavailable, proceeding without coordination',
            r'Failed to check (pipeline queue|pipeline lock|review cycle state)',
        ),
        degradation=True,
    ),
)


SWEEPS: Dict[str, SweepSpec] = {
    'empty_output_watchdog': SweepSpec(
        name='empty_output_watchdog',
        description=(
            "WorkExecutionStateTracker.detect_and_retry_empty_successful_executions() "
            "-- rewrites a 'success' record to 'failure' when the agent produced no "
            "visible GitHub output, which redispatches the agent. Inert today: its "
            "last gate requires completed_at, which nothing writes (#166)."
        ),
        build=_build_execution_tracker,
        resolved_dirs=_execution_tracker_dirs,
        run=lambda m: m.detect_and_retry_empty_successful_executions(),
        gates=_EMPTY_OUTPUT_GATES,
        examined_patterns=(r'Watchdog: Checking (\d+) execution state files',),
        terminal_patterns=(r'marking as failure to trigger retry',),
        external_reads=(
            'GitHub GraphQL/REST (issue comments), once per record that reaches PROTECTION 6',
        ),
    ),
    'stuck_in_progress': SweepSpec(
        name='stuck_in_progress',
        description=(
            "WorkExecutionStateTracker.cleanup_stuck_in_progress_states() -- rewrites "
            "'in_progress' records to 'failure' when their container is gone."
        ),
        build=_build_execution_tracker,
        resolved_dirs=_execution_tracker_dirs,
        run=lambda m: m.cleanup_stuck_in_progress_states(),
        gates=_STUCK_IN_PROGRESS_GATES,
        examined_patterns=(r'Checking (\d+) execution state files for stuck in_progress',),
        terminal_patterns=(r'Found stuck in_progress execution:',),
        external_reads=('docker ps, once per candidate record',),
        external_writes=(
            'Redis: services.cleanup_guard.try_claim_cleanup() sets a claim key per '
            'candidate, in the PRODUCTION Redis, which suppresses the real sweep for '
            'the claim TTL',
        ),
    ),
}


# ---------------------------------------------------------------------------
# Tree snapshotting
# ---------------------------------------------------------------------------


def file_digest(path: Path) -> str:
    """sha256 of one file's bytes."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(_HASH_CHUNK), b''):
            h.update(chunk)
    return h.hexdigest()


def snapshot_tree(root: Path) -> Dict[str, Tuple[int, str]]:
    """Map every regular file under `root` to (size, sha256), keyed by relpath.

    Symlinks are followed only when they resolve to a regular file inside the
    tree; anything else (sockets, dangling links) is skipped rather than made
    fatal -- state/ is a bind-mounted host directory and this must not become
    the reason a dry run cannot be attempted.
    """
    manifest: Dict[str, Tuple[int, str]] = {}
    root = Path(root)
    for path in sorted(root.rglob('*')):
        try:
            if not path.is_file():
                continue
            manifest[str(path.relative_to(root))] = (path.stat().st_size, file_digest(path))
        except (FileNotFoundError, PermissionError, OSError):
            # A file that vanished mid-walk is a concurrent write by the running
            # orchestrator; recorded as absent, and the before/after comparison
            # reports it as such.
            continue
    return manifest


def manifest_digest(manifest: Dict[str, Tuple[int, str]]) -> str:
    """One digest over a whole manifest -- the printable 'proof' value."""
    h = hashlib.sha256()
    for relpath in sorted(manifest):
        size, digest = manifest[relpath]
        h.update(f'{relpath}\0{size}\0{digest}\n'.encode('utf-8'))
    return h.hexdigest()


def diff_manifests(
    before: Dict[str, Tuple[int, str]], after: Dict[str, Tuple[int, str]]
) -> Dict[str, List[str]]:
    """Added / removed / changed relpaths between two manifests."""
    before_keys, after_keys = set(before), set(after)
    return {
        'added': sorted(after_keys - before_keys),
        'removed': sorted(before_keys - after_keys),
        'changed': sorted(k for k in before_keys & after_keys if before[k] != after[k]),
    }


def copy_tree_from_manifest(
    source_root: Path, dest_root: Path, manifest: Dict[str, Tuple[int, str]]
) -> Tuple[List[str], List[str]]:
    """Copy exactly the files the manifest names, retrying content mismatches.

    Copying from the manifest rather than with shutil.copytree() is what makes
    step 2's verification meaningful: the set of files copied is the set of files
    that were hashed, so a count mismatch afterwards is a real defect rather than
    a file the walk and the copy happened to disagree about.

    A file that changes between the hash and the copy is a concurrent write by
    the running orchestrator, not a harness bug, so it is retried a few times and
    reported; one that vanishes entirely is reported and dropped.

    Returns (vanished, unstable).
    """
    vanished: List[str] = []
    unstable: List[str] = []

    for relpath, (_size, expected_digest) in manifest.items():
        src = source_root / relpath
        dest = dest_root / relpath
        dest.parent.mkdir(parents=True, exist_ok=True)

        settled = False
        for _attempt in range(3):
            try:
                shutil.copy2(src, dest)
            except FileNotFoundError:
                vanished.append(relpath)
                settled = True
                break
            if file_digest(dest) == expected_digest:
                settled = True
                break
            try:
                # Re-baseline on the source: if the source itself moved on, the
                # copy is still a faithful copy of *something*, just not of the
                # snapshot this run started from. Re-hashing keeps the manifest
                # and the copy in agreement; failing to settle after three tries
                # is recorded as unstable rather than silently accepted.
                expected_digest = file_digest(src)
                manifest[relpath] = (src.stat().st_size, expected_digest)
            except (FileNotFoundError, OSError):
                vanished.append(relpath)
                settled = True
                break

        if not settled:
            unstable.append(relpath)

    for relpath in vanished:
        manifest.pop(relpath, None)

    return vanished, unstable


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------


class IsolationError(RuntimeError):
    """A path that must have been inside the scratch root was not.

    Always fatal, always before the sweep runs: the whole value of this harness
    is that a mistake here is loud rather than a production mutation.
    """


def is_under(path: Path, root: Path) -> bool:
    """True when `path` is `root` or lives inside it."""
    try:
        resolved = Path(path).resolve()
        root_resolved = Path(root).resolve()
    except OSError:
        return False
    return resolved == root_resolved or root_resolved in resolved.parents


def assert_under(path: Path, root: Path, what: str) -> None:
    if not is_under(path, root):
        raise IsolationError(
            f"{what} resolved to {Path(path).resolve()}, which is NOT under the "
            f"scratch root {Path(root).resolve()} -- refusing to run the sweep. "
            f"This is the mistake the harness exists to catch: the sweep would "
            f"have mutated live state."
        )


def repoint_runtime(scratch_root: Path, config_root: Path) -> Dict[str, str]:
    """Point every state-owning path mechanism at the scratch copy.

    Must run BEFORE any `services.*` import -- see the module docstring. Returns
    a label -> resolved-path map for the report, so the isolation is something an
    operator reads rather than something they take on faith.
    """
    os.environ['ORCHESTRATOR_ROOT'] = str(scratch_root)

    resolved: Dict[str, str] = {'ORCHESTRATOR_ROOT': str(scratch_root)}

    # config/manager.py: config_root comes from Path(__file__).parent, i.e. the
    # checkout this code runs from. From a worktree that is usually a config/
    # with no projects/ directory at all, which turns every project lookup into
    # a "could not load project config" degradation and silently shrinks the
    # sweep. Repoint the whole config root, caches included.
    from config.manager import config_manager

    config_manager.config_root = Path(config_root)
    config_manager.foundations_dir = Path(config_root) / 'foundations'
    config_manager.projects_dir = Path(config_root) / 'projects'
    config_manager._agents = None
    config_manager._mcp_servers = None
    config_manager._pipeline_templates = None
    config_manager._workflow_templates = None
    config_manager._project_configs = {}
    resolved['ConfigManager.projects_dir'] = str(config_manager.projects_dir)
    resolved['ConfigManager.foundations_dir'] = str(config_manager.foundations_dir)

    # config/state_manager.py derives its root from Path(__file__).parent.parent
    # with NO environment override (#181), so ORCHESTRATOR_ROOT above does not
    # move it. Mutated IN PLACE rather than replaced: modules that already did
    # `from config.state_manager import state_manager` hold a reference to this
    # very object, and rebinding the module attribute would leave them pointed
    # at the live tree.
    from config import state_manager as state_manager_module

    live_state_manager = state_manager_module.state_manager
    live_state_manager.state_root = scratch_root / 'state'
    live_state_manager.projects_state_dir = scratch_root / 'state' / 'projects'
    live_state_manager.orchestrator_state_dir = scratch_root / 'state' / 'orchestrator'
    live_state_manager.projects_state_dir.mkdir(parents=True, exist_ok=True)
    live_state_manager.orchestrator_state_dir.mkdir(parents=True, exist_ok=True)
    live_state_manager.config_manager = config_manager
    resolved['GitHubStateManager.state_root'] = str(live_state_manager.state_root)

    return resolved


#  (module, singleton attribute, path attribute, subdirectory of state/)
_RUNTIME_SINGLETONS: Tuple[Tuple[str, str, str, str], ...] = (
    ('services.work_execution_state', 'work_execution_tracker', 'state_dir', 'execution_history'),
    ('services.dev_container_state', 'dev_container_state', 'state_dir', 'dev_containers'),
)


def bind_runtime_singletons(scratch_root: Path) -> Dict[str, str]:
    """Point the import-time singletons at the copy, forcing any that predate it.

    These bind ORCHESTRATOR_ROOT the moment their module is imported, so in this
    script's own process -- which has no orchestrator imports at module scope --
    importing them here is enough. A process that already imported one before
    repoint_runtime() ran (a test runner, an interactive session) holds a
    singleton still pointed at whatever root was in effect then, which is the
    live tree for anything running inside the orchestrator container. Those are
    rewritten in place rather than merely reported: an isolation check that a
    caller can defeat by importing a module in the wrong order is not a check.
    """
    import importlib

    resolved: Dict[str, str] = {}

    for module_name, singleton_name, path_attr, subdir in _RUNTIME_SINGLETONS:
        try:
            module = importlib.import_module(module_name)
            singleton = getattr(module, singleton_name)
        except Exception as e:  # pragma: no cover - optional dependency chain
            logger.debug(f"{module_name}.{singleton_name} not importable, not bound: {e}")
            continue

        label = f'{module_name}.{singleton_name}.{path_attr}'
        current = Path(getattr(singleton, path_attr))
        if not is_under(current, scratch_root):
            forced = scratch_root / 'state' / subdir
            forced.mkdir(parents=True, exist_ok=True)
            setattr(singleton, path_attr, forced)
            logger.info(
                f'{label} was already bound to {current} (imported before the '
                f'repoint) -- forced onto the scratch copy at {forced}'
            )
            current = forced
        resolved[label] = str(current)

    from config.state_manager import state_manager

    resolved['config.state_manager.state_manager.state_root'] = str(state_manager.state_root)

    return resolved


# ---------------------------------------------------------------------------
# Log capture and gate accounting
# ---------------------------------------------------------------------------


class _CapturingHandler(logging.Handler):
    """Collects the sweep's own log records so its accounting can be read back."""

    def __init__(self, logger_names: Tuple[str, ...]):
        super().__init__(level=logging.DEBUG)
        self._logger_names = logger_names
        self.records: List[Tuple[str, str, str]] = []

    def emit(self, record: logging.LogRecord) -> None:
        if not any(
            record.name == name or record.name.startswith(name + '.')
            for name in self._logger_names
        ):
            return
        try:
            message = record.getMessage()
        except Exception:  # pragma: no cover - a broken format string
            message = str(record.msg)
        self.records.append((record.name, record.levelname, message))


def classify_records(
    spec: SweepSpec, records: List[Tuple[str, str, str]]
) -> Dict[str, Any]:
    """Bucket captured log lines by gate, and pull out the sweep's own counts."""
    compiled = [
        (gate, tuple(re.compile(p) for p in gate.patterns)) for gate in spec.gates
    ]
    examined_res = [re.compile(p) for p in spec.examined_patterns]
    terminal_res = [re.compile(p) for p in spec.terminal_patterns]

    gate_hits: Dict[str, List[str]] = {gate.name: [] for gate in spec.gates}
    examined: Optional[int] = None
    terminal: List[str] = []
    errors: List[str] = []
    unclassified: List[str] = []

    for _name, level, message in records:
        if examined is None:
            match = next(
                (m for m in (p.search(message) for p in examined_res) if m), None
            )
            if match:
                examined = int(match.group(1))
                continue

        if any(pattern.search(message) for pattern in terminal_res):
            terminal.append(message)
            continue

        for gate, patterns in compiled:
            if any(pattern.search(message) for pattern in patterns):
                gate_hits[gate.name].append(message)
                break
        else:
            if level in ('ERROR', 'CRITICAL'):
                errors.append(message)
            elif level in ('WARNING',):
                unclassified.append(f'[{level}] {message}')
            else:
                unclassified.append(message)

    return {
        'examined': examined,
        'gate_hits': gate_hits,
        'terminal': terminal,
        'errors': errors,
        'unclassified': unclassified,
    }


# ---------------------------------------------------------------------------
# Mutation attribution
# ---------------------------------------------------------------------------


def describe_execution_record_changes(
    before_root: Path, after_root: Path, relpaths: List[str]
) -> List[Dict[str, Any]]:
    """Name every changed execution record by project/issue/agent.

    Derived from the before/after content of the COPY, not from the sweep's log
    output: this is the answer the acceptance criterion actually turns on, and it
    must not depend on the sweep having decided to log what it did.
    """
    import yaml

    described: List[Dict[str, Any]] = []

    for relpath in relpaths:
        entry: Dict[str, Any] = {'file': relpath}
        try:
            before_path, after_path = before_root / relpath, after_root / relpath
            before_state = (
                yaml.safe_load(before_path.read_text()) if before_path.exists() else None
            )
            after_state = (
                yaml.safe_load(after_path.read_text()) if after_path.exists() else None
            )
        except Exception as e:
            entry['error'] = f'could not parse: {e}'
            described.append(entry)
            continue

        if not isinstance(after_state, dict):
            entry['error'] = 'not a YAML mapping after the sweep'
            described.append(entry)
            continue

        entry['project'] = after_state.get('project_name')
        entry['issue_number'] = after_state.get('issue_number')

        before_history = (before_state or {}).get('execution_history') or []
        after_history = after_state.get('execution_history') or []

        changes: List[Dict[str, Any]] = []
        for index, after_exec in enumerate(after_history):
            before_exec = before_history[index] if index < len(before_history) else None
            if before_exec == after_exec:
                continue
            fields = {}
            keys = set(after_exec or {}) | set(before_exec or {})
            for key in sorted(keys):
                old = (before_exec or {}).get(key)
                new = (after_exec or {}).get(key)
                if old != new:
                    fields[key] = {'before': old, 'after': new}
            changes.append(
                {
                    'index': index,
                    'agent': (after_exec or {}).get('agent'),
                    'column': (after_exec or {}).get('column'),
                    'task_id': (after_exec or {}).get('task_id'),
                    'timestamp': (after_exec or {}).get('timestamp'),
                    'new_record': before_exec is None,
                    'fields': fields,
                }
            )

        entry['record_changes'] = changes
        described.append(entry)

    return described


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _out(line: str = '') -> None:
    print(line, flush=True)


def _rule(title: str) -> None:
    _out()
    _out(f'== {title} ' + '=' * max(0, 74 - len(title)))


def print_report(report: Dict[str, Any]) -> None:
    spec_name = report['sweep']
    _rule(f'DRY RUN: {spec_name}')
    _out(report['description'])
    _out()
    _out(f"live state tree : {report['live_state_root']}")
    _out(f"scratch root    : {report['scratch_root']}")
    _out(f"config root     : {report['config_root']}")

    _rule('1-2. COPY VERIFICATION')
    copy_info = report['copy']
    _out(f"files snapshotted from live : {copy_info['live_file_count']}")
    _out(f"files verified in the copy  : {copy_info['copy_file_count']}")
    _out(f"live manifest digest        : {copy_info['live_manifest_digest']}")
    _out(f"copy manifest digest        : {copy_info['copy_manifest_digest']}")
    if copy_info['vanished']:
        _out(f"vanished mid-copy (concurrent writes): {len(copy_info['vanished'])}")
        for relpath in copy_info['vanished'][:20]:
            _out(f"    - {relpath}")
    if copy_info['unstable']:
        _out(f"UNSTABLE mid-copy (never hashed the same twice): {len(copy_info['unstable'])}")
        for relpath in copy_info['unstable'][:20]:
            _out(f"    - {relpath}")
    _out(f"VERDICT: {copy_info['verdict']}")

    _rule('3. ISOLATION')
    for label, path in sorted(report['isolation'].items()):
        _out(f"  {label} = {path}")
    _out(f"VERDICT: {report['isolation_verdict']}")

    _rule('4-5. SWEEP RESULT, PER GATE')
    _out(f"sweep return value          : {report['sweep_return']!r}")
    _out(f"state files present in copy : {report['state_files_in_copy']}")
    examined = report['classification']['examined']
    _out(f"records examined (sweep's own count) : "
         f"{examined if examined is not None else 'not reported by this sweep'}")
    _out()
    for gate in report['gates']:
        marker = 'DEGRADED' if gate['degradation'] else 'skipped '
        _out(f"  {marker} {gate['count']:>6}  {gate['name']}")
        _out(f"           {'':>6}  why: {gate['why']}")
    _out()
    unaccounted = report.get('unaccounted')
    if unaccounted is not None:
        _out(f"  {unaccounted:>6}  dropped before the first named gate by the sweep's own "
             f"pre-gate filters (e.g. 'last execution is not a success') -- NOT protected, "
             f"just never a candidate")
    _out()
    _out(f"  reached terminal decision: {len(report['classification']['terminal'])}")
    for message in report['classification']['terminal'][:50]:
        _out(f"      * {message}")
    if report['classification']['errors']:
        _out()
        _out(f"  ERRORS raised inside the sweep: {len(report['classification']['errors'])}")
        for message in report['classification']['errors'][:20]:
            _out(f"      ! {message}")
    if report['classification']['unclassified']:
        _out()
        _out(f"  unclassified sweep log lines: "
             f"{len(report['classification']['unclassified'])} (shown verbatim so "
             f"nothing hides in a bucket that does not exist yet)")
        for message in report['classification']['unclassified'][:20]:
            _out(f"      ? {message}")

    _rule('5b. RECORDS THIS SWEEP WOULD MUTATE')
    mutations = report['mutations']
    if not mutations:
        _out('  none -- the sweep changed no execution record in the copy')
    for entry in mutations:
        _out(f"  {entry.get('project')}/#{entry.get('issue_number')}  ({entry['file']})")
        if entry.get('error'):
            _out(f"      ! {entry['error']}")
        for change in entry.get('record_changes', []):
            _out(
                f"      agent={change['agent']} column={change['column']} "
                f"task_id={change['task_id']} started={change['timestamp']}"
                + ('  [NEW RECORD]' if change['new_record'] else '')
            )
            for key, delta in change['fields'].items():
                _out(f"          {key}: {delta['before']!r} -> {delta['after']!r}")

    _rule('6. COPY DIFF (before -> after)')
    copy_diff = report['copy_diff']
    for kind in ('added', 'removed', 'changed'):
        _out(f"  {kind}: {len(copy_diff[kind])}")
        for relpath in copy_diff[kind][:100]:
            _out(f"      {relpath}")

    _rule('7. PROOF THE LIVE TREE WAS NOT TOUCHED')
    live = report['live_check']
    _out(f"  before : {live['before_count']} files, digest {live['before_digest']}")
    _out(f"  after  : {live['after_count']} files, digest {live['after_digest']}")
    if live['identical']:
        _out('  VERDICT: BYTE-IDENTICAL -- production state was not modified')
    else:
        _out('  VERDICT: LIVE TREE CHANGED')
        for kind in ('added', 'removed', 'changed'):
            paths = live['diff'][kind]
            if paths:
                _out(f"    {kind}: {len(paths)}")
                for relpath in paths[:100]:
                    leak = ' <-- ALSO MUTATED IN THE COPY: ATTRIBUTABLE TO THIS RUN' \
                        if relpath in live['leaked'] else ''
                    _out(f"        {relpath}{leak}")
        if live['leaked']:
            _out('  ** LEAK: the sweep mutated these paths live. This run was NOT isolated. **')
        else:
            _out('  (no changed path was one this sweep mutated in the copy -- consistent '
                 'with concurrent writes by the running orchestrator, but NOT proof)')

    _rule('VERDICT')
    _out(f"  {report['verdict']}  (exit {report['exit_code']})")
    _out()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def run_dry_run(
    spec: SweepSpec,
    deployment_root: Path,
    scratch_root: Path,
    config_root: Path,
    allow_concurrent_writes: bool = False,
) -> Dict[str, Any]:
    """Execute the seven steps. Returns the report dict; prints nothing."""
    live_state_root = deployment_root / 'state'
    scratch_state_root = scratch_root / 'state'

    report: Dict[str, Any] = {
        'sweep': spec.name,
        'description': spec.description,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'live_state_root': str(live_state_root),
        'scratch_root': str(scratch_root),
        'config_root': str(config_root),
    }

    # --- 1. snapshot live -------------------------------------------------
    live_before = snapshot_tree(live_state_root)

    # --- 2. copy and verify ----------------------------------------------
    scratch_state_root.mkdir(parents=True, exist_ok=True)
    vanished, unstable = copy_tree_from_manifest(
        live_state_root, scratch_state_root, live_before
    )
    copy_manifest = snapshot_tree(scratch_state_root)

    copy_ok = copy_manifest == live_before and not unstable
    report['copy'] = {
        'live_file_count': len(live_before),
        'copy_file_count': len(copy_manifest),
        'live_manifest_digest': manifest_digest(live_before),
        'copy_manifest_digest': manifest_digest(copy_manifest),
        'vanished': vanished,
        'unstable': unstable,
        'verdict': 'copy verified byte-for-byte' if copy_ok else 'COPY VERIFICATION FAILED',
    }
    if not copy_ok:
        mismatch = diff_manifests(live_before, copy_manifest)
        report['copy']['mismatch'] = mismatch
        report['isolation'] = {}
        report['isolation_verdict'] = 'not evaluated -- copy verification failed'
        report['verdict'] = 'ABORTED: the scratch copy is not a faithful copy of live state'
        report['exit_code'] = 1
        report['gates'] = []
        report['classification'] = {
            'examined': None, 'gate_hits': {}, 'terminal': [], 'errors': [], 'unclassified': []
        }
        report['mutations'] = []
        report['copy_diff'] = {'added': [], 'removed': [], 'changed': []}
        report['sweep_return'] = None
        report['state_files_in_copy'] = len(copy_manifest)
        report['live_check'] = {
            'before_count': len(live_before),
            'before_digest': report['copy']['live_manifest_digest'],
            'after_count': len(live_before),
            'after_digest': report['copy']['live_manifest_digest'],
            'identical': True,
            'diff': {'added': [], 'removed': [], 'changed': []},
            'leaked': [],
        }
        return report

    # A pristine second copy, kept aside, so step 5b can diff record CONTENT
    # rather than only file hashes. The sweep is about to rewrite the copy in
    # place, and "which records changed and how" is the answer the whole run
    # exists to produce.
    pristine_root = scratch_root / 'pristine'
    pristine_root.mkdir(parents=True, exist_ok=True)
    copy_tree_from_manifest(scratch_state_root, pristine_root, dict(copy_manifest))

    # --- 3. repoint and assert isolation ----------------------------------
    resolved = repoint_runtime(scratch_root, config_root)
    manager = spec.build(scratch_state_root)
    resolved.update(
        {label: str(path) for label, path in spec.resolved_dirs(manager).items()}
    )
    resolved.update(bind_runtime_singletons(scratch_root))
    report['isolation'] = resolved

    for label, path in resolved.items():
        if label == 'ORCHESTRATOR_ROOT' or label.startswith('ConfigManager.'):
            continue
        assert_under(Path(path), scratch_root, label)
    # The config root is the one path that must deliberately NOT be scratch: the
    # sweep has to see the deployment's real project set. It is read-only for
    # every sweep registered here, and the live-tree check below covers state/.
    if not Path(config_root).is_dir():
        raise IsolationError(f'config root {config_root} does not exist')
    report['isolation_verdict'] = (
        'every state-owning path resolved inside the scratch root'
    )

    # --- 4. run the real sweep -------------------------------------------
    handler = _CapturingHandler(spec.loggers)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    for name in spec.loggers:
        logging.getLogger(name).setLevel(logging.DEBUG)
    try:
        sweep_return = spec.run(manager)
    finally:
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)

    report['sweep_return'] = sweep_return
    report['state_files_in_copy'] = len(copy_manifest)

    # --- 5. per-gate accounting ------------------------------------------
    classification = classify_records(spec, handler.records)
    report['classification'] = classification
    report['gates'] = [
        {
            'name': gate.name,
            'why': gate.why,
            'degradation': gate.degradation,
            'count': len(classification['gate_hits'].get(gate.name, [])),
        }
        for gate in spec.gates
    ]

    # Close the arithmetic. Every sweep drops records before its first named
    # gate -- detect_and_retry_empty_successful_executions() silently `continue`s
    # on any record whose last execution is not a 'success' -- and those are
    # invisible in a per-gate table. Reporting the residual explicitly is what
    # stops "0 reached the terminal decision" from being read as "every record
    # was individually considered and protected".
    if classification['examined'] is not None:
        accounted = sum(gate['count'] for gate in report['gates']) + len(
            classification['terminal']
        )
        report['unaccounted'] = classification['examined'] - accounted
    else:
        report['unaccounted'] = None

    # --- 6. diff the copy -------------------------------------------------
    copy_after = snapshot_tree(scratch_state_root)
    copy_diff = diff_manifests(copy_manifest, copy_after)
    report['copy_diff'] = copy_diff
    report['mutations'] = describe_execution_record_changes(
        pristine_root, scratch_state_root, copy_diff['changed'] + copy_diff['added']
    )

    # --- 7. prove the live tree is untouched ------------------------------
    live_after = snapshot_tree(live_state_root)
    live_diff = diff_manifests(live_before, live_after)
    changed_live = set(live_diff['added']) | set(live_diff['removed']) | set(live_diff['changed'])
    mutated_in_copy = set(copy_diff['changed']) | set(copy_diff['added']) | set(copy_diff['removed'])
    leaked = sorted(changed_live & mutated_in_copy)

    report['live_check'] = {
        'before_count': len(live_before),
        'before_digest': manifest_digest(live_before),
        'after_count': len(live_after),
        'after_digest': manifest_digest(live_after),
        'identical': not changed_live,
        'diff': live_diff,
        'leaked': leaked,
    }

    if leaked:
        report['verdict'] = (
            'FAILED: the sweep mutated live state -- the run was not isolated'
        )
        report['exit_code'] = 1
    elif not changed_live:
        report['verdict'] = 'PASSED: live state is byte-identical'
        report['exit_code'] = 0
    elif allow_concurrent_writes:
        report['verdict'] = (
            'PASSED WITH DRIFT: live state changed, but no changed path is one this '
            'sweep mutated (--allow-concurrent-writes)'
        )
        report['exit_code'] = 0
    else:
        report['verdict'] = (
            'UNVERIFIED: live state changed. No changed path is one this sweep '
            'mutated, so this is consistent with the running orchestrator writing '
            'its own state -- but it is not proof. Re-run against a stopped '
            'orchestrator, or pass --allow-concurrent-writes.'
        )
        report['exit_code'] = 3

    return report


def _resolve_deployment_root(explicit: Optional[str]) -> Path:
    """Where the LIVE state/ and config/ live.

    Not derived from ORCHESTRATOR_ROOT: this harness overwrites that variable,
    and a test runner may already have pointed it at a scratch directory (#181),
    so trusting it here would snapshot the wrong tree and then "prove" it
    unchanged.
    """
    if explicit:
        return Path(explicit).resolve()
    if Path('/app/state').is_dir():
        return Path('/app')
    return Path(__file__).resolve().parent.parent


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            'Dry-run a state sweep against a verified copy of the live state tree, '
            'and prove production was not touched.'
        )
    )
    parser.add_argument('--sweep', help='Name of the registered sweep to dry-run')
    parser.add_argument(
        '--list', action='store_true', help='List the registered sweeps and exit'
    )
    parser.add_argument(
        '--deployment-root',
        help=(
            'Root of the LIVE deployment (the checkout holding state/ and config/). '
            "Defaults to /app when it exists, else this script's own checkout."
        ),
    )
    parser.add_argument(
        '--config-root',
        help=(
            'Config directory the sweep should read projects/foundations from. '
            'Defaults to <deployment-root>/config -- NOT the running checkout, '
            'which from a worktree usually has no projects/ at all.'
        ),
    )
    parser.add_argument('--scratch', help='Scratch directory (default: a fresh temp dir)')
    parser.add_argument(
        '--keep-scratch',
        action='store_true',
        help='Keep the scratch copy after the run (it holds the mutated records)',
    )
    parser.add_argument(
        '--allow-concurrent-writes',
        action='store_true',
        help=(
            'Treat live-tree drift that is NOT attributable to this run as success. '
            'A leak -- a live path the sweep also mutated in the copy -- still fails.'
        ),
    )
    parser.add_argument(
        '--allow-external-side-effects',
        action='store_true',
        help='Required for sweeps that write outside state/ (e.g. Redis claim keys)',
    )
    parser.add_argument('--json', dest='json_path', help='Also write the report as JSON here')
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    if args.list:
        for name, spec in sorted(SWEEPS.items()):
            _out(f'{name}')
            _out(f'    {spec.description}')
            for note in spec.external_reads:
                _out(f'    reads outside state/: {note}')
            for note in spec.external_writes:
                _out(f'    WRITES outside state/: {note}')
        return 0

    if not args.sweep:
        parser.error('--sweep is required (or --list)')

    spec = SWEEPS.get(args.sweep)
    if spec is None:
        parser.error(
            f"unknown sweep {args.sweep!r}; known: {', '.join(sorted(SWEEPS))}"
        )

    if spec.external_writes and not args.allow_external_side_effects:
        _out(f"REFUSING to run {spec.name}: it writes outside state/, which this "
             f"harness does not checksum and cannot prove it left alone:")
        for note in spec.external_writes:
            _out(f'    - {note}')
        _out('Pass --allow-external-side-effects if that is acceptable.')
        return 2

    deployment_root = _resolve_deployment_root(args.deployment_root)
    if not (deployment_root / 'state').is_dir():
        _out(f'No state/ directory under deployment root {deployment_root}')
        return 2

    config_root = Path(args.config_root).resolve() if args.config_root \
        else deployment_root / 'config'

    scratch_root = Path(args.scratch).resolve() if args.scratch else Path(
        tempfile.mkdtemp(prefix='switchyard-dry-run-')
    )
    scratch_root.mkdir(parents=True, exist_ok=True)

    try:
        report = run_dry_run(
            spec,
            deployment_root=deployment_root,
            scratch_root=scratch_root,
            config_root=config_root,
            allow_concurrent_writes=args.allow_concurrent_writes,
        )
    except IsolationError as e:
        _out()
        _out('!! ISOLATION CHECK FAILED -- the sweep was NOT run !!')
        _out(str(e))
        return 1

    print_report(report)

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2, default=str))
        _out(f'JSON report written to {args.json_path}')

    if args.keep_scratch:
        _out(f'Scratch copy kept at {scratch_root}')
    else:
        shutil.rmtree(scratch_root, ignore_errors=True)

    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())

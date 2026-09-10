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
  4. Neutralizes the effects it cannot checksum (see "External effects"), then
     runs the REAL sweep method — not a reimplementation of it — with its log
     output captured.
  5. Reports, per RECORD: how many the sweep examined, how many each protection
     skipped and why, how many reached the terminal decision, and exactly which
     records would be mutated, named by project/issue/agent.
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
  * Some writers build a path relative to the process CWD rather than any root
    at all (`services/review_cycle.py` does `os.path.join('state', ...)`), so
    the sweep runs with the CWD set to the scratch root.

External effects — the harness checksums `state/` and nothing else, so anything
a sweep writes elsewhere is, by construction, something it cannot prove it left
alone. Both registered sweeps write to production Redis, to the production
observability stream, and -- through get_pipeline_run_manager(), whose __init__
PUTs an ILM policy and an index template before it does anything else -- to the
production Elasticsearch cluster; the stuck sweep's Redis writes include
DELETING a real agent's persisted result. Those effects are therefore
NEUTRALIZED by default: Redis and Elasticsearch reads still go to the live
servers (so every guard sees the truth it would see in production), their WRITES
are intercepted and recorded, and observability emission is replaced by a
recorder. Section 4b of the report lists exactly what was intercepted.
`--no-neutralize-external-effects` runs them for real, and then a sweep with
declared writes is refused unless `--allow-external-side-effects`.

Neutralizing a write can disable a guard that is built on one -- the cleanup
coordination claim is a SET NX, and a neutralized SET NX always reports a
successful claim. Such a guard is declared in the spec's `inert_guards` and its
row in section 5 is marked as structurally unreachable, so its `0` is never read
as a measurement.

Usage:
    python scripts/dry_run_state_sweep.py --list
    python scripts/dry_run_state_sweep.py --sweep empty_output_watchdog
    python scripts/dry_run_state_sweep.py --sweep empty_output_watchdog \\
        --deployment-root /app --keep-scratch --json /tmp/report.json

Exit codes:
    0  the sweep ran and no live state file changed (a `<state file>.lock` flock
       artifact carries no state and is reported, not counted against this)
    1  a hard failure: copy verification failed, a manager resolved to a live
       path, or the sweep's own work showed up in the live tree (a leak)
    2  usage error
    3  the run could not be verified: the live tree drifted in ways not
       attributable to this run (the orchestrator is running and writing its own
       state), or a file could not be read. Pass --allow-concurrent-writes to
       treat drift OUTSIDE the sweep's own subtrees as success; drift INSIDE
       them cannot be distinguished from a leak by path and is never downgraded.
    4  the sweep itself raised. Steps 5-7 still ran and are printed, so the
       live-tree proof for the partial run is still available.
"""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
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

#  Every sweep message that names a record names it the same way. Attributing
#  log lines to RECORDS rather than counting lines is what keeps the per-gate
#  table and the residual arithmetic honest: several protections log an
#  annotation and then fall through to a later gate, so one record legitimately
#  produces two or three lines.
_RECORD_KEY_RE = re.compile(r'([A-Za-z0-9_.\-]+)/#(\d+)')

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
    #  lock/queue read) or annotate a record another gate goes on to skip.
    #  Those are counted separately and are NEVER summed into the accounting:
    #  they are not dispositions, they are missing safety on a record some
    #  other bucket already owns.
    degradation: bool = False
    #  Why this gate cannot fire while external effects are neutralized. A gate
    #  whose count is structurally 0 must say so on its own row: '0' next to a
    #  named protection reads as a MEASUREMENT ("no record needed it"), and the
    #  wrong-but-plausible reassurance is exactly what this harness exists to
    #  prevent. Empty for every gate that really is measured.
    inert_when_neutralized: str = ''


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
    # Regexes marking a record reaching the sweep's terminal decision. These
    # MUST match a line the sweep logs AFTER every guard -- a pre-guard line
    # reports candidates entering the chain, not decisions, and counting it here
    # inflates the one number an operator uses to size the blast radius.
    terminal_patterns: Tuple[str, ...]
    # Regexes marking a record ENTERING the guard chain. Reported separately, so
    # a candidate that leaves the chain through a bare `continue` shows up as
    # "entered and left undecided" rather than as "never a candidate".
    candidate_patterns: Tuple[str, ...] = ()
    loggers: Tuple[str, ...] = ('services.work_execution_state',)
    # Effects outside state/ that a run has. Reads are informational; writes are
    # neutralized by default (see the module docstring) and, when neutralization
    # is turned off, require --allow-external-side-effects, because a harness
    # whose whole purpose is "prove nothing was touched" must not quietly touch
    # something it does not checksum.
    external_reads: Tuple[str, ...] = ()
    external_writes: Tuple[str, ...] = ()
    # Subdirectories of state/ this sweep writes. ANY live change under one of
    # these is treated as unverifiable rather than as third-party drift: a sweep
    # that escapes its isolation writes ONLY the live path and never the copy,
    # so a path-intersection leak test cannot see it.
    owned_state_subtrees: Tuple[str, ...] = ()
    # Guards that read in-process state owned by the RUNNING orchestrator (an
    # in-memory dict on a singleton). A fresh harness process sees them empty by
    # construction, so they can never fire here and the dry run is strictly LESS
    # protected than production for the records they would have skipped.
    inert_guards: Tuple[str, ...] = ()


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
        name='GUARD: cleanup already claimed',
        why='another cleanup mechanism holds the coordination claim for this issue',
        patterns=(r'Cleanup for \S+/#\d+ already claimed by',),
        inert_when_neutralized=(
            "try_claim_cleanup()'s SET NX is neutralized to return True, so the claim "
            'always looks taken by THIS run and the "already claimed by" line is '
            'unreachable -- this count is 0 by construction, not by measurement'
        ),
    ),
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
        name='GUARD: active feedback loop',
        why='a running human feedback loop owns this execution',
        patterns=(r'has active feedback loop - skipping stuck state cleanup',),
    ),
    Gate(
        name='GUARD: a guard raised, record skipped (fail-safe)',
        why=(
            'the lock / review-cycle / feedback-loop guard could not be evaluated, so the '
            'record was SKIPPED rather than cleaned up -- the safest outcome, not a degradation'
        ),
        patterns=(
            r'Failed to check (pipeline lock|review cycle state|feedback loop state) for',
        ),
    ),
    #  The last two dispositions in the chain, and on a busy orchestrator the
    #  most common ones. Both are logged AFTER every named guard, so a record
    #  that reaches either has been individually considered and left alone --
    #  without a gate of its own each landed in `undecided_candidates`, whose
    #  printed explanation names a cleanup claim it cannot have been.
    Gate(
        name='GUARD: container still running',
        why=(
            "docker ps found a live container for the issue, so the record is not stuck -- "
            "the sweep's strongest protection, and the last one in the chain"
        ),
        patterns=(r'Agent container still running for \S+/#\d+',),
    ),
    Gate(
        name='DEFERRED: dev container state not reconcilable yet',
        why=(
            'a stuck dev_environment_setup/verifier record whose dev container state '
            'could not be reconciled under the dev_container_build lock -- left '
            'in_progress on purpose for the next pass rather than consumed'
        ),
        patterns=(r'Leaving stuck \S+ execution \S+/#\d+ as in_progress',),
    ),
    Gate(
        name='DEGRADED: a guard could not be evaluated',
        why=(
            'the queue check or the cleanup claim raised and the sweep continued WITHOUT '
            'that protection -- these two fall through rather than skipping'
        ),
        patterns=(
            r'Cleanup guard unavailable, proceeding without coordination',
            r'Failed to check pipeline queue',
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
            'Elasticsearch: pipeline-runs-* search, per record that reaches PROTECTION 4 '
            '(get_active_pipeline_run() falls through to ES on a Redis mapping miss; it is '
            'called with restore_to_redis=False so the hit is not written back)',
        ),
        external_writes=(
            'Elasticsearch: PipelineRunManager.__init__ PUTs the "pipeline-runs-ilm-policy" '
            'ILM policy and the "pipeline-runs-template" index template into the production '
            'cluster the first time PROTECTION 4 calls get_pipeline_run_manager(). Idempotent, '
            'but it is a write to a datastore this harness does not checksum',
            'Observability: EventType.RETRY_ATTEMPTED per record that reaches the terminal '
            'decision, into the production event stream and Elasticsearch '
            '(services/work_execution_state.py, end of the retry branch). Inert while #166 '
            'keeps the last gate closed -- and fired once per candidate on the very run that '
            'dry-runs the FIXED sweep, which is what this harness is for',
            'Redis: the GitHub API client caches its issue-comment reads, so PROTECTION 6 '
            'writes cache entries in the production Redis',
        ),
        owned_state_subtrees=('execution_history',),
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
        # Both terminal lines are logged only after every guard has passed. The
        # 'Found stuck in_progress execution:' line is logged BEFORE the claim
        # and before all five guards, so it is a candidate marker, not a
        # decision -- counting it as terminal reported every skipped record as
        # one the sweep would rewrite.
        terminal_patterns=(
            r'Marked stuck execution as failed:',
            r'Reconciled successful execution from Redis:',
        ),
        candidate_patterns=(r'Found stuck in_progress execution:',),
        loggers=('services.work_execution_state', 'services.cleanup_guard'),
        external_reads=(
            'docker ps / docker inspect, once per candidate record',
            'Elasticsearch: pipeline-runs-* search, per record reaching the failure branch '
            '(get_active_pipeline_run(restore_to_redis=False), for an id to stamp on an event)',
        ),
        external_writes=(
            'Elasticsearch: PipelineRunManager.__init__ PUTs the "pipeline-runs-ilm-policy" '
            'ILM policy and the "pipeline-runs-template" index template into the production '
            'cluster the first time the failure branch calls get_pipeline_run_manager(). '
            'Idempotent, but it is a write to a datastore this harness does not checksum',
            'Redis: services.cleanup_guard.try_claim_cleanup() sets a claim key per '
            'candidate, in the PRODUCTION Redis, which suppresses the real sweep for the '
            'claim TTL -- and suppresses a SECOND dry run within that TTL, which would then '
            'report the sweep as inert',
            'Redis: DELETES agent_result:{project}:{issue}:{task_id} after applying a '
            'recovered result (_apply_redis_result). Destructive and irreversible: the dry '
            'run consumes a real agent outcome, and the real sweep then finds nothing and '
            "marks that execution 'failure', losing a successful run's recorded outcome",
            'Redis: DELETES repair_cycle:container:{project}:{issue} on the orphaned-tracking '
            'branch',
            "Redis: _repair_missing_redis_tracking() re-registers agent:container:{name} "
            "with hset (repaired='true', started_at reset to now) against a live container",
            'Observability: emit_execution_state_reconciled / emit_error_decision '
            '(ExecutionContainerLost) / PIPELINE_RUN_FAILED, into the production decision and '
            'lifecycle indices, which the web UI and pattern detection consume as real',
        ),
        #  pipeline_locks is written, not merely read: the dev-container
        #  reconciliation this sweep performs for a stuck dev_environment_setup/
        #  verifier record takes the dev_container_build RESOURCE lock, which is
        #  a PipelineLockManager grant and lands as
        #  state/pipeline_locks/<project>___resource__dev_container_build.yaml.
        #  Under neutralization the Redis grant path cannot run (pipeline() is
        #  not a read command, so it is intercepted and `with None as pipe`
        #  raises) and it always falls through to _create_lock_yaml_only().
        owned_state_subtrees=('execution_history', 'dev_containers', 'pipeline_locks'),
        inert_guards=(
            'active review cycle (review_cycle_executor.active_cycles is an in-memory dict '
            'on the running orchestrator; empty in this process, so the guard cannot fire)',
            'active human feedback loop (human_feedback_loop_executor.active_loops, same '
            'shape, same consequence)',
            'cleanup coordination claim, while neutralization is on (the default): '
            "try_claim_cleanup()'s SET NX is intercepted and reports a successful claim, so "
            'no candidate is ever seen as claimed by another mechanism. Production skips '
            'those records at the FIRST guard in the chain; this run walks every one of '
            'them on into the destructive agent_result branch',
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


def snapshot_tree(root: Path, unreadable: Optional[List[str]] = None) -> Dict[str, Tuple[int, str]]:
    """Map every regular file under `root` to (size, sha256), keyed by relpath.

    Symlinks are followed only when they resolve to a regular file inside the
    tree; anything else (sockets, dangling links) is skipped rather than made
    fatal -- state/ is a bind-mounted host directory and this must not become
    the reason a dry run cannot be attempted.

    A file that VANISHES mid-walk is a concurrent write by the running
    orchestrator and is dropped silently; the before/after comparison reports it
    as removed. A file that cannot be READ is a different thing entirely -- a
    root-owned file in the bind mount, an ACL, an EIO -- and it is appended to
    `unreadable` rather than dropped, because a file absent from BOTH the before
    and the after manifest is invisible to every check in this harness while the
    verdict still says the tree is byte-identical.
    """
    manifest: Dict[str, Tuple[int, str]] = {}
    root = Path(root)
    for path in sorted(root.rglob('*')):
        try:
            if not path.is_file():
                continue
            manifest[str(path.relative_to(root))] = (path.stat().st_size, file_digest(path))
        except FileNotFoundError:
            # Vanished mid-walk: a concurrent write by the running orchestrator.
            continue
        except (PermissionError, OSError) as e:
            try:
                relpath = str(path.relative_to(root))
            except ValueError:  # pragma: no cover - rglob always yields children
                relpath = str(path)
            logger.warning(
                f'Could not read {path} while snapshotting {root} -- it is in NEITHER '
                f'the before nor the after manifest, so no check in this run covers it: {e}'
            )
            if unreadable is not None:
                unreadable.append(relpath)
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


class ExternalWriteRefused(RuntimeError):
    """The sweep writes outside state/ and nothing was going to stop it.

    Raised from run_dry_run() rather than only checked in main(), so a
    programmatic caller cannot get past the gate the CLI enforces.
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
#
#  The first three bind at IMPORT time. The last two are lazy module globals
#  that are None until something calls their getter -- which is harmless in a
#  fresh process (the getter reads ORCHESTRATOR_ROOT, already repointed by
#  then), and is exactly the hazard this list exists for in a process that
#  called the getter before run_dry_run().
_RUNTIME_SINGLETONS: Tuple[Tuple[str, str, str, str], ...] = (
    ('services.work_execution_state', 'work_execution_tracker', 'state_dir', 'execution_history'),
    ('services.dev_container_state', 'dev_container_state', 'state_dir', 'dev_containers'),
    (
        'services.conversational_session_state',
        'conversational_session_state',
        'state_dir',
        'conversational_sessions',
    ),
    ('services.pipeline_lock_manager', '_pipeline_lock_manager', 'state_dir', 'pipeline_locks'),
    (
        'services.pipeline_semaphore_manager',
        '_pipeline_semaphore_manager',
        'state_dir',
        'pipeline_semaphores',
    ),
)


def bind_runtime_singletons(scratch_root: Path) -> Tuple[Dict[str, str], List[Tuple[str, str]]]:
    """Point the import-time singletons at the copy, forcing any that predate it.

    These bind ORCHESTRATOR_ROOT the moment their module is imported, so in this
    script's own process -- which has no orchestrator imports at module scope --
    importing them here is enough. A process that already imported one before
    repoint_runtime() ran (a test runner, an interactive session) holds a
    singleton still pointed at whatever root was in effect then, which is the
    live tree for anything running inside the orchestrator container. Those are
    rewritten in place rather than merely reported: an isolation check that a
    caller can defeat by importing a module in the wrong order is not a check.

    Returns (resolved, unbound). A singleton whose module will not import is
    reported in `unbound` rather than swallowed at DEBUG -- an isolation section
    that quietly omits a path is the wrong-but-plausible reassurance this
    harness exists to make impossible.
    """
    import importlib

    resolved: Dict[str, str] = {}
    unbound: List[Tuple[str, str]] = []

    for module_name, singleton_name, path_attr, subdir in _RUNTIME_SINGLETONS:
        label = f'{module_name}.{singleton_name}.{path_attr}'
        try:
            module = importlib.import_module(module_name)
        except Exception as e:  # pragma: no cover - optional dependency chain
            unbound.append((label, f'import failed: {e}'))
            continue

        singleton = getattr(module, singleton_name, None)
        if singleton is None:
            # A lazy getter's cache that nothing has populated: nothing to
            # repoint yet. It is NOT left unasserted -- forced_lazy_singletons()
            # constructs it a step later, inside the neutralization window
            # (which is where it has to be built, see that function), and its
            # real path replaces this placeholder in the same map.
            resolved[label] = 'not constructed (lazy singleton, unset in this process)'
            continue

        try:
            current = Path(getattr(singleton, path_attr))
        except (AttributeError, TypeError) as e:
            unbound.append((label, f'no usable {path_attr}: {e}'))
            continue

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

    return resolved, unbound


# ---------------------------------------------------------------------------
# External-effect neutralization
# ---------------------------------------------------------------------------


#  Redis commands that only READ. Everything else is neutralized, so a command
#  nobody thought of is intercepted rather than executed -- the fail-safe
#  direction for a harness whose claim is "nothing outside state/ was written".
#  Reads still go to the live server on purpose: a guard that reads an empty
#  scratch Redis answers "no lock, not queued, no tracking", which makes the dry
#  run strictly LESS protected than production, which is the opposite of useful.
_REDIS_READ_COMMANDS = frozenset({
    'get', 'mget', 'exists', 'keys', 'scan', 'scan_iter', 'type', 'ttl', 'pttl',
    'strlen', 'getrange', 'randomkey', 'dbsize', 'ping', 'info', 'config_get',
    'hget', 'hgetall', 'hkeys', 'hvals', 'hexists', 'hlen', 'hmget', 'hscan',
    'hscan_iter', 'hrandfield',
    'lrange', 'llen', 'lindex', 'lpos',
    'smembers', 'sismember', 'scard', 'sscan', 'sscan_iter', 'srandmember',
    'zrange', 'zrevrange', 'zrangebyscore', 'zscore', 'zcard', 'zcount', 'zscan',
    'xrange', 'xrevrange', 'xlen', 'xinfo_stream',
    'memory_usage', 'object', 'client_getname', 'connection_pool',
})

#  What a neutralized write returns. Chosen so the sweep proceeds exactly as it
#  would on a first real run -- try_claim_cleanup()'s SET NX must look like it
#  claimed, or every candidate would be reported as skipped-by-claim.
_NEUTRALIZED_REDIS_RETURNS: Dict[str, Any] = {
    'set': True, 'setex': True, 'setnx': True, 'mset': True, 'getset': None,
    'delete': 0, 'unlink': 0, 'expire': True, 'pexpire': True, 'persist': True,
    'hset': 0, 'hmset': True, 'hdel': 0, 'hincrby': 0, 'incr': 0, 'decr': 0,
    'lpush': 0, 'rpush': 0, 'lpop': None, 'rpop': None, 'ltrim': True, 'lrem': 0,
    'sadd': 0, 'srem': 0, 'zadd': 0, 'zrem': 0, 'publish': 0, 'xadd': '0-0',
    'xtrim': 0, 'rename': True, 'flushdb': True,
}

#  Module globals that cache a neutralizable client across calls -- a Redis
#  client directly, or a get-or-create manager that holds one (and, for the
#  pipeline run manager, an Elasticsearch client too). A client built before the
#  patch went in would bypass it entirely, and one built DURING the patch would
#  outlive it, so they are cleared on the way in and reset on the way out -- see
#  _module_global_discarded() for why the second half matters more than the
#  first in this process.
#
#  Every entry is a global a REGISTERED sweep actually reaches:
#    * cleanup_guard._redis_client        -- try_claim_cleanup(), stuck_in_progress
#    * github_api_client._shared_redis_client / _github_client -- PROTECTION 6 and
#      _should_retry_failed_execution(); GitHubAPIClient.__init__ builds a
#      GitHubBreaker, which builds its own redis.Redis
#    * pipeline_run._pipeline_run_manager -- get_pipeline_run_manager(), reached by
#      BOTH sweeps (_should_retry_failed_execution() and the stuck sweep's failure
#      branch); PipelineRunManager.__init__ builds a redis.Redis AND an
#      Elasticsearch client
#  services.pipeline_lock_manager._pipeline_lock_manager and its semaphore twin
#  are the same shape and are discarded by forced_lazy_singletons() instead,
#  which also asserts their state_dir.
_CACHED_CLIENT_GLOBALS: Tuple[Tuple[str, str], ...] = (
    ('services.cleanup_guard', '_redis_client'),
    ('services.github_api_client', '_shared_redis_client'),
    ('services.github_api_client', '_github_client'),
    ('services.pipeline_run', '_pipeline_run_manager'),
)

#  Lazy module globals that hold a state-owning manager AND a Redis client, and
#  are None until their getter runs. Nothing imports them at module scope here,
#  so the sweep constructs them itself, mid-run: they are therefore built inside
#  the neutralization window (which is what keeps their Redis writes
#  intercepted) and must be discarded with it. They are also the only
#  state_dir the isolation assertion could not cover, because bind_runtime_
#  singletons() runs before anything has constructed them -- forced_lazy_
#  singletons() builds them early, inside the window, so their paths are
#  asserted like every other manager's.
#  (module, getter, module global it caches, path attribute)
_LAZY_SINGLETON_GETTERS: Tuple[Tuple[str, str, str, str], ...] = (
    (
        'services.pipeline_lock_manager',
        'get_pipeline_lock_manager',
        '_pipeline_lock_manager',
        'state_dir',
    ),
    (
        'services.pipeline_semaphore_manager',
        'get_pipeline_semaphore_manager',
        '_pipeline_semaphore_manager',
        'state_dir',
    ),
)


@dataclass
class ExternalEffectRecorder:
    """Everything the sweep tried to do outside state/, and did not get to do."""

    redis_writes: List[Dict[str, Any]] = field(default_factory=list)
    es_writes: List[Dict[str, Any]] = field(default_factory=list)
    observability_events: List[Dict[str, Any]] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    def record_redis(self, command: str, args: Tuple[Any, ...]) -> None:
        self.redis_writes.append(
            {'command': command, 'key': str(args[0]) if args else None}
        )

    def record_es(self, operation: str, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> None:
        #  elasticsearch-py is keyword-only for every call this intercepts, so
        #  the target is 'index' (a document write) or 'name' (an ILM policy, an
        #  index template); args is checked anyway so an older positional-style
        #  call still names something rather than None.
        target = kwargs.get('index') or kwargs.get('name')
        if target is None and args:
            target = args[0]
        self.es_writes.append(
            {'operation': operation, 'target': str(target) if target is not None else None}
        )

    def record_event(self, event_type: Any, args: Tuple[Any, ...], kwargs: Dict[str, Any]) -> None:
        self.observability_events.append(
            {
                'event_type': getattr(event_type, 'value', str(event_type)),
                'project': kwargs.get('project') or (args[2] if len(args) > 2 else None),
            }
        )

    def summary(self) -> Dict[str, Any]:
        def _tally(rows: List[Dict[str, Any]], key: str) -> Dict[str, int]:
            counts: Dict[str, int] = {}
            for row in rows:
                counts[str(row.get(key))] = counts.get(str(row.get(key)), 0) + 1
            return dict(sorted(counts.items()))

        return {
            'redis_writes': {
                'total': len(self.redis_writes),
                'by_command': _tally(self.redis_writes, 'command'),
                'samples': self.redis_writes[:20],
            },
            'es_writes': {
                'total': len(self.es_writes),
                'by_operation': _tally(self.es_writes, 'operation'),
                'samples': self.es_writes[:20],
            },
            'observability_events': {
                'total': len(self.observability_events),
                'by_type': _tally(self.observability_events, 'event_type'),
                'samples': self.observability_events[:20],
            },
            'notes': list(self.notes),
        }


def _make_neutralized_redis_class(real_cls, recorder: ExternalEffectRecorder):
    """A redis client that reads production and writes nowhere.

    Deliberately a CLASS, not a factory function: `redis.Redis` appears in type
    annotations evaluated at import time (`Optional[redis.Redis]` in
    services/pipeline_run.py and friends), and `Optional[<function>]` raises.
    """

    class NeutralizedRedis:
        def __init__(self, *args, **kwargs):
            self._delegate = real_cls(*args, **kwargs)

        def __getattr__(self, name):
            attribute = getattr(self._delegate, name)
            if name in _REDIS_READ_COMMANDS or not callable(attribute):
                return attribute

            def _neutralized(*args, **kwargs):
                recorder.record_redis(name, args)
                return _NEUTRALIZED_REDIS_RETURNS.get(name)

            return _neutralized

    return NeutralizedRedis


#  Elasticsearch methods that only READ, on the client itself and on its
#  namespace sub-clients (es.indices, es.ilm, es.cluster, ...). Same policy as
#  Redis for the same reason: reads go to the live cluster so every guard sees
#  the truth it would see in production, and anything not on this list is
#  intercepted -- an operation nobody thought of is recorded rather than
#  executed.
_ES_READ_METHODS = frozenset({
    'search', 'msearch', 'search_template', 'msearch_template', 'scroll',
    'clear_scroll', 'get', 'mget', 'get_source', 'exists', 'exists_source',
    'count', 'ping', 'info', 'explain', 'field_caps', 'termvectors',
    'mtermvectors', 'rank_eval', 'terms_enum', 'health', 'stats', 'state',
    'get_lifecycle', 'explain_lifecycle', 'get_index_template', 'get_template',
    'exists_index_template', 'exists_template', 'exists_alias', 'get_alias',
    'get_mapping', 'get_field_mapping', 'get_settings', 'get_data_stream',
    'resolve_index', 'get_pipeline', 'indices', 'aliases', 'nodes', 'shards',
})
#  Deliberately NOT on that list: `es.options(...)`, which looks like a read but
#  returns a fresh REAL client that every subsequent call would go through. It
#  is intercepted like any other unlisted method, so a caller that starts using
#  it fails loudly here instead of writing to production quietly. No sweep
#  reaches it today.

#  What a neutralized Elasticsearch write returns. The ILM/template PUTs and
#  es.index() are the only ones a registered sweep reaches; the shapes below are
#  what their callers check, so the sweep proceeds exactly as it would against a
#  healthy cluster instead of taking an error branch that production would not.
_NEUTRALIZED_ES_RETURNS: Dict[str, Any] = {
    'index': {'result': 'created', '_shards': {'failed': 0}},
    'create': {'result': 'created', '_shards': {'failed': 0}},
    'update': {'result': 'updated', '_shards': {'failed': 0}},
    'delete': {'result': 'deleted', '_shards': {'failed': 0}},
    'bulk': {'errors': False, 'items': []},
}
_NEUTRALIZED_ES_DEFAULT: Dict[str, Any] = {'acknowledged': True}


def _is_es_namespace(attribute: Any) -> bool:
    """True for an elasticsearch-py sub-client (es.indices, es.ilm, es.cluster).

    Recognised by shape rather than by a hardcoded list of names: a namespace
    nobody enumerated must still be wrapped, because a namespace returned
    unwrapped hands the sweep the REAL client for every method on it -- and
    `indices` is in _ES_READ_METHODS for the sake of `es.cat.indices`, so a
    missed `es.indices` is returned raw by the read-method branch too.

    The structural test is the load-bearing one: a sub-client is the object that
    holds a back-reference to its parent client AND can issue requests, which is
    what elasticsearch-py's NamespacedClient carries and what neither the
    top-level client nor the transport does (neither has `_client`). It is
    checked first because the nominal test underneath it is the fragile half:
    it assumes a namespace is non-callable and that its type lives under the
    `elasticsearch` package, and a release that gives NamespacedClient a
    __call__ or hands the namespace back through a proxy defined elsewhere would
    silently turn interception off. NamespacedClient itself is not importable
    from a public path (elasticsearch 9.5 keeps it in
    elasticsearch._sync.client._base), so isinstance() is not an option.
    """
    if hasattr(attribute, '_client') and hasattr(attribute, 'perform_request'):
        return True
    return (
        not callable(attribute)
        and type(attribute).__module__.split('.')[0] == 'elasticsearch'
    )


class _NeutralizedESNamespace:
    """es.ilm / es.indices / ... with its writes recorded instead of performed."""

    def __init__(self, namespace: Any, label: str, recorder: ExternalEffectRecorder):
        self._namespace = namespace
        self._label = label
        self._recorder = recorder

    def __getattr__(self, name):
        attribute = getattr(self._namespace, name)
        if name in _ES_READ_METHODS or not callable(attribute):
            return attribute

        def _neutralized(*args, **kwargs):
            self._recorder.record_es(f'{self._label}.{name}', args, kwargs)
            return _NEUTRALIZED_ES_RETURNS.get(name, _NEUTRALIZED_ES_DEFAULT)

        return _neutralized


def _make_neutralized_elasticsearch_class(real_cls, recorder: ExternalEffectRecorder):
    """An Elasticsearch client that reads production and writes nowhere.

    A CLASS for the same reason NeutralizedRedis is one: `Elasticsearch` appears
    in type annotations evaluated at import time (`Optional[Elasticsearch]` in
    services/pipeline_run.py), and `Optional[<function>]` raises.

    Truthiness matters here and is deliberately left at the default True:
    PipelineRunManager.__init__ does `if self.es: self._setup_elasticsearch()`,
    and a falsy stub would skip the very PUTs this wrapper exists to intercept
    and report -- turning a neutralized write into an invisible one.
    """

    class NeutralizedElasticsearch:
        def __init__(self, *args, **kwargs):
            self._delegate = real_cls(*args, **kwargs)

        def __getattr__(self, name):
            attribute = getattr(self._delegate, name)
            if _is_es_namespace(attribute):
                return _NeutralizedESNamespace(attribute, name, recorder)
            if name in _ES_READ_METHODS or not callable(attribute):
                return attribute

            def _neutralized(*args, **kwargs):
                recorder.record_es(name, args, kwargs)
                return _NEUTRALIZED_ES_RETURNS.get(name, _NEUTRALIZED_ES_DEFAULT)

            return _neutralized

    return NeutralizedElasticsearch


def _elasticsearch_symbol_holders(target_cls) -> List[Tuple[Any, str]]:
    """Every imported module whose module-scope `Elasticsearch` is `target_cls`.

    Redis is reached as `redis.Redis(...)` everywhere, so patching one package
    attribute covers every construction site. Elasticsearch is not: every module
    in this codebase does `from elasticsearch import Elasticsearch`, which copies
    the class into that module's globals at import time, and a patch on the
    `elasticsearch` package alone does not reach it.

    Looked up by identity rather than by a list of module names so a module
    nobody enumerated is still covered, and through __dict__ rather than
    getattr() so a module-level __getattr__ hook is not triggered by an audit
    whose whole job is to be side-effect free.
    """
    holders: List[Tuple[Any, str]] = []
    for module in list(sys.modules.values()):
        namespace = getattr(module, '__dict__', None)
        if not isinstance(namespace, dict):
            continue
        if namespace.get('Elasticsearch') is target_cls:
            holders.append((module, 'Elasticsearch'))
    return holders


@contextlib.contextmanager
def _elasticsearch_neutralized(real_cls, wrapper):
    """Swap the real Elasticsearch class for `wrapper` everywhere, and back again.

    The exit scan is re-run rather than replayed, and that is the half that
    matters here. `services.pipeline_run` is not in sys.modules when the window
    opens -- this script has no orchestrator imports at module scope and the
    sweep imports it mid-run -- so it does `from elasticsearch import
    Elasticsearch` while the package attribute IS the wrapper and binds the
    wrapper into its own globals permanently. Restoring only what was patched on
    the way in would leave that binding behind, pointed at a dead
    ExternalEffectRecorder: exactly the hazard _module_global_discarded() exists
    for, one datastore over.
    """
    for holder, attribute in _elasticsearch_symbol_holders(real_cls):
        setattr(holder, attribute, wrapper)
    try:
        yield
    finally:
        for holder, attribute in _elasticsearch_symbol_holders(wrapper):
            setattr(holder, attribute, real_cls)


class _RecordingObservability:
    """Stands in for ObservabilityManager: records emissions, publishes none."""

    def __init__(self, recorder: ExternalEffectRecorder):
        self._recorder = recorder
        self.es = None
        self.redis = None

    def emit(self, event_type, *args, **kwargs):
        self._recorder.record_event(event_type, args, kwargs)

    def __getattr__(self, name):
        def _noop(*args, **kwargs):
            self._recorder.notes.append(f'observability.{name}() called and suppressed')
            return None

        return _noop


@contextlib.contextmanager
def neutralize_external_effects(enabled: bool):
    """Intercept the writes this harness cannot checksum, for the sweep's run.

    Three independent patches, because the two sweeps reach production three
    ways: Redis (claim keys, the destructive agent_result delete, repair-cycle
    keys, container tracking), the observability manager (decision events and
    pipeline lifecycle events, into the live stream and Elasticsearch), and a
    DIRECT Elasticsearch client -- both sweeps call get_pipeline_run_manager(),
    whose __init__ builds its own Elasticsearch and unconditionally PUTs the
    'pipeline-runs-ilm-policy' ILM policy and the 'pipeline-runs-template' index
    template into the live cluster before it has done anything else.

    Redis is patched at `redis.Redis`, so every construction site -- all of them
    build their client inline with a hardcoded host -- picks up the wrapper.
    Elasticsearch cannot be patched the same way: every module here does
    `from elasticsearch import Elasticsearch`, so the package attribute is
    patched for modules imported later AND each already-imported module's own
    copy is patched by identity (see _elasticsearch_symbol_holders).
    Observability is patched twice on purpose: get_observability_manager() so no
    real manager (and no ES client) is built at all, and ObservabilityManager.emit
    so a manager some other module already holds is covered too.
    """
    recorder = ExternalEffectRecorder()
    if not enabled:
        recorder.notes.append(
            'NEUTRALIZATION OFF -- every effect below reached production for real'
        )
        yield recorder
        return

    with contextlib.ExitStack() as stack:
        try:
            import redis
        except Exception as e:  # pragma: no cover - redis is a hard dependency in prod
            recorder.notes.append(f'redis not importable, no Redis writes intercepted: {e}')
        else:
            for attribute in ('Redis', 'StrictRedis'):
                real_cls = getattr(redis, attribute, None)
                if real_cls is None:  # pragma: no cover - both exist in redis-py
                    continue
                stack.enter_context(
                    _patched(redis, attribute, _make_neutralized_redis_class(real_cls, recorder))
                )

        try:
            import elasticsearch
        except Exception as e:  # pragma: no cover - elasticsearch is a hard dependency in prod
            recorder.notes.append(
                f'elasticsearch not importable, no ES writes intercepted: {e}'
            )
        else:
            real_es = getattr(elasticsearch, 'Elasticsearch', None)
            if real_es is not None:  # pragma: no branch - the package always defines it
                stack.enter_context(
                    _elasticsearch_neutralized(
                        real_es, _make_neutralized_elasticsearch_class(real_es, recorder)
                    )
                )

        for module_name, attribute in _CACHED_CLIENT_GLOBALS:
            previous = stack.enter_context(
                _module_global_discarded(module_name, attribute)
            )
            if previous is not None:
                recorder.notes.append(
                    f'{module_name}.{attribute} already held a live client; cleared '
                    f'for the run so it is rebuilt through the interception'
                )

        try:
            from monitoring import observability as observability_module
        except Exception as e:  # pragma: no cover - optional dependency chain
            recorder.notes.append(
                f'monitoring.observability not importable, no events intercepted: {e}'
            )
        else:
            stub = _RecordingObservability(recorder)
            stack.enter_context(
                _patched(observability_module, 'get_observability_manager', lambda: stub)
            )
            stack.enter_context(
                _patched(
                    observability_module.ObservabilityManager,
                    'emit',
                    lambda _self, event_type, *args, **kwargs: recorder.record_event(
                        event_type, args, kwargs
                    ),
                )
            )

        yield recorder


@contextlib.contextmanager
def _patched(target: Any, attribute: str, replacement: Any):
    """setattr for the duration of the block, restoring what was there."""
    sentinel = object()
    previous = getattr(target, attribute, sentinel)
    setattr(target, attribute, replacement)
    try:
        yield
    finally:
        if previous is sentinel:  # pragma: no cover - every patched attr exists
            delattr(target, attribute)
        else:
            setattr(target, attribute, previous)


@contextlib.contextmanager
def _module_global_discarded(module_name: str, attribute: str):
    """Clear a cached module global for the block, and reset it on the way out.

    Unlike _patched(), the module is looked up in sys.modules TWICE -- once on
    the way in and again on the way out -- because the normal case here is that
    it is not imported yet when the block opens. This script has no orchestrator
    imports at module scope, so `services.cleanup_guard` is absent from
    sys.modules when neutralization starts and is imported by the sweep moments
    later; its `_get_redis()` then builds a client while `redis.Redis` is patched
    and caches it. Restoring "whatever was there" on the way out therefore means
    restoring None, which DISCARDS that client.

    That second half is the point. A NeutralizedRedis that outlives the window is
    bound to a dead ExternalEffectRecorder: a second run in the same process
    records its Redis writes into run 1's recorder and reports "Redis writes
    intercepted: 0" for a sweep that attempted them, and try_claim_cleanup()'s
    SET NX answers True forever, so the cross-mechanism cleanup guard is dead for
    the rest of the process.

    Yields whatever the global held on entry (None when the module was not
    imported yet), so the caller can tell "already had a live client" from
    "nothing there".
    """
    module = sys.modules.get(module_name)
    previous = getattr(module, attribute, None) if module is not None else None
    if module is not None:
        setattr(module, attribute, None)
    try:
        yield previous
    finally:
        module = sys.modules.get(module_name)
        if module is not None:
            setattr(module, attribute, previous)


@contextlib.contextmanager
def forced_lazy_singletons(scratch_root: Path):
    """Build the lazy lock/semaphore singletons for the sweep's window only.

    bind_runtime_singletons() reports these as 'not constructed', and
    run_dry_run() skips any such path when it asserts isolation -- so
    `state/pipeline_locks/` was the one subtree a sweep writes that NO assertion
    covered, precisely because it is built later, inside the sweep. The stuck
    sweep reaches it through _transition_dev_container_state() -> the
    dev_container_build resource lock -> PipelineLockManager, whose YAML fallback
    writes `<project>___resource__dev_container_build.yaml`.

    Constructing them HERE rather than in bind_runtime_singletons() is
    deliberate and is the whole reason this is a context manager: this runs
    inside neutralize_external_effects(), so each manager's Redis client is the
    intercepting wrapper the sweep needs it to be. Built one step earlier they
    would hold a live client and their lock grants would reach production Redis
    -- trading an unasserted path for a real external write.

    They are discarded on exit for the same reason the cached Redis clients are:
    a manager holding a neutralized client, pointed at a scratch directory that
    is about to be deleted, must not be what the next caller in this process
    takes a lock through.

    Yields (resolved, unbound) in bind_runtime_singletons()'s shape, keyed by the
    same labels so the ISOLATION section shows one entry per singleton.
    """
    import importlib

    resolved: Dict[str, str] = {}
    unbound: List[Tuple[str, str]] = []

    with contextlib.ExitStack() as stack:
        for module_name, getter_name, global_name, path_attr in _LAZY_SINGLETON_GETTERS:
            label = f'{module_name}.{global_name}.{path_attr}'
            try:
                module = importlib.import_module(module_name)
            except Exception as e:  # pragma: no cover - optional dependency chain
                unbound.append((label, f'import failed: {e}'))
                continue

            # Cleared first, then rebuilt: a singleton some earlier import
            # constructed holds both the wrong state_dir and a live Redis
            # client, and repointing only the path would leave the client.
            stack.enter_context(_module_global_discarded(module_name, global_name))
            try:
                singleton = getattr(module, getter_name)()
                resolved[label] = str(Path(getattr(singleton, path_attr)))
            except Exception as e:  # pragma: no cover - construction is cheap
                unbound.append((label, f'could not be constructed: {e}'))

        yield resolved, unbound


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


def _record_key(message: str, line_index: int) -> str:
    """The <project>/#<issue> a message names, or a unique stand-in.

    A message with no record in it (the per-state-file "could not load project
    config" warning is the live example) gets a key nothing else can collide
    with, so it counts as exactly one unit and never merges with another line.
    """
    match = _RECORD_KEY_RE.search(message)
    if match:
        return f'{match.group(1)}/#{match.group(2)}'
    return f'<unattributed line {line_index}>'


def classify_records(
    spec: SweepSpec, records: List[Tuple[str, str, str]]
) -> Dict[str, Any]:
    """Bucket the sweep's log lines by gate, attributing them to RECORDS.

    Counting lines instead of records is what made the residual arithmetic
    nonsense: several protections log an annotation ("could not date this one",
    "project config unavailable", "PROTECTION 2 failed") and then fall THROUGH
    to a later gate, so a single record legitimately produces two or three
    lines. Each record is counted once, against the first non-degradation gate
    it hits; degradations are an overlay that is reported and never summed.
    """
    compiled = [
        (gate, tuple(re.compile(p) for p in gate.patterns)) for gate in spec.gates
    ]
    examined_res = [re.compile(p) for p in spec.examined_patterns]
    terminal_res = [re.compile(p) for p in spec.terminal_patterns]
    candidate_res = [re.compile(p) for p in spec.candidate_patterns]

    gate_hits: Dict[str, List[str]] = {gate.name: [] for gate in spec.gates}
    gate_records: Dict[str, set] = {gate.name: set() for gate in spec.gates}
    attributed: Dict[str, str] = {}
    multi_gate: Dict[str, List[str]] = {}
    examined: Optional[int] = None
    terminal: List[str] = []
    terminal_records: set = set()
    candidate_records: set = set()
    errors: List[str] = []
    unclassified: List[str] = []

    for index, (_name, level, message) in enumerate(records):
        if examined is None:
            match = next(
                (m for m in (p.search(message) for p in examined_res) if m), None
            )
            if match:
                examined = int(match.group(1))
                continue

        key = _record_key(message, index)

        if any(pattern.search(message) for pattern in candidate_res):
            candidate_records.add(key)
            continue

        if any(pattern.search(message) for pattern in terminal_res):
            terminal.append(message)
            terminal_records.add(key)
            continue

        for gate, patterns in compiled:
            if any(pattern.search(message) for pattern in patterns):
                gate_hits[gate.name].append(message)
                if gate.degradation:
                    gate_records[gate.name].add(key)
                elif key in attributed and attributed[key] != gate.name:
                    # Two skip gates for one record: only one of them decided
                    # its fate, and summing both is how the residual went
                    # negative. Counted once, surfaced as an anomaly.
                    multi_gate.setdefault(key, [attributed[key]]).append(gate.name)
                else:
                    attributed[key] = gate.name
                    gate_records[gate.name].add(key)
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
        'gate_records': {name: sorted(keys) for name, keys in gate_records.items()},
        'terminal': terminal,
        'terminal_records': sorted(terminal_records),
        'candidate_records': sorted(candidate_records),
        'undecided_candidates': sorted(
            candidate_records - terminal_records - set(attributed)
        ),
        'multi_gate_records': {key: gates for key, gates in sorted(multi_gate.items())},
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


def _mutation_fingerprints(entries: List[Dict[str, Any]]) -> set:
    """(project, issue, agent, index, changed fields) for every described change.

    A leak that lands at a DIFFERENT relpath than the copy's is invisible to a
    path intersection, so attribution falls back to content: a live record whose
    delta is the same decision the sweep made in the copy is this run's work,
    wherever it was written.
    """
    fingerprints = set()
    for entry in entries:
        for change in entry.get('record_changes') or []:
            fields = tuple(
                (key, repr(delta.get('before')), repr(delta.get('after')))
                for key, delta in sorted((change.get('fields') or {}).items())
            )
            if not fields:
                continue
            fingerprints.add(
                (
                    entry.get('project'),
                    entry.get('issue_number'),
                    change.get('agent'),
                    change.get('index'),
                    fields,
                )
            )
    return fingerprints


def _is_under_subtree(relpath: str, subtrees: Tuple[str, ...]) -> bool:
    return any(relpath == s or relpath.startswith(s + '/') for s in subtrees)


def _is_lock_artifact(relpath: str) -> bool:
    """A `<state file>.lock` flock target, not state.

    utils.file_lock creates one beside every state file it locks and leaves it
    there, so the copy grows a set of them on every run and the live tree grows
    the same set whenever the orchestrator touches the same records. They carry
    no work, they collide by relpath by construction, and counting a collision
    as a leak would make exit 1 the routine outcome of the harness's primary use
    -- an operator who learns to ignore exit 1 has lost the whole check. They
    stay in the manifests (so the digests still cover every byte) and are listed
    separately in the report; they are just not evidence of anything.
    """
    return relpath.endswith('.lock')


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
    unreadable = report.get('unreadable') or []
    if unreadable:
        _out(f"UNREADABLE, in no manifest and covered by no check: {len(unreadable)}")
        for relpath in unreadable[:20]:
            _out(f"    - {relpath}")
    _out(f"VERDICT: {copy_info['verdict']}")

    _rule('3. ISOLATION')
    for label, path in sorted(report['isolation'].items()):
        _out(f"  {label} = {path}")
    for label, reason in report.get('isolation_unbound') or []:
        _out(f"  {label} = NOT BOUND ({reason})")
    _out(f"VERDICT: {report['isolation_verdict']}")

    inert = report.get('inert_guards') or []
    if inert:
        _rule('3b. GUARDS THAT CANNOT FIRE IN THIS PROCESS')
        _out('  These guards cannot answer here the way they answer in production -- either')
        _out('  they read in-memory state owned by the RUNNING orchestrator (a fresh process')
        _out('  sees it empty), or neutralization makes their answer unconditional. For any')
        _out('  record they would have protected, THIS DRY RUN IS LESS PROTECTED THAN')
        _out('  PRODUCTION: it walks past that record and on into the sweep\'s decision path.')
        for note in inert:
            _out(f"    - {note}")

    _rule('4. SWEEP EXECUTION')
    _out(f"sweep return value          : {report['sweep_return']!r}")
    _out(f"state files present in copy : {report['state_files_in_copy']}")
    if report.get('sweep_error'):
        _out()
        _out(f"  !! THE SWEEP RAISED: {report['sweep_error']}")
        _out('  The run is partial. Steps 5-7 below still ran, so the live-tree proof')
        _out('  covers whatever the sweep managed to do before it raised.')

    _rule('4b. EXTERNAL EFFECTS (outside state/, not checksummed)')
    effects = report['external_effects']
    _out(f"  neutralized by this run : {effects['neutralized']}")
    for note in effects['declared_reads']:
        _out(f"  reads  : {note}")
    for note in effects['declared_writes']:
        _out(f"  WRITES : {note}")
    _out()
    redis_writes = effects['redis_writes']
    _out(f"  Redis writes intercepted        : {redis_writes['total']}")
    for command, count in redis_writes['by_command'].items():
        _out(f"      {count:>6}  {command}")
    for sample in redis_writes['samples']:
        _out(f"        e.g. {sample['command']} {sample['key']}")
    es_writes = effects.get('es_writes') or {'total': 0, 'by_operation': {}, 'samples': []}
    _out(f"  Elasticsearch writes intercepted: {es_writes['total']}")
    for operation, count in es_writes['by_operation'].items():
        _out(f"      {count:>6}  {operation}")
    for sample in es_writes['samples']:
        _out(f"        e.g. {sample['operation']} {sample['target']}")
    events = effects['observability_events']
    _out(f"  observability events intercepted: {events['total']}")
    for event_type, count in events['by_type'].items():
        _out(f"      {count:>6}  {event_type}")
    for note in effects['notes']:
        _out(f"  note: {note}")

    _rule('5. SWEEP RESULT, PER RECORD')
    classification = report['classification']
    examined = classification['examined']
    _out(f"records examined (sweep's own count) : "
         f"{examined if examined is not None else 'not reported by this sweep'}")
    if classification.get('candidate_records'):
        _out(f"records that entered the guard chain : "
             f"{len(classification['candidate_records'])}")
    _out()
    _out('  counts below are DISTINCT RECORDS, not log lines: a record is counted once,')
    _out('  against the first protection that skipped it.')
    _out()
    for gate in report['gates']:
        marker = 'annot.  ' if gate['degradation'] else 'skipped '
        _out(f"  {marker} {gate['count']:>6}  {gate['name']}")
        _out(f"           {'':>6}  why: {gate['why']}")
        if gate['degradation']:
            _out(f"           {'':>6}  (an annotation, not a disposition -- these records are")
            _out(f"           {'':>6}   also counted under whichever gate actually skipped them,")
            _out(f"           {'':>6}   and are never summed into the accounting below)")
        if gate.get('inert'):
            _out(f"           {'':>6}  !! CANNOT FIRE IN THIS RUN, so the count above is not a")
            _out(f"           {'':>6}     measurement: {gate['inert']}")
    _out()
    undecided = classification.get('undecided_candidates') or []
    if undecided:
        _out(f"  {len(undecided):>6}  entered the guard chain and left it with no named gate and no")
        _out(f"          terminal decision -- a bare `continue` somewhere in the chain, or a")
        _out(f"          disposition this harness has no gate for yet. Read the unclassified")
        _out(f"          lines below before concluding anything. NOT 'never a candidate'.")
    unaccounted = report.get('unaccounted')
    if unaccounted is not None:
        _out(f"  {unaccounted:>6}  dropped before the first named gate by the sweep's own "
             f"pre-gate filters (e.g. 'last execution is not a success') -- NOT protected, "
             f"just never a candidate")
    if report.get('accounting_anomaly'):
        _out(f"  !! ACCOUNTING ANOMALY: {report['accounting_anomaly']}")
    if classification.get('multi_gate_records'):
        _out(f"  !! {len(classification['multi_gate_records'])} record(s) matched more than one "
             f"skip gate; each is counted once, against the first:")
        for key, gates in list(classification['multi_gate_records'].items())[:20]:
            _out(f"      {key}: {' , '.join(gates)}")
    _out()
    _out(f"  reached terminal decision: {len(classification['terminal_records'])} record(s), "
         f"{len(classification['terminal'])} log line(s)")
    for message in classification['terminal'][:50]:
        _out(f"      * {message}")
    if classification['errors']:
        _out()
        _out(f"  ERRORS raised inside the sweep: {len(classification['errors'])}")
        for message in classification['errors'][:20]:
            _out(f"      ! {message}")
    if classification['unclassified']:
        _out()
        _out(f"  unclassified sweep log lines: "
             f"{len(classification['unclassified'])} (shown verbatim so "
             f"nothing hides in a bucket that does not exist yet)")
        for message in classification['unclassified'][:20]:
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

    _rule('7. PROOF THE state/ TREE WAS NOT TOUCHED')
    live = report['live_check']
    _out(f"  before : {live['before_count']} files, digest {live['before_digest']}")
    _out(f"  after  : {live['after_count']} files, digest {live['after_digest']}")
    if unreadable:
        _out(f"  {len(unreadable)} file(s) could not be read and are in NEITHER manifest --")
        _out('  no check in this run covers them.')
    lock_artifacts_only = live['identical'] and live.get('lock_artifacts')
    if lock_artifacts_only:
        # `identical` means "no state file changed", which is what the exit code
        # turns on. Claiming BYTE-IDENTICAL below it would contradict the two
        # digests printed directly above, so this case gets its own wording and
        # names the artifacts.
        _out(f"  {len(live['lock_artifacts'])} flock artifact(s) appeared or changed. They "
             f"carry no state and are not evidence of anything:")
        for relpath in live['lock_artifacts'][:100]:
            _out(f"      {relpath}")
    if live['identical'] and not unreadable:
        _out(
            '  VERDICT: NO STATE FILE CHANGED -- only flock artifacts differ'
            if lock_artifacts_only
            else '  VERDICT: BYTE-IDENTICAL -- the live state/ tree was not modified'
        )
    elif live['identical']:
        _out('  VERDICT: UNVERIFIED -- every readable state file is unchanged, but the '
             'unreadable ones were never checked')
    else:
        _out('  VERDICT: LIVE TREE CHANGED')
        for kind in ('added', 'removed', 'changed'):
            paths = live['diff'][kind]
            if paths:
                _out(f"    {kind}: {len(paths)}")
                for relpath in paths[:100]:
                    if relpath in live['leaked']:
                        marker = '  <-- ATTRIBUTABLE TO THIS RUN: LEAK'
                    elif relpath in live['unattributed_owned']:
                        marker = "  <-- inside a subtree this sweep OWNS: indistinguishable from a leak"
                    elif relpath in live.get('lock_artifacts', []):
                        marker = '  (flock artifact, carries no state -- not evidence)'
                    else:
                        marker = ''
                    _out(f"        {relpath}{marker}")
        if live['leaked']:
            _out('  ** LEAK: the sweep\'s own work is in the live tree. This run was NOT isolated. **')
        elif live['unattributed_owned']:
            _out('  ** UNVERIFIABLE: the live tree changed inside a subtree this sweep writes.')
            _out('     A sweep that escapes isolation writes ONLY the live path, so drift there')
            _out('     cannot be told apart from a leak. Re-run against a stopped orchestrator. **')
        else:
            _out('  (no changed path is one this sweep owns or mutated -- consistent with '
                 'concurrent writes by the running orchestrator, but NOT proof)')

    _rule('VERDICT')
    _out(f"  {report['verdict']}  (exit {report['exit_code']})")
    _out()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _aborted_report(report: Dict[str, Any], live_before, copy_manifest, mismatch) -> Dict[str, Any]:
    """Fill in the shape print_report() expects when step 2 refused to continue."""
    report['copy']['mismatch'] = mismatch
    report['isolation'] = {}
    report['isolation_unbound'] = []
    report['isolation_verdict'] = 'not evaluated -- copy verification failed'
    report['verdict'] = 'ABORTED: the scratch copy is not a faithful copy of live state'
    report['exit_code'] = 1
    report['gates'] = []
    report['classification'] = {
        'examined': None, 'gate_hits': {}, 'gate_records': {}, 'terminal': [],
        'terminal_records': [], 'candidate_records': [], 'undecided_candidates': [],
        'multi_gate_records': {}, 'errors': [], 'unclassified': []
    }
    report['mutations'] = []
    report['copy_diff'] = {'added': [], 'removed': [], 'changed': []}
    report['sweep_return'] = None
    report['sweep_error'] = None
    report['external_effects'] = ExternalEffectRecorder().summary()
    report['external_effects'].update(
        {'neutralized': False, 'declared_reads': [], 'declared_writes': []}
    )
    report['state_files_in_copy'] = len(copy_manifest)
    report['unaccounted'] = None
    report['live_check'] = {
        'before_count': len(live_before),
        'before_digest': report['copy']['live_manifest_digest'],
        'after_count': len(live_before),
        'after_digest': report['copy']['live_manifest_digest'],
        'identical': True,
        'diff': {'added': [], 'removed': [], 'changed': []},
        'leaked': [],
        'unattributed_owned': [],
        'lock_artifacts': [],
    }
    return report


def _assert_resolved_paths_isolated(resolved: Dict[str, str], scratch_root: Path) -> None:
    """Every state-owning path in `resolved` must be under the scratch root."""
    for label, path in resolved.items():
        if label == 'ORCHESTRATOR_ROOT' or label.startswith('ConfigManager.'):
            continue
        if path.startswith('not constructed'):
            continue
        assert_under(Path(path), scratch_root, label)


def _isolation_verdict(unbound: List[Tuple[str, str]]) -> str:
    return (
        'every state-owning path resolved inside the scratch root'
        if not unbound
        else (
            f'{len(unbound)} state-owning path(s) could NOT be bound and are therefore '
            f'NOT covered by this assertion -- see NOT BOUND above'
        )
    )


def run_dry_run(
    spec: SweepSpec,
    deployment_root: Path,
    scratch_root: Path,
    config_root: Path,
    allow_concurrent_writes: bool = False,
    neutralize_external_effects_enabled: bool = True,
    allow_external_side_effects: bool = False,
) -> Dict[str, Any]:
    """Execute the seven steps. Returns the report dict; prints nothing."""
    live_state_root = deployment_root / 'state'
    scratch_state_root = scratch_root / 'state'

    # The refusal gate lives here, not only in main(): a programmatic caller
    # that skipped the CLI must not be able to run a sweep's real production
    # writes just by not passing a flag it never saw.
    if (
        spec.external_writes
        and not neutralize_external_effects_enabled
        and not allow_external_side_effects
    ):
        raise ExternalWriteRefused(
            f"{spec.name} writes outside state/, which this harness does not checksum "
            f"and cannot prove it left alone:\n"
            + '\n'.join(f'    - {note}' for note in spec.external_writes)
        )

    report: Dict[str, Any] = {
        'sweep': spec.name,
        'description': spec.description,
        'started_at': datetime.now(timezone.utc).isoformat(),
        'live_state_root': str(live_state_root),
        'scratch_root': str(scratch_root),
        'config_root': str(config_root),
        'inert_guards': list(spec.inert_guards),
    }

    unreadable: List[str] = []

    # --- 1. snapshot live -------------------------------------------------
    live_before = snapshot_tree(live_state_root, unreadable)

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
    report['unreadable'] = sorted(set(unreadable))
    if not copy_ok:
        return _aborted_report(
            report, live_before, copy_manifest, diff_manifests(live_before, copy_manifest)
        )

    # A pristine second copy, kept aside, so step 5b can diff record CONTENT
    # rather than only file hashes. The sweep is about to rewrite the copy in
    # place, and "which records changed and how" is the answer the whole run
    # exists to produce. It doubles as the baseline for the live tree, which is
    # how a leak at a DIFFERENT relpath is caught in step 7.
    pristine_root = scratch_root / 'pristine'
    pristine_root.mkdir(parents=True, exist_ok=True)
    copy_tree_from_manifest(scratch_state_root, pristine_root, dict(copy_manifest))

    # --- 3. repoint and assert isolation ----------------------------------
    resolved = repoint_runtime(scratch_root, config_root)
    manager = spec.build(scratch_state_root)
    resolved.update(
        {label: str(path) for label, path in spec.resolved_dirs(manager).items()}
    )
    bound, unbound = bind_runtime_singletons(scratch_root)
    resolved.update(bound)
    report['isolation'] = resolved
    report['isolation_unbound'] = unbound

    _assert_resolved_paths_isolated(resolved, scratch_root)
    # The config root is the one path that must deliberately NOT be scratch: the
    # sweep has to see the deployment's real project set. It is read-only for
    # every sweep registered here, and the live-tree check below covers state/.
    if not Path(config_root).is_dir():
        raise IsolationError(f'config root {config_root} does not exist')
    report['isolation_verdict'] = _isolation_verdict(unbound)

    # --- 4. run the real sweep -------------------------------------------
    handler = _CapturingHandler(spec.loggers)
    root_logger = logging.getLogger()
    previous_level = root_logger.level
    previous_cwd = os.getcwd()
    root_logger.addHandler(handler)
    root_logger.setLevel(logging.DEBUG)
    for name in spec.loggers:
        logging.getLogger(name).setLevel(logging.DEBUG)

    sweep_return: Any = None
    sweep_error: Optional[str] = None
    recorder = ExternalEffectRecorder()
    try:
        with neutralize_external_effects(neutralize_external_effects_enabled) as recorder:
            # Inside the neutralization window on purpose -- see
            # forced_lazy_singletons(). Their paths join the ISOLATION section
            # and are asserted exactly like the import-time singletons', which
            # is what closes the one subtree ('pipeline_locks') that nothing
            # asserted because the sweep constructs its manager mid-run.
            with forced_lazy_singletons(scratch_root) as (lazy_resolved, lazy_unbound):
                resolved.update(lazy_resolved)
                unbound = unbound + lazy_unbound
                report['isolation'] = resolved
                report['isolation_unbound'] = unbound
                report['isolation_verdict'] = _isolation_verdict(unbound)
                _assert_resolved_paths_isolated(lazy_resolved, scratch_root)
                try:
                    # Some writers build their path from the CWD rather than any
                    # root (services/review_cycle.py joins a relative 'state/...'),
                    # so the CWD has to be inside the scratch root too.
                    os.chdir(scratch_root)
                    sweep_return = spec.run(manager)
                except Exception as e:
                    # Deliberately not re-raised. A sweep that raises has already
                    # done some of its work, and "did the partial run leak?" is the
                    # question this harness exists to answer -- skipping steps 5-7
                    # to print a traceback answers it with an exit code that means
                    # something else entirely.
                    sweep_error = f'{type(e).__name__}: {e}'
                    logger.error(f'The sweep raised; continuing to the live-tree proof: {e}',
                                 exc_info=True)
    finally:
        os.chdir(previous_cwd)
        root_logger.removeHandler(handler)
        root_logger.setLevel(previous_level)

    report['sweep_return'] = sweep_return
    report['sweep_error'] = sweep_error
    report['state_files_in_copy'] = len(copy_manifest)
    report['external_effects'] = recorder.summary()
    report['external_effects'].update(
        {
            'neutralized': neutralize_external_effects_enabled,
            'declared_reads': list(spec.external_reads),
            'declared_writes': list(spec.external_writes),
        }
    )

    # --- 5. per-record accounting ----------------------------------------
    classification = classify_records(spec, handler.records)
    report['classification'] = classification
    report['gates'] = [
        {
            'name': gate.name,
            'why': gate.why,
            'degradation': gate.degradation,
            # Only reported as inert when neutralization is actually on: with
            # --no-neutralize-external-effects the real SET NX runs and the
            # count IS a measurement.
            'inert': (
                gate.inert_when_neutralized
                if gate.inert_when_neutralized and neutralize_external_effects_enabled
                else ''
            ),
            'count': len(classification['gate_records'].get(gate.name, [])),
        }
        for gate in spec.gates
    ]

    # Close the arithmetic. Every sweep drops records before its first named
    # gate -- detect_and_retry_empty_successful_executions() silently `continue`s
    # on any record whose last execution is not a 'success' -- and those are
    # invisible in a per-gate table. Reporting the residual explicitly is what
    # stops "0 reached the terminal decision" from being read as "every record
    # was individually considered and protected".
    #
    # Degradation gates are excluded on purpose: they annotate a record some
    # other gate also accounts for, so summing them double-counts and drives the
    # residual negative -- printing a negative under a sentence that claims a
    # specific safety meaning is worse than printing nothing.
    report['accounting_anomaly'] = None
    if classification['examined'] is not None:
        accounted = (
            sum(gate['count'] for gate in report['gates'] if not gate['degradation'])
            + len(classification['terminal_records'])
            + len(classification['undecided_candidates'])
        )
        residual = classification['examined'] - accounted
        report['unaccounted'] = max(0, residual)
        if residual < 0:
            report['accounting_anomaly'] = (
                f'the sweep accounted for {accounted} records but reported examining only '
                f'{classification["examined"]}; the residual would be {residual}. Either a '
                f'gate pattern matches a line it should not, or the examined count is not '
                f'what it claims -- this is a harness defect, not a property of the sweep.'
            )
    else:
        report['unaccounted'] = None

    # --- 6. diff the copy -------------------------------------------------
    copy_after = snapshot_tree(scratch_state_root)
    copy_diff = diff_manifests(copy_manifest, copy_after)
    report['copy_diff'] = copy_diff
    report['mutations'] = describe_execution_record_changes(
        pristine_root,
        scratch_state_root,
        [
            relpath for relpath in copy_diff['changed'] + copy_diff['added']
            if not _is_lock_artifact(relpath)
        ],
    )

    # --- 7. prove the live tree is untouched ------------------------------
    unreadable_after: List[str] = []
    live_after = snapshot_tree(live_state_root, unreadable_after)
    report['unreadable'] = sorted(set(unreadable) | set(unreadable_after))
    live_diff = diff_manifests(live_before, live_after)
    changed_live = set(live_diff['added']) | set(live_diff['removed']) | set(live_diff['changed'])
    lock_artifacts = sorted(p for p in changed_live if _is_lock_artifact(p))
    changed_live_state = changed_live - set(lock_artifacts)
    mutated_in_copy = {
        p for p in set(copy_diff['changed']) | set(copy_diff['added']) | set(copy_diff['removed'])
        if not _is_lock_artifact(p)
    }

    # Same relpath in both trees: the classic escape, and the only one a path
    # intersection can see.
    leaked = set(changed_live_state & mutated_in_copy)

    # A sweep that escaped isolation writes ONLY the live path -- the copy's
    # version of that relpath is untouched, so the intersection above is empty
    # by construction for exactly the failure mode this harness exists to catch.
    # Two further checks close that hole:
    #   (a) content attribution -- a live record whose delta is a decision this
    #       sweep made is this run's work, whatever path it landed at;
    #   (b) subtree ownership -- any other change under a subtree this sweep
    #       writes is indistinguishable from a leak and is never downgraded to
    #       "concurrent writes".
    owned_live_changes = sorted(
        p for p in changed_live_state if _is_under_subtree(p, spec.owned_state_subtrees)
    )
    if owned_live_changes:
        live_deltas = describe_execution_record_changes(
            pristine_root, live_state_root, owned_live_changes
        )
        decided = _mutation_fingerprints(report['mutations'])
        for entry in live_deltas:
            if _mutation_fingerprints([entry]) & decided:
                leaked.add(entry['file'])
    leaked_sorted = sorted(leaked)
    unattributed_owned = sorted(set(owned_live_changes) - leaked)

    report['live_check'] = {
        'before_count': len(live_before),
        'before_digest': manifest_digest(live_before),
        'after_count': len(live_after),
        'after_digest': manifest_digest(live_after),
        #  changed_live_state, not changed_live: flock artifacts are filtered out
        #  of every OTHER conclusion in this block (leaked, unattributed_owned),
        #  and _is_lock_artifact() exists precisely because they are "not
        #  evidence of anything". Letting them drive this flag drove the exit
        #  code too, so a run against the live orchestrator -- the harness's
        #  primary use -- reported UNVERIFIED/exit 3 the moment the orchestrator
        #  opened one new execution record beside a record this run also read.
        #  They stay in `lock_artifacts` for display.
        'identical': not changed_live_state,
        'diff': live_diff,
        'leaked': leaked_sorted,
        'unattributed_owned': unattributed_owned,
        'lock_artifacts': lock_artifacts,
    }

    # Ordering note (and why sweep_error does NOT come second): "the sweep
    # raised" and "this run could not be verified" are independent facts, and a
    # raise is the WEAKER of the two. Ranked above them, exit 4's "No leak was
    # detected" was asserting the one sentence an operator reads over the top of
    # an UNVERIFIABLE owned-subtree change -- the one thing the docstring, the
    # owned_state_subtrees comment and --allow-concurrent-writes' help text all
    # promise is never downgraded. Both unverifiable branches now outrank it and
    # compose with it, so a partial run that also drifted says both.
    unverifiable: List[str] = []
    if unattributed_owned:
        unverifiable.append(
            f'{len(unattributed_owned)} live path(s) changed inside a subtree this sweep '
            f'writes. A sweep that escapes isolation writes only the live path, so this '
            f'cannot be told apart from a leak. (--allow-concurrent-writes does not '
            f'downgrade this.)'
        )
    if report['unreadable']:
        unverifiable.append(
            f"{len(report['unreadable'])} file(s) under state/ could not be read, so they "
            f'are in neither manifest and no check in this run covers them.'
        )
    raised_prefix = f'SWEEP RAISED: {sweep_error}. The run is partial. ' if sweep_error else ''

    if leaked_sorted:
        report['verdict'] = (
            'FAILED: the sweep mutated live state -- the run was not isolated'
        )
        report['exit_code'] = 1
    elif unverifiable:
        report['verdict'] = (
            f'{raised_prefix}UNVERIFIED: ' + ' '.join(unverifiable)
            + ' Re-run against a stopped orchestrator.'
        )
        report['exit_code'] = 3
    elif sweep_error:
        report['verdict'] = (
            f'SWEEP RAISED: {sweep_error}. No leak was detected in what it managed to '
            f'do, but the run is partial and proves nothing about the sweep as a whole.'
        )
        report['exit_code'] = 4
    elif not changed_live_state:
        report['verdict'] = (
            'PASSED: the live state/ tree is byte-identical'
            if not lock_artifacts
            else (
                f'PASSED: no live state file changed. The only difference is '
                f'{len(lock_artifacts)} flock artifact(s), which carry no state'
            )
        )
        report['exit_code'] = 0
    elif allow_concurrent_writes:
        report['verdict'] = (
            'PASSED WITH DRIFT: live state changed outside every subtree this sweep '
            'writes, and no change matches a decision it made (--allow-concurrent-writes)'
            #  Said explicitly rather than left to the reader: a lock artifact
            #  INSIDE an owned subtree is filtered out before that claim is
            #  computed, so without this the sentence asserts something wider
            #  than what was checked.
            + (
                f'. {len(lock_artifacts)} flock artifact(s), which carry no state, are '
                f'excluded from that claim'
                if lock_artifacts
                else ''
            )
        )
        report['exit_code'] = 0
    else:
        report['verdict'] = (
            'UNVERIFIED: live state changed. No changed path is one this sweep '
            'owns or mutated, so this is consistent with the running orchestrator '
            'writing its own state -- but it is not proof. Re-run against a stopped '
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
    parser.add_argument(
        '--scratch',
        help=(
            'Scratch directory (default: a fresh temp dir). Must not already exist '
            'with contents -- see --keep-scratch.'
        ),
    )
    parser.add_argument(
        '--keep-scratch',
        action='store_true',
        help=(
            'Keep the scratch copy after the run (it holds the mutated records). '
            'Without this, a scratch directory THIS RUN CREATED is deleted; one that '
            'already existed is never deleted.'
        ),
    )
    parser.add_argument(
        '--allow-concurrent-writes',
        action='store_true',
        help=(
            'Treat live-tree drift that is NOT attributable to this run as success. '
            'Drift inside a subtree the sweep writes is never downgraded -- there it '
            'cannot be told apart from a leak.'
        ),
    )
    parser.add_argument(
        '--no-neutralize-external-effects',
        dest='neutralize_external_effects',
        action='store_false',
        help=(
            "Let the sweep's Redis writes and observability events reach production "
            'instead of being intercepted and reported. Requires '
            '--allow-external-side-effects for a sweep that declares writes.'
        ),
    )
    parser.add_argument(
        '--allow-external-side-effects',
        action='store_true',
        help=(
            'Required with --no-neutralize-external-effects for sweeps that write '
            'outside state/ (Redis keys, observability events)'
        ),
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
                _out(f'    WRITES outside state/ (neutralized unless '
                     f'--no-neutralize-external-effects): {note}')
            for note in spec.inert_guards:
                _out(f'    guard that cannot fire out-of-process: {note}')
        return 0

    if not args.sweep:
        parser.error('--sweep is required (or --list)')

    spec = SWEEPS.get(args.sweep)
    if spec is None:
        parser.error(
            f"unknown sweep {args.sweep!r}; known: {', '.join(sorted(SWEEPS))}"
        )

    deployment_root = _resolve_deployment_root(args.deployment_root)
    if not (deployment_root / 'state').is_dir():
        _out(f'No state/ directory under deployment root {deployment_root}')
        return 2

    config_root = Path(args.config_root).resolve() if args.config_root \
        else deployment_root / 'config'

    # A scratch directory this run did not create is never deleted. The teardown
    # below is a recursive delete on an operator-supplied path, and "--scratch
    # /workspace" on a tool whose entire premise is "prove nothing was
    # destroyed" must not be the way a working directory disappears.
    harness_created_scratch = False
    if args.scratch:
        scratch_root = Path(args.scratch).resolve()
        if scratch_root.exists():
            if not scratch_root.is_dir():
                _out(f'--scratch {scratch_root} exists and is not a directory')
                return 2
            if any(scratch_root.iterdir()):
                _out(f'--scratch {scratch_root} already exists and is not empty. Refusing '
                     f'to use it: the harness writes a state/ copy into the scratch '
                     f'directory and would then have to decide what of yours to delete. '
                     f'Name an empty or non-existent directory.')
                return 2
        else:
            harness_created_scratch = True
        scratch_root.mkdir(parents=True, exist_ok=True)
    else:
        scratch_root = Path(tempfile.mkdtemp(prefix='switchyard-dry-run-'))
        harness_created_scratch = True

    try:
        report = run_dry_run(
            spec,
            deployment_root=deployment_root,
            scratch_root=scratch_root,
            config_root=config_root,
            allow_concurrent_writes=args.allow_concurrent_writes,
            neutralize_external_effects_enabled=args.neutralize_external_effects,
            allow_external_side_effects=args.allow_external_side_effects,
        )
    except ExternalWriteRefused as e:
        _out(f'REFUSING to run {spec.name} with --no-neutralize-external-effects:')
        _out(str(e))
        _out('Pass --allow-external-side-effects if that is acceptable, or drop '
             '--no-neutralize-external-effects and let the harness intercept them.')
        return 2
    except IsolationError as e:
        _out()
        _out('!! ISOLATION CHECK FAILED -- the sweep was NOT run !!')
        _out(str(e))
        return 1

    print_report(report)

    if args.json_path:
        Path(args.json_path).write_text(json.dumps(report, indent=2, default=str))
        _out(f'JSON report written to {args.json_path}')

    if args.keep_scratch or not harness_created_scratch:
        _out(f'Scratch copy kept at {scratch_root}')
    else:
        try:
            shutil.rmtree(scratch_root)
        except OSError as e:
            # Not ignore_errors=True: a cleanup that failed leaves a full copy of
            # production state on disk, which the operator has to know about.
            _out(f'WARNING: could not remove the scratch copy at {scratch_root}: {e}')

    return report['exit_code']


if __name__ == '__main__':
    sys.exit(main())

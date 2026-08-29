"""
Pipeline Semaphore Manager

Manages counted concurrency for pipeline execution: up to N issues (epics) may
hold the semaphore for the same (project, board) at once, instead of
PipelineLockManager's single exclusive holder. Structurally mirrors
PipelineLockManager's Redis + YAML dual-store durability pattern — same key
scoping, same "read both stores, fail closed only when both are unreadable"
philosophy — generalized from one holder to a counted set of holders.

This is deliberately a separate class, not a modification of PipelineLockManager
itself: a lock and a semaphore have different correctness properties (there is
no single "the" holder to compare against, staleness has to be evaluated
per-holder rather than for one lock as a whole), and every existing caller of
PipelineLockManager depends on its current single-holder semantics unchanged.

Scope note: this is intentionally the minimal, correctly-shaped piece of the
concurrency redesign's per-epic semaphore (see switchyard #55) needed to have
the right abstraction in place — it is NOT yet wired into any live dispatch
path. Wiring it in as planning_design's (and later sdlc_execution's) actual
board-dispatch gate, alongside the concurrency config schema that determines
max_concurrent per project, is #63's job, not this one's. At the default
max_concurrent=1 a single-issue "semaphore" behaves like a lock, but nothing
here assumes or special-cases that — it is a genuine N-holder implementation
from the start.
"""

import yaml
import redis
import logging
import os
import time
from pathlib import Path
from typing import List, Optional, Tuple
from datetime import datetime, timezone
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# How long a holder may sit unreleased before it's considered abandoned and
# pruned on the next acquire attempt — self-healing against a process that
# died without releasing, the same role PipelineLockManager's Redis TTL plays
# for its single holder. Sized like PipelineLockManager's own lock TTL (2h);
# unlike a lock (held for one issue's whole pipeline run), a semaphore holder
# here represents "this epic is occupying one of the N dispatch slots" for a
# potentially longer window, so this errs generous rather than reclaiming a
# genuinely-still-active epic's slot out from under it.
DEFAULT_STALENESS_SECONDS = 14400  # 4 hours

# Atomically prune stale holders, treat a re-acquire by an existing holder as a
# no-op refresh, and otherwise admit a new holder only if under capacity — all
# in one server-side operation, so there is no window between checking
# capacity and claiming a slot for a concurrent caller to race into.
# KEYS[1] = redis hash key (field = issue_number, value = acquired_at unix ts)
# ARGV[1] = staleness cutoff (now - staleness_seconds)
# ARGV[2] = max_concurrent
# ARGV[3] = issue_number (as string)
# ARGV[4] = now (unix ts)
_TRY_ACQUIRE_SCRIPT = """
local all = redis.call('HGETALL', KEYS[1])
for i = 1, #all, 2 do
    local field = all[i]
    local value = tonumber(all[i + 1])
    if value and value < tonumber(ARGV[1]) then
        redis.call('HDEL', KEYS[1], field)
    end
end

if redis.call('HEXISTS', KEYS[1], ARGV[3]) == 1 then
    redis.call('HSET', KEYS[1], ARGV[3], ARGV[4])
    redis.call('EXPIRE', KEYS[1], 7200)
    return 1
end

local current = redis.call('HLEN', KEYS[1])
if current < tonumber(ARGV[2]) then
    redis.call('HSET', KEYS[1], ARGV[3], ARGV[4])
    redis.call('EXPIRE', KEYS[1], 7200)
    return 1
else
    return 0
end
"""


@dataclass
class PipelineSemaphoreState:
    """Current holders of a (project, board) semaphore."""
    project: str
    board: str
    holders: List[int] = field(default_factory=list)  # issue numbers, order not meaningful


class PipelineSemaphoreManager:
    """Manages counted concurrency locks with Redis + YAML persistence."""

    def __init__(self, state_dir: Path = None, redis_client=None):
        """
        Initialize pipeline semaphore manager.

        Args:
            state_dir: Directory for YAML state persistence
            redis_client: Optional Redis client (will create if not provided)
        """
        if state_dir is None:
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "pipeline_semaphores"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        self.redis_client = redis_client
        if self.redis_client is None:
            try:
                redis_host = os.environ.get('REDIS_HOST', 'redis')
                redis_port = int(os.environ.get('REDIS_PORT', 6379))
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=True,
                    socket_connect_timeout=5,
                    socket_timeout=5
                )
                self.redis_client.ping()
                logger.info(f"Connected to Redis at {redis_host}:{redis_port} for pipeline semaphores")
            except Exception as e:
                logger.warning(f"Redis connection failed for semaphores, using YAML only: {e}")
                self.redis_client = None

        logger.info(f"PipelineSemaphoreManager initialized with state_dir: {state_dir}")

    def _get_semaphore_key(self, project: str, board: str) -> str:
        """Get Redis key for the semaphore's holder hash."""
        return f"pipeline_semaphore:{project}:{board}"

    def _get_state_file(self, project: str, board: str) -> Path:
        """Get YAML state file path for the semaphore."""
        return self.state_dir / f"{project}_{board}.yaml"

    def _read_redis_holders_only(self, project: str, board: str) -> Tuple[Optional[List[int]], bool]:
        """Read holder issue numbers from Redis only. Returns (holders_or_None, read_succeeded)."""
        if not self.redis_client:
            return None, True  # no Redis configured isn't a read failure
        try:
            data = self.redis_client.hgetall(self._get_semaphore_key(project, board))
            if not data:
                return None, True
            return sorted(int(k) for k in data.keys()), True
        except Exception as e:
            logger.warning(f"Failed to get semaphore holders from Redis: {e}")
            return None, False

    def _read_yaml_holders_only(self, project: str, board: str) -> Tuple[Optional[List[int]], bool]:
        """Read holder issue numbers from the YAML file only. Returns (holders_or_None, read_succeeded)."""
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if not state_file.exists():
            return None, True
        try:
            lock_file = state_file.with_suffix(state_file.suffix + '.lock')
            with file_lock(lock_file):
                if not state_file.exists():
                    return None, True
                with open(state_file, 'r') as f:
                    data = yaml.safe_load(f)
                    if not data or not data.get('holders'):
                        return None, True
                    return sorted(int(h) for h in data['holders']), True
        except Exception as e:
            logger.error(f"Failed to load semaphore state from YAML: {e}")
            return None, False

    def get_holders(self, project: str, board: str) -> List[int]:
        """
        Get the current holder issue numbers for a (project, board) semaphore.

        Reads Redis first (fresher); falls back to YAML only if Redis has no
        entry. Best-effort — for a safety-critical acquire decision, use
        try_acquire()'s own internal fail-closed handling instead of building
        one on top of this method.
        """
        redis_holders, _ = self._read_redis_holders_only(project, board)
        if redis_holders is not None:
            return redis_holders
        yaml_holders, _ = self._read_yaml_holders_only(project, board)
        return yaml_holders or []

    def is_held_by(self, project: str, board: str, issue_number: int) -> bool:
        """True if issue_number currently holds a slot on this semaphore."""
        return issue_number in self.get_holders(project, board)

    def try_acquire(
        self,
        project: str,
        board: str,
        issue_number: int,
        max_concurrent: int = 1,
        staleness_seconds: int = DEFAULT_STALENESS_SECONDS,
    ) -> Tuple[bool, str]:
        """
        Attempt to acquire a semaphore slot for issue_number.

        Idempotent: if issue_number already holds a slot, this succeeds and
        refreshes its staleness timestamp rather than double-counting it.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number attempting to acquire a slot
            max_concurrent: Maximum concurrent holders for this (project, board)
            staleness_seconds: Age past which an unreleased holder is pruned

        Returns:
            (acquired: bool, reason: str)
        """
        if self.redis_client:
            try:
                now = time.time()
                cutoff = now - staleness_seconds
                acquired = self.redis_client.eval(
                    _TRY_ACQUIRE_SCRIPT,
                    1,
                    self._get_semaphore_key(project, board),
                    cutoff,
                    max_concurrent,
                    str(issue_number),
                    now,
                )
                if acquired:
                    self._sync_yaml_from_redis(project, board)
                    logger.info(
                        f"Pipeline semaphore slot acquired: {project}/{board} by issue "
                        f"#{issue_number} (max_concurrent={max_concurrent})"
                    )
                    return True, "acquired"

                holders, _ = self._read_redis_holders_only(project, board)
                logger.debug(
                    f"Pipeline semaphore {project}/{board} at capacity "
                    f"({max_concurrent} held by {holders}) — refusing issue #{issue_number}"
                )
                return False, f"at_capacity_held_by_{holders}"
            except Exception as e:
                logger.warning(
                    f"Redis error during semaphore acquire for {project}/{board}, "
                    f"falling back to YAML-only: {e}"
                )
                # Fall through to YAML-only path below.

        # YAML-only fallback (Redis unavailable or errored). file_lock() around
        # the whole read-modify-write gives the same atomicity the Lua script
        # gives on the Redis path, just via an exclusive file lock instead of
        # Redis's single-threaded script execution.
        return self._try_acquire_yaml_only(project, board, issue_number, max_concurrent, staleness_seconds)

    def _try_acquire_yaml_only(
        self,
        project: str,
        board: str,
        issue_number: int,
        max_concurrent: int,
        staleness_seconds: int,
    ) -> Tuple[bool, str]:
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        lock_file = state_file.with_suffix(state_file.suffix + '.lock')
        now = datetime.now(timezone.utc)

        try:
            with file_lock(lock_file):
                data = {}
                if state_file.exists():
                    with open(state_file, 'r') as f:
                        data = yaml.safe_load(f) or {}

                holder_entries = data.get('holder_entries', {})  # issue_number(str) -> iso timestamp
                cutoff = now.timestamp() - staleness_seconds
                holder_entries = {
                    k: v for k, v in holder_entries.items()
                    if datetime.fromisoformat(v).timestamp() >= cutoff
                }

                key = str(issue_number)
                if key not in holder_entries and len(holder_entries) >= max_concurrent:
                    return False, f"at_capacity_held_by_{sorted(int(k) for k in holder_entries)}"

                holder_entries[key] = now.isoformat()
                data.update({
                    'project': project,
                    'board': board,
                    'holder_entries': holder_entries,
                    'holders': sorted(int(k) for k in holder_entries),
                })
                with open(state_file, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)

                logger.info(
                    f"Pipeline semaphore slot acquired (YAML-only): {project}/{board} "
                    f"by issue #{issue_number} (max_concurrent={max_concurrent})"
                )
                return True, "acquired_yaml_only"
        except Exception as e:
            logger.error(f"Failed to acquire semaphore slot in YAML for {project}/{board}: {e}")
            return False, "yaml_write_failed"

    def release(self, project: str, board: str, issue_number: int) -> bool:
        """
        Release issue_number's semaphore slot for (project, board), if held.

        Safe to call even if the issue doesn't currently hold a slot (no-op).
        Returns True if the release was recorded in at least one durable
        store (mirrors PipelineLockManager's fail-open-across-two-stores
        pattern for release), False only if both Redis and YAML writes fail.
        """
        redis_ok = True
        if self.redis_client:
            try:
                self.redis_client.hdel(self._get_semaphore_key(project, board), str(issue_number))
            except Exception as e:
                logger.warning(f"Failed to release semaphore slot in Redis: {e}")
                redis_ok = False

        yaml_ok = self._release_yaml_only(project, board, issue_number)

        if not redis_ok and not yaml_ok:
            logger.error(
                f"release: BOTH Redis and YAML writes failed for {project}/{board} "
                f"issue #{issue_number} — slot may remain held until staleness prunes it"
            )
            return False

        logger.info(f"Pipeline semaphore slot released: {project}/{board} by issue #{issue_number}")
        return True

    def _release_yaml_only(self, project: str, board: str, issue_number: int) -> bool:
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if not state_file.exists():
            return True  # nothing to release
        lock_file = state_file.with_suffix(state_file.suffix + '.lock')
        try:
            with file_lock(lock_file):
                if not state_file.exists():
                    return True
                with open(state_file, 'r') as f:
                    data = yaml.safe_load(f) or {}
                holder_entries = data.get('holder_entries', {})
                holder_entries.pop(str(issue_number), None)
                data['holder_entries'] = holder_entries
                data['holders'] = sorted(int(k) for k in holder_entries)
                with open(state_file, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
            return True
        except Exception as e:
            logger.error(f"Failed to release semaphore slot in YAML: {e}")
            return False

    def _sync_yaml_from_redis(self, project: str, board: str) -> None:
        """Best-effort mirror of Redis's current holder set into YAML, so a
        later Redis outage can still fall back to a reasonably fresh view.
        Failure here is logged, not escalated — Redis remains the source of
        truth on the happy path; YAML is the durability net for when Redis
        itself is unavailable at acquire/release time."""
        try:
            redis_holders, redis_ok = self._read_redis_holders_only(project, board)
            if not redis_ok:
                return
            from utils.file_lock import safe_yaml_write

            state_file = self._get_state_file(project, board)
            with safe_yaml_write(state_file):
                data = {}
                if state_file.exists():
                    with open(state_file, 'r') as f:
                        data = yaml.safe_load(f) or {}
                now_iso = datetime.now(timezone.utc).isoformat()
                holder_entries = data.get('holder_entries', {})
                # Keep existing timestamps for holders Redis still agrees are
                # active; stamp any holder Redis has that YAML didn't know
                # about yet with "now" rather than losing its real acquire time.
                new_entries = {
                    str(h): holder_entries.get(str(h), now_iso)
                    for h in (redis_holders or [])
                }
                data.update({
                    'project': project,
                    'board': board,
                    'holder_entries': new_entries,
                    'holders': sorted(redis_holders or []),
                })
                with open(state_file, 'w') as f:
                    yaml.dump(data, f, default_flow_style=False, sort_keys=False)
        except Exception as e:
            logger.debug(f"Failed to sync semaphore YAML mirror for {project}/{board}: {e}")


# Singleton instance
_pipeline_semaphore_manager = None


def get_pipeline_semaphore_manager() -> PipelineSemaphoreManager:
    """Get singleton instance of PipelineSemaphoreManager"""
    global _pipeline_semaphore_manager
    if _pipeline_semaphore_manager is None:
        _pipeline_semaphore_manager = PipelineSemaphoreManager()
    return _pipeline_semaphore_manager

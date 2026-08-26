"""
Pipeline Lock Manager

Manages exclusive locks for pipeline execution to prevent concurrent work
on multiple issues within the same pipeline (project + board).

Only ONE issue can hold the pipeline lock at a time. Other issues wait in queue.
"""

import yaml
import redis
import logging
import os
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)


@dataclass
class PipelineLock:
    """Exclusive lock for pipeline execution"""
    project: str
    board: str
    locked_by_issue: int
    lock_acquired_at: str
    lock_status: str  # 'locked', 'unlocked'
    # Set by mark_lock_failed() when a pipeline run for the holding issue is durably
    # marked as failed. Non-None means: this lock must NEVER be auto-recovered by
    # staleness/TTL/restart-sync logic, and the holding issue must never be
    # re-dispatched. Only an explicit release_lock() call (a human recovery action,
    # e.g. via scripts/release_lock.py) clears it. This is the durable replacement
    # for the old, non-durable work_execution_state halt-marker mechanism — it lives
    # on the lock itself (Redis + YAML, no TTL on the YAML copy) rather than on the
    # PipelineRun record, which is only cached in Redis for a few hours and rolled
    # off Elasticsearch after 7 days.
    retained_reason: Optional[str] = None
    retained_at: Optional[str] = None

    def __post_init__(self):
        # Normalize a whitespace-only or empty retained_reason to None at the
        # type level, not just in PipelineLockManager.mark_lock_failed. Every
        # read site in the codebase checks `if lock.retained_reason:` — a
        # falsy-but-non-None string (constructed directly, or deserialized from
        # a hand-edited/malformed YAML file, e.g. retained_reason: "") would
        # otherwise be silently treated as "not retained" everywhere, exactly
        # the collapse mark_lock_failed's own validation guards against for its
        # one call path. This closes it for every other way a PipelineLock can
        # come into existence too.
        if self.retained_reason is not None and not self.retained_reason.strip():
            self.retained_reason = None
            self.retained_at = None

    @property
    def is_retained(self) -> bool:
        """True if this lock is durably marked retained-due-to-failure."""
        return bool(self.retained_reason)


class PipelineLockManager:
    """Manages pipeline execution locks with Redis + YAML persistence"""

    def __init__(self, state_dir: Path = None, redis_client=None):
        """
        Initialize pipeline lock manager.

        Args:
            state_dir: Directory for YAML state persistence
            redis_client: Optional Redis client (will create if not provided)
        """
        if state_dir is None:
            import os
            orchestrator_root = os.environ.get('ORCHESTRATOR_ROOT', '/app')
            state_dir = Path(orchestrator_root) / "state" / "pipeline_locks"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        # Initialize Redis client
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
                logger.info(f"Connected to Redis at {redis_host}:{redis_port} for pipeline locks")
            except Exception as e:
                logger.warning(f"Redis connection failed for locks, using YAML only: {e}")
                self.redis_client = None

        logger.info(f"PipelineLockManager initialized with state_dir: {state_dir}")

    def _get_lock_key(self, project: str, board: str) -> str:
        """Get Redis key for lock"""
        return f"pipeline_lock:{project}:{board}"

    def _get_state_file(self, project: str, board: str) -> Path:
        """Get YAML state file path for lock"""
        return self.state_dir / f"{project}_{board}.yaml"

    @staticmethod
    def _lock_to_redis_mapping(lock: PipelineLock) -> dict:
        """
        Serialize a PipelineLock for redis hset. redis-py rejects None values in a
        hset mapping, so Optional fields (retained_reason/retained_at) are encoded
        as empty string and decoded back to None in _lock_from_redis_data.
        """
        data = asdict(lock)
        return {k: ('' if v is None else v) for k, v in data.items()}

    @staticmethod
    def _lock_from_redis_data(lock_data: dict) -> PipelineLock:
        """Deserialize a PipelineLock from a redis hgetall result (see _lock_to_redis_mapping)."""
        return PipelineLock(
            project=lock_data['project'],
            board=lock_data['board'],
            locked_by_issue=int(lock_data['locked_by_issue']),
            lock_acquired_at=lock_data['lock_acquired_at'],
            lock_status=lock_data['lock_status'],
            retained_reason=(lock_data.get('retained_reason') or None),
            retained_at=(lock_data.get('retained_at') or None),
        )

    def _read_redis_lock_only(self, project: str, board: str) -> Tuple[Optional[PipelineLock], bool]:
        """Read the lock from Redis only. Returns (lock_or_None, read_succeeded)."""
        if not self.redis_client:
            return None, True  # no Redis configured isn't a read failure
        try:
            lock_data = self.redis_client.hgetall(self._get_lock_key(project, board))
            if lock_data and lock_data.get('lock_status') == 'locked':
                return self._lock_from_redis_data(lock_data), True
            return None, True
        except Exception as e:
            logger.warning(f"Failed to get lock from Redis: {e}")
            return None, False

    def _read_yaml_lock_only(self, project: str, board: str) -> Tuple[Optional[PipelineLock], bool]:
        """Read the lock from the YAML file only. Returns (lock_or_None, read_succeeded)."""
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if not state_file.exists():
            return None, True
        try:
            lock_file = state_file.with_suffix(state_file.suffix + '.lock')
            with file_lock(lock_file):
                if not state_file.exists():  # Check again inside lock
                    return None, True
                with open(state_file, 'r') as f:
                    lock_data = yaml.safe_load(f)
                    if lock_data and lock_data.get('lock_status') == 'locked':
                        return PipelineLock(**lock_data), True
                    return None, True
        except Exception as e:
            logger.error(f"Failed to load lock state from YAML: {e}")
            return None, False

    def get_lock(self, project: str, board: str) -> Optional[PipelineLock]:
        """
        Get current lock state for a pipeline.

        Reads BOTH Redis and the YAML file (not Redis-then-fallback-only-if-empty)
        specifically so that retained_reason — the durable failure marker — can
        never be masked by a stale copy in the other store. Redis is preferred as
        the base record (fresher for ordinary fields), but if either store shows
        retained_reason set and the other doesn't (e.g. mark_lock_failed's Redis
        write raced or failed while the YAML write succeeded, or vice versa), the
        retained fields are merged in from whichever store has them. Without this,
        a transient write failure on one side could silently un-block a failed
        issue until the stale side's data changed on its own.

        Returns:
            PipelineLock if locked, None if unlocked
        """
        redis_lock, _ = self._read_redis_lock_only(project, board)
        yaml_lock, _ = self._read_yaml_lock_only(project, board)

        if redis_lock and yaml_lock and redis_lock.locked_by_issue == yaml_lock.locked_by_issue:
            if yaml_lock.retained_reason and not redis_lock.retained_reason:
                redis_lock.retained_reason = yaml_lock.retained_reason
                redis_lock.retained_at = yaml_lock.retained_at
            return redis_lock

        if redis_lock:
            return redis_lock
        return yaml_lock

    def get_lock_fail_closed(self, project: str, board: str) -> Tuple[Optional[PipelineLock], bool]:
        """
        Like get_lock(), but also reports whether the read was trustworthy.

        Returns (lock_or_None, reads_healthy). reads_healthy is False only when
        BOTH Redis and YAML reads raised — i.e. lock state is genuinely unknown,
        not just "confirmed empty". Callers making a safety-critical decision
        (see try_acquire_lock's durable retained-lock check) should treat
        reads_healthy=False as "assume retained, refuse" rather than "assume
        unlocked, proceed" — the whole point of this durable check is to never
        silently grant a lock it can't actually verify is safe to grant.
        """
        redis_lock, redis_ok = self._read_redis_lock_only(project, board)
        yaml_lock, yaml_ok = self._read_yaml_lock_only(project, board)

        if not redis_ok and not yaml_ok:
            return None, False

        # Asymmetric case that matters: YAML is the only non-expiring store for
        # retained_reason (Redis's TTL can lapse on a lock nothing is
        # legitimately re-touching, which is expected for a retained lock). If
        # YAML couldn't be read AND Redis has no entry either (redis_lock is
        # None — could mean "genuinely unlocked" or "TTL'd out on a retained
        # lock"), we cannot distinguish those two cases, so this must be
        # unhealthy too — not just the both-raised case above. If Redis DOES
        # have an entry, it's a definitive answer on its own regardless of
        # YAML's state, so that case is left to the normal merge below.
        if not yaml_ok and redis_lock is None:
            return None, False

        if redis_lock and yaml_lock and redis_lock.locked_by_issue == yaml_lock.locked_by_issue:
            if yaml_lock.retained_reason and not redis_lock.retained_reason:
                redis_lock.retained_reason = yaml_lock.retained_reason
                redis_lock.retained_at = yaml_lock.retained_at
            return redis_lock, True

        return (redis_lock or yaml_lock), True

    def try_acquire_lock(
        self,
        project: str,
        board: str,
        issue_number: int
    ) -> Tuple[bool, str]:
        """
        Attempt to acquire pipeline lock with safety checks.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number attempting to acquire lock

        Returns:
            (can_execute: bool, reason: str)
        """
        # DURABLE SAFETY CHECK — must run before any Redis TTL/staleness logic below.
        # get_lock_fail_closed() consults BOTH Redis and YAML (see get_lock()'s
        # docstring), so this is correct even if the Redis copy of the lock has
        # expired, was never synced back after a restart, or one store's write
        # failed while the other's succeeded. A lock retained after a pipeline run
        # failure (see PipelineLock.retained_reason / mark_lock_failed) must never
        # be recovered by the 4-hour staleness heuristics further down — those exist
        # for ordinary abandoned locks, not deliberately-retained ones — and it must
        # never look "gone" just because its Redis TTL lapsed while nothing was
        # legitimately re-touching it (which is expected: nothing should be retrying
        # a failed, retained issue).
        #
        # Refuses for the SAME issue too, not just other issues: the dispatch gate
        # in project_monitor.py is supposed to refuse a retained/failed issue before
        # ever calling try_acquire_lock again for it, but this is defense-in-depth
        # against any current or future code path that skips that gate (e.g. a
        # non-trigger-column re-entry) — such a path must not be able to silently
        # slip through via the "already_holds_lock" branch, which (via
        # _create_lock_yaml_only) would otherwise also risk overwriting the durable
        # YAML copy. The one legitimate "same issue, not retained" case (refreshing
        # TTL on an active lock) is unaffected — this only fires when retained_reason
        # is actually set.
        #
        # Fails CLOSED, not open: if both Redis and YAML reads fail (lock state is
        # genuinely unknown, not just confirmed-empty), refuse acquisition rather
        # than silently proceeding as if the lock were free.
        existing_lock, reads_healthy = self.get_lock_fail_closed(project, board)
        if not reads_healthy:
            logger.error(
                f"try_acquire_lock: could not determine lock state for {project}/{board} "
                f"(both Redis and YAML reads failed) — refusing acquisition by issue "
                f"#{issue_number} rather than risk granting it while a retained/failed "
                f"lock might actually be held"
            )
            return False, "lock_state_unknown_failing_closed"
        if existing_lock and existing_lock.retained_reason:
            logger.debug(
                f"Pipeline {project}/{board} lock is retained (failed run, issue "
                f"#{existing_lock.locked_by_issue}: {existing_lock.retained_reason}) — "
                f"refusing acquisition by issue #{issue_number}"
            )
            return False, f"locked_by_issue_{existing_lock.locked_by_issue}_failed"

        # Try to acquire via Redis using atomic transaction (WATCH/MULTI)
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                
                # Use pipeline for optimistic locking
                with self.redis_client.pipeline() as pipe:
                    while True:
                        try:
                            # Watch the lock key for changes
                            pipe.watch(lock_key)
                            
                            # Check if lock exists
                            if pipe.exists(lock_key):
                                # Lock exists - check who owns it
                                # We must execute this read immediately (not in transaction)
                                # But pipe is in watch mode, so commands are buffered? 
                                # No, in redis-py pipeline, commands are buffered unless we call execute()
                                # But we need to read the value to decide.
                                # Standard pattern: pipe.watch(key); val = pipe.get(key); ...
                                
                                # We need a separate client or break out of pipeline to read?
                                # No, the pipeline object acts as a client.
                                # But in redis-py, calling methods on pipeline buffers them.
                                # EXCEPT when using watch, we can read before multi().
                                
                                # Actually, let's just read it.
                                # pipe.watch(lock_key) puts us in watch mode.
                                # We can't read with 'pipe' and get result immediately?
                                # Yes we can, before multi().
                                
                                # Wait, redis-py pipeline behavior:
                                # "When using a pipeline, commands are buffered..."
                                # But we need to read.
                                # Correct pattern:
                                # pipe.watch(key)
                                # current_value = pipe.hgetall(key) # This might return the pipeline object, not result?
                                # No, standard redis-py pipeline does not return results immediately.
                                
                                # We should use the callback form of transaction or just use the client for reading.
                                # But we need to watch.
                                pass
                                
                            # Let's use the transaction method which is cleaner in redis-py
                            # self.redis_client.transaction(func, *keys)
                            
                            def acquire_lock_tx(pipe):
                                lock_data = pipe.hgetall(lock_key)
                                
                                if lock_data and lock_data.get('lock_status') == 'locked':
                                    # Lock exists
                                    locked_by = int(lock_data.get('locked_by_issue', 0))
                                    if locked_by == issue_number:
                                        # Already held by us - refresh TTL
                                        pipe.multi()
                                        pipe.expire(lock_key, 7200)
                                        return "already_holds_lock"
                                    
                                    # Check for stale lock
                                    try:
                                        lock_acquired_at = lock_data.get('lock_acquired_at')
                                        if lock_acquired_at:
                                            acquired_time = datetime.fromisoformat(lock_acquired_at)
                                            lock_age = datetime.now(timezone.utc) - acquired_time
                                            if lock_age > timedelta(hours=4):
                                                # Stale - overwrite it
                                                # Proceed to acquire logic below
                                                pass
                                            else:
                                                # Locked by someone else
                                                return f"locked_by_issue_{locked_by}"
                                        else:
                                            return f"locked_by_issue_{locked_by}"
                                    except Exception:
                                        return f"locked_by_issue_{locked_by}"
                                
                                # Not locked or stale - acquire it
                                new_lock = PipelineLock(
                                    project=project,
                                    board=board,
                                    locked_by_issue=issue_number,
                                    lock_acquired_at=datetime.now(timezone.utc).isoformat(),
                                    lock_status='locked'
                                )
                                
                                pipe.multi()
                                pipe.hset(lock_key, mapping=self._lock_to_redis_mapping(new_lock))
                                pipe.expire(lock_key, 7200)
                                return "lock_acquired"

                            result = self.redis_client.transaction(acquire_lock_tx, lock_key, value_from_callable=True)
                            
                            # If we got here, transaction succeeded (or returned early)
                            if result in ["lock_acquired", "already_holds_lock", "stale_lock_recovered"]:
                                # We acquired/held the lock in Redis. Now sync to YAML.
                                # Note: There is a small window where Redis has lock but YAML doesn't.
                                # This is acceptable as Redis is primary.
                                self._create_lock_yaml_only(project, board, issue_number)
                                return True, result
                            else:
                                return False, result

                        except redis.WatchError:
                            # Lock changed while we were watching - retry loop
                            continue
                            
            except Exception as e:
                logger.warning(f"Redis lock acquisition failed, falling back to YAML: {e}")
                # Fall through to YAML fallback

        # Fallback to YAML (original logic, but only if Redis failed or not available)
        # Note: If Redis is available but we failed to acquire (locked by other), we returned False above.
        # We only reach here if self.redis_client is None or Redis threw an exception (connection error).
        
        lock = self.get_lock(project, board)

        # Case 1: No existing lock - acquire immediately
        if not lock or lock.lock_status == 'unlocked':
            self._create_lock(project, board, issue_number)
            return True, "lock_acquired"

        # Case 2: Lock held by THIS issue - already has access
        if lock.locked_by_issue == issue_number:
            logger.debug(f"Issue #{issue_number} already holds lock for {project}/{board}")
            return True, "already_holds_lock"

        # Case 3: Lock held by another issue - check if lock is stale
        try:
            lock_acquired_time = datetime.fromisoformat(lock.lock_acquired_at)
            lock_age = datetime.now(timezone.utc) - lock_acquired_time

            # Stale lock threshold: 4 hours
            if lock_age > timedelta(hours=4):
                # Defense-in-depth re-check: the upfront durable check at the top
                # of this function already refuses when this lock is retained, so
                # this should be unreachable in the normal case — but this is a
                # separate, later read of the same lock (this whole branch only
                # runs when Redis is down, forcing the YAML-fallback path), so a
                # narrow race against a concurrent mark_lock_failed() call between
                # that check and this one isn't provably impossible. Re-check
                # rather than let _create_lock silently wipe a retained lock.
                if lock.retained_reason:
                    logger.error(
                        f"Refusing to auto-recover 'stale' lock for {project}/{board} "
                        f"— it is actually retained due to a failed run on issue "
                        f"#{lock.locked_by_issue} ({lock.retained_reason})"
                    )
                    return False, f"locked_by_issue_{lock.locked_by_issue}_failed"

                logger.warning(
                    f"Stale lock detected for {project}/{board} "
                    f"(held by #{lock.locked_by_issue} for {lock_age})"
                )

                # Auto-release stale lock and acquire
                logger.info(
                    f"Auto-releasing stale lock (issue #{lock.locked_by_issue})"
                )
                released = self.release_lock(project, board, lock.locked_by_issue)
                if not released:
                    logger.error(
                        f"Could not release stale lock for {project}/{board} held "
                        f"by issue #{lock.locked_by_issue} — refusing to steal it "
                        f"via a fresh lock creation."
                    )
                    return False, f"locked_by_issue_{lock.locked_by_issue}"
                self._create_lock(project, board, issue_number)
                return True, "stale_lock_recovered"
        except Exception as e:
            logger.warning(f"Failed to check lock age: {e}")

        # Case 4: Lock held by another issue (not stale)
        logger.debug(
            f"Pipeline {project}/{board} locked by issue #{lock.locked_by_issue}, "
            f"issue #{issue_number} must wait"
        )
        return False, f"locked_by_issue_{lock.locked_by_issue}"

    def _create_lock_yaml_only(self, project: str, board: str, issue_number: int):
        """
        Sync the Redis-acquired lock to YAML (helper for Redis sync — called after
        every successful try_acquire_lock, including the "already_holds_lock" result
        which fires on every poll while an issue holds the lock).

        If a YAML lock already exists for this (project, board) held by the same
        issue, its fields — most importantly retained_reason/retained_at — are
        preserved rather than overwritten with a fresh, blank PipelineLock. Blindly
        overwriting here would silently wipe the durable failure marker back to
        whatever the (TTL'd) Redis copy still has the moment anything re-touches the
        lock, defeating the durability this mechanism exists to provide. In practice
        try_acquire_lock's upfront durable check refuses before ever reaching here
        for a retained lock, but this stays defensive against that check's own
        edge cases (e.g. a narrow race with mark_lock_failed's writes) rather than
        relying on a single layer of protection.
        """
        existing, _ = self._read_yaml_lock_only(project, board)
        if existing and existing.locked_by_issue == issue_number:
            if existing.lock_status == 'locked':
                return  # nothing changed — avoid an unnecessary rewrite
            existing.lock_status = 'locked'
            self._save_lock_to_yaml(existing)
            return

        lock = PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked'
        )
        self._save_lock_to_yaml(lock)

    def _create_lock(self, project: str, board: str, issue_number: int):
        """Create a new lock (Legacy/Fallback method)"""
        lock = PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked'
        )

        # Write to Redis with 2 hour TTL
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                self.redis_client.hset(lock_key, mapping=self._lock_to_redis_mapping(lock))
                self.redis_client.expire(lock_key, 7200)  # 2 hours
                logger.debug(f"Created lock in Redis: {lock_key}")
            except Exception as e:
                logger.error(f"Failed to create lock in Redis: {e}")

        # Write to YAML for persistence
        self._save_lock_to_yaml(lock)

        logger.info(
            f"Pipeline lock acquired: {project}/{board} by issue #{issue_number}"
        )

    def release_lock(self, project: str, board: str, issue_number: int, force: bool = False) -> bool:
        """
        Release pipeline lock safely.

        Refuses to release a lock currently marked retained-due-to-failure
        (PipelineLock.retained_reason) unless force=True — this is the
        enforcement point for "only an explicit human recovery action clears a
        retained lock" (see mark_lock_failed's docstring). Without this guard,
        several ordinary, automatic call sites (closing the GitHub issue,
        reaching an exit column, pipeline_progression's own release-and-advance
        logic) would silently release a retained lock outside
        scripts/release_lock.py, which is the only caller that should ever pass
        force=True — it does so only after its own explicit confirmation.

        Also fails CLOSED like try_acquire_lock: if lock state genuinely can't
        be determined (both Redis and YAML reads fail) and force is not set,
        refuses rather than risk releasing a lock that might be retained.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number releasing the lock
            force: Release even if the lock is retained-due-to-failure, or if
                lock state couldn't be determined. Only scripts/release_lock.py
                should ever pass this.

        Returns:
            True if lock was released, False if not held by this issue, or if
            it's retained/unknown and force was not set.
        """
        if not force:
            existing_lock, reads_healthy = self.get_lock_fail_closed(project, board)
            if not reads_healthy:
                logger.error(
                    f"release_lock: could not determine lock state for "
                    f"{project}/{board} (both Redis and YAML reads failed) — "
                    f"refusing to release without force=True"
                )
                return False
            if existing_lock and existing_lock.retained_reason:
                # Deliberately NOT conditioned on
                # existing_lock.locked_by_issue == issue_number — a retained
                # lock must refuse release regardless of which issue_number the
                # caller names. Several automatic call sites release on behalf
                # of a queue entry or a different issue than the current holder
                # (e.g. cleanup/failsafe paths); if this guard only fired for
                # the exact holder, any of those could still delete a retained
                # lock out from under a different, failed issue.
                logger.warning(
                    f"release_lock: refusing to release {project}/{board} "
                    f"(requested on behalf of issue #{issue_number}) — it is "
                    f"retained due to a failed run on issue "
                    f"#{existing_lock.locked_by_issue} "
                    f"({existing_lock.retained_reason}). Only "
                    f"scripts/release_lock.py's deliberate recovery flow may "
                    f"clear this (pass force=True)."
                )
                return False

        # Atomic release via Redis. Tracks whether Redis actually reached a
        # definitive, trustworthy ownership result — used below to decide
        # whether the YAML ownership check can be safely skipped. Previously
        # this was inferred from `self.redis_client` being configured at all,
        # which is also true when Redis IS configured but the transaction
        # raised (connection drop, timeout) — in that case the YAML delete
        # below would proceed with should_delete defaulting to True and no
        # ownership re-check, potentially deleting the one non-expiring copy
        # of a lock held by someone else.
        redis_confirmed_ownership = False
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                
                with self.redis_client.pipeline() as pipe:
                    while True:
                        try:
                            pipe.watch(lock_key)
                            
                            # Check if lock exists and who owns it
                            # We need to read inside the watch block
                            # Since we can't easily read-and-branch in a pipeline without custom logic,
                            # we'll use the transaction callback pattern again or just read.
                            
                            # Read directly (watched)
                            # Note: In redis-py, if we watch a key, we can read it with the client (not pipe)
                            # or use the transaction callback.
                            
                            def release_lock_tx(pipe):
                                lock_data = pipe.hgetall(lock_key)
                                if not lock_data:
                                    # Lock doesn't exist - nothing to release
                                    return "not_found"
                                
                                locked_by = int(lock_data.get('locked_by_issue', 0))
                                if locked_by != issue_number:
                                    # Held by someone else
                                    return "held_by_other"
                                
                                # Held by us - delete it
                                pipe.multi()
                                pipe.delete(lock_key)
                                return "released"

                            result = self.redis_client.transaction(release_lock_tx, lock_key, value_from_callable=True)
                            
                            if result == "held_by_other":
                                logger.warning(
                                    f"Issue #{issue_number} attempted to release lock for {project}/{board} "
                                    f"but it is held by another issue"
                                )
                                return False
                            elif result == "not_found":
                                # Already gone from Redis, consider it success
                                # (idempotent) — but this is NOT an ownership
                                # confirmation, so redis_confirmed_ownership stays
                                # False. A retained lock's Redis copy is EXPECTED
                                # to TTL out (2h) since nothing legitimately
                                # re-touches a retained lock, so "not_found" is
                                # actually the normal steady state for a lock
                                # retained more than two hours — treating it as a
                                # confirmed release here would skip the YAML
                                # ownership check below and let the one
                                # non-expiring copy of a retained lock be deleted
                                # with no validation. This was a real bug in the
                                # first version of this fix.
                                logger.debug(f"Lock for {project}/{board} already gone from Redis during release by #{issue_number}")
                                # Fall through to clean up YAML just in case
                                break
                            else:
                                logger.debug(f"Deleted lock from Redis: {project}/{board}")
                                redis_confirmed_ownership = True
                                break
                                
                        except redis.WatchError:
                            continue
                            
            except Exception as e:
                logger.error(f"Failed to delete lock from Redis: {e}")

        # Update YAML to unlocked state (Fallback/Sync)
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if state_file.exists():
            try:
                # Use file lock when deleting to prevent race with writers
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(lock_file):
                    if state_file.exists():  # Check again inside lock
                        # Double check ownership in YAML unless Redis already gave us
                        # a definitive, trustworthy answer. Gated on
                        # redis_confirmed_ownership, NOT on `self.redis_client` being
                        # configured — those are different things: Redis can be
                        # configured but the transaction above can still have raised
                        # (connection drop, timeout), in which case
                        # redis_confirmed_ownership stays False and we must not skip
                        # this check, or a lock held by someone else could be
                        # deleted here with no ownership validation at all.
                        should_delete = True
                        if not redis_confirmed_ownership:
                            try:
                                with open(state_file, 'r') as f:
                                    lock_data = yaml.safe_load(f)
                                    if lock_data and int(lock_data.get('locked_by_issue', 0)) != issue_number:
                                        should_delete = False
                                        logger.warning(f"YAML lock held by {lock_data.get('locked_by_issue')} != {issue_number}")
                                    elif lock_data and lock_data.get('retained_reason') and not force:
                                        # Same defense-in-depth as the upfront guard —
                                        # this YAML record is retained; refuse even if
                                        # locked_by_issue happens to match (shouldn't
                                        # be reachable given the upfront check, but
                                        # this is the last line of defense before
                                        # deleting the only non-expiring copy). Respects
                                        # force just like the upfront guard does — this
                                        # is the deliberate-recovery path's own release
                                        # call, which legitimately needs to delete a
                                        # retained lock's YAML record.
                                        should_delete = False
                                        logger.warning(
                                            f"YAML lock for {project}/{board} is retained "
                                            f"due to a failed run — refusing to delete "
                                            f"(reason: {lock_data.get('retained_reason')})"
                                        )
                            except Exception as read_err:
                                # Fail CLOSED, not open: this read exists specifically
                                # to verify ownership before deleting the one
                                # non-expiring copy of a lock. An unreadable file
                                # (I/O error, corrupt/partial YAML) means that
                                # verification could not happen — it must not be
                                # treated as "safe to delete anyway".
                                should_delete = False
                                logger.error(
                                    f"Failed to read YAML lock state for {project}/{board} "
                                    f"during ownership verification — refusing to delete "
                                    f"rather than risk removing a lock held by someone "
                                    f"else: {read_err}"
                                )

                        if should_delete:
                            try:
                                state_file.unlink()
                            except Exception as unlink_err:
                                # Deliberately NOT covered by the outer except below —
                                # that one is scoped to the ownership-check machinery
                                # above (file_lock acquisition, etc.) and is allowed to
                                # fall through since it runs before any destructive
                                # action is taken. This one guards the destructive
                                # action itself: if unlink() raises, the (possibly
                                # still-retained) YAML record survives on disk, and
                                # this must return False rather than fall through to
                                # the success path below — previously it did, so
                                # release_lock reported success while a retained
                                # lock's YAML record silently remained.
                                logger.error(
                                    f"Failed to delete lock YAML file {state_file}: {unlink_err}"
                                )
                                return False
                            logger.debug(f"Deleted lock YAML file: {state_file}")
                        else:
                            return False

            except Exception as e:
                # Any failure here (e.g. file_lock acquisition) means we could not
                # even attempt/verify the deletion above — fail closed rather than
                # report a release that may not have happened.
                logger.error(f"Failed to delete lock YAML: {e}")
                return False

        logger.info(
            f"Pipeline lock released: {project}/{board} by issue #{issue_number}"
        )
        return True

    def steal_lock(self, project: str, board: str, new_issue_number: int) -> Tuple[bool, str]:
        """
        Forcibly transfer the (project, board) lock to new_issue_number,
        refusing if the current holder's lock is retained-due-to-failure.

        This is the ONLY sanctioned way to hand a lock to a different issue
        than its current (non-retained) holder — e.g. repair cycles, which
        have priority over whatever ordinary agent currently holds a board's
        lock. It exists specifically so _create_lock()/_create_lock_yaml_only()
        never need to be called directly from outside this class again for
        this purpose: two real bugs across this PR's review rounds came from
        call sites doing the retained_reason check and the lock-construction
        call as two separately-maintained steps, where a future edit can drop
        the check while leaving the construction call intact and unnoticed.
        Centralizing both here removes that failure mode structurally instead
        of relying on every caller to re-derive it correctly.

        Uses get_lock_fail_closed() internally — refuses (rather than silently
        proceeding as if unlocked) when lock state genuinely can't be
        determined.

        Returns:
            (True, "stolen") — the lock was released from its prior holder and
                now belongs to new_issue_number.
            (True, "already_held") — new_issue_number already held the lock;
                nothing changed.
            (True, "acquired") — nothing was locked; a fresh lock was created.
            (False, "retained:<reason>") — the current holder's lock is
                retained due to a failed run; refused.
            (False, "lock_state_unknown") — lock state could not be
                determined (both Redis and YAML reads failed); refused.
            (False, "release_failed") — stealing required releasing the prior
                holder's lock and that release itself failed.
        """
        current_lock, reads_healthy = self.get_lock_fail_closed(project, board)
        if not reads_healthy:
            logger.error(
                f"steal_lock: could not determine lock state for {project}/{board} "
                f"(both Redis and YAML reads failed) — refusing to steal/create "
                f"rather than risk creating a lock while a retained one might "
                f"actually be held"
            )
            return False, "lock_state_unknown"

        if current_lock and current_lock.locked_by_issue != new_issue_number:
            if current_lock.retained_reason:
                logger.error(
                    f"steal_lock: {project}/{board} cannot be stolen for issue "
                    f"#{new_issue_number} — it is retained due to a failed run on "
                    f"issue #{current_lock.locked_by_issue} "
                    f"({current_lock.retained_reason}). A human must run "
                    f"scripts/release_lock.py before this board can be used by "
                    f"any other issue."
                )
                return False, f"retained:{current_lock.retained_reason}"

            logger.warning(
                f"steal_lock: transferring {project}/{board} from issue "
                f"#{current_lock.locked_by_issue} to issue #{new_issue_number}"
            )
            # force=True is safe here: we already confirmed above the lock isn't
            # retained, so this releases an ordinary, actively-held lock, not
            # bypassing the recovery flow.
            released = self.release_lock(project, board, current_lock.locked_by_issue, force=True)
            if not released:
                logger.error(
                    f"steal_lock: could not release the lock held by issue "
                    f"#{current_lock.locked_by_issue} for {project}/{board} — "
                    f"refusing to steal it via a fresh lock creation."
                )
                return False, "release_failed"
            self._create_lock(project, board, new_issue_number)
            return True, "stolen"
        elif not current_lock:
            self._create_lock(project, board, new_issue_number)
            return True, "acquired"
        else:
            return True, "already_held"

    def mark_lock_failed(self, project: str, board: str, issue_number: int, reason: str) -> bool:
        """
        Durably mark the current lock as retained due to a failed pipeline run.

        This is the enforcement-critical write for the whole "Failed" pipeline-run
        design: once retained_reason is set, try_acquire_lock() refuses acquisition
        by any other issue regardless of Redis TTL/staleness/restart state, and the
        watchdog/rescan reconciliation logic (project_monitor.py) must never treat
        this lock as leaked. Only release_lock() (the deliberate human recovery
        action) clears it.

        Safe to call even if no PipelineRun object exists for this attempt (e.g. a
        pre-dispatch failure) — this only touches the lock, which is guaranteed to
        already be held by issue_number in every call site that uses this.

        Returns:
            True if the lock was found (held by issue_number) and marked in at
            least one durable store, False if no lock is currently held by this
            issue (a bug upstream — logged as an error since this should never
            happen) or if `reason` was empty (also a bug upstream — an empty
            retained_reason would be indistinguishable from "not retained" by
            every read site, silently disabling the whole mechanism).
        """
        if not reason or not reason.strip():
            logger.error(
                f"mark_lock_failed: refusing to mark {project}/{board} issue "
                f"#{issue_number} with an empty reason — this would silently "
                f"disable the retained-lock protection (empty string reads as "
                f"'not retained' everywhere)"
            )
            return False

        lock = self.get_lock(project, board)
        if not lock or lock.locked_by_issue != issue_number:
            logger.error(
                f"mark_lock_failed: no lock held by issue #{issue_number} for "
                f"{project}/{board} (current holder: "
                f"{lock.locked_by_issue if lock else 'none'}) — cannot mark failed"
            )
            return False

        lock.retained_reason = reason
        lock.retained_at = datetime.now(timezone.utc).isoformat()

        redis_ok = False
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                self.redis_client.hset(lock_key, mapping=self._lock_to_redis_mapping(lock))
                self.redis_client.expire(lock_key, 7200)
                redis_ok = True
            except Exception as e:
                logger.error(f"Failed to mark lock failed in Redis: {e}")

        yaml_ok = self._save_lock_to_yaml(lock)

        if not redis_ok and not yaml_ok:
            # Neither durable store actually recorded the retention — the return
            # value must reflect that this call did NOT achieve its purpose,
            # rather than unconditionally claiming success. get_lock()'s merge
            # logic (see its docstring) covers the case where only ONE of the two
            # writes fails; it cannot cover both failing.
            logger.error(
                f"mark_lock_failed: BOTH Redis and YAML writes failed for "
                f"{project}/{board} issue #{issue_number} — the retained-failure "
                f"state was NOT durably recorded anywhere. Reason was: {reason}"
            )
            return False

        logger.warning(
            f"Pipeline lock for {project}/{board} durably retained (issue #{issue_number} "
            f"failed: {reason}) — blocked until a human runs scripts/release_lock.py"
        )
        return True

    def get_retained_reason(self, project: str, board: str, issue_number: int) -> Optional[str]:
        """
        Return the retained_reason if issue_number currently holds a failed/retained
        lock for this pipeline, else None.

        This is a convenience wrapper around PipelineLock.retained_reason for
        callers that only have project/board/issue and not the lock object
        itself — it is NOT the only place this field is consulted. Every
        dispatch/rescan/reconciliation gate ultimately keys off this same field,
        but some (e.g. _reconcile_active_runs, _rescan_boards_for_stalled_items in
        project_monitor.py) read lock.retained_reason directly off a PipelineLock
        they've already fetched for other reasons, rather than calling this method
        again. What matters is that every one of those gates checks the field —
        not that they all go through this specific function.

        This is a dispatch-gate call — the highest-frequency, first-line-of-
        defense check in the whole mechanism (called on every poll for every
        column) — so, like try_acquire_lock, it fails CLOSED via
        get_lock_fail_closed(): if lock state genuinely can't be determined
        (both Redis and YAML reads failed), a synthetic non-None reason is
        returned so callers treat it as "retained, refuse" rather than
        silently proceeding as if nothing were wrong.
        """
        lock, reads_healthy = self.get_lock_fail_closed(project, board)
        if not reads_healthy:
            return (
                "lock state unknown (both Redis and YAML reads failed) — "
                "refusing as a precaution"
            )
        if lock and lock.locked_by_issue == issue_number:
            return lock.retained_reason
        return None

    def _save_lock_to_yaml(self, lock: PipelineLock) -> bool:
        """Save lock state to YAML file with thread-safe file locking. Returns
        True on success, False if the write failed (the caller decides how loudly
        to escalate — see mark_lock_failed, which treats "both stores failed" as
        a hard failure rather than a routine log line)."""
        from utils.file_lock import safe_yaml_write

        state_file = self._get_state_file(lock.project, lock.board)
        try:
            with safe_yaml_write(state_file):
                with open(state_file, 'w') as f:
                    yaml.dump(asdict(lock), f, default_flow_style=False, sort_keys=False)
            logger.debug(f"Saved lock to YAML: {state_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to save lock to YAML: {e}")
            return False

    def get_all_locks(self) -> list[PipelineLock]:
        """
        Get all active locks (for monitoring/recovery — this backs
        /active-pipeline-runs' failed-run listing and
        scripts/list_failed_pipeline_runs.py, both of which exist specifically so
        a retained/failed lock can never be silently undiscoverable).

        Scans BOTH YAML files and Redis lock keys and unions the (project, board)
        pairs found in either — a YAML-only scan would miss a lock that was
        successfully written to Redis but whose YAML write failed (see
        mark_lock_failed), making it correctly *enforced* but invisible to the
        discovery tools built to surface exactly this state. Each pair is then
        re-read through get_lock(), which already merges retained state from
        whichever store has it, so callers get one consistent view regardless of
        which store this method happened to discover the pair from.

        Returns:
            List of all active PipelineLock objects
        """
        pairs = set()

        for state_file in self.state_dir.glob("*.yaml"):
            try:
                with open(state_file, 'r') as f:
                    lock_data = yaml.safe_load(f)
                    if lock_data and lock_data.get('lock_status') == 'locked':
                        pairs.add((lock_data.get('project'), lock_data.get('board')))
            except Exception as e:
                logger.error(f"Failed to load lock from {state_file}: {e}")

        if self.redis_client:
            try:
                for key in self.redis_client.keys("pipeline_lock:*"):
                    # Skip non-lock keys that share the prefix (e.g. the old
                    # pipeline_lock:repair_failed:* marker, if any still linger
                    # from before this PR — that mechanism was removed, but stale
                    # keys could still exist until their TTL expires).
                    if key.count(':') != 2:
                        continue
                    _, project, board = key.split(':', 2)
                    pairs.add((project, board))
            except Exception as e:
                logger.error(f"Failed to scan lock keys from Redis: {e}")

        locks = []
        for project, board in pairs:
            if not project or not board:
                continue
            lock = self.get_lock(project, board)
            if lock:
                locks.append(lock)

        return locks

    def sync_yaml_locks_to_redis(self) -> int:
        """
        Sync all YAML locks to Redis during startup recovery.

        This ensures Redis (source of truth) matches YAML persistence
        after orchestrator restart when Redis may have been cleared.

        IMPORTANT: This has a known race condition if multiple orchestrator
        instances start simultaneously. Proper fix requires leader election.

        Returns:
            Number of locks synced to Redis
        """
        if not self.redis_client:
            logger.warning("Cannot sync locks to Redis - Redis not available")
            return 0

        synced_count = 0
        skipped_count = 0
        locks = self.get_all_locks()  # Read from YAML

        for lock in locks:
            try:
                lock_key = self._get_lock_key(lock.project, lock.board)

                # Validate lock age - don't sync stale locks (older than 4 hours),
                # UNLESS the lock is durably retained after a pipeline run failure
                # (retained_reason set) — those must always be synced regardless of
                # age, or a restart would leave the lock invisible to Redis-based
                # dispatch checks until the next sibling's try_acquire_lock call
                # falls back to the (still-correct) YAML read.
                from datetime import datetime, timezone, timedelta
                lock_age_threshold = datetime.now(timezone.utc) - timedelta(hours=4)
                lock_acquired_time = datetime.fromisoformat(lock.lock_acquired_at.replace('Z', '+00:00'))

                if lock_acquired_time < lock_age_threshold and not lock.retained_reason:
                    logger.warning(
                        f"Skipping stale lock sync: {lock.project}/{lock.board} "
                        f"held by issue #{lock.locked_by_issue} (age: {datetime.now(timezone.utc) - lock_acquired_time})"
                    )
                    skipped_count += 1
                    continue

                # Check if lock already exists in Redis
                existing_lock = self.redis_client.hgetall(lock_key)

                if not existing_lock:
                    # Lock missing in Redis - sync it
                    self.redis_client.hset(lock_key, mapping=self._lock_to_redis_mapping(lock))
                    self.redis_client.expire(lock_key, 7200)  # 2 hour TTL
                    logger.info(
                        f"Synced lock to Redis: {lock.project}/{lock.board} "
                        f"held by issue #{lock.locked_by_issue}"
                    )
                    synced_count += 1
                else:
                    # Lock exists in Redis - don't overwrite to avoid conflicts
                    existing_holder = existing_lock.get(b'locked_by_issue', existing_lock.get('locked_by_issue'))
                    logger.warning(
                        f"Lock already in Redis: {lock.project}/{lock.board} "
                        f"(Redis: issue #{existing_holder}, YAML: issue #{lock.locked_by_issue}) - "
                        f"not overwriting to prevent race condition"
                    )
                    skipped_count += 1
            except Exception as e:
                logger.error(f"Failed to sync lock to Redis: {e}")

        if skipped_count > 0:
            logger.info(f"Lock sync summary: {synced_count} synced, {skipped_count} skipped")

        return synced_count

    def get_lock_holder(self, project: str, board: str) -> Optional[int]:
        """
        Get the issue number that currently holds the lock for a pipeline.

        Args:
            project: Project name
            board: Board name

        Returns:
            Issue number holding the lock, or None if unlocked
        """
        lock = self.get_lock(project, board)
        return lock.locked_by_issue if lock else None

    def is_locked_by_issue(self, project: str, board: str, issue_number: int) -> bool:
        """
        Check if a specific issue currently holds the lock.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number to check

        Returns:
            True if the issue holds the lock, False otherwise
        """
        lock_holder = self.get_lock_holder(project, board)
        return lock_holder == issue_number if lock_holder is not None else False

    def get_lock_status_for_issue(
        self,
        project: str,
        board: str,
        issue_number: int
    ) -> str:
        """
        Get the lock status for a specific issue in a pipeline.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number to check

        Returns:
            'holding_lock' - Issue currently holds the lock
            'waiting_for_lock' - Issue is waiting, another issue holds lock
            'no_lock' - Pipeline is unlocked
        """
        lock = self.get_lock(project, board)

        if not lock:
            return 'no_lock'

        if lock.locked_by_issue == issue_number:
            return 'holding_lock'
        else:
            return 'waiting_for_lock'


# Singleton instance
_pipeline_lock_manager = None


def get_pipeline_lock_manager() -> PipelineLockManager:
    """Get singleton instance of PipelineLockManager"""
    global _pipeline_lock_manager
    if _pipeline_lock_manager is None:
        _pipeline_lock_manager = PipelineLockManager()
    return _pipeline_lock_manager

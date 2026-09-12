"""
Pipeline Lock Manager

Manages exclusive locks for pipeline execution to prevent concurrent work
on multiple issues within the same pipeline (project + board).

Only ONE issue can hold the pipeline lock at a time. Other issues wait in queue.
"""

import yaml
import redis
from redis.backoff import NoBackoff
from redis.retry import Retry
import logging
import os
import sys
import threading
import uuid
from enum import Enum
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime, timezone, timedelta
from dataclasses import dataclass, asdict

logger = logging.getLogger(__name__)

# TTL applied to every Redis lock key this class writes. Hoisted out of the
# seven separate `expire(lock_key, 7200)` literals it used to be spelled as
# (found in review, #146 WI-1): services/project_checkout_lock.py's heartbeat
# interval and its sustained-failure escalation threshold are both calibrated
# against this exact number, and nothing tied the two modules together -- so
# shortening the TTL here to speed up stale-lock recovery would silently leave
# that heartbeat racing its own key expiry with zero margin. It is imported
# there rather than restated, and asserted against in
# tests/unit/services/test_project_checkout_lock.py.
LOCK_TTL_SECONDS = 7200

# How long any single acquisition of the inner '<state>.yaml.lock' will wait
# before giving up. EVERY acquisition of that file made by this class passes it
# (found in the WI-8 review round: two of the three taken inside the
# '<state>.yaml.acquire.lock' guard were still blocking acquires, which left
# the guard's own hold unbounded and made the retry budget below underivable).
#
# Sized against what actually holds this lock rather than left to
# utils.file_lock's incidental 10s default: every critical section under it is
# a single small-YAML read, write or unlink against the local filesystem, with
# no Redis call and no other network I/O anywhere inside it -- the Redis legs
# of try_acquire_lock()/touch_lock()/release_lock() all run OUTSIDE it. Real
# holds are sub-millisecond, so this is not a budget a healthy holder can
# consume; it exists only to turn a wedged or abandoned holder in another
# process (scripts/release_lock.py, the observability server) into a reported
# failure instead of a thread parked forever.
STATE_LOCK_TIMEOUT_SECONDS = 20

# Socket budget for every Redis call this class makes, and -- with
# LOCK_REDIS_RETRY below -- the whole cost of one round trip against a Redis
# that is not answering. Named rather than left as two literals because
# RELEASE_GUARD_RETRY_TIMEOUT_SECONDS is sized in multiples of it.
LOCK_REDIS_SOCKET_TIMEOUT_SECONDS = 5

# ONE attempt per Redis command, with no backoff (found in the #139 review
# round). redis-py does NOT default to this: since 6.0 every client is built
# with Retry(ExponentialWithJitterBackoff(), retries=10) unless a retry policy
# is passed, so a command against a host that drops SYNs costs eleven
# socket_connect_timeout waits plus backoff -- measured at ~59s per call
# against this deployment's redis 8.1.0, not the 5s the timeouts above read as.
# That multiplier lands squarely inside the '<state>.yaml.acquire.lock' guard,
# whose worst-case hold is what the release budget below is derived from, and
# it is invisible at every call site.
#
# Dropping the retries costs this class nothing it does not already have: every
# Redis call here is inside a try/except whose fallback is the YAML store (see
# _try_acquire_lock_unguarded's fall-through and get_lock()'s two-store read),
# and every caller of try_acquire_lock() polls, so a blip that redis-py's
# default would have papered over becomes one guarded YAML-path attempt and a
# retry a few seconds later rather than a minute parked inside the guard.
LOCK_REDIS_RETRY = Retry(NoBackoff(), 0)

# How long release_lock() waits for the '<state>.yaml.acquire.lock' guard on
# its FIRST attempt. Spelled out rather than left to utils.file_lock's own
# default so the two budgets below read as one deliberate decision.
RELEASE_GUARD_TIMEOUT_SECONDS = 10

# How long release_lock() will wait for that same guard on its SECOND attempt,
# after the budget above has already expired once (found in the WI-8 review
# round).
#
# A release is not symmetric with an acquire, and the 10s first attempt --
# which was an incidental property of file_lock(), not a budget anyone sized
# for this -- is far too small on its own. Every acquire site polls, so a
# refused acquire is retried within seconds; a release has already been
# authorized by the caller, and abandoning it leaks the lock until the
# LOCK_TTL_SECONDS Redis TTL or the 4-hour staleness heuristic, blocking every
# dispatch for that (project, board) in the meantime
# (services/project_checkout_lock.py's release site spells this consequence out
# at its call to _release_and_warn). The contention is also
# asymmetric in the wrong direction: every try_acquire_lock() takes this same
# guard, and when Redis is unavailable -- which is when this contention
# actually happens -- each attempt holds it for several seconds of Redis socket
# timeouts, so two waiters polling every DEFAULT_POLL_INTERVAL_SECONDS can hold
# it almost continuously. Since the #139 review round the guard covers
# try_acquire_lock()'s Redis branch as well as its YAML fallback, so an
# unreachable Redis is paid for inside the guard on BOTH of them.
#
# Sized against the longest a guard holder can now legitimately hold it. The
# longest guarded section is a try_acquire_lock() whose Redis branch fails and
# whose YAML fallback then recovers a stale lock. That takes the inner
# '<state>.yaml.lock' four times -- get_lock()'s read, the fail-closed read
# inside _release_lock_unguarded(), that release's own delete, and
# _create_lock()'s write -- and makes five Redis calls that each have to fail
# before it can move on: the Redis branch's opening watch, get_lock()'s read,
# the fail-closed read's, the Redis leg of the release, and _create_lock()'s
# hset.
#
# Both multipliers are bounded only because something makes them so, and both
# had to be made so deliberately:
#
#   - all four inner-lock acquisitions pass STATE_LOCK_TIMEOUT_SECONDS (they
#     did not, before the WI-8 review round, which is what made the 35s figure
#     this used to cite underivable), and
#   - one Redis call really costs LOCK_REDIS_SOCKET_TIMEOUT_SECONDS only
#     because LOCK_REDIS_RETRY replaces redis-py's default ten retries with
#     one attempt -- with that default the same five calls are ~59s each and
#     nothing below is derivable.
#
# So the worst case is 4 * STATE_LOCK_TIMEOUT_SECONDS + 5 *
# LOCK_REDIS_SOCKET_TIMEOUT_SECONDS = 105s, and this budget covers it.
# tests/unit/services/test_pipeline_lock_manager.py asserts both multipliers
# (TestEveryInnerStateLockAcquisitionInsideTheGuardIsBounded and
# TestOneRedisRoundTripIsBoundedByItsSocketTimeout) rather than leaving this
# derivation as prose that can drift from the code again.
#
# Still BOUNDED rather than a blocking acquire, and a release that exhausts
# even this budget is reported as ReleaseResult.SERIALIZATION_FAILED rather
# than as a refusal. Spending a budget this size is only safe because the async
# holders no longer wait it out on the asyncio event loop:
# services/project_checkout_lock.py's _release_and_warn_async() offloads the
# release to a worker thread (shape (b) in that module's docstring) instead of
# running it inline, which it did while this budget was 60s.
RELEASE_GUARD_RETRY_TIMEOUT_SECONDS = 120


def _derive_process_role() -> str:
    """
    Name the KIND of process this is, from its entry point.

    docker-compose.yml runs each long-lived orchestrator process as its own
    container with its own `command:` -- `python main.py` (the orchestrator),
    `python -m services.observability_server` (the API/rebuild server) -- and
    the hand-run admin scripts are their own entry points again
    (scripts/rebuild_project_images.py, scripts/set_dev_container_verified.py).
    sys.argv[0] is the script path in every one of those cases (`python -m pkg.mod`
    sets argv[0] to the module's own file), so its basename is a stable, automatic
    name for the process kind with nothing to keep in sync by hand.

    Used by PROCESS_OWNER_ID below, whose only consumer is
    ProjectResourceLockManager.recover_orphaned_resource_locks() -- see that
    method for why the KIND, not just the instance, is the part that matters.
    """
    try:
        entry = sys.argv[0] if sys.argv else ''
    except Exception:  # pragma: no cover -- sys.argv is always present in practice
        entry = ''
    return Path(entry).name or 'unknown'


# Identity of THIS process incarnation, stamped onto every lock it acquires
# (PipelineLock.owner_process). Two halves, both load-bearing:
#
#   <role>#<instance>
#
# `role` names the process KIND (see _derive_process_role). Every one of those
# kinds is a singleton -- docker-compose runs exactly one orchestrator container
# and one observability-server container -- so "a lock owned by MY role that I
# did not acquire" is provably a dead predecessor of mine, while "a lock owned
# by a DIFFERENT role" may well be a live peer.
#
# `instance` distinguishes two incarnations of the same role, so a log line can
# say which one.
PROCESS_OWNER_ID = f"{_derive_process_role()}#{uuid.uuid4().hex[:12]}"


def owner_process_role(owner_process: Optional[str]) -> Optional[str]:
    """Role half of an owner_process stamp (see PROCESS_OWNER_ID), or None."""
    if not owner_process:
        return None
    return owner_process.split('#', 1)[0] or None


# try_acquire_lock() reasons that refuse the acquisition WITHOUT the caller
# having stopped being the recorded holder.
#
# Most False try_acquire_lock() returns mean "you do not hold this lock" --
# contention, a retained failure, an unreadable store, a write that landed
# nowhere -- and several call sites are written directly against that reading:
# services/project_monitor.py's review-cycle and repair-cycle gates tear the
# pipeline run down on a refusal, and end_pipeline_run() RELEASES the lock
# whenever it finds the run's own issue recorded as the holder. Found in the
# #139 review round: _refuse_unmirrored_redis_grant()'s "already_holds_lock"
# branch deliberately leaves a live holder's Redis key alone (rolling it back
# would release a lock out from under a running pipeline), which made that
# reading false for the first time -- so those teardowns released the very lock
# the refusal was written to protect.
#
# This set holds only the reasons that PROVE, from the reason string alone, that
# the caller is still the holder; the reasons that decide nothing either way
# live in LOCK_REFUSAL_REASONS_HOLDER_UNDECIDED below. A teardown call site has
# to honor both, which is what refusal_must_not_end_caller_run() is for -- do
# not reach for this set on its own.
LOCK_REFUSAL_REASONS_CALLER_STILL_HOLDS = frozenset({
    "lock_mirror_write_failed_while_held",
})


# try_acquire_lock() reasons that decide NOTHING about who holds the lock: both
# are raised by the acquire guard itself, before either store is read or
# written, so whoever was the recorded holder still is -- and on the two
# project_monitor gates above, whose whole premise is an issue that may already
# hold this lock from an earlier stage, that is quite often the caller itself.
#
# Found in the #174 review round. The acquire/release asymmetry makes it the
# likely outcome rather than a race: the acquire guard is taken with the
# default 10s file-lock timeout, while release_lock() deliberately waits
# RELEASE_GUARD_TIMEOUT_SECONDS then RELEASE_GUARD_RETRY_TIMEOUT_SECONDS for
# the same guard (see release_lock()) -- so a guard contended past 10s refuses
# the acquire and then grants the release, and a gate that treats the refusal
# as "someone else has this board" ends its own live run, releases its own
# lock, and hands the board to the next queued issue.
#
# Unlike the set above, these reasons cannot answer the question on their own --
# the caller may be the holder or may be a genuine contender. Deciding requires
# actually reading the holder, which is what refusal_must_not_end_caller_run()
# does.
LOCK_REFUSAL_REASONS_HOLDER_UNDECIDED = frozenset({
    "lock_acquire_serialization_timeout",
    "lock_acquire_serialization_unavailable",
})


def refusal_leaves_caller_holding_lock(reason: Optional[str]) -> bool:
    """
    True when a False from try_acquire_lock() carrying this reason means "this
    refresh failed, but you are still the recorded holder" rather than "you do
    not hold this lock".

    Only proves the positive case -- a False here is NOT proof the caller lost
    the lock, see LOCK_REFUSAL_REASONS_HOLDER_UNDECIDED. Callers that respond to
    a refusal by ending the run and/or releasing the lock want
    refusal_must_not_end_caller_run(), which covers both sets.
    """
    return reason in LOCK_REFUSAL_REASONS_CALLER_STILL_HOLDS


def refusal_must_not_end_caller_run(
    reason: Optional[str],
    lock_manager: 'PipelineLockManager',
    project: str,
    board: str,
    issue_number: int,
) -> Optional[str]:
    """
    The question a dispatch gate actually has after a False try_acquire_lock():
    "is it safe to end this issue's pipeline run on this refusal?"

    It is not safe whenever the refusal leaves this issue the recorded holder,
    because end_pipeline_run() reads the holder rather than asking who called
    it -- finding this issue there, it releases the lock, cancels the issue for
    an hour and dispatches the board to the next queued issue, all out from
    under a pipeline that is still running.

    Returns a log-ready explanation when the run MUST be left in place (retry on
    a later poll instead), or None when the refusal is a genuine "you do not
    hold this lock" the caller is free to clean up after.

    Fails closed on the undecided refusals: an unreadable or raising holder
    lookup is reported as "leave it alone" -- the cost of being wrong that way
    is a pipeline run that lingers until the zombie watchdog sweeps it, against
    a live pipeline losing its board the other way.
    """
    if refusal_leaves_caller_holding_lock(reason):
        return (
            f"issue #{issue_number} is still the recorded holder ({reason} refuses "
            f"the refresh without giving the lock up)"
        )

    if reason not in LOCK_REFUSAL_REASONS_HOLDER_UNDECIDED:
        return None

    try:
        holder, reads_healthy = lock_manager.get_lock_holder_fail_closed(project, board)
    except Exception as e:
        return (
            f"{reason} decided nothing about who holds the lock and the holder could "
            f"not be read to find out ({e}) — assuming issue #{issue_number} may still "
            f"hold it"
        )

    if not reads_healthy:
        return (
            f"{reason} decided nothing about who holds the lock and neither store could "
            f"be read to find out — assuming issue #{issue_number} may still hold it"
        )

    if holder == issue_number:
        return (
            f"issue #{issue_number} is still the recorded holder ({reason} was refused "
            f"by the acquire guard before either store was touched)"
        )

    return None


class TouchResult(Enum):
    """
    Outcome of touch_lock() -- three genuinely different states that a bare
    bool collapsed into one.

    Found in review (#146 WI-1): touch_lock() catches every store failure
    internally (Redis errors around hset/expire, _read_redis_lock_only/
    _read_yaml_lock_only, _save_lock_to_yaml) and converts each of them into
    a False return, so its only caller --
    services/project_checkout_lock.py's heartbeat -- could not tell "another
    holder now owns this lock" from "the stores are down and this holder's
    liveness was NOT extended". It treated both as the former, logging a
    specific and alarming "the lock was LOST, you may be racing a different
    holder" ERROR every tick of a Redis outage (directly contradicting the
    ERROR touch_lock itself logs immediately before it), while the sustained-
    failure escalation written for exactly that outage sat unreachable
    behind an `except Exception` that store failures never reach.

    __bool__ is defined so every existing truthiness-based caller/assertion
    (`if not still_held`, assertTrue/assertFalse) keeps its original meaning:
    only REFRESHED is truthy.
    """

    REFRESHED = "refreshed"       # confirmed held by this holder, liveness extended
    NOT_HELD = "not_held"         # confirmed NOT held by this holder (lost, or never held)
    REFRESH_FAILED = "refresh_failed"  # state unknown / not written -- liveness NOT extended

    def __bool__(self) -> bool:
        return self is TouchResult.REFRESHED


class LockStateSerializationError(RuntimeError):
    """
    A lock read or write could not be SERIALIZED against a concurrent one --
    the inner '<state>.yaml.lock' was still held after
    STATE_LOCK_TIMEOUT_SECONDS.

    Raised rather than folded into the surrounding "the read failed" / "the
    write failed" bool (found in the WI-8 review round) because for a release
    those are opposite facts. A failed read means the lock's state is genuinely
    unknown, so release_lock() must fail closed and refuse -- which its callers
    correctly report as "held by this issue but likely retained due to a failed
    run". A serialization timeout establishes nothing about that state and
    changes nothing either, so reporting it as the same refusal sends an
    operator to scripts/release_lock.py chasing a durable failure record that
    does not exist while the board stays wedged behind a release that is still
    outstanding. That is precisely the misattribution the ReleaseResult split
    below exists to remove, relocated from the outer guard to the inner lock.

    Caught by _release_lock_to_result(), which maps it to
    ReleaseResult.SERIALIZATION_FAILED.
    """


class ReleaseResult(Enum):
    """
    Outcome of release_lock() -- the same three-state split TouchResult made
    for touch_lock(), for the same reason (#153 WI-8 review round).

    release_lock() gained an acquire guard in this work item, and its guard
    timeout returned the same bare False the method already used for "refused:
    not held by this issue, retained, or state unknown". Those are opposite
    facts about the lock: a refusal means the release was CONSIDERED and
    correctly declined, so the lock's state is exactly what the caller was
    told; a serialization failure means nothing was attempted at all and the
    release is still outstanding. Every caller read False as the former, and
    the three that log about it told operators the lock was "likely retained
    due to a failed run" -- a wrong-but-plausible diagnosis that points at
    scripts/release_lock.py instead of at the contention that actually
    happened.

    __bool__ is defined so every existing truthiness-based caller/assertion
    (`if not released`, assertTrue/assertFalse) keeps its original meaning:
    only RELEASED is truthy.
    """

    RELEASED = "released"          # the lock is confirmed gone from the stores
    NOT_RELEASED = "not_released"  # considered and refused: not held, retained, or state unknown
    # Could not be serialized against a concurrent lock writer, either on the
    # '<state>.yaml.acquire.lock' guard (nothing attempted at all) or on the
    # inner '<state>.yaml.lock' (see LockStateSerializationError). Either way
    # the release did NOT complete and is still outstanding -- which is a
    # different instruction to the caller than NOT_RELEASED's "considered, and
    # correctly declined".
    SERIALIZATION_FAILED = "serialization_failed"

    def __bool__(self) -> bool:
        return self is ReleaseResult.RELEASED


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
    # Which process incarnation acquired this lock (PROCESS_OWNER_ID at the time
    # of acquisition). Deliberately defaults to None rather than to the CURRENT
    # process: a lock deserialized from a Redis hash or a YAML file written
    # before this field existed must read back as "owner unknown", not as
    # "owned by whoever happens to be reading it".
    #
    # Read by ProjectResourceLockManager.recover_orphaned_resource_locks(), which
    # cannot otherwise tell a lock left behind by its own dead predecessor from
    # one a live sibling process is holding right now -- see that method.
    owner_process: Optional[str] = None

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

    def __init__(self, state_dir: Path = None, redis_client=None, use_redis: bool = True):
        """
        Initialize pipeline lock manager.

        Args:
            state_dir: Directory for YAML state persistence
            redis_client: Optional Redis client (one is created from REDIS_HOST
                if not provided, unless use_redis is False)
            use_redis: Whether this instance may talk to Redis at all. False is
                the ONLY way to ask for the YAML-only fallback deliberately --
                see below.

        Raises:
            ValueError: use_redis=False was combined with an explicit
                redis_client. That is a contradiction, and silently honouring
                either half of it is how this class got into trouble the first
                time.

        On use_redis (#139): until this was added there was no way to say "run
        without Redis" -- redis_client=None is documented as, and means,
        "connect one yourself". Callers who wanted YAML-only passed None anyway
        and got it, but only by accident: the redundant `import os` that used to
        sit inside the `state_dir is None` branch made `os` local to this whole
        method (any name assigned anywhere in a function is local to all of it),
        so a caller supplying state_dir and omitting redis_client hit an
        UnboundLocalError on the os.environ.get() in the connect block, the
        except swallowed it, and the instance latched into YAML-only mode with a
        "Redis connection failed" line in the log for a connection that was
        never attempted.

        The import is hoisted (`os` is imported at module level, line 13) and
        the intent is now expressible, because the accident was load-bearing for
        the test suite: every lock test passing a state_dir was exercising the
        YAML fallback while reading as though it covered the Redis WATCH/MULTI
        path. PR #155 found a real double-grant bug in that fallback under
        coverage that had never once run against Redis. Those call sites now say
        which path they mean.
        """
        if state_dir is None:
            # One resolver, validated and absolute (#202) -- see
            # config.state_manager.orchestrator_state_root().
            from config.state_manager import orchestrator_state_root
            state_dir = orchestrator_state_root() / "pipeline_locks"

        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)

        if redis_client is not None and not use_redis:
            raise ValueError(
                "PipelineLockManager(redis_client=..., use_redis=False) is "
                "contradictory: pass use_redis=False for YAML-only operation, or "
                "a redis_client to use it, not both."
            )

        # Initialize Redis client. use_redis is kept on the instance because
        # `self.redis_client is None` is checked in a dozen places and means
        # two different things -- "deliberately YAML-only" and "Redis fell over
        # at boot and we silently degraded" -- which health reporting and the
        # observability endpoint have no other way to tell apart.
        self.use_redis = use_redis
        self.redis_client = redis_client
        if self.redis_client is None and use_redis:
            # Read and parsed OUTSIDE the try, deliberately (#139 review round).
            # A non-numeric REDIS_PORT (a typo, or REDIS_PORT=redis copied from
            # REDIS_HOST) is a misconfiguration, not an outage, and reporting
            # its ValueError as "Redis connection failed" is the same
            # laundering the UnboundLocalError above got away with: the
            # operator goes looking for a Redis that is perfectly healthy while
            # every lock in the process runs through the YAML fallback PR #155
            # found a double-grant in. Fail loudly at startup instead.
            redis_host = os.environ.get('REDIS_HOST', 'redis')
            redis_port = int(os.environ.get('REDIS_PORT', 6379))
            try:
                self.redis_client = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=True,
                    socket_connect_timeout=LOCK_REDIS_SOCKET_TIMEOUT_SECONDS,
                    socket_timeout=LOCK_REDIS_SOCKET_TIMEOUT_SECONDS,
                    # Without this the two timeouts above are not the cost of a
                    # call -- see LOCK_REDIS_RETRY, and
                    # RELEASE_GUARD_RETRY_TIMEOUT_SECONDS, which is derived
                    # from them.
                    retry=LOCK_REDIS_RETRY,
                )
                self.redis_client.ping()
                logger.info(f"Connected to Redis at {redis_host}:{redis_port} for pipeline locks")
            except (redis.RedisError, OSError) as e:
                # Narrow on purpose, and for the same reason as the parse
                # above: these two are what "the service is not reachable"
                # actually raises. Anything else here (a TypeError from a bad
                # kwarg, an AttributeError from a partially-imported redis) is
                # a bug in this code, and must not be reported as an outage nor
                # silently latch the instance into YAML-only -- with no
                # reconnect -- for the life of the process.
                logger.warning(f"Redis connection failed for locks, using YAML only: {e}")
                self.redis_client = None
        elif not use_redis:
            # Distinct from the warning above on purpose: no connection was
            # attempted, so reporting one as failed would send an operator
            # looking for a Redis outage that isn't happening.
            logger.info("PipelineLockManager running YAML-only (use_redis=False)")

        logger.info(f"PipelineLockManager initialized with state_dir: {state_dir}")

    def _get_lock_key(self, project: str, board: str) -> str:
        """Get Redis key for lock"""
        return f"pipeline_lock:{project}:{board}"

    def _get_state_file(self, project: str, board: str) -> Path:
        """Get YAML state file path for lock"""
        return self.state_dir / f"{project}_{board}.yaml"

    def _get_acquire_guard_file(self, project: str, board: str) -> Path:
        """
        The OUTER of this class's two advisory lock files for a (project, board).

        Every operation that decides who holds this lock by reading the current
        record and then writing a different one -- BOTH of try_acquire_lock()'s
        branches, touch_lock(), release_lock() -- takes this file for the whole
        of that read-modify-write, so no two of them can interleave, across
        threads AND across processes (scripts/release_lock.py or the
        observability server's release endpoint running alongside the
        orchestrator).

        try_acquire_lock()'s Redis branch was the exception until the #139
        review round, on the reasoning that its WATCH/MULTI transaction is
        already atomic. The transaction is; the grant is not. Mirroring it to
        disk with _create_lock_yaml_only() is a second, unguarded write, and
        release_lock() holds this guard across ITS two-store delete while
        skipping the YAML ownership re-check (Redis already confirmed
        ownership) -- so an acquire landing between a release's Redis leg and
        its YAML leg had its brand-new state file unlinked by the departing
        holder. What survived existed only in Redis: invisible to
        get_all_locks(), reported by _read_yaml_lock_only() as a HEALTHY "no
        lock", and gone for good once the LOCK_TTL_SECONDS key lapsed -- at
        which point try_acquire_lock()'s transaction reads the absent key back
        as an empty dict and grants the same board to a second issue while the
        first one's run is still live.

        Deliberately a DIFFERENT file from the '<state>.yaml.lock' those
        operations take internally around the individual file read/write: the
        inner lock makes one store access atomic, this one makes the whole
        decision atomic, and fcntl.flock() conflicts between two descriptors of
        the same file even within a single process, so they cannot be the same
        path. The nesting order is always this file OUTER, '<state>.yaml.lock'
        INNER -- nothing in this class ever takes them the other way round, so
        there is no ordering cycle to deadlock on.
        """
        state_file = self._get_state_file(project, board)
        return state_file.with_suffix(state_file.suffix + '.acquire.lock')

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
            owner_process=(lock_data.get('owner_process') or None),
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
        """
        Read the lock from the YAML file only. Returns (lock_or_None, read_succeeded).

        A read that could not be SERIALIZED collapses into read_succeeded=False
        here, which every caller of this method already treats fail-closed.
        The one caller that must tell that apart from a genuine read failure --
        the release path, see LockStateSerializationError -- goes through
        _read_yaml_lock_only_detail() instead.
        """
        lock, read_ok, _serialization_failed = self._read_yaml_lock_only_detail(project, board)
        return lock, read_ok

    def _read_yaml_lock_only_detail(
        self, project: str, board: str
    ) -> Tuple[Optional[PipelineLock], bool, bool]:
        """
        _read_yaml_lock_only(), plus WHY it failed. Returns
        (lock_or_None, read_succeeded, serialization_failed).

        The inner '<state>.yaml.lock' is taken with enforce_timeout=True
        (found in the WI-8 review round): this read runs inside the
        '<state>.yaml.acquire.lock' guard on every one of the three paths that
        decide who holds the lock, and a blocking acquire here made that
        OUTER guard's hold unbounded -- so release_lock()'s own bounded wait
        for the guard could expire against a holder that was itself parked
        indefinitely on this inner lock.
        """
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        if not state_file.exists():
            return None, True, False
        try:
            lock_file = state_file.with_suffix(state_file.suffix + '.lock')
            with file_lock(
                lock_file, timeout=STATE_LOCK_TIMEOUT_SECONDS, enforce_timeout=True
            ):
                lock, read_ok = self._read_yaml_lock_only_unlocked(project, board)
                return lock, read_ok, False
        except TimeoutError as e:
            # Checked before the generic handler below -- TimeoutError is an
            # OSError, so it would otherwise be reported as an unknown lock
            # state, which is a durable fact this read never established.
            logger.error(
                f"Could not take the YAML state lock to read {project}/{board} within "
                f"{STATE_LOCK_TIMEOUT_SECONDS}s: {e}"
            )
            return None, False, True
        except Exception as e:
            logger.error(f"Failed to load lock state from YAML: {e}")
            return None, False, False

    def _read_yaml_lock_only_unlocked(
        self, project: str, board: str
    ) -> Tuple[Optional[PipelineLock], bool]:
        """
        _read_yaml_lock_only() without taking '<state>.yaml.lock'. MUST only be
        called with that lock already held.

        Exists so a read and the write that depends on it can share ONE
        critical section (see _touch_lock_yaml_unguarded) -- fcntl.flock()
        conflicts between two descriptors of the same file even within one
        process, so the locking variant cannot be nested inside its own lock
        (utils.file_lock raises ReentrantFileLockError rather than hanging on
        it).
        """
        state_file = self._get_state_file(project, board)
        try:
            if not state_file.exists():
                return None, True
            with open(state_file, 'r') as f:
                lock_data = yaml.safe_load(f)
                if lock_data and lock_data.get('lock_status') == 'locked':
                    return PipelineLock(**lock_data), True
                return None, True
        except Exception as e:
            logger.error(f"Failed to load lock state from YAML: {e}")
            return None, False

    def redis_lock_is_missing(self, project: str, board: str) -> bool:
        """
        True only when Redis is configured, its read SUCCEEDED, and it holds no
        'locked' record for this (project, board).

        Deliberately NOT `_read_redis_lock_only(...) == (None, True)`: that
        method also returns (None, True) when no Redis client is configured at
        all ("no Redis configured isn't a read failure"), and a YAML-only
        deployment must not have every one of its locks reported as a key that
        Redis has lost.

        Exists for project_monitor's board-lock heartbeat sweep, which needs to
        tell "this hold's liveness refresh isn't due yet" from "the Redis key
        under this hold is GONE" -- the second is not a liveness question at
        all and cannot wait for the refresh interval, because
        try_acquire_lock()'s Redis transaction reads an absent key back as an
        empty dict and grants the board to the next queued issue without ever
        consulting the still-'locked' durable record.
        """
        if not self.redis_client:
            return False
        redis_lock, read_ok = self._read_redis_lock_only(project, board)
        return read_ok and redis_lock is None

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
        lock, reads_healthy, _serialization_failed = self._get_lock_fail_closed_detail(
            project, board
        )
        return lock, reads_healthy

    def _get_lock_fail_closed_detail(
        self, project: str, board: str
    ) -> Tuple[Optional[PipelineLock], bool, bool]:
        """
        get_lock_fail_closed(), plus whether the YAML leg failed because it
        could not be SERIALIZED rather than because it could not be read.

        Only the release path needs the distinction -- see
        LockStateSerializationError -- and it needs it whether or not the read
        as a whole came back healthy: a serialized-out YAML read that Redis
        happens to answer for still means this call never saw the one
        non-expiring copy of retained_reason.
        """
        redis_lock, redis_ok = self._read_redis_lock_only(project, board)
        yaml_lock, yaml_ok, yaml_serialization_failed = self._read_yaml_lock_only_detail(
            project, board
        )

        if not redis_ok and not yaml_ok:
            return None, False, yaml_serialization_failed

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
            return None, False, yaml_serialization_failed

        if redis_lock and yaml_lock and redis_lock.locked_by_issue == yaml_lock.locked_by_issue:
            if yaml_lock.retained_reason and not redis_lock.retained_reason:
                redis_lock.retained_reason = yaml_lock.retained_reason
                redis_lock.retained_at = yaml_lock.retained_at
            return redis_lock, True, yaml_serialization_failed

        return (redis_lock or yaml_lock), True, yaml_serialization_failed

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

        # Both branches below run under the acquire guard -- see
        # _get_acquire_guard_file() for the full contract, and for why the
        # Redis branch's own atomicity is not enough on its own. Taken ONCE,
        # around both: utils.file_lock refuses a re-entrant acquire
        # (ReentrantFileLockError) rather than hanging on it, and the Redis
        # branch falls through to the YAML one when Redis is unavailable.
        #
        # Deliberately outside the durable safety check above, which is a pure
        # read: the guard's hold is what release_lock() and touch_lock() wait
        # on, so it covers the decision and nothing else.
        from utils.file_lock import file_lock

        acquire_guard = self._get_acquire_guard_file(project, board)
        try:
            with file_lock(acquire_guard, enforce_timeout=True):
                return self._try_acquire_lock_unguarded(project, board, issue_number)
        except TimeoutError as e:
            # Refuse rather than fall through unguarded: an unguarded
            # read-modify-write is precisely the double-grant this exists to
            # prevent, and every caller of this method polls, so a refusal is
            # retried rather than fatal.
            logger.error(
                f"try_acquire_lock: could not serialize the acquisition "
                f"for {project}/{board} (issue #{issue_number}): {e} — refusing rather "
                f"than performing an unguarded read-modify-write"
            )
            return False, "lock_acquire_serialization_timeout"
        except OSError as e:
            # The guard file itself could not be opened/locked (unwritable
            # state dir, fd exhaustion). Same fail-closed posture as the
            # unhealthy-reads check at the top of this method: refuse rather
            # than grant a lock this call cannot make safe.
            logger.error(
                f"try_acquire_lock: could not take the acquisition guard "
                f"for {project}/{board} (issue #{issue_number}): {e} — refusing rather "
                f"than performing an unguarded read-modify-write"
            )
            return False, "lock_acquire_serialization_unavailable"

    def _try_acquire_lock_unguarded(
        self,
        project: str,
        board: str,
        issue_number: int
    ) -> Tuple[bool, str]:
        """
        try_acquire_lock()'s two grant paths. MUST only be called with that
        method's acquire guard held -- see _get_acquire_guard_file().
        """
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
                                        pipe.expire(lock_key, LOCK_TTL_SECONDS)
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
                                    lock_status='locked',
                                    owner_process=PROCESS_OWNER_ID,
                                )
                                
                                pipe.multi()
                                pipe.hset(lock_key, mapping=self._lock_to_redis_mapping(new_lock))
                                pipe.expire(lock_key, LOCK_TTL_SECONDS)
                                return "lock_acquired"

                            result = self.redis_client.transaction(acquire_lock_tx, lock_key, value_from_callable=True)
                            
                            # If we got here, transaction succeeded (or returned early)
                            if result in ["lock_acquired", "already_holds_lock", "stale_lock_recovered"]:
                                # We acquired/held the lock in Redis. Now sync to YAML.
                                #
                                # The sync's result is what authorizes the
                                # success below (#139 review round). It used to
                                # be discarded, on a "Redis is primary" note
                                # that the rest of this class does not agree
                                # with: Redis is the FRESH store, YAML is the
                                # only NON-EXPIRING one, and a grant that
                                # exists solely in Redis is the double-grant
                                # _get_acquire_guard_file() and
                                # _create_lock_yaml_only() both describe.
                                if self._create_lock_yaml_only(project, board, issue_number):
                                    return True, result
                                return self._refuse_unmirrored_redis_grant(
                                    project, board, issue_number, result
                                )
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
        #
        # Everything in _try_acquire_lock_yaml_unguarded() is a plain
        # read-modify-write (read the current lock, decide, then create one)
        # with nothing making it atomic — unlike the Redis branch above, whose
        # WATCH/MULTI transaction is exactly that. Found in review (#146 WI-1):
        # while every async acquisition ran inline on the single-threaded event
        # loop, the loop was accidentally supplying the missing atomicity; once
        # the attempt is offloaded to a worker thread (see
        # project_checkout_lock._acquire_and_start_heartbeat_off_loop) several
        # waiters released by the same poll tick genuinely interleave here and
        # every one of them reads "no lock" before any of them writes one — so
        # every one of them is granted the same lock. The acquire guard the
        # caller holds closes that, and the cross-process case with it
        # (scripts/rebuild_project_images.py or scripts/release_lock.py running
        # alongside the orchestrator).
        return self._try_acquire_lock_yaml_unguarded(project, board, issue_number)

    def _refuse_unmirrored_redis_grant(
        self,
        project: str,
        board: str,
        issue_number: int,
        result: str
    ) -> Tuple[bool, str]:
        """
        Report a Redis grant whose durable YAML mirror did not land as a
        REFUSED acquisition, and undo the Redis side of it where undoing it is
        safe.

        Reported rather than silently downgraded because every caller of
        try_acquire_lock() polls: a refusal costs one poll cycle and is retried,
        while the "success" this replaces put a live dispatch on a lock that
        vanishes with its TTL.

        The rollback is conditional on which grant this was:

          - a NEW grant ("lock_acquired", or the defensive
            "stale_lock_recovered") wrote the key in the transaction that just
            ran, so deleting it restores exactly the state this call found; and
          - "already_holds_lock" did NOT -- the key belongs to a holder that
            was already live and is still running, and deleting it would
            release a lock out from under it. That one is refused without a
            rollback, so the mirror is retried on the holder's next poll.

        Those two also get DIFFERENT reason strings, because a refusal that
        leaves the caller holding the lock breaks the "False means you do not
        hold it" reading the teardown call sites were written against -- see
        LOCK_REFUSAL_REASONS_CALLER_STILL_HOLDS.

        A rollback that itself fails leaves a Redis-only key blocking this
        (project, board) until LOCK_TTL_SECONDS. That is the fail-CLOSED side
        of this failure -- a stalled board an operator can see and clear with
        scripts/release_lock.py, rather than two runs on one board.
        """
        if result == "already_holds_lock":
            logger.error(
                f"try_acquire_lock: issue #{issue_number} holds the Redis lock for "
                f"{project}/{board} but its durable YAML copy could not be written "
                f"— refusing this acquisition rather than reporting a lock only "
                f"Redis knows about. The Redis key is left alone: it belongs to a "
                f"live holder, which is also why this refusal reports "
                f"'lock_mirror_write_failed_while_held' — the caller must not tear "
                f"its run down or release the lock over it."
            )
            return False, "lock_mirror_write_failed_while_held"

        self._rollback_redis_grant(
            project, board, issue_number,
            "could not write the durable YAML copy",
        )
        return False, "lock_mirror_write_failed"

    def _rollback_redis_grant(
        self,
        project: str,
        board: str,
        issue_number: int,
        why: str
    ) -> None:
        """
        Delete a Redis lock key this call itself just wrote, so a grant whose
        durable YAML copy did not land leaves the store exactly as it found it.

        ONLY safe for a NEW grant. A key that belonged to an already-live
        holder before this call must be left alone -- deleting that one
        releases a lock out from under a running pipeline (see
        _refuse_unmirrored_redis_grant's "already_holds_lock" branch).

        A failed rollback is logged and swallowed: the caller refuses either
        way, and the orphaned key fails CLOSED (it blocks this board until
        LOCK_TTL_SECONDS lapses or scripts/release_lock.py clears it) rather
        than open.
        """
        if not self.redis_client:
            return
        try:
            self.redis_client.delete(self._get_lock_key(project, board))
            logger.error(
                f"try_acquire_lock: granted {project}/{board} to issue "
                f"#{issue_number} in Redis but {why} — the Redis grant has been "
                f"rolled back and the acquisition refused"
            )
        except Exception as e:
            logger.error(
                f"try_acquire_lock: granted {project}/{board} to issue "
                f"#{issue_number} in Redis, {why}, and could not roll the Redis "
                f"grant back either: {e} — the acquisition is refused, but that "
                f"key will block this board until its {LOCK_TTL_SECONDS}s TTL "
                f"lapses or scripts/release_lock.py clears it"
            )

    def _try_acquire_lock_yaml_unguarded(
        self,
        project: str,
        board: str,
        issue_number: int
    ) -> Tuple[bool, str]:
        """
        try_acquire_lock()'s YAML-fallback read-modify-write. MUST only be
        called with that method's acquire guard held — see
        _try_acquire_lock_unguarded()'s own docstring and the comment at its
        one call site for why this is not atomic on its own.
        """
        lock = self.get_lock(project, board)

        # Case 1: No existing lock - acquire immediately
        if not lock or lock.lock_status == 'unlocked':
            # _create_lock()'s bool is the grant, not a log line (#139 review
            # round). require_durable_copy=True because "recorded in at least
            # one store" is not enough for a grant: this path is reached
            # whenever the Redis transaction raised, and redis-py reconnects
            # before _create_lock's own hset, so the Redis leg routinely
            # succeeds while the non-expiring YAML copy is the one that fails.
            # Same posture as _refuse_unmirrored_redis_grant(): refuse and let
            # the caller's poll retry, rather than dispatch onto a lock that
            # disappears with its TTL.
            if not self._create_lock(
                project, board, issue_number, require_durable_copy=True
            ):
                return False, "lock_write_failed"
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
                # _release_lock_unguarded, not release_lock: this whole method
                # already runs inside the acquire guard release_lock() now
                # takes for itself, and utils.file_lock refuses a re-entrant
                # acquire (ReentrantFileLockError) rather than hanging on it.
                try:
                    released = self._release_lock_unguarded(project, board, lock.locked_by_issue)
                except LockStateSerializationError as e:
                    # Caught here rather than left to the generic handler below,
                    # whose "Failed to check lock age" message would misreport
                    # it. An acquire is the easy side of this: refusing costs
                    # one poll cycle, and this caller's own poll loop retries.
                    logger.warning(
                        f"try_acquire_lock: could not auto-release the stale lock on "
                        f"{project}/{board} held by issue #{lock.locked_by_issue}: {e} "
                        f"— refusing this acquisition rather than stealing the lock"
                    )
                    return False, f"locked_by_issue_{lock.locked_by_issue}"
                if not released:
                    logger.error(
                        f"Could not release stale lock for {project}/{board} held "
                        f"by issue #{lock.locked_by_issue} — refusing to steal it "
                        f"via a fresh lock creation."
                    )
                    return False, f"locked_by_issue_{lock.locked_by_issue}"
                # See Case 1 above: a write whose durable copy did not land is
                # not a grant, and here the stale holder's record has already
                # been deleted, so reporting success would put a dispatch on a
                # board whose only lock record is a TTL'd Redis key -- or none
                # at all.
                if not self._create_lock(
                    project, board, issue_number, require_durable_copy=True
                ):
                    return False, "lock_write_failed"
                return True, "stale_lock_recovered"
        except Exception as e:
            logger.warning(f"Failed to check lock age: {e}")

        # Case 4: Lock held by another issue (not stale)
        logger.debug(
            f"Pipeline {project}/{board} locked by issue #{lock.locked_by_issue}, "
            f"issue #{issue_number} must wait"
        )
        return False, f"locked_by_issue_{lock.locked_by_issue}"

    def _create_lock_yaml_only(self, project: str, board: str, issue_number: int) -> bool:
        """
        Sync the Redis-acquired lock to YAML (helper for Redis sync — called after
        every successful try_acquire_lock, including the "already_holds_lock" result
        which fires on every poll while an issue holds the lock).

        Returns:
            True when the durable copy of this grant is on disk, False when the
            write did not land — a full state dir, a read-only mount, or the
            inner '<state>.yaml.lock' still held by another process after
            STATE_LOCK_TIMEOUT_SECONDS. Found in the #139 review round: this
            used to swallow _save_lock_to_yaml()'s bool, so its caller reported
            a grant whose only copy was the Redis key — invisible to
            get_all_locks(), reported by _read_yaml_lock_only() as a HEALTHY
            "no lock", and gone once LOCK_TTL_SECONDS lapsed, at which point
            try_acquire_lock()'s transaction reads the absent key back as an
            empty dict and grants the same board to a second issue. That is the
            exact outcome _get_acquire_guard_file() exists to prevent, reached
            through a failed write instead of through a concurrent release.

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
                return True  # nothing changed — avoid an unnecessary rewrite
            existing.lock_status = 'locked'
            return self._save_lock_to_yaml(existing)

        lock = PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked',
            owner_process=PROCESS_OWNER_ID,
        )
        return self._save_lock_to_yaml(lock)

    def _create_lock(
        self,
        project: str,
        board: str,
        issue_number: int,
        require_durable_copy: bool = False
    ) -> bool:
        """
        Create a new lock (Legacy/Fallback method).

        Args:
            require_durable_copy: set by the callers for whom this write IS the
                grant — try_acquire_lock()'s YAML-fallback path. See below.

        Returns:
            With require_durable_copy=False (the default, used by tests and
            other bookkeeping callers): True if the lock was recorded in at
            least one store, mirroring mark_lock_failed's fail-open-across-
            two-stores pattern, and False only if BOTH writes failed.

            With require_durable_copy=True: True only when the NON-EXPIRING
            YAML copy landed, and the Redis write is rolled back when it did
            not. Found in the #139 review round: "at least one store" is the
            right bar for a durable failure MARKER but not for a grant, and
            the YAML-fallback path is reached whenever the Redis transaction
            raised — a connection blip redis-py reconnects from before this
            method's own hset/expire, so redis_ok is routinely True here. On a
            YAML write failure that made this report a grant whose only copy
            was the TTL'd Redis key: invisible to get_all_locks(), a HEALTHY
            "no lock" to _read_yaml_lock_only(), and gone at LOCK_TTL_SECONDS,
            at which point try_acquire_lock()'s transaction reads the absent
            key back as an empty dict and grants the same board to a second
            issue while the first run is still live. That is the exact
            double-grant _create_lock_yaml_only()'s docstring says must not
            happen, reached through the sibling path.

            The rollback is unconditionally safe here in a way it is not in
            _refuse_unmirrored_redis_grant(): both require_durable_copy callers
            are NEW grants — Case 1 found no lock at all, and Case 3 has
            already released the stale holder's record — so deleting the key
            this method just wrote restores what the call found.
        """
        lock = PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked',
            owner_process=PROCESS_OWNER_ID,
        )

        # Write to Redis with 2 hour TTL
        redis_ok = False
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                self.redis_client.hset(lock_key, mapping=self._lock_to_redis_mapping(lock))
                self.redis_client.expire(lock_key, LOCK_TTL_SECONDS)
                redis_ok = True
                logger.debug(f"Created lock in Redis: {lock_key}")
            except Exception as e:
                logger.error(f"Failed to create lock in Redis: {e}")

        # Write to YAML for persistence
        yaml_ok = self._save_lock_to_yaml(lock)

        if require_durable_copy and not yaml_ok:
            if redis_ok:
                self._rollback_redis_grant(
                    project, board, issue_number,
                    "could not write the durable YAML copy on the fallback path",
                )
            logger.error(
                f"_create_lock: the durable YAML copy of the lock for "
                f"{project}/{board} issue #{issue_number} could not be written "
                f"— refusing this acquisition rather than reporting a grant that "
                f"only a TTL'd Redis key records"
            )
            return False

        if not redis_ok and not yaml_ok:
            logger.error(
                f"_create_lock: BOTH Redis and YAML writes failed for "
                f"{project}/{board} issue #{issue_number} — no lock was "
                f"actually recorded anywhere"
            )
            return False

        logger.info(
            f"Pipeline lock acquired: {project}/{board} by issue #{issue_number}"
        )
        return True

    def touch_lock(self, project: str, board: str, issue_number: int) -> TouchResult:
        """
        Refresh an ALREADY-HELD lock's liveness markers -- both the Redis TTL
        AND lock_acquired_at -- without changing its holder.

        Added for services/project_checkout_lock.py's heartbeat mechanism
        (found necessary in code review, #56): try_acquire_lock()'s
        "already_holds_lock" branch already refreshes the Redis TTL on a
        repeat call from the same issue_number, but it does NOT reset
        lock_acquired_at -- so a lock held by a caller that only ever
        re-calls try_acquire_lock() (never this method) would still be
        judged stale by the 4-hour age-based heuristic elsewhere in this
        class and could be handed to a different caller while the original
        holder is still genuinely alive and heartbeating. This method exists
        specifically to reset that clock too.

        Callers must already know they hold this lock (e.g. a heartbeat loop
        started immediately after a successful try_acquire_lock() for this
        exact issue_number) -- this does NOT acquire on behalf of a new
        holder.

        Each store's write is a COMPARE-AND-SET against that store's own
        current record, not a blind overwrite (#153 WI-8, from #140 item 15).
        The read below stays as a cheap upfront rejection, but it is no longer
        what authorizes the write: the Redis leg re-reads and writes inside a
        WATCH/MULTI transaction (the same shape try_acquire_lock() already
        uses), and the YAML leg re-reads and writes under one held
        '<state>.yaml.lock'. Before that, a heartbeat that was very late --
        late enough for its own holder to have been judged stale (7200s Redis
        TTL, then the 4-hour age heuristic) and the lock handed to a SECOND
        caller in the meantime -- could still land its refresh on top of that
        second caller's record and silently take the lock back from a live
        holder. See _touch_lock_redis()/_touch_lock_yaml_unguarded() for each
        leg.

        The WHOLE method runs under the '<state>.yaml.acquire.lock' guard, and
        release_lock() now takes that same guard for the whole of its own
        two-store delete (found in the WI-8 review round; see
        _get_acquire_guard_file). That is what makes "the record is gone from a
        store" unambiguous here. Without it, a touch and a release genuinely
        interleaved in two ways, both of which put a released lock back:

          - between this method's two legs -- the Redis leg refreshed the key,
            the release then deleted the key AND unlinked the state file, and
            the YAML leg re-created the durable record from `fallback_lock`
            because the Redis leg had said REFRESHED; and
          - inside the YAML leg itself, whose read and write each took and
            released '<state>.yaml.lock' separately, so a release could unlink
            in the gap and the write would re-create the file regardless of
            what re-establishment was authorized.

        Either one left a durable 'locked' record naming an issue whose run had
        ended, with the 4h staleness clock reset -- and nothing reclaims that
        at runtime (see _refresh_held_board_locks()' docstring). Serializing
        against release_lock() closes both, so the only remaining reading of
        "gone from a store" is the one this method should self-heal: a store
        that lost a record while the other still positively names this holder
        (a lapsed Redis TTL under a live hold, or a YAML write that failed at
        acquisition time).

        Args:
            project: Project name
            board: Board name
            issue_number: The issue this caller believes holds the lock

        Returns:
            TouchResult.REFRESHED if the lock was found (held by
            issue_number) and its liveness genuinely extended -- which means
            the Redis write landed whenever a Redis client is configured,
            since Redis holds the only expiring copy (see the write path
            below); TouchResult.NOT_HELD if it is confirmed NOT currently held
            by issue_number (including "no lock exists"); TouchResult.
            REFRESH_FAILED if liveness could not be extended because the
            stores themselves failed (both reads failed, so the state is
            genuinely unknown, or the writes that matter failed). Only REFRESHED is
            truthy (see TouchResult), so callers written against the
            original bool return keep their original meaning while callers
            that need to distinguish "lost to another holder" from "the
            stores are down" now can -- see project_checkout_lock.py's
            _heartbeat_worker(), which logs and escalates the two very
            differently.

        Note (found in PR #138 review, /pr-review-toolkit:review-pr): reads via
        get_lock_fail_closed(), not the plain get_lock() this method used
        before -- get_lock() collapses "confirmed unlocked" and "both Redis
        and YAML reads raised" into the same None, so a transient dual-store
        outage would be indistinguishable from "lock genuinely lost to
        another holder" to this method's caller. project_checkout_lock.py's
        heartbeat logs the latter as a specific, alarming "lock lost to a
        competing holder" ERROR -- which would have been a false alarm for a
        momentary storage hiccup. Reads being unhealthy is logged distinctly
        below AND returned distinctly (REFRESH_FAILED, not NOT_HELD) rather
        than folded into the "genuinely not held" case.
        """
        from utils.file_lock import file_lock

        try:
            with file_lock(self._get_acquire_guard_file(project, board), enforce_timeout=True):
                return self._touch_lock_guarded(project, board, issue_number)
        except TimeoutError as e:
            # Same posture as try_acquire_lock()'s guarded path and as the YAML
            # leg had on its own before the guard was hoisted here: an
            # unserialized read-modify-write across two stores is exactly the
            # race this guard exists to close, so report the refresh as failed
            # rather than perform one. Every caller of this method is a
            # heartbeat loop, so a refusal is retried on the next tick.
            logger.warning(
                f"touch_lock: could not serialize the refresh for {project}/{board} "
                f"(issue #{issue_number}): {e} -- reporting it as failed rather than "
                f"racing a concurrent acquire or release"
            )
            return TouchResult.REFRESH_FAILED
        except OSError as e:
            logger.warning(
                f"touch_lock: could not take the refresh guard for {project}/{board} "
                f"(issue #{issue_number}): {e}"
            )
            return TouchResult.REFRESH_FAILED

    def _touch_lock_guarded(self, project: str, board: str, issue_number: int) -> TouchResult:
        """
        touch_lock()'s body. MUST only be called with that method's
        '<state>.yaml.acquire.lock' guard held -- see its docstring for what
        the guard makes true about the two legs below.
        """
        lock, reads_healthy = self.get_lock_fail_closed(project, board)
        if not reads_healthy:
            logger.error(
                f"touch_lock: could not determine lock state for {project}/{board} "
                f"(both Redis and YAML reads failed) -- cannot confirm issue "
                f"#{issue_number} still holds this lock, but this is NOT confirmed "
                f"loss to another holder either; refusing to refresh liveness "
                f"rather than silently reporting a false 'lock lost' condition"
            )
            return TouchResult.REFRESH_FAILED
        if not lock or lock.locked_by_issue != issue_number:
            return TouchResult.NOT_HELD

        redis_ok = False
        redis_result = None
        redis_key_present = False
        if self.redis_client:
            # allow_reestablish=False on this first pass, always: re-creating
            # an absent key from `lock` -- a snapshot read before any of this
            # -- is exactly what let a touch undo a concurrent release. Only
            # the guarded YAML read below may authorize that, in the second
            # pass at the bottom of this method.
            redis_result, redis_key_present = self._touch_lock_redis(
                project, board, issue_number, lock, allow_reestablish=False
            )
            if redis_result is TouchResult.NOT_HELD and redis_key_present:
                # Redis is the primary store AND the only expiring one, so its
                # compare-and-set losing is a definitive "this lock is somebody
                # else's now" -- return without touching YAML, which would
                # otherwise write a record contradicting Redis.
                return TouchResult.NOT_HELD
            redis_ok = redis_result is TouchResult.REFRESHED

        yaml_result, yaml_record = self._touch_lock_yaml_unguarded(
            project, board, issue_number, lock,
            # A missing/unlocked durable record may only be re-created while
            # Redis still positively names this holder. No release can be in
            # flight (this method holds the guard release_lock() takes), so
            # "Redis has it, YAML doesn't" is a YAML write that failed at
            # acquisition time -- precisely what this leg should heal.
            allow_reestablish=redis_ok,
        )
        if yaml_result is TouchResult.NOT_HELD and redis_result is not TouchResult.REFRESHED:
            # The durable record is somebody else's, or gone with nothing in
            # Redis contradicting it. Gated on Redis having actually produced a
            # REFRESHED verdict rather than on a client merely being configured
            # (found in the WI-8 review round): a REFRESH_FAILED leg answered
            # nothing at all, so folding a YAML-confirmed loss into "both legs
            # failed" hid the one store that knew -- and left
            # project_checkout_lock's "may now be racing a different holder"
            # ERROR unreachable during exactly the Redis outage that produces
            # the loss. An absent Redis key is likewise no contradiction: it is
            # what a completed release leaves behind.
            return TouchResult.NOT_HELD
        yaml_ok = yaml_result is TouchResult.REFRESHED

        if (self.redis_client and not redis_key_present
                and redis_result is TouchResult.NOT_HELD and yaml_record is not None):
            # The Redis key's TTL lapsed under a hold the durable copy -- read
            # fresh by the YAML leg just above under the state file's own lock,
            # NOT from this call's opening snapshot -- still records as this
            # holder's. Re-establish it from that record: this is the self-heal
            # the heartbeat exists to provide, and routing it through that
            # re-read is what keeps it from resurrecting a lock release_lock()
            # has already deleted.
            redis_result, _ = self._touch_lock_redis(
                project, board, issue_number, yaml_record, allow_reestablish=True
            )
            if redis_result is TouchResult.NOT_HELD:
                # Somebody acquired the key between the guarded YAML read and
                # this re-establish. Confirmed loss, not a store failure.
                return TouchResult.NOT_HELD
            redis_ok = redis_result is TouchResult.REFRESHED

        if not redis_ok and not yaml_ok:
            logger.error(
                f"touch_lock: BOTH Redis and YAML refresh writes failed for "
                f"{project}/{board} issue #{issue_number} -- liveness was NOT "
                f"extended, this lock may be stolen by the staleness heuristic"
            )
            return TouchResult.REFRESH_FAILED

        # A Redis write failure is REFRESH_FAILED even when the YAML write
        # succeeded -- found in a later review round (#146 WI-1). The two
        # stores are NOT interchangeable for this method's purpose: only the
        # Redis lock key has a TTL (LOCK_TTL_SECONDS), and extending it is the
        # entire reason the heartbeat that calls this exists (see
        # project_checkout_lock.HEARTBEAT_INTERVAL_SECONDS). The YAML copy
        # never expires, so refreshing it alone buys nothing against that
        # clock -- and try_acquire_lock()'s Redis transaction reads an expired
        # key back as an empty dict, which is falsy, so it grants the lock to
        # a second caller without ever consulting the still-valid YAML copy.
        # OR-ing the two legs reported that outage (Redis writes failing while
        # its reads still succeed: OOM under noeviction, MISCONF after a failed
        # BGSAVE, READONLY after a failover) as a full success, which reset the
        # heartbeat's failure run every tick and left the sustained-failure
        # escalation written for exactly that case unreachable.
        if self.redis_client and not redis_ok:
            logger.error(
                f"touch_lock: Redis refresh write failed for {project}/{board} "
                f"issue #{issue_number} -- the {LOCK_TTL_SECONDS}s lock-key TTL was NOT extended "
                f"(only the TTL-less YAML copy was), so this hold is still on the "
                f"clock and may be acquired by a second caller when the key lapses"
            )
            return TouchResult.REFRESH_FAILED

        return TouchResult.REFRESHED

    def _refreshed_from(self, project: str, board: str, issue_number: int, current: PipelineLock) -> PipelineLock:
        """
        The record touch_lock() writes: `current` with lock_acquired_at reset
        to now. Shared by both legs so they can never drift in which fields a
        refresh is allowed to change.

        retained_reason/retained_at are preserved rather than silently cleared
        -- mirrors _create_lock_yaml_only()'s own defensive preservation. In
        practice this lock should never be retained while something is still
        successfully heartbeating it (mark_lock_failed() is for a run that has
        already ended), but this stays defensive against that edge case rather
        than relying on a single layer of protection.

        owner_process is preserved, not re-stamped: a heartbeat normally runs
        in the holding process, so re-stamping would be a no-op -- but a touch
        that ever ran anywhere else must not silently re-attribute the lock and
        make a live foreign holder look like this process's own dead
        predecessor to recover_orphaned_resource_locks().
        """
        return PipelineLock(
            project=project,
            board=board,
            locked_by_issue=issue_number,
            lock_acquired_at=datetime.now(timezone.utc).isoformat(),
            lock_status='locked',
            retained_reason=current.retained_reason,
            retained_at=current.retained_at,
            owner_process=current.owner_process,
        )

    def _touch_lock_redis(
        self, project: str, board: str, issue_number: int, fallback_lock: PipelineLock,
        allow_reestablish: bool
    ) -> Tuple[TouchResult, bool]:
        """
        touch_lock()'s Redis leg, as a WATCH/MULTI compare-and-set (#153 WI-8).

        Reads the lock key and writes the refreshed record inside one
        transaction watching that key, so an acquire that lands between the two
        aborts and retries this whole callable (redis-py's Redis.transaction()
        loops on WatchError) -- at which point the re-read sees the new holder
        and returns NOT_HELD instead of overwriting it. That is the entire
        point: the previous blind hset could land a very late heartbeat on top
        of a second caller's freshly-won record.

        WATCH does NOT make an ABSENT key safe to re-create, which an earlier
        version of this leg claimed: it aborts on a concurrent create, but the
        absent branch is reached precisely by a concurrent DELETE, and the
        WatchError retry then re-reads the same absent key and writes anyway.
        So re-establishing is gated on `allow_reestablish` instead, and
        touch_lock() only passes True once a guarded read of the durable YAML
        record -- taken after this leg observed the key missing -- has
        confirmed the hold is still this holder's. Whether the key was there is
        returned alongside the result so that caller can tell "confirmed
        somebody else's" from "gone".

        Returns (result, key_was_present): REFRESHED (written), NOT_HELD
        (confirmed somebody else's, or absent with re-establishment not
        authorized), or REFRESH_FAILED (the transaction itself raised -- a
        connection drop, or a write refused by OOM/MISCONF/READONLY, both of
        which surface out of the transaction's own execute()).
        """
        lock_key = self._get_lock_key(project, board)

        def touch_lock_tx(pipe):
            lock_data = pipe.hgetall(lock_key)
            if not lock_data:
                if not allow_reestablish:
                    return "absent"
                current = fallback_lock
            else:
                if lock_data.get('lock_status') != 'locked':
                    return "not_held"
                if int(lock_data.get('locked_by_issue', 0)) != issue_number:
                    return "not_held"
                current = self._lock_from_redis_data(lock_data)

            refreshed = self._refreshed_from(project, board, issue_number, current)
            pipe.multi()
            pipe.hset(lock_key, mapping=self._lock_to_redis_mapping(refreshed))
            pipe.expire(lock_key, LOCK_TTL_SECONDS)
            return "refreshed" if lock_data else "reestablished"

        try:
            result = self.redis_client.transaction(
                touch_lock_tx, lock_key, value_from_callable=True
            )
        except Exception as e:
            logger.warning(f"touch_lock: failed to refresh Redis for {project}/{board}: {e}")
            return TouchResult.REFRESH_FAILED, False

        if result == "not_held":
            logger.warning(
                f"touch_lock: {project}/{board} is no longer held by issue "
                f"#{issue_number} in Redis -- refusing to overwrite the current "
                f"holder's record with this refresh"
            )
            return TouchResult.NOT_HELD, True
        if result == "absent":
            logger.debug(
                f"touch_lock: the Redis lock key for {project}/{board} is gone -- "
                f"not re-creating it for issue #{issue_number} from this refresh's "
                f"own opening read"
            )
            return TouchResult.NOT_HELD, False
        return TouchResult.REFRESHED, result == "refreshed"

    def _touch_lock_yaml_unguarded(
        self, project: str, board: str, issue_number: int, fallback_lock: PipelineLock,
        allow_reestablish: bool
    ) -> Tuple[TouchResult, Optional[PipelineLock]]:
        """
        touch_lock()'s YAML leg (#153 WI-8). MUST only be called with
        touch_lock()'s '<state>.yaml.acquire.lock' guard held -- that is what
        serializes it against try_acquire_lock()'s YAML-fallback grant and
        against release_lock(), the only other two operations that decide who
        holds this lock.

        Everything here is a read-modify-write: read the current record, verify
        it is still ours, rewrite it. It runs entirely inside ONE held
        '<state>.yaml.lock' (found in the WI-8 review round -- the read and the
        write used to take and release that lock separately, and release_lock()
        takes only that inner lock, so a release landing in the gap was
        re-created by the write regardless of what the read had seen).
        _read_yaml_lock_only_unlocked()/_save_lock_to_yaml_unlocked() exist for
        exactly this: the locking variants cannot be nested inside their own
        lock, because fcntl.flock() conflicts between two descriptors of the
        same file even within one process (utils.file_lock raises
        ReentrantFileLockError rather than hanging on it).

        The two lock files nest, and always in this order: '.acquire.lock'
        OUTER, '<state>.yaml.lock' INNER. That is the same order
        try_acquire_lock()'s guarded path and release_lock() establish, and
        nothing in this class ever takes '.acquire.lock' while holding
        '<state>.yaml.lock', so there is no ordering cycle to deadlock on.

        Returns (result, written_record): REFRESHED (written, and the record
        written is returned so the Redis leg can re-establish a lapsed key from
        exactly the same content), NOT_HELD (the YAML record names a different
        holder, or is missing/unlocked with re-establishment not authorized),
        or REFRESH_FAILED (the inner lock could not be taken, the read failed,
        or the write failed).
        """
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project, board)
        state_lock = state_file.with_suffix(state_file.suffix + '.lock')
        try:
            with file_lock(
                state_lock, timeout=STATE_LOCK_TIMEOUT_SECONDS, enforce_timeout=True
            ):
                existing, read_ok = self._read_yaml_lock_only_unlocked(project, board)
                if not read_ok:
                    return TouchResult.REFRESH_FAILED, None
                if existing is not None and existing.locked_by_issue != issue_number:
                    logger.warning(
                        f"touch_lock: the YAML lock record for {project}/{board} names "
                        f"issue #{existing.locked_by_issue}, not #{issue_number} -- "
                        f"refusing to overwrite it with this refresh"
                    )
                    return TouchResult.NOT_HELD, None
                if existing is None and not allow_reestablish:
                    # Missing or unlocked, with nothing in Redis contradicting
                    # it: that is what release_lock() leaves behind, and
                    # re-creating it from fallback_lock -- a snapshot read
                    # taken before this call's guard was even acquired -- would
                    # silently undo it and wedge the board behind an issue
                    # whose run has already ended.
                    logger.warning(
                        f"touch_lock: the YAML lock record for {project}/{board} is "
                        f"missing or unlocked and Redis does not name issue "
                        f"#{issue_number} either -- treating this as a released lock "
                        f"rather than re-creating it from this refresh's opening read"
                    )
                    return TouchResult.NOT_HELD, None
                # existing is None here only when Redis still positively names
                # this holder, i.e. the durable copy is the one that is missing
                # (a YAML write that failed at acquisition time). Re-create it
                # from fallback_lock.
                current = existing if existing is not None else fallback_lock
                refreshed = self._refreshed_from(project, board, issue_number, current)
                if not self._save_lock_to_yaml_unlocked(refreshed):
                    return TouchResult.REFRESH_FAILED, None
                return TouchResult.REFRESHED, refreshed
        except TimeoutError as e:
            logger.warning(
                f"touch_lock: could not take the YAML state lock for {project}/{board} "
                f"(issue #{issue_number}): {e} -- reporting the YAML leg as failed rather "
                f"than performing a read and a write that a release could land between"
            )
            return TouchResult.REFRESH_FAILED, None
        except OSError as e:
            logger.warning(
                f"touch_lock: could not take the YAML state lock for {project}/{board} "
                f"(issue #{issue_number}): {e}"
            )
            return TouchResult.REFRESH_FAILED, None

    def release_lock(
        self, project: str, board: str, issue_number: int, force: bool = False
    ) -> ReleaseResult:
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

        Runs under the '<state>.yaml.acquire.lock' guard for the whole of its
        two-store delete (found in the WI-8 review round; see
        _get_acquire_guard_file). Deleting the Redis key and unlinking the
        state file are two separate writes, and touch_lock() reads both stores
        and writes both stores -- so with no shared guard, a heartbeat that
        overlapped a release re-created the lock from a snapshot that predated
        it, leaving a durable 'locked' record for an issue whose run had ended
        and a reset 4h staleness clock. Nothing reclaims that at runtime. The
        guard makes the release atomic with respect to touch_lock() and to
        BOTH of try_acquire_lock()'s grant paths -- its Redis path was the one
        exception until the #139 review round, where an acquire landing between
        this method's two legs had its brand-new state file unlinked here (the
        YAML ownership re-check below is skipped when Redis confirmed
        ownership, so the mismatch was invisible).

        Unlike try_acquire_lock()'s guarded path, a guard timeout here is NOT
        simply refused (found in the WI-8 review round). An acquire that is
        refused is retried by its own poll loop within seconds; a release has
        already been authorized by its caller and, if dropped, leaks the lock
        until the LOCK_TTL_SECONDS Redis TTL or the 4-hour staleness heuristic
        -- blocking every dispatch for that (project, board) meanwhile, and
        with no automatic re-attempt at the site that matters most
        (pipeline_progression._release_lock_on_exit_column fires only on the
        move INTO an exit column, so it never re-fires). So the guard gets a
        second, much longer bounded attempt (RELEASE_GUARD_RETRY_TIMEOUT_SECONDS),
        and only a release that exhausts that too is reported -- distinctly, as
        SERIALIZATION_FAILED rather than as a refusal. The same distinction is
        made for the INNER '<state>.yaml.lock' this method's body takes twice
        (see _release_lock_unguarded and LockStateSerializationError): a
        release that cannot be serialized there is SERIALIZATION_FAILED too,
        not the "state unknown, refusing" that a failed read means.

        Args:
            project: Project name
            board: Board name
            issue_number: Issue number releasing the lock
            force: Release even if the lock is retained-due-to-failure, or if
                lock state couldn't be determined. Only scripts/release_lock.py
                should ever pass this.

        Returns:
            ReleaseResult.RELEASED if the lock was released;
            ReleaseResult.NOT_RELEASED if the release was considered and
            refused (not held by this issue, retained/unknown without force, or
            a store's delete failed); ReleaseResult.SERIALIZATION_FAILED if the
            acquire guard could not be taken at all, in which case NOTHING was
            attempted and the lock is still held exactly as it was. Only
            RELEASED is truthy (see ReleaseResult), so callers written against
            the original bool return keep their original meaning, while the
            three that report a failed release to operators can stop
            attributing a contention timeout to a retained failed run.
        """
        from utils.file_lock import file_lock

        guard_file = self._get_acquire_guard_file(project, board)
        try:
            try:
                with file_lock(
                    guard_file, timeout=RELEASE_GUARD_TIMEOUT_SECONDS, enforce_timeout=True
                ):
                    return self._release_lock_to_result(project, board, issue_number, force)
            except TimeoutError as first_timeout:
                # Not fatal on its own -- see the docstring. The usual cause is
                # a try_acquire_lock() running against an unavailable Redis:
                # both of its branches take this same guard, and both spend
                # LOCK_REDIS_SOCKET_TIMEOUT_SECONDS per Redis call inside it
                # while its waiters re-poll faster than it lets go. The whole
                # of that worst case is what
                # RELEASE_GUARD_RETRY_TIMEOUT_SECONDS is derived from.
                logger.warning(
                    f"release_lock: the acquire guard for {project}/{board} (issue "
                    f"#{issue_number}) was still contended after "
                    f"{RELEASE_GUARD_TIMEOUT_SECONDS}s: "
                    f"{first_timeout} -- retrying for up to "
                    f"{RELEASE_GUARD_RETRY_TIMEOUT_SECONDS}s rather than abandoning "
                    f"an authorized release"
                )
                with file_lock(
                    guard_file, timeout=RELEASE_GUARD_RETRY_TIMEOUT_SECONDS, enforce_timeout=True
                ):
                    return self._release_lock_to_result(project, board, issue_number, force)
        except TimeoutError as e:
            # Still not serialized. An unguarded release is exactly the
            # interleaving this guard exists to remove, so it is not performed
            # -- but this is reported as SERIALIZATION_FAILED, NOT as a
            # refusal: nothing was attempted, the lock is unchanged, and the
            # release is still outstanding.
            logger.error(
                f"release_lock: could not serialize the release of {project}/{board} "
                f"(issue #{issue_number}) within {RELEASE_GUARD_RETRY_TIMEOUT_SECONDS}s: "
                f"{e} -- the lock is UNCHANGED and still held; this is contention on "
                f"'{guard_file.name}', not a retained/failed lock"
            )
            return ReleaseResult.SERIALIZATION_FAILED
        except OSError as e:
            logger.error(
                f"release_lock: could not take the release guard for {project}/{board} "
                f"(issue #{issue_number}): {e} -- the lock is UNCHANGED and still held; "
                f"this is a guard-file failure, not a retained/failed lock"
            )
            return ReleaseResult.SERIALIZATION_FAILED

    def _release_lock_to_result(
        self, project: str, board: str, issue_number: int, force: bool
    ) -> ReleaseResult:
        """
        _release_lock_unguarded()'s bool, mapped onto release_lock()'s
        three-state return. MUST only be called with the acquire guard held.

        Its False covers only outcomes the release actually CONSIDERED (not
        held by this issue, retained without force, unknown state, a store's
        delete failing), all of which are NOT_RELEASED. The release's own
        contention on the inner '<state>.yaml.lock' arrives as
        LockStateSerializationError instead and joins the guard timeout in
        SERIALIZATION_FAILED -- see that exception for why it must not be
        reported as one of the considered outcomes.
        """
        try:
            released = self._release_lock_unguarded(project, board, issue_number, force=force)
        except LockStateSerializationError as e:
            logger.error(
                f"release_lock: {e} -- the release did NOT complete and is still "
                f"outstanding; this is contention on '{self._get_state_file(project, board).name}"
                f".lock', not a retained/failed lock"
            )
            return ReleaseResult.SERIALIZATION_FAILED
        return ReleaseResult.RELEASED if released else ReleaseResult.NOT_RELEASED

    def _release_lock_unguarded(
        self, project: str, board: str, issue_number: int, force: bool = False
    ) -> bool:
        """
        release_lock()'s body. MUST only be called with that method's
        '<state>.yaml.acquire.lock' guard held -- see its docstring for why.

        Called directly by _try_acquire_lock_yaml_unguarded()'s stale-lock
        recovery, which already runs inside that same guard (utils.file_lock
        refuses a re-entrant acquire rather than hanging on it).

        Raises:
            LockStateSerializationError: the inner '<state>.yaml.lock' could
                not be taken within STATE_LOCK_TIMEOUT_SECONDS, for either the
                fail-closed state read or the YAML delete. Deliberately not
                folded into the False return -- see that exception. Both
                callers handle it: _release_lock_to_result() maps it to
                ReleaseResult.SERIALIZATION_FAILED, and the stale-lock recovery
                refuses the acquisition it was clearing the way for.
        """
        if not force:
            existing_lock, reads_healthy, serialization_failed = self._get_lock_fail_closed_detail(
                project, board
            )
            if serialization_failed:
                # NOT the fail-closed refusal below (found in the WI-8 review
                # round): a read that could not be serialized established
                # nothing about this lock, so reporting it as "state unknown,
                # refusing" hands the caller a durable-sounding fact that was
                # never determined. See LockStateSerializationError.
                raise LockStateSerializationError(
                    f"could not serialize the state read for {project}/{board} (issue "
                    f"#{issue_number}) against a concurrent holder of the YAML state "
                    f"lock within {STATE_LOCK_TIMEOUT_SECONDS}s"
                )
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
                # Use file lock when deleting to prevent race with writers.
                # Bounded like every other acquisition of this file (found in
                # the WI-8 review round -- this one was still a blocking
                # acquire, which left the enclosing '.acquire.lock' guard's
                # hold unbounded no matter what the reads above did).
                lock_file = state_file.with_suffix(state_file.suffix + '.lock')
                with file_lock(
                    lock_file, timeout=STATE_LOCK_TIMEOUT_SECONDS, enforce_timeout=True
                ):
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
                                # Has its own except (rather than letting this fall
                                # into the outer except below, which also now
                                # correctly returns False) specifically so this
                                # failure gets its own distinct log message about
                                # the surviving YAML file — the outer except's
                                # message is generic and scoped to the
                                # ownership-check machinery above (file_lock
                                # acquisition, etc.), which runs before any
                                # destructive action is taken. Before this fix
                                # existed, an unlink() failure WAS silently
                                # swallowed and fell through to the success path
                                # below — release_lock reported success while a
                                # retained lock's YAML record silently remained.
                                logger.error(
                                    f"Failed to delete lock YAML file {state_file}: {unlink_err}"
                                )
                                return False
                            logger.debug(f"Deleted lock YAML file: {state_file}")
                        else:
                            return False

            except TimeoutError as e:
                # Checked before the generic handler below -- TimeoutError is an
                # OSError. The deletion was neither attempted nor verified, and
                # the reason is contention rather than an unreadable/undeletable
                # record, so this is a serialization failure the caller can
                # retry rather than the considered refusal a bare False means.
                raise LockStateSerializationError(
                    f"could not serialize the YAML delete for {project}/{board} (issue "
                    f"#{issue_number}) against a concurrent holder of the YAML state "
                    f"lock within {STATE_LOCK_TIMEOUT_SECONDS}s: {e}"
                )
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
                self.redis_client.expire(lock_key, LOCK_TTL_SECONDS)
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

    def clear_retained_reason(self, project: str, board: str, issue_number: int) -> bool:
        """
        Clear a durable retained_reason while leaving the lock's locked_by_issue
        untouched — the inverse of mark_lock_failed().

        This exists for a narrow, deliberate case: a watchdog-driven self-heal
        (zombie auto-retry within budget, or an active-resume after the Claude
        Code breaker closes) that has decided THIS SPECIFIC failure gets one
        more automatic attempt. The caller must immediately follow this with a
        re-dispatch of the same issue — never leave a lock in "cleared but
        nothing redispatched" limbo, since nothing else will notice the retry is
        owed.

        Deliberately does NOT release the lock (that's release_lock(), the
        human-recovery action, or a plain end_pipeline_run(retain_lock=False)
        release). Keeping locked_by_issue set for the whole self-heal window is
        the point: it stops any other issue from acquiring the pipeline lock in
        the gap between "this attempt failed" and "the retry actually starts",
        which is exactly the race that orphaned issue #853 in incident
        e42ca133 (in the managed documentation_robotics project's repo, not
        this one — investigated live in a pipeline-investigate session, not a
        separate written postmortem) — the lock was released outright, and a
        different issue (#854) acquired it before the stalled-issue rescan
        could re-pick #853.

        Safe to call even if the lock has no retained_reason set (no-op,
        returns True) — callers don't need to check get_retained_reason() first.

        NOT the mirror image of mark_lock_failed()'s "succeeded if at least
        one store wrote" contract, even though the write logic below looks
        identical to it. get_lock()/get_lock_fail_closed()'s merge is
        asymmetric: it merges retained_reason IN from whichever store has it
        set, with no corresponding case for merging a clear OUT. So if only
        one store's write here actually succeeds, the next read anywhere
        (most importantly the retained-lock dispatch gate in
        project_monitor.py) would silently re-merge the stale store's
        retained_reason back in — reporting True here while the lock still
        reads as retained is worse than reporting the clear failed, since the
        caller's fail-safe branch (re-mark failed + notify a human) never
        fires. Both writes must succeed for a True return; a partial write is
        treated the same as a total failure.

        Returns:
            True if the lock is held by issue_number AND retained_reason is
            durably clear in every store that's configured (already-clear is
            a no-op True). False if no lock is currently held by this issue,
            or if any configured store's write failed — callers MUST treat
            False as "still retained, do not proceed with redispatch."
        """
        lock = self.get_lock(project, board)
        if not lock or lock.locked_by_issue != issue_number:
            logger.error(
                f"clear_retained_reason: no lock held by issue #{issue_number} for "
                f"{project}/{board} (current holder: "
                f"{lock.locked_by_issue if lock else 'none'}) — cannot clear"
            )
            return False

        if not lock.retained_reason:
            # Already clear — nothing to do, but this is not an error condition.
            return True

        lock.retained_reason = None
        lock.retained_at = None

        redis_ok = False
        if self.redis_client:
            try:
                lock_key = self._get_lock_key(project, board)
                self.redis_client.hset(lock_key, mapping=self._lock_to_redis_mapping(lock))
                self.redis_client.expire(lock_key, LOCK_TTL_SECONDS)
                redis_ok = True
            except Exception as e:
                logger.error(f"Failed to clear retained reason in Redis: {e}")

        yaml_ok = self._save_lock_to_yaml(lock)

        # Unlike mark_lock_failed, a clear requires EVERY configured store to
        # have actually succeeded — see the docstring's asymmetric-merge
        # rationale above. redis_ok stays False (correctly) when no
        # redis_client is configured at all, so guard for that case
        # explicitly rather than requiring a write that was never attempted.
        redis_satisfied = redis_ok or not self.redis_client
        if not (redis_satisfied and yaml_ok):
            logger.error(
                f"clear_retained_reason: could not durably clear retained_reason "
                f"for {project}/{board} issue #{issue_number} in every store "
                f"(redis_ok={redis_ok}, yaml_ok={yaml_ok}, "
                f"redis_configured={bool(self.redis_client)}) — the lock may "
                f"still read as retained via get_lock()'s merge; treating this "
                f"as a failed clear"
            )
            return False

        logger.info(
            f"Pipeline lock for {project}/{board} issue #{issue_number} — "
            f"retained_reason cleared for self-heal retry (lock still held by "
            f"this issue)"
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
            # Bounded like every other acquisition of '<state>.yaml.lock' made
            # here (found in the WI-8 review round -- this one was still a
            # blocking acquire, and it runs inside the '.acquire.lock' guard via
            # _create_lock(), so it could park that guard's hold indefinitely).
            # A timeout is just a write failure, which every caller of this
            # method already models.
            with safe_yaml_write(
                state_file, timeout=STATE_LOCK_TIMEOUT_SECONDS, enforce_timeout=True
            ):
                return self._save_lock_to_yaml_unlocked(lock)
        except Exception as e:
            logger.error(f"Failed to save lock to YAML: {e}")
            return False

    def _save_lock_to_yaml_unlocked(self, lock: PipelineLock) -> bool:
        """
        _save_lock_to_yaml() without taking '<state>.yaml.lock'. MUST only be
        called with that lock already held -- see
        _read_yaml_lock_only_unlocked() for why the pair exists.
        """
        state_file = self._get_state_file(lock.project, lock.board)
        try:
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
                    self.redis_client.expire(lock_key, LOCK_TTL_SECONDS)
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

    def get_lock_holder_fail_closed(
        self, project: str, board: str
    ) -> Tuple[Optional[int], bool]:
        """
        Like get_lock_holder(), but also reports whether the read was trustworthy.

        get_lock_holder() goes through get_lock(), which discards the health flag
        both stores return -- and _read_redis_lock_only()/_read_yaml_lock_only()
        swallow their own exceptions, so a total store outage surfaces there as
        "no lock holder", indistinguishable from "board is free". A caller making
        a safety decision from that answer (work_execution_state's PROTECTION 2)
        would then proceed as if the board were idle precisely when it cannot
        tell. This exposes the same (value, reads_healthy) contract
        get_lock_fail_closed() already provides for the lock itself.

        Args:
            project: Project name
            board: Board name

        Returns:
            (issue_number holding the lock or None, reads_healthy). Treat
            reads_healthy=False as "unknown -- assume locked", never as unlocked.
        """
        lock, reads_healthy = self.get_lock_fail_closed(project, board)
        return (lock.locked_by_issue if lock else None), reads_healthy

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
_pipeline_lock_manager_init_guard = threading.Lock()


def get_pipeline_lock_manager() -> PipelineLockManager:
    """
    Get singleton instance of PipelineLockManager.

    Double-checked locking around the lazy init (#54 review): this used to be
    a bare check-then-set with no guard, which was fine while every caller
    ran on the same thread at startup. #54 added a genuinely concurrent
    caller (main.py's asyncio.to_thread(initialize_all_projects), racing the
    event-loop thread's own ProjectResourceLockManager()-default-construction
    callers), so two threads could otherwise both observe
    `_pipeline_lock_manager is None` and each construct their own
    PipelineLockManager -- each opening its own Redis connection, with the
    loser's silently orphaned and any caller holding a reference to it
    missing lock state updates made through the winning instance's
    connection.
    """
    global _pipeline_lock_manager
    if _pipeline_lock_manager is None:
        with _pipeline_lock_manager_init_guard:
            if _pipeline_lock_manager is None:
                _pipeline_lock_manager = PipelineLockManager()
    return _pipeline_lock_manager

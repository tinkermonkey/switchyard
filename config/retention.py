"""One retention window, for everything the orchestrator stores.

Before this, retention was declared in eleven places and agreed in none of
them. Elasticsearch had eight ILM policies with hand-written phase ages (7d,
14d, 30d, 180d); the filesystem had three windows picked by hand (14d, 30d,
90d) plus one count-based rule ("keep the 10 newest"), which is not aging at
all -- ten state backups is four days on a busy project and nine months on a
quiet one. Ten indices had no policy whatsoever, and nine filesystem locations
had no sweep.

The results were visible on the live deployment: 9.2GB of unrotated logs, a
metrics JSONL "backup" kept 90 days of data whose Elasticsearch original is
deleted after 7 (so for 83 of those days it backed up nothing), and 2,606
per-agent-launch temp files dating to December.

Everything now resolves its window from RETENTION_DAYS here. Changing it
changes both halves at once, and -- because ILM re-reads a policy rather than
stamping it onto an index -- changing it takes effect on data that already
exists, not just on data written afterwards.

    RETENTION_DAYS=7 docker compose up -d    # or set it in .env

WHAT THIS DOES NOT COVER, ON PURPOSE
------------------------------------
"Age everything at 30 days" is the right rule for history and artifacts. It is
the wrong rule for LIVE state, and applying it there would be destructive:
`state/pipeline_locks/`, `state/pipeline_queues/`, `state/dev_containers/` and
`state/projects/<p>/github_state.yaml` describe what is true right now, and a
lock file is no less valid for being three months old. Those are cleaned by
orphan detection -- no matching project config -- not by age. See
services/data_retention.py for which locations fall on which side of that line.
"""

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

_DEFAULT_RETENTION_DAYS = 30


def _resolve_retention_days() -> int:
    """RETENTION_DAYS from the environment, falling back on anything unusable.

    This is read during startup by modules that run before logging is fully
    configured, and a typo in an env var must not be able to stop the
    orchestrator from booting -- or, worse, resolve to 0, which ILM reads as
    "delete immediately" and a file sweep would read as "delete everything".
    """
    raw = os.environ.get('RETENTION_DAYS')
    if raw is None:
        return _DEFAULT_RETENTION_DAYS
    try:
        value = int(raw)
    except (TypeError, ValueError):
        logger.warning(
            f"Ignoring unparseable RETENTION_DAYS={raw!r}; "
            f"using {_DEFAULT_RETENTION_DAYS} days"
        )
        return _DEFAULT_RETENTION_DAYS
    if value <= 0:
        logger.warning(
            f"Ignoring non-positive RETENTION_DAYS={raw!r} -- 0 means "
            f"'delete immediately' to both ILM and the file sweep; "
            f"using {_DEFAULT_RETENTION_DAYS} days"
        )
        return _DEFAULT_RETENTION_DAYS
    return value


RETENTION_DAYS = _resolve_retention_days()
RETENTION_SECONDS = RETENTION_DAYS * 86400

# The span of data one index holds, for the families that name an index per
# calendar month (`...-%Y.%m`). Passed as build_ilm_policy(index_period_days=)
# so those indices are not deleted while still being written to -- see
# delete_phase_days(). 31 rather than 30 so the longest month still clears.
MONTHLY_INDEX_PERIOD_DAYS = 31


def warm_phase_days() -> int:
    """When an index moves to the warm phase.

    A quarter of the window, which is 7 days at the default. Derived rather
    than configured because it is a performance tier, not a retention decision
    -- and because a hard-coded value here is exactly what breaks when someone
    sets RETENTION_DAYS=3: ILM rejects a policy whose phases are not in
    strictly increasing order, so a fixed "warm at 7d" would make every policy
    put fail at once.
    """
    return max(1, RETENTION_DAYS // 4)


def delete_phase_days(index_period_days: int = 0) -> int:
    """How old an INDEX must be before ILM may delete it.

    Not the same number as RETENTION_DAYS, and the difference matters.

    Without a rollover action ILM ages an index from its creation date, not
    from the age of the documents inside it. For a daily index that is the same
    thing to within a day. For a MONTHLY index -- which is what
    `orchestrator-test-cycle-records-%Y.%m`, `agent-execution-summaries-%Y.%m`,
    `token-metrics-*-%Y.%m` and `project-metrics-%Y.%m` all are -- it is not:
    `...-2026.03` is created on 1 March and turns 30 days old on 31 March,
    taking that morning's writes with it. Effective retention would oscillate
    between roughly zero and RETENTION_DAYS instead of holding at it, and a
    "30-day window" that deletes three-day-old records is the same class of
    quiet untruth this module exists to remove.

    So a family declares the span of data ONE of its indices holds, and the
    delete age becomes that span plus the window. Documents then live at least
    RETENTION_DAYS, and at most RETENTION_DAYS + the index period. Erring long
    is deliberate: over-retention costs disk, under-retention loses data from
    inside the window we promised to keep it for.

    There is still exactly one configured number. `index_period_days` is a
    property of how a family names its indices, not a retention decision, and
    it is 0 for anything that rolls over or writes one index per day.
    """
    return RETENTION_DAYS + max(0, index_period_days)


def build_ilm_policy(
    hot_actions: Optional[Dict[str, Any]] = None,
    warm_priority: int = 50,
    hot_priority: int = 100,
    index_period_days: int = 0,
) -> Dict[str, Any]:
    """The one ILM policy body, used for every index family.

    `hot_actions` merges into the hot phase for the families that roll over on
    size/age as well as on date (the metrics indices, and the OTEL data
    streams, which cannot be aged out at all without one).

    `index_period_days` is the span of data a single index of this family
    holds -- see delete_phase_days(). 0 for daily or rolled-over indices, ~31
    for the monthly ones.

    The warm phase is omitted entirely when the window is too short for it to
    sit strictly between hot and delete. Emitting `warm.min_age == delete
    .min_age` would be rejected by Elasticsearch and take the whole policy
    down with it; dropping a performance tier is a far smaller loss than
    having no retention applied at all.
    """
    warm_days = warm_phase_days()
    delete_days = delete_phase_days(index_period_days)

    phases: Dict[str, Any] = {
        "hot": {
            "min_age": "0ms",
            "actions": {"set_priority": {"priority": hot_priority}},
        },
    }
    if hot_actions:
        phases["hot"]["actions"].update(hot_actions)

    if warm_days < delete_days:
        phases["warm"] = {
            "min_age": f"{warm_days}d",
            "actions": {
                "set_priority": {"priority": warm_priority},
                # ILM injects a `migrate` action into the warm phase unless it
                # is explicitly disabled, and migrate BLOCKS until every shard
                # copy is active. On a single-node cluster any index with
                # replicas > 0 is permanently yellow, so migrate waits forever
                # and the index never reaches the delete phase at all -- which
                # is how the two claude-otel data streams sat in
                # `warm/migrate/check-migration` for two months, unaged, while
                # their policy claimed a 14-day window. There are no data tiers
                # to migrate between here, and warm is a performance tier
                # rather than a retention decision, so switching it off costs
                # nothing and removes the only way this policy can silently
                # stall short of deleting anything.
                "migrate": {"enabled": False},
            },
        }

    phases["delete"] = {
        "min_age": f"{delete_days}d",
        "actions": {"delete": {}},
    }

    return {"policy": {"phases": phases}}


def describe() -> str:
    """One line for startup logs, so the active window is never a guess."""
    return (
        f"retention: {RETENTION_DAYS} days (RETENTION_DAYS), applied to both "
        f"Elasticsearch ILM and the filesystem sweep"
    )

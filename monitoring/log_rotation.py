"""Bounded file logging for the orchestrator.

Every file handler in this codebase was a plain `logging.FileHandler`, which
never rotates and never truncates. On the live deployment that produced:

    orchestrator_data/logs/orchestrator_all.log          8.6 GB
    orchestrator_data/logs/orchestrator_orchestrator.log 606 MB

against 31 MB of actual state and 0.7 GB of Elasticsearch -- i.e. the logs were
~97% of everything the orchestrator had written to disk, and the single oldest
line in the 8.6 GB file was older than most of the projects in it. Nothing read
them at that size either: grepping the 8.6 GB file for one pattern takes
minutes, which in practice means the log is consulted by tail and nothing else.

The numbers below are per-file caps chosen so the whole of
`orchestrator_data/logs/` stays around 1 GB while still holding roughly a
fortnight of the busiest deployment's output -- enough to investigate an
incident from the previous week, which is what these files are actually for.
Both are overridable for a deployment that wants more or less.

Semantics are otherwise exactly `logging.FileHandler`'s, including opening the
file eagerly. That is not incidental: `pipeline/repair_cycle_runner.py` wraps
its handler construction in a try/except and falls back to stdout-only logging
when the epic worktree it writes into is missing or unwritable, which only
works if the open happens here rather than at the first emit. An earlier
version of this module passed `delay=True` to avoid creating empty files, and
that quietly disarmed the guard -- caught by
tests/unit/test_repair_cycle_runner_init_guards.py, which exists because the
guard was added after a real incident.

Rotation itself is left to logging's own locking rather than reimplemented.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


def _positive_int(name: str, default: int) -> int:
    """Read an int env override, falling back on anything unusable.

    A malformed override must not stop logging from being set up at all -- this
    runs during startup, before there is anywhere useful to report the problem
    to.
    """
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


# Bytes per file before rotating. 256 MB is a few hours of the busiest observed
# traffic, and it is small enough to grep in seconds.
LOG_MAX_BYTES = _positive_int('LOG_MAX_BYTES', 256 * 1024 * 1024)

# Rotated files kept alongside the live one, so the ceiling per logical log is
# (backups + 1) * LOG_MAX_BYTES -- 1 GB at the defaults.
LOG_BACKUP_COUNT = _positive_int('LOG_BACKUP_COUNT', 3)


def rotating_file_handler(
    path: Path,
    level: Optional[int] = None,
    formatter: Optional[logging.Formatter] = None,
) -> RotatingFileHandler:
    """A size-capped file handler for `path`, with the caps above applied.

    Does NOT create the parent directory. Both orchestrator callers already
    mkdir their log directory, and the repair-cycle runner deliberately relies
    on the open FAILING when its directory is absent -- that is how it detects
    an epic worktree that was never mounted. Creating the tree here would turn
    that detection into a stray directory inside a project checkout.
    """
    handler = RotatingFileHandler(
        path,
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )
    if level is not None:
        handler.setLevel(level)
    if formatter is not None:
        handler.setFormatter(formatter)
    return handler

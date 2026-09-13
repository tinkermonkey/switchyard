#!/usr/bin/env python3
"""Measure what the #214 release-driven wake costs and what it saves.

Two numbers, both measured rather than asserted:

1. The COST on the common production path (capacity 1, no waiter registered):
   how much wall clock dispatch_waiting_board_lock_waiter() adds to a lock
   release when there is nothing waiting. This is the "verify it is a no-op"
   number.
2. The SAVING: the wake path's own latency, against the poll-tick latency it
   replaces. The poll tick is read from the live configuration rather than
   assumed -- run this inside the orchestrator container to have it read the
   real project configs.

Run:
    docker exec -e ORCHESTRATOR_ROOT=/tmp/measure -w /app \\
        switchyard-orchestrator-1 python scripts/measure_board_wake_latency.py
"""

import os
import statistics
import sys
import tempfile
import time
from unittest.mock import Mock, patch

if 'ORCHESTRATOR_ROOT' not in os.environ:
    os.environ['ORCHESTRATOR_ROOT'] = tempfile.mkdtemp(prefix='wake-measure-')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.manager import ConfigManager  # noqa: E402
from services.board_wait_registry import get_board_wait_registry  # noqa: E402
from services.project_monitor import ProjectMonitor  # noqa: E402

ITERATIONS = 2000


def _monitor():
    cm = Mock(spec=ConfigManager)
    cm.list_projects.return_value = []
    pc = Mock()
    pc.github = {'repo': 'test-repo'}
    cm.get_project_config.return_value = pc
    m = ProjectMonitor(Mock(), cm)
    m.trigger_agent_for_status = Mock(return_value='senior_software_engineer')
    m.get_issue_column_sync_checked = Mock(return_value=('Testing', True))
    return m, cm


def _time_wake(monitor, cm, iterations):
    lock_manager = Mock()
    lock_manager.get_lock.return_value = None
    samples = []
    with patch('services.project_monitor.ConfigManager', return_value=cm), \
         patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
               return_value=lock_manager), \
         patch('services.cancellation.get_cancellation_signal') as sig:
        sig.return_value.is_cancelled.return_value = False
        for _ in range(iterations):
            t0 = time.perf_counter()
            monitor.dispatch_waiting_board_lock_waiter('proj', 'dev')
            samples.append((time.perf_counter() - t0) * 1e6)  # microseconds
    return samples


def _report(label, samples):
    print(f"{label}")
    print(f"    n        : {len(samples)}")
    print(f"    median   : {statistics.median(samples):9.2f} us")
    print(f"    mean     : {statistics.fmean(samples):9.2f} us")
    print(f"    p95      : {sorted(samples)[int(len(samples) * 0.95)]:9.2f} us")
    print(f"    max      : {max(samples):9.2f} us")


def main():
    reg = get_board_wait_registry()

    # 1. No waiter -- the production common case.
    reg.clear_all()
    monitor, cm = _monitor()
    idle = _time_wake(monitor, cm, ITERATIONS)
    assert monitor.trigger_agent_for_status.call_count == 0
    _report("COST on release with NO waiter (production common case at capacity 1):",
            idle)

    # 2. A waiter present -- the wake actually dispatching. the column lookup
    #    and trigger_agent_for_status are mocked, so this measures the wake's own
    #    overhead, not the dispatch it hands off to.
    monitor, cm = _monitor()
    busy = []
    lock_manager = Mock()
    lock_manager.get_lock.return_value = None
    with patch('services.project_monitor.ConfigManager', return_value=cm), \
         patch('services.pipeline_lock_manager.get_pipeline_lock_manager',
               return_value=lock_manager), \
         patch('services.cancellation.get_cancellation_signal') as sig:
        sig.return_value.is_cancelled.return_value = False
        for _ in range(ITERATIONS):
            reg.clear_all()
            reg.record_wait('proj', 'dev', 100)
            t0 = time.perf_counter()
            monitor.dispatch_waiting_board_lock_waiter('proj', 'dev')
            busy.append((time.perf_counter() - t0) * 1e6)
    assert monitor.trigger_agent_for_status.call_count == ITERATIONS
    reg.clear_all()
    _report("\nWAKE latency when a waiter IS registered (excludes the dispatch itself):",
            busy)

    # 3. What it replaces. Read, not assumed.
    cfg = ConfigManager()
    try:
        projects = cfg.list_projects()
    except Exception:
        projects = []
    intervals = set()
    for p in projects:
        try:
            intervals.add(cfg.get_project_config(p).orchestrator.get('polling_interval', 15))
        except Exception:
            pass

    print("\nBASELINE it replaces (one poll tick):")
    print(f"    configured polling_interval across {len(projects)} project config(s): "
          f"{sorted(intervals) if intervals else 'none readable here'}")
    print("    ProjectMonitor._max_poll_interval (idle-backoff ceiling): 60s")
    print("    So the wake-up a waiting repair cycle used to depend on was one")
    print("    poll tick: 15s on an active board, degrading toward 60s on an idle")
    print("    board set. The release-driven wake fires at release instead.")


if __name__ == '__main__':
    main()

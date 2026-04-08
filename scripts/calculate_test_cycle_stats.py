#!/usr/bin/env python3
"""
calculate_test_cycle_stats.py — Weekly test-cycle duration rollup

Reads per-iteration records from orchestrator-test-cycle-records-* (written by
TestCycleRecorder after each repair cycle) and computes min/max/avg/median/p90
duration statistics per project + test_type.

Results are upserted into orchestrator-test-cycle-stats with doc_id
{project}_{test_type}, so re-running is always safe (idempotent).

Usage:
    # Dry-run — prints stats, writes nothing
    python scripts/calculate_test_cycle_stats.py --dry-run

    # Single project
    python scripts/calculate_test_cycle_stats.py --project context-library

    # Custom lookback window (default 180 days)
    python scripts/calculate_test_cycle_stats.py --lookback-days 90

    # Full run (all projects, all test types found in records)
    python scripts/calculate_test_cycle_stats.py
"""

import argparse
import logging
import os
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from elasticsearch import Elasticsearch

# Ensure the project root is on the path when run directly
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger(__name__)

RECORDS_INDEX = "orchestrator-test-cycle-records-*"
STATS_INDEX = "orchestrator-test-cycle-stats"
DEFAULT_LOOKBACK_DAYS = 180
# ─── Statistics helpers ───────────────────────────────────────────────────────

def _percentile(sorted_values: list[float], pct: float) -> float:
    """Linear interpolation percentile on a pre-sorted list."""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    idx = (pct / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    if hi >= n:
        return sorted_values[-1]
    frac = idx - lo
    return sorted_values[lo] + frac * (sorted_values[hi] - sorted_values[lo])


def _duration_stats(values: list[float]) -> dict[str, Any]:
    """Compute min/max/avg/median/p90 on a list of floats."""
    sv = sorted(values)
    return {
        "sample_count": len(sv),
        "min_s":    sv[0],
        "max_s":    sv[-1],
        "avg_s":    statistics.mean(sv),
        "median_s": statistics.median(sv),
        "p90_s":    _percentile(sv, 90),
    }


# ─── Elasticsearch helpers ────────────────────────────────────────────────────

def _fetch_records(
    es: Elasticsearch,
    lookback_days: int,
    project_filter: str | None,
) -> list[dict[str, Any]]:
    """
    Fetch all per-iteration records from the last `lookback_days` days.
    Optionally filter to a single project.
    """
    since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

    must_filters: list[dict] = [{"range": {"@timestamp": {"gte": since}}}]
    if project_filter:
        must_filters.append({"term": {"project": project_filter}})

    try:
        resp = es.search(
            index=RECORDS_INDEX,
            body={
                "size": 10000,
                "query": {"bool": {"filter": must_filters}},
                "_source": True,
            },
        )
        hits = resp.get("hits", {}).get("hits", [])
        return [h["_source"] for h in hits]
    except Exception as exc:
        logger.error(f"Failed to fetch records from {RECORDS_INDEX}: {exc}")
        return []


def _upsert_stats(es: Elasticsearch, doc_id: str, document: dict[str, Any]) -> None:
    es.index(index=STATS_INDEX, id=doc_id, document=document)


# ─── Core computation ─────────────────────────────────────────────────────────

def _compute_stats_for_group(
    records: list[dict[str, Any]],
    project: str,
    test_type: str,
) -> dict[str, Any] | None:
    """
    Compute rollup stats for a single (project, test_type) group.

    Returns None if there are no records at all.
    """
    if not records:
        return None

    timestamps = []
    for r in records:
        ts = r.get("@timestamp")
        if ts:
            timestamps.append(ts)

    data_from = min(timestamps) if timestamps else None
    data_to = max(timestamps) if timestamps else None

    pipeline_run_ids = {r["pipeline_run_id"] for r in records if r.get("pipeline_run_id")}

    # --- All executions ---
    all_durations = [
        r["test_execution_duration_s"]
        for r in records
        if r.get("test_execution_duration_s") is not None
    ]
    passed_count = sum(1 for r in records if r.get("test_execution_passed") is True)
    failed_count = len(records) - passed_count

    all_stats: dict[str, Any] = {}
    if all_durations:
        s = _duration_stats(all_durations)
        all_stats = {
            "all_exec_sample_count":      s["sample_count"],
            "all_exec_duration_min_s":    s["min_s"],
            "all_exec_duration_max_s":    s["max_s"],
            "all_exec_duration_avg_s":    s["avg_s"],
            "all_exec_duration_median_s": s["median_s"],
            "all_exec_duration_p90_s":    s["p90_s"],
            "all_exec_pass_rate":         passed_count / len(records) if records else 0.0,
        }

    # --- Clean baseline: first sub-run of first iteration that passed immediately ---
    clean_records = [
        r for r in records
        if r.get("iteration_number") == 1
        and r.get("sub_run_index") == 1
        and r.get("test_execution_passed") is True
    ]
    clean_durations = [
        r["test_execution_duration_s"]
        for r in clean_records
        if r.get("test_execution_duration_s") is not None
    ]

    clean_stats: dict[str, Any] = {}
    if clean_durations:
        s = _duration_stats(clean_durations)
        clean_stats = {
            "clean_pass_sample_count":      s["sample_count"],
            "clean_pass_duration_min_s":    s["min_s"],
            "clean_pass_duration_max_s":    s["max_s"],
            "clean_pass_duration_avg_s":    s["avg_s"],
            "clean_pass_duration_median_s": s["median_s"],
            "clean_pass_duration_p90_s":    s["p90_s"],
        }

    # --- Failure / fix profile ---
    failing_records = [r for r in records if r.get("test_failure_count", 0) > 0]
    avg_failure_count = (
        statistics.mean(r["test_failure_count"] for r in failing_records)
        if failing_records else None
    )

    fix_records = [
        r for r in records
        if r.get("had_fix_cycle") is True and r.get("fix_cycle_duration_s") is not None
    ]
    avg_fix_duration = (
        statistics.mean(r["fix_cycle_duration_s"] for r in fix_records)
        if fix_records else None
    )

    # Average iterations per distinct pipeline run
    runs_iterations: dict[str, list[int]] = {}
    for r in records:
        run_id = r.get("pipeline_run_id")
        it = r.get("iteration_number")
        if run_id and it is not None:
            runs_iterations.setdefault(run_id, []).append(it)
    avg_iterations = (
        statistics.mean(max(v) for v in runs_iterations.values())
        if runs_iterations else None
    )

    doc: dict[str, Any] = {
        "project":             project,
        "test_type":           test_type,
        "calculated_at":       datetime.now(timezone.utc).isoformat(),
        "total_iterations":    len(records),
        "total_pipeline_runs": len(pipeline_run_ids),
        "data_from":           data_from,
        "data_to":             data_to,
        **all_stats,
        **clean_stats,
    }
    if avg_failure_count is not None:
        doc["avg_failure_count_when_failing"] = avg_failure_count
    if avg_fix_duration is not None:
        doc["avg_fix_cycle_duration_s"] = avg_fix_duration
    if avg_iterations is not None:
        doc["avg_iterations_per_repair_cycle"] = avg_iterations

    return doc


def calculate_stats(
    es: Elasticsearch,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    project_filter: str | None = None,
    dry_run: bool = False,
) -> int:
    """
    Main entry point.  Fetches records, groups by (project, test_type),
    computes stats, and upserts into orchestrator-test-cycle-stats.

    Returns the number of stats documents written (or that would be written in
    dry-run mode).
    """
    logger.info(
        f"Starting test-cycle stats calculation "
        f"(lookback={lookback_days}d, project={project_filter or 'all'}, dry_run={dry_run})"
    )

    records = _fetch_records(es, lookback_days, project_filter)
    if not records:
        logger.info("No records found — nothing to compute")
        return 0

    # Group by (project, test_type)
    groups: dict[tuple[str, str], list[dict]] = {}
    for r in records:
        project = r.get("project")
        test_type = r.get("test_type")
        if not project or not test_type:
            continue
        groups.setdefault((project, test_type), []).append(r)

    written = 0
    for (project, test_type), grp_records in sorted(groups.items()):
        doc = _compute_stats_for_group(grp_records, project, test_type)
        if doc is None:
            continue

        doc_id = f"{project}_{test_type}"

        if dry_run:
            _print_dry_run(doc, doc_id)
        else:
            try:
                _upsert_stats(es, doc_id, doc)
                clean_avg = doc.get("clean_pass_duration_avg_s")
                if clean_avg is not None:
                    logger.info(
                        f"Upserted stats for {project}/{test_type}: "
                        f"{len(grp_records)} iterations, "
                        f"clean_pass_avg={clean_avg:.1f}s"
                    )
                else:
                    logger.info(
                        f"Upserted stats for {project}/{test_type}: "
                        f"{len(grp_records)} iterations"
                    )
            except Exception as exc:
                logger.error(f"Failed to upsert stats for {project}/{test_type}: {exc}")
                continue

        written += 1

    logger.info(f"Test-cycle stats calculation complete: {written} groups processed")
    return written


def _print_dry_run(doc: dict[str, Any], doc_id: str) -> None:
    clean_avg = doc.get("clean_pass_duration_avg_s")
    all_avg = doc.get("all_exec_duration_avg_s")
    lines = (
        f"\n[DRY RUN] {doc_id}\n"
        f"  Iterations:    {doc['total_iterations']} across {doc['total_pipeline_runs']} runs\n"
        f"  Window:        {doc.get('data_from', '?')} -> {doc.get('data_to', '?')}\n"
        f"  Pass rate:     {doc.get('all_exec_pass_rate', 0)*100:.1f}%"
    )
    if all_avg is not None:
        lines += f"\n  All exec avg:  {all_avg:.1f}s"
    print(lines)
    if clean_avg is not None:
        print(
            f"  Clean avg:     {clean_avg:.1f}s  "
            f"(min={doc.get('clean_pass_duration_min_s', 0):.1f}s  "
            f"max={doc.get('clean_pass_duration_max_s', 0):.1f}s  "
            f"p90={doc.get('clean_pass_duration_p90_s', 0):.1f}s)"
        )
    if doc.get("avg_fix_cycle_duration_s") is not None:
        print(f"  Fix cycle avg: {doc['avg_fix_cycle_duration_s']:.1f}s")


# ─── CLI entry point ──────────────────────────────────────────────────────────

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Compute weekly test-cycle duration stats from orchestrator records"
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print stats without writing to Elasticsearch"
    )
    parser.add_argument(
        "--project", default=None,
        help="Restrict computation to a single project name"
    )
    parser.add_argument(
        "--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS,
        help=f"How many days of records to include (default: {DEFAULT_LOOKBACK_DAYS})"
    )
    parser.add_argument(
        "--es-url", default=None,
        help="Elasticsearch URL (default: $ELASTICSEARCH_URL or http://elasticsearch:9200)"
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable debug logging"
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = _parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    es_url = args.es_url or os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")
    es = Elasticsearch([es_url])

    count = calculate_stats(
        es=es,
        lookback_days=args.lookback_days,
        project_filter=args.project,
        dry_run=args.dry_run,
    )

    sys.exit(0 if count >= 0 else 1)

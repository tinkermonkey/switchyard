"""
Test Cycle Recorder

Populates `orchestrator-test-cycle-records-YYYY.MM` with one document per
test-cycle iteration immediately after a repair cycle completes.

Each record captures a single test-execution iteration:
  - How long the test container ran
  - Whether tests passed, and how many failures / warnings
  - Whether a fix cycle followed, and how long it took

This granular approach lets the weekly rollup distinguish the pure cost of
running tests (iteration=1, passed) from the overhead of iterative fixing.

Usage:
    recorder = TestCycleRecorder(es_hosts=["http://elasticsearch:9200"])
    await recorder.record_repair_cycle(pipeline_run_id, project)
"""

import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from elasticsearch import Elasticsearch, NotFoundError

logger = logging.getLogger(__name__)

INDEX_PREFIX = "orchestrator-test-cycle-records"
ILM_POLICY_NAME = "test-cycle-records-ilm-policy"
INDEX_TEMPLATE_NAME = "test-cycle-records-template"
STATS_INDEX = "orchestrator-test-cycle-stats"

# Event types we query from decision-events
_TEST_EXEC_STARTED = "repair_cycle_test_execution_started"
_TEST_EXEC_COMPLETED = "repair_cycle_test_execution_completed"
_FIX_CYCLE_STARTED = "repair_cycle_fix_cycle_started"
_FIX_CYCLE_COMPLETED = "repair_cycle_fix_cycle_completed"

_QUERY_EVENT_TYPES = [
    _TEST_EXEC_STARTED,
    _TEST_EXEC_COMPLETED,
    _FIX_CYCLE_STARTED,
    _FIX_CYCLE_COMPLETED,
]


def _index_name(dt: datetime) -> str:
    return f"{INDEX_PREFIX}-{dt.strftime('%Y.%m')}"


def _parse_ts(ts: str) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp string to UTC datetime."""
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        return None


class TestCycleRecorder:
    """
    Queries decision-events for a completed repair cycle and writes one
    iteration record per test-execution into orchestrator-test-cycle-records.

    Designed to be called synchronously from ScheduledTasksService or
    directly after repair cycle completion (async-safe via asyncio executor).

    All writes are idempotent: doc_id = {pipeline_run_id}_{test_type}_{iteration}.
    """

    def __init__(self, es_hosts: list[str] | None = None):
        if es_hosts is None:
            es_hosts = [os.environ.get("ELASTICSEARCH_URL", "http://elasticsearch:9200")]
        self.es = Elasticsearch(es_hosts)
        self._ensure_indices()

    def _ensure_indices(self) -> None:
        """Create ILM policy and index template if they don't exist yet."""
        from services.pattern_detection_schema import (
            TEST_CYCLE_RECORDS_ILM_POLICY,
            TEST_CYCLE_RECORDS_MAPPING,
            TEST_CYCLE_STATS_MAPPING,
        )

        # ILM policy
        try:
            self.es.ilm.get_lifecycle(name=ILM_POLICY_NAME)
        except NotFoundError:
            try:
                self.es.ilm.put_lifecycle(name=ILM_POLICY_NAME, body=TEST_CYCLE_RECORDS_ILM_POLICY)
                logger.info(f"Created ILM policy: {ILM_POLICY_NAME}")
            except Exception as exc:
                logger.warning(f"Could not create ILM policy {ILM_POLICY_NAME}: {exc}")
        except Exception as exc:
            logger.warning(f"Could not check ILM policy {ILM_POLICY_NAME}: {exc}")

        # Index template for records
        try:
            self.es.indices.get_index_template(name=INDEX_TEMPLATE_NAME)
        except NotFoundError:
            try:
                self.es.indices.put_index_template(
                    name=INDEX_TEMPLATE_NAME,
                    body={
                        "index_patterns": [f"{INDEX_PREFIX}-*"],
                        "priority": 100,
                        "template": TEST_CYCLE_RECORDS_MAPPING,
                    },
                )
                logger.info(f"Created index template: {INDEX_TEMPLATE_NAME}")
            except Exception as exc:
                logger.warning(f"Could not create index template {INDEX_TEMPLATE_NAME}: {exc}")
        except Exception as exc:
            logger.warning(f"Could not check index template {INDEX_TEMPLATE_NAME}: {exc}")

        # Stats index (no template needed — single index, fixed name)
        try:
            if not self.es.indices.exists(index=STATS_INDEX):
                self.es.indices.create(index=STATS_INDEX, body=TEST_CYCLE_STATS_MAPPING)
                logger.info(f"Created index: {STATS_INDEX}")
        except Exception as exc:
            logger.warning(f"Could not ensure stats index {STATS_INDEX}: {exc}")

    # -------------------------------------------------------------------------
    # Public API
    # -------------------------------------------------------------------------

    def record_repair_cycle(self, pipeline_run_id: str, project: str) -> int:
        """
        Query decision-events for `pipeline_run_id` and write one record per
        test-execution iteration into orchestrator-test-cycle-records-YYYY.MM.

        Returns the number of records written (0 if no data found).
        Idempotent: re-running for the same pipeline_run_id is safe.
        """
        if not pipeline_run_id:
            logger.warning("record_repair_cycle called with empty pipeline_run_id, skipping")
            return 0

        events = self._fetch_iteration_events(pipeline_run_id)
        if not events:
            logger.info(f"No test-cycle events found for pipeline_run_id={pipeline_run_id}")
            return 0

        iterations = self._build_iteration_records(events, pipeline_run_id, project)
        written = 0
        for rec in iterations:
            try:
                doc_id = f"{pipeline_run_id}_{rec['test_type']}_{rec['iteration_number']}_{rec['sub_run_index']}"
                index = _index_name(datetime.now(timezone.utc))
                self.es.index(index=index, id=doc_id, document=rec)
                written += 1
            except Exception as exc:
                logger.error(
                    f"Failed to write test-cycle record "
                    f"(pipeline_run_id={pipeline_run_id}, "
                    f"test_type={rec.get('test_type')}, "
                    f"iteration={rec.get('iteration_number')}): {exc}"
                )

        logger.info(
            f"Recorded {written} test-cycle iteration records for "
            f"pipeline_run_id={pipeline_run_id} project={project}"
        )
        return written

    def backfill_from_decision_events(
        self,
        project_filter: str | None = None,
        run_stats_after: bool = True,
        lookback_days: int = 7,
    ) -> dict[str, Any]:
        """
        Discover all completed repair cycles in decision-events-* and write
        iteration records for any that are not yet in orchestrator-test-cycle-records-*.

        Because decision-events-* has 7-day ILM retention, `lookback_days` is
        capped at 7.  The recorder is idempotent, so re-running for already-recorded
        pipeline runs is safe (it just overwrites with identical data).

        Args:
            project_filter: Restrict backfill to a single project name.
            run_stats_after: Run the weekly stats rollup after backfilling (default True).
            lookback_days: How far back to search (max 7, default 7).

        Returns:
            dict with keys: discovered, already_recorded, newly_recorded, failed, errors
        """
        lookback_days = min(lookback_days, 7)
        since = (datetime.now(timezone.utc) - timedelta(days=lookback_days)).isoformat()

        logger.info(
            f"Starting test-cycle backfill "
            f"(lookback={lookback_days}d, project={project_filter or 'all'})"
        )

        # ── Step 1: Find completed repair cycles in decision-events-* ────────
        completed_runs = self._fetch_completed_repair_cycles(since, project_filter)
        logger.info(f"Backfill: found {len(completed_runs)} completed repair cycle run(s)")

        # ── Step 2: Check which pipeline_run_ids already have records ────────
        existing_ids = self._fetch_already_recorded_run_ids(
            {r["pipeline_run_id"] for r in completed_runs}
        )
        logger.info(f"Backfill: {len(existing_ids)} run(s) already have iteration records")

        # ── Step 3: Process the missing ones ─────────────────────────────────
        newly_recorded = 0
        failed = 0
        errors: list[str] = []

        for run in completed_runs:
            run_id  = run["pipeline_run_id"]
            project = run["project"]
            already = run_id in existing_ids

            if already:
                logger.debug(f"Backfill: skipping already-recorded run {run_id}")
                continue

            try:
                count = self.record_repair_cycle(run_id, project)
                newly_recorded += count
                logger.info(f"Backfill: recorded {count} iteration(s) for run {run_id} ({project})")
            except Exception as exc:
                failed += 1
                msg = f"run_id={run_id} project={project}: {exc}"
                errors.append(msg)
                logger.error(f"Backfill failed for {msg}")

        result = {
            "discovered":       len(completed_runs),
            "already_recorded": len(existing_ids),
            "newly_recorded":   newly_recorded,
            "failed":           failed,
            "errors":           errors,
        }

        # ── Step 4: Optional stats rollup ─────────────────────────────────────
        if run_stats_after and (newly_recorded > 0 or len(completed_runs) > 0):
            try:
                from scripts.calculate_test_cycle_stats import calculate_stats
                stats_count = calculate_stats(
                    es=self.es,
                    project_filter=project_filter,
                    lookback_days=lookback_days,
                )
                result["stats_groups_updated"] = stats_count
                logger.info(f"Backfill: stats rollup updated {stats_count} group(s)")
            except Exception as exc:
                result["stats_error"] = str(exc)
                logger.error(f"Backfill: stats rollup failed (non-fatal): {exc}")

        logger.info(
            f"Backfill complete: discovered={result['discovered']} "
            f"already_recorded={result['already_recorded']} "
            f"newly_recorded={result['newly_recorded']} "
            f"failed={result['failed']}"
        )
        return result

    # -------------------------------------------------------------------------
    # Internal helpers
    # -------------------------------------------------------------------------

    def _fetch_completed_repair_cycles(
        self, since: str, project_filter: str | None
    ) -> list[dict[str, str]]:
        """
        Return a list of {pipeline_run_id, project} dicts for all
        REPAIR_CYCLE_COMPLETED events in the lookback window.

        Uses a composite aggregation to page through all distinct pipeline_run_ids
        so we never miss runs even with high cardinality.
        """
        must_filters: list[dict] = [
            {"term": {"event_type": "repair_cycle_completed"}},
            {"range": {"timestamp": {"gte": since}}},
            {"exists": {"field": "pipeline_run_id"}},
        ]
        if project_filter:
            must_filters.append({"term": {"project": project_filter}})

        results: list[dict[str, str]] = []
        after_key: dict | None = None

        while True:
            agg_body: dict[str, Any] = {
                "sources": [
                    {"pipeline_run_id": {"terms": {"field": "pipeline_run_id"}}},
                    {"project":         {"terms": {"field": "project"}}},
                ],
                "size": 1000,
            }
            if after_key:
                agg_body["after"] = after_key

            try:
                resp = self.es.search(
                    index="decision-events-*",
                    body={
                        "size": 0,
                        "query": {"bool": {"filter": must_filters}},
                        "aggs": {"runs": {"composite": agg_body}},
                    },
                )
            except Exception as exc:
                logger.error(f"Failed to query completed repair cycles: {exc}")
                break

            buckets = resp.get("aggregations", {}).get("runs", {}).get("buckets", [])
            for bucket in buckets:
                key = bucket.get("key", {})
                run_id  = key.get("pipeline_run_id")
                project = key.get("project")
                if run_id and project:
                    results.append({"pipeline_run_id": run_id, "project": project})

            after_key = resp.get("aggregations", {}).get("runs", {}).get("after_key")
            if not after_key or len(buckets) < 1000:
                break  # No more pages

        return results

    def _fetch_already_recorded_run_ids(self, candidate_ids: set[str]) -> set[str]:
        """
        Return the subset of `candidate_ids` that already have at least one
        iteration record in orchestrator-test-cycle-records-*.
        """
        if not candidate_ids:
            return set()

        try:
            resp = self.es.search(
                index=f"{INDEX_PREFIX}-*",
                body={
                    "size": 0,
                    "query": {
                        "terms": {"pipeline_run_id": list(candidate_ids)}
                    },
                    "aggs": {
                        "existing_runs": {
                            "terms": {
                                "field": "pipeline_run_id",
                                "size": len(candidate_ids) + 1,
                            }
                        }
                    },
                },
            )
            buckets = (
                resp.get("aggregations", {})
                    .get("existing_runs", {})
                    .get("buckets", [])
            )
            return {b["key"] for b in buckets}
        except Exception as exc:
            logger.warning(
                f"Could not check existing records index (will process all runs): {exc}"
            )
            return set()

    def _fetch_iteration_events(self, pipeline_run_id: str) -> list[dict[str, Any]]:
        """
        Fetch all test-execution and fix-cycle events for a pipeline run from
        decision-events-*, sorted by timestamp ascending.
        """
        try:
            result = self.es.search(
                index="decision-events-*",
                body={
                    "size": 10000,
                    "query": {
                        "bool": {
                            "filter": [
                                {"term": {"pipeline_run_id": pipeline_run_id}},
                                {"terms": {"event_type": _QUERY_EVENT_TYPES}},
                            ]
                        }
                    },
                    "sort": [{"timestamp": {"order": "asc"}}],
                    "_source": True,
                },
            )
            hits = result.get("hits", {}).get("hits", [])
            return [h["_source"] for h in hits]
        except Exception as exc:
            logger.error(f"Failed to fetch iteration events for {pipeline_run_id}: {exc}")
            return []

    def _build_iteration_records(
        self,
        events: list[dict[str, Any]],
        pipeline_run_id: str,
        project: str,
    ) -> list[dict[str, Any]]:
        """
        Group events by (test_type, test_cycle_iteration) and emit one record
        per sub-run (each matched STARTED+COMPLETED pair).

        Within a single iteration, `_run_tests()` can be called more than once
        with the same iteration number — for example, when re-running after
        warning fixes or after a systemic fix sub-cycle.  Each call emits its
        own STARTED/COMPLETED pair.

        Each sub-run becomes its own record with its own duration and pass/fail
        state, identified by `sub_run_index` (1-based within the iteration).
        This preserves individual test-execution times rather than summing them,
        which is what the stats rollup needs.

        Fix cycle association: when a fix cycle is recorded for an iteration it
        follows the last failed sub-run.  That sub-run's record gets
        `had_fix_cycle=True` and `fix_cycle_duration_s`.

        A record is only emitted when at least one matched STARTED+COMPLETED
        pair exists (skips partial data from interrupted cycles).
        """
        # Collect ALL events per key in timestamp-sorted order.
        # events is already sorted ascending by timestamp from the ES query.
        test_exec_started_list:   dict[tuple, list[dict]] = {}
        test_exec_completed_list: dict[tuple, list[dict]] = {}
        fix_cycle_started:        dict[tuple, dict] = {}   # at most one per iteration
        fix_cycle_completed:      dict[tuple, dict] = {}

        for ev in events:
            et = ev.get("event_type", "")
            test_type = ev.get("test_type")
            iteration = ev.get("test_cycle_iteration")
            if test_type is None or iteration is None:
                continue
            key = (test_type, iteration)
            if et == _TEST_EXEC_STARTED:
                test_exec_started_list.setdefault(key, []).append(ev)
            elif et == _TEST_EXEC_COMPLETED:
                test_exec_completed_list.setdefault(key, []).append(ev)
            elif et == _FIX_CYCLE_STARTED:
                fix_cycle_started[key] = ev
            elif et == _FIX_CYCLE_COMPLETED:
                fix_cycle_completed[key] = ev

        records = []
        now_iso = datetime.now(timezone.utc).isoformat()

        all_keys = set(test_exec_started_list.keys()) | set(test_exec_completed_list.keys())
        for key in sorted(all_keys):
            test_type, iteration_number = key
            started_list   = test_exec_started_list.get(key, [])
            completed_list = test_exec_completed_list.get(key, [])

            sub_run_count = min(len(started_list), len(completed_list))
            if sub_run_count == 0:
                logger.debug(
                    f"Skipping incomplete iteration "
                    f"test_type={test_type} iteration={iteration_number} "
                    f"(unmatched STARTED/COMPLETED events)"
                )
                continue

            # Fix cycle timing (at most one per iteration).
            # The fix cycle follows the last failed sub-run, so we attach it to
            # sub-run (sub_run_count - 1) when there are multiple sub-runs, or
            # to sub-run 1 when there is only one.
            fix_started_ev   = fix_cycle_started.get(key)
            fix_completed_ev = fix_cycle_completed.get(key)
            has_fix_cycle = fix_started_ev is not None and fix_completed_ev is not None
            fix_duration_s  = None
            fix_files_fixed = None
            if has_fix_cycle:
                fx_start = _parse_ts(fix_started_ev.get("timestamp"))
                fx_end   = _parse_ts(fix_completed_ev.get("timestamp"))
                if fx_start and fx_end:
                    fix_duration_s = max(0.0, (fx_end - fx_start).total_seconds())
                fix_files_fixed = fix_completed_ev.get("files_fixed")

            # Index of the sub-run that triggered the fix cycle (0-based).
            # When sub_run_count > 1 the fix falls between the penultimate and
            # last sub-runs; when there is only one sub-run it falls on that one.
            fix_sub_run_idx = sub_run_count - 2 if sub_run_count > 1 else 0

            for i in range(sub_run_count):
                start_ts = _parse_ts(started_list[i].get("timestamp"))
                end_ts   = _parse_ts(completed_list[i].get("timestamp"))

                if start_ts is None or end_ts is None:
                    logger.warning(
                        f"Missing timestamp on sub-run {i+1} for "
                        f"test_type={test_type} iteration={iteration_number}, skipping"
                    )
                    continue

                completed_ev   = completed_list[i]
                sub_run_number = i + 1  # 1-based
                this_sub_has_fix = has_fix_cycle and i == fix_sub_run_idx

                rec: dict[str, Any] = {
                    "pipeline_run_id":           pipeline_run_id,
                    "project":                   project,
                    "test_type":                 test_type,
                    "test_type_index":           started_list[i].get("test_type_index"),
                    "iteration_number":          iteration_number,
                    "sub_run_index":             sub_run_number,
                    # Test result for this specific sub-run
                    "test_execution_passed":     not completed_ev.get("has_failures", True),
                    "test_failure_count":        completed_ev.get("failed", 0),
                    "test_warning_count":        completed_ev.get("warnings", 0),
                    # Duration of this sub-run only (not summed)
                    "test_execution_duration_s": max(0.0, (end_ts - start_ts).total_seconds()),
                    # Fix cycle attached to the sub-run that triggered it
                    "had_fix_cycle":             this_sub_has_fix,
                    "fix_cycle_files_fixed":     fix_files_fixed if this_sub_has_fix else None,
                    "fix_cycle_duration_s":      fix_duration_s if this_sub_has_fix else None,
                    # Metadata
                    "@timestamp":                end_ts.isoformat(),
                    "recorded_at":               now_iso,
                }
                records.append(rec)

        return records


# ─── Module-level singleton ───────────────────────────────────────────────────

_recorder: TestCycleRecorder | None = None


def get_test_cycle_recorder() -> TestCycleRecorder:
    global _recorder
    if _recorder is None:
        _recorder = TestCycleRecorder()
    return _recorder

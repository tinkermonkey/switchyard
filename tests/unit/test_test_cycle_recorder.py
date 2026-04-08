"""
Unit tests for TestCycleRecorder

Tests the core logic that groups decision-events into per-iteration records and
writes them to orchestrator-test-cycle-records.  All Elasticsearch I/O is mocked.
"""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call
from elasticsearch import NotFoundError


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_ts(offset_seconds: float = 0.0) -> str:
    """Return an ISO-8601 UTC timestamp offset from a fixed base."""
    base = datetime(2025, 10, 17, 12, 0, 0, tzinfo=timezone.utc)
    from datetime import timedelta
    dt = base + timedelta(seconds=offset_seconds)
    return dt.isoformat()


def _ev(event_type: str, test_type: str, iteration: int, extra: dict = None, ts_offset: float = 0) -> dict:
    """Build a minimal decision-event dict."""
    d = {
        "event_type": event_type,
        "test_type": test_type,
        "test_cycle_iteration": iteration,
        "test_type_index": 1,
        "timestamp": _make_ts(ts_offset),
    }
    if extra:
        d.update(extra)
    return d


# ─── Fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_es():
    """A MagicMock that mimics the Elasticsearch client."""
    es = MagicMock()
    # ILM policy exists → no creation needed
    es.ilm.get_lifecycle.return_value = {}
    # Index template exists
    es.indices.get_index_template.return_value = {}
    # Stats index exists
    es.indices.exists.return_value = True
    return es


@pytest.fixture
def recorder(mock_es):
    """TestCycleRecorder with a mocked ES client."""
    with patch("monitoring.test_cycle_recorder.Elasticsearch", return_value=mock_es):
        from monitoring.test_cycle_recorder import TestCycleRecorder
        rec = TestCycleRecorder(es_hosts=["http://localhost:9200"])
        rec.es = mock_es  # re-inject in case constructor replaced it
        return rec


# ─── _build_iteration_records tests ──────────────────────────────────────────

class TestBuildIterationRecords:

    def test_single_passing_iteration_no_fix_cycle(self, recorder):
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {
                "has_failures": False,
                "failed": 0,
                "warnings": 2,
                "passed": 5,
            }, ts_offset=45),
        ]
        records = recorder._build_iteration_records(events, "run123", "my-project")

        assert len(records) == 1
        r = records[0]
        assert r["test_type"] == "unit"
        assert r["iteration_number"] == 1
        assert r["test_execution_passed"] is True
        assert r["test_failure_count"] == 0
        assert r["test_warning_count"] == 2
        assert abs(r["test_execution_duration_s"] - 45.0) < 0.01
        assert r["had_fix_cycle"] is False
        assert r["fix_cycle_duration_s"] is None
        assert r["fix_cycle_files_fixed"] is None
        assert r["pipeline_run_id"] == "run123"
        assert r["project"] == "my-project"

    def test_failing_iteration_with_fix_cycle(self, recorder):
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {
                "has_failures": True,
                "failed": 3,
                "warnings": 0,
            }, ts_offset=30),
            _ev("repair_cycle_fix_cycle_started", "unit", 1, ts_offset=31),
            _ev("repair_cycle_fix_cycle_completed", "unit", 1, {
                "files_fixed": 2,
            }, ts_offset=91),
        ]
        records = recorder._build_iteration_records(events, "run456", "proj")

        assert len(records) == 1
        r = records[0]
        assert r["test_execution_passed"] is False
        assert r["test_failure_count"] == 3
        assert abs(r["test_execution_duration_s"] - 30.0) < 0.01
        assert r["had_fix_cycle"] is True
        assert abs(r["fix_cycle_duration_s"] - 60.0) < 0.01
        assert r["fix_cycle_files_fixed"] == 2

    def test_multiple_iterations(self, recorder):
        events = [
            # Iteration 1 — fails
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": True, "failed": 2, "warnings": 0}, ts_offset=20),
            _ev("repair_cycle_fix_cycle_started", "unit", 1, ts_offset=21),
            _ev("repair_cycle_fix_cycle_completed", "unit", 1, {"files_fixed": 1}, ts_offset=81),
            # Iteration 2 — passes
            _ev("repair_cycle_test_execution_started", "unit", 2, ts_offset=82),
            _ev("repair_cycle_test_execution_completed", "unit", 2, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=127),
        ]
        records = recorder._build_iteration_records(events, "runX", "proj")

        assert len(records) == 2
        by_iter = {r["iteration_number"]: r for r in records}

        assert by_iter[1]["test_execution_passed"] is False
        assert by_iter[1]["had_fix_cycle"] is True
        assert by_iter[2]["test_execution_passed"] is True
        assert by_iter[2]["had_fix_cycle"] is False

    def test_multiple_test_types(self, recorder):
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=30),
            _ev("repair_cycle_test_execution_started", "integration", 1, ts_offset=60),
            _ev("repair_cycle_test_execution_completed", "integration", 1, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=180),
        ]
        records = recorder._build_iteration_records(events, "runY", "proj")

        assert len(records) == 2
        types = {r["test_type"] for r in records}
        assert types == {"unit", "integration"}

        unit_r = next(r for r in records if r["test_type"] == "unit")
        integ_r = next(r for r in records if r["test_type"] == "integration")
        assert abs(unit_r["test_execution_duration_s"] - 30.0) < 0.01
        assert abs(integ_r["test_execution_duration_s"] - 120.0) < 0.01

    def test_skips_incomplete_iteration(self, recorder):
        """An iteration with only STARTED (no COMPLETED) is excluded."""
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            # No COMPLETED event — cycle was interrupted
        ]
        records = recorder._build_iteration_records(events, "run-partial", "proj")
        assert records == []

    def test_fix_cycle_without_completed_event(self, recorder):
        """FIX_CYCLE_STARTED without FIX_CYCLE_COMPLETED → had_fix_cycle=False."""
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": True, "failed": 1, "warnings": 0}, ts_offset=20),
            _ev("repair_cycle_fix_cycle_started", "unit", 1, ts_offset=21),
            # No FIX_CYCLE_COMPLETED (orchestrator crashed mid-fix)
        ]
        records = recorder._build_iteration_records(events, "run-crash", "proj")
        assert len(records) == 1
        assert records[0]["had_fix_cycle"] is False

    def test_zero_duration_compilation_failure(self, recorder):
        """Near-instant test execution (compilation failure) records correctly."""
        events = [
            _ev("repair_cycle_test_execution_started", "compilation", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "compilation", 1, {
                "has_failures": True, "failed": 10, "warnings": 0,
            }, ts_offset=1),  # 1 second — essentially instant
        ]
        records = recorder._build_iteration_records(events, "run-compile", "proj")
        assert len(records) == 1
        assert records[0]["test_execution_duration_s"] == pytest.approx(1.0, abs=0.01)
        assert records[0]["test_execution_passed"] is False

    def test_warning_rerun_same_iteration_emits_two_records(self, recorder):
        """
        When _run_tests() is called twice within the same iteration (e.g. after
        warning fixes), two STARTED/COMPLETED pairs are emitted for the same
        (test_type, iteration) key.  The recorder emits one record per sub-run
        so each individual test execution time is preserved.
        """
        events = [
            # Sub-run 1: initial run — passes but has warnings
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {
                "has_failures": False, "failed": 0, "warnings": 3,
            }, ts_offset=30),
            # Sub-run 2: re-run after warning fixes — passes cleanly
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=60),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {
                "has_failures": False, "failed": 0, "warnings": 0,
            }, ts_offset=95),
        ]
        records = recorder._build_iteration_records(events, "run-warn", "proj")

        assert len(records) == 2
        by_sub = {r["sub_run_index"]: r for r in records}

        r1 = by_sub[1]
        assert r1["test_execution_duration_s"] == pytest.approx(30.0, abs=0.01)
        assert r1["test_execution_passed"] is True
        assert r1["test_warning_count"] == 3
        assert r1["had_fix_cycle"] is False  # warning re-run, not a fix cycle

        r2 = by_sub[2]
        assert r2["test_execution_duration_s"] == pytest.approx(35.0, abs=0.01)
        assert r2["test_execution_passed"] is True
        assert r2["test_warning_count"] == 0

    def test_systemic_fix_rerun_same_iteration_emits_two_records(self, recorder):
        """
        After a systemic fix sub-cycle, _run_tests() is called again with the
        same iteration number.  The recorder emits one record per sub-run; the
        fix cycle is attached to the first (failed) sub-run.
        """
        events = [
            # Sub-run 1: fails badly
            _ev("repair_cycle_test_execution_started", "integration", 2, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "integration", 2, {
                "has_failures": True, "failed": 8, "warnings": 0,
            }, ts_offset=45),
            # Fix cycle
            _ev("repair_cycle_fix_cycle_started", "integration", 2, ts_offset=46),
            _ev("repair_cycle_fix_cycle_completed", "integration", 2, {"files_fixed": 3}, ts_offset=120),
            # Sub-run 2: still fails (2 remaining failures)
            _ev("repair_cycle_test_execution_started", "integration", 2, ts_offset=121),
            _ev("repair_cycle_test_execution_completed", "integration", 2, {
                "has_failures": True, "failed": 2, "warnings": 0,
            }, ts_offset=166),
        ]
        records = recorder._build_iteration_records(events, "run-systemic", "proj")

        assert len(records) == 2
        by_sub = {r["sub_run_index"]: r for r in records}

        r1 = by_sub[1]
        assert r1["test_execution_duration_s"] == pytest.approx(45.0, abs=0.01)
        assert r1["test_execution_passed"] is False
        assert r1["test_failure_count"] == 8
        # Fix cycle belongs to the sub-run that triggered it
        assert r1["had_fix_cycle"] is True
        assert r1["fix_cycle_files_fixed"] == 3
        assert r1["fix_cycle_duration_s"] == pytest.approx(74.0, abs=0.01)

        r2 = by_sub[2]
        assert r2["test_execution_duration_s"] == pytest.approx(45.0, abs=0.01)
        assert r2["test_execution_passed"] is False
        assert r2["test_failure_count"] == 2
        assert r2["had_fix_cycle"] is False

    def test_sub_run_index_on_simple_iteration(self, recorder):
        """A simple iteration with no re-runs produces one record with sub_run_index=1."""
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=30),
        ]
        records = recorder._build_iteration_records(events, "run-simple", "proj")
        assert len(records) == 1
        assert records[0]["sub_run_index"] == 1


# ─── record_repair_cycle integration tests ───────────────────────────────────

class TestRecordRepairCycle:

    def _make_es_search_response(self, events: list[dict]) -> dict:
        return {
            "hits": {
                "total": {"value": len(events)},
                "hits": [{"_source": ev} for ev in events],
            }
        }

    def test_writes_one_doc_per_iteration(self, recorder, mock_es):
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=45),
            _ev("repair_cycle_test_execution_started", "unit", 2, ts_offset=100),
            _ev("repair_cycle_test_execution_completed", "unit", 2, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=145),
        ]
        mock_es.search.return_value = self._make_es_search_response(events)

        count = recorder.record_repair_cycle("pipeline-run-001", "my-project")

        assert count == 2
        assert mock_es.index.call_count == 2

        # Verify doc IDs include sub_run_index
        doc_ids = {c.kwargs["id"] for c in mock_es.index.call_args_list}
        assert doc_ids == {
            "pipeline-run-001_unit_1_1",
            "pipeline-run-001_unit_2_1",
        }

    def test_idempotent_doc_id(self, recorder, mock_es):
        """Calling twice for the same pipeline_run_id produces the same doc IDs."""
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=30),
        ]
        mock_es.search.return_value = self._make_es_search_response(events)

        recorder.record_repair_cycle("run-idempotent", "proj")
        recorder.record_repair_cycle("run-idempotent", "proj")

        # Both calls use the same doc_id → safe upsert
        ids = [c.kwargs["id"] for c in mock_es.index.call_args_list]
        assert ids.count("run-idempotent_unit_1_1") == 2

    def test_empty_pipeline_run_id_skipped(self, recorder, mock_es):
        count = recorder.record_repair_cycle("", "proj")
        assert count == 0
        mock_es.search.assert_not_called()

    def test_no_events_returns_zero(self, recorder, mock_es):
        mock_es.search.return_value = self._make_es_search_response([])
        count = recorder.record_repair_cycle("run-empty", "proj")
        assert count == 0
        mock_es.index.assert_not_called()

    def test_es_search_failure_returns_zero(self, recorder, mock_es):
        mock_es.search.side_effect = ConnectionError("ES down")
        count = recorder.record_repair_cycle("run-es-fail", "proj")
        assert count == 0

    def test_partial_write_failure_continues(self, recorder, mock_es):
        """If one write fails, the others still succeed."""
        events = [
            _ev("repair_cycle_test_execution_started", "unit", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "unit", 1, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=30),
            _ev("repair_cycle_test_execution_started", "unit", 2, ts_offset=50),
            _ev("repair_cycle_test_execution_completed", "unit", 2, {"has_failures": False, "failed": 0, "warnings": 0}, ts_offset=80),
        ]
        mock_es.search.return_value = self._make_es_search_response(events)

        call_number = {"n": 0}
        def side_effect(**kwargs):
            call_number["n"] += 1
            if call_number["n"] == 1:
                raise Exception("write failed")
        mock_es.index.side_effect = side_effect

        count = recorder.record_repair_cycle("run-partial-fail", "proj")
        # One failed, one succeeded
        assert count == 1


# ─── Per-sub-run doc ID tests ────────────────────────────────────────────────

class TestSubRunDocIds:

    def _make_es_search_response(self, events: list[dict]) -> dict:
        return {
            "hits": {
                "total": {"value": len(events)},
                "hits": [{"_source": ev} for ev in events],
            }
        }

    def test_two_sub_runs_produce_distinct_doc_ids(self, recorder, mock_es):
        """Two sub-runs within one iteration must have different doc IDs."""
        events = [
            _ev("repair_cycle_test_execution_started", "regression", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "regression", 1, {
                "has_failures": True, "failed": 5, "warnings": 0,
            }, ts_offset=7200),   # 2h — fails
            _ev("repair_cycle_fix_cycle_started", "regression", 1, ts_offset=7201),
            _ev("repair_cycle_fix_cycle_completed", "regression", 1, {"files_fixed": 2}, ts_offset=8400),
            _ev("repair_cycle_test_execution_started", "regression", 1, ts_offset=8401),
            _ev("repair_cycle_test_execution_completed", "regression", 1, {
                "has_failures": False, "failed": 0, "warnings": 0,
            }, ts_offset=14400),  # another 1.7h — passes
        ]
        mock_es.search.return_value = self._make_es_search_response(events)

        count = recorder.record_repair_cycle("run-long", "proj")

        assert count == 2
        doc_ids = {c.kwargs["id"] for c in mock_es.index.call_args_list}
        assert doc_ids == {"run-long_regression_1_1", "run-long_regression_1_2"}

    def test_two_sub_run_durations_are_independent(self, recorder, mock_es):
        """Each sub-run record has its own duration, not a sum."""
        events = [
            _ev("repair_cycle_test_execution_started", "regression", 1, ts_offset=0),
            _ev("repair_cycle_test_execution_completed", "regression", 1, {
                "has_failures": True, "failed": 3, "warnings": 0,
            }, ts_offset=7200),
            _ev("repair_cycle_fix_cycle_started", "regression", 1, ts_offset=7201),
            _ev("repair_cycle_fix_cycle_completed", "regression", 1, {"files_fixed": 1}, ts_offset=8400),
            _ev("repair_cycle_test_execution_started", "regression", 1, ts_offset=8401),
            _ev("repair_cycle_test_execution_completed", "regression", 1, {
                "has_failures": False, "failed": 0, "warnings": 0,
            }, ts_offset=14400),
        ]
        mock_es.search.return_value = self._make_es_search_response(events)
        recorder.record_repair_cycle("run-long2", "proj")

        docs = {c.kwargs["id"]: c.kwargs["document"] for c in mock_es.index.call_args_list}
        assert docs["run-long2_regression_1_1"]["test_execution_duration_s"] == pytest.approx(7200.0, abs=1)
        assert docs["run-long2_regression_1_2"]["test_execution_duration_s"] == pytest.approx(5999.0, abs=1)
        # Fix cycle attached to the failing sub-run
        assert docs["run-long2_regression_1_1"]["had_fix_cycle"] is True
        assert docs["run-long2_regression_1_2"]["had_fix_cycle"] is False


# ─── Rollup stats tests ───────────────────────────────────────────────────────

class TestComputeStatsForGroup:

    def _make_record(self, passed: bool, duration: float, iteration: int = 1,
                     sub_run: int = 1, fix_duration: float = None) -> dict:
        return {
            "pipeline_run_id": f"run-{iteration}-{duration}",
            "test_execution_passed": passed,
            "test_failure_count": 0 if passed else 3,
            "test_warning_count": 0,
            "test_execution_duration_s": duration,
            "had_fix_cycle": fix_duration is not None,
            "fix_cycle_duration_s": fix_duration,
            "iteration_number": iteration,
            "sub_run_index": sub_run,
            "@timestamp": _make_ts(duration),
        }

    def test_clean_pass_baseline_requires_iter1_subrun1_passed(self):
        from scripts.calculate_test_cycle_stats import _compute_stats_for_group
        records = [
            self._make_record(passed=True,  duration=30.0, iteration=1, sub_run=1),  # run-A: clean
            self._make_record(passed=True,  duration=45.0, iteration=1, sub_run=1),  # run-B: clean
            self._make_record(passed=True,  duration=32.0, iteration=2, sub_run=1),  # not iter 1
            self._make_record(passed=True,  duration=28.0, iteration=1, sub_run=2),  # not sub_run 1
        ]
        records[0]["pipeline_run_id"] = "run-A"
        records[1]["pipeline_run_id"] = "run-B"
        records[2]["pipeline_run_id"] = "run-B"
        records[3]["pipeline_run_id"] = "run-C"

        doc = _compute_stats_for_group(records, "proj", "unit")
        # Clean baseline: iteration=1, sub_run=1, passed=True → run-A(30) + run-B(45) only
        assert doc["clean_pass_sample_count"] == 2
        assert abs(doc["clean_pass_duration_avg_s"] - 37.5) < 0.01

    def test_all_exec_includes_all_iterations(self):
        from scripts.calculate_test_cycle_stats import _compute_stats_for_group
        records = [
            self._make_record(passed=False, duration=20.0, iteration=1),
            self._make_record(passed=True,  duration=25.0, iteration=2),
        ]
        doc = _compute_stats_for_group(records, "proj", "unit")
        assert doc["total_iterations"] == 2
        assert abs(doc["all_exec_pass_rate"] - 0.5) < 0.01

    def test_avg_fix_cycle_duration(self):
        from scripts.calculate_test_cycle_stats import _compute_stats_for_group
        records = [
            self._make_record(passed=False, duration=20.0, iteration=1, fix_duration=60.0),
            self._make_record(passed=False, duration=22.0, iteration=1, fix_duration=80.0),
        ]
        records[0]["pipeline_run_id"] = "run-1"
        records[1]["pipeline_run_id"] = "run-2"
        doc = _compute_stats_for_group(records, "proj", "unit")
        assert abs(doc["avg_fix_cycle_duration_s"] - 70.0) < 0.01

    def test_returns_none_for_empty_records(self):
        from scripts.calculate_test_cycle_stats import _compute_stats_for_group
        assert _compute_stats_for_group([], "proj", "unit") is None

    def test_doc_id_format(self):
        from scripts.calculate_test_cycle_stats import _compute_stats_for_group
        records = [self._make_record(passed=True, duration=30.0)]
        doc = _compute_stats_for_group(records, "my-project", "integration")
        assert doc["project"] == "my-project"
        assert doc["test_type"] == "integration"

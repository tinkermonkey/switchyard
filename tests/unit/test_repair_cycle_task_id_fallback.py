"""
Unit tests for RepairCycleStage's task_id fallback behavior.

pipeline/repair_cycle_runner.py's containerized entrypoint never sets
`task_id` in the context it builds, so every real repair-cycle run falls
back to a task_id derived inside RepairCycleStage. Prior to this fix, that
fallback was `f"repair_cycle_{self.name}"` (or, at one call site,
`"unknown"`) - identical for every issue running the same stage, causing
derived correlation IDs (e.g. `{task_id}_test_iter{n}`) to collide between
concurrent repair cycles on the same project/stage.

These tests assert the issue number is included in the fallback-derived
task_id, and that an explicit task_id in context is left untouched.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock

from pipeline.repair_cycle import (
    RepairCycleStage,
    RepairTestRunConfig,
    CycleResult,
)


def _make_stage(name: str = "Testing") -> RepairCycleStage:
    """Helper: create a RepairCycleStage without Docker-only imports."""
    return RepairCycleStage(
        name=name,
        test_configs=[RepairTestRunConfig(test_type="unit")],
        agent_name="senior_software_engineer",
    )


def _make_cycle_result(passed: bool = True) -> CycleResult:
    return CycleResult(
        test_type="unit",
        passed=passed,
        iterations=1,
        final_result=None,
        files_fixed=0,
        warnings_reviewed=0,
        duration_seconds=1.0,
    )


class TestEmitCycleMetricsTaskIdFallback:
    """Covers the differently-shaped fallback: context.get("task_id", "unknown")."""

    def test_fallback_includes_issue_number(self):
        stage = _make_stage()
        obs = MagicMock()
        context = {
            "observability": obs,
            "project": "test_project",
            "issue_number": 4242,
            # no "task_id" key -> fallback must be used
        }

        stage._emit_cycle_metrics(_make_cycle_result(), context)

        assert obs.emit_performance_metric.called
        used_task_id = obs.emit_performance_metric.call_args_list[0].kwargs["task_id"]
        assert "4242" in used_task_id
        assert used_task_id != "unknown"

    def test_explicit_task_id_is_not_overridden(self):
        """No behavior change when task_id is already present in context."""
        stage = _make_stage()
        obs = MagicMock()
        context = {
            "observability": obs,
            "project": "test_project",
            "issue_number": 4242,
            "task_id": "explicit-task-id-123",
        }

        stage._emit_cycle_metrics(_make_cycle_result(), context)

        used_task_id = obs.emit_performance_metric.call_args_list[0].kwargs["task_id"]
        assert used_task_id == "explicit-task-id-123"

    def test_fallback_unique_across_concurrent_issues_same_stage(self):
        """Two concurrent repair cycles on the same stage but different issues
        must not derive the same fallback task_id."""
        stage = _make_stage(name="Testing")
        obs_a = MagicMock()
        obs_b = MagicMock()

        stage._emit_cycle_metrics(
            _make_cycle_result(),
            {"observability": obs_a, "project": "proj", "issue_number": 101},
        )
        stage._emit_cycle_metrics(
            _make_cycle_result(),
            {"observability": obs_b, "project": "proj", "issue_number": 202},
        )

        task_id_a = obs_a.emit_performance_metric.call_args_list[0].kwargs["task_id"]
        task_id_b = obs_b.emit_performance_metric.call_args_list[0].kwargs["task_id"]
        assert task_id_a != task_id_b

    def test_explicit_none_issue_number_falls_back_to_unknown_string(self):
        """context.get('issue_number', 'unknown') only substitutes on a MISSING
        key, not on a present-but-None value — context['issue_number'] = None
        would otherwise produce the literal substring "None" instead of
        "unknown", and (worse) would let two concurrent cycles that both have
        issue_number=None collide on the same fallback task_id."""
        stage = _make_stage()
        obs = MagicMock()
        context = {
            "observability": obs,
            "project": "test_project",
            "issue_number": None,
        }

        stage._emit_cycle_metrics(_make_cycle_result(), context)

        used_task_id = obs.emit_performance_metric.call_args_list[0].kwargs["task_id"]
        assert "None" not in used_task_id
        assert "unknown" in used_task_id


class TestExecuteTaskIdFallback:
    """Covers the primary pattern: context.get("task_id", f"repair_cycle_{self.name}")."""

    @pytest.mark.asyncio
    async def test_fallback_includes_issue_number(self, monkeypatch):
        stage = _make_stage(name="Testing")
        monkeypatch.setattr(
            stage, "_run_test_cycle", AsyncMock(return_value=_make_cycle_result())
        )

        obs = MagicMock()
        context = {
            "observability": obs,
            "project": "test_project",
            "issue_number": 8675,
            # no "task_id", no "pipeline_run_id" -> skips optional analytics recording
        }

        await stage.execute(context)

        used_task_id = obs.emit_task_received.call_args[0][1]
        assert "8675" in used_task_id
        assert used_task_id != f"repair_cycle_{stage.name}"

    @pytest.mark.asyncio
    async def test_explicit_task_id_is_not_overridden(self, monkeypatch):
        """No behavior change when task_id is already present in context."""
        stage = _make_stage(name="Testing")
        monkeypatch.setattr(
            stage, "_run_test_cycle", AsyncMock(return_value=_make_cycle_result())
        )

        obs = MagicMock()
        context = {
            "observability": obs,
            "project": "test_project",
            "issue_number": 8675,
            "task_id": "explicit-task-id-456",
        }

        await stage.execute(context)

        used_task_id = obs.emit_task_received.call_args[0][1]
        assert used_task_id == "explicit-task-id-456"

    @pytest.mark.asyncio
    async def test_fallback_unique_across_concurrent_issues_same_stage(self, monkeypatch):
        """Two concurrent repair cycles on the same stage but different issues
        must not derive the same fallback task_id (this is what caused
        correlation-ID collisions between concurrent runs)."""
        stage = _make_stage(name="Testing")
        monkeypatch.setattr(
            stage, "_run_test_cycle", AsyncMock(return_value=_make_cycle_result())
        )

        obs_a = MagicMock()
        obs_b = MagicMock()

        await stage.execute({"observability": obs_a, "project": "proj", "issue_number": 111})
        await stage.execute({"observability": obs_b, "project": "proj", "issue_number": 222})

        task_id_a = obs_a.emit_task_received.call_args[0][1]
        task_id_b = obs_b.emit_task_received.call_args[0][1]
        assert task_id_a != task_id_b

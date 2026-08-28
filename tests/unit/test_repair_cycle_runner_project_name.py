"""
Regression test for pipeline/repair_cycle_runner.py's RepairCycleStage
construction: it must pass project_name through (from self.context['project'],
already used for this exact purpose elsewhere in the same method) so the
stage's CircuitBreaker is namespaced by project (see #42) instead of falling
back to the pre-#42 un-namespaced key and reproducing cross-project bleed.
"""

from pathlib import Path
from unittest.mock import patch

from pipeline.repair_cycle_runner import RepairCycleRunner


def _make_runner(tmp_path, project="acme-project"):
    # Bypass __init__ entirely (it writes a log file under /workspace/{project},
    # which doesn't exist outside the container) — set only what
    # initialize_stage() actually reads.
    runner = object.__new__(RepairCycleRunner)
    runner.project_dir = tmp_path
    runner.context = {
        "project": project,
        "issue_number": 123,
        "test_configs": [{"test_type": "unit"}],
    }
    return runner


def test_initialize_stage_passes_project_name_to_repair_cycle_stage(tmp_path):
    runner = _make_runner(tmp_path)

    with patch("pipeline.repair_cycle_checkpoint.RepairCycleCheckpoint"):
        ok = runner.initialize_stage()

    assert ok is True
    assert runner.stage is not None
    assert runner.stage.project_name == "acme-project"
    # And the underlying CircuitBreaker must actually be namespaced by it.
    assert runner.stage.circuit_breaker.name.startswith("acme-project:")

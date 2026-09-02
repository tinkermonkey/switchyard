"""
Tests for the two error-handling guards added to pipeline/repair_cycle_runner.py
to fix the epic-worktree repair-cycle startup crash (context-studio #1203,
codetoreum #1019, 2026-09-02): a repair-cycle container launched with an epic
worktree as its project_dir was exiting instantly with code 1, 0 duration, 0
agent calls, and no result written to Redis -- traced to RepairCycleRunner
.__init__ doing unguarded file I/O (a per-worktree .repair_cycle.log
FileHandler) before any of run()'s own error handling exists.

Covers:
1. RepairCycleRunner.__init__'s own guard: an OSError opening the log file
   (e.g. project_dir doesn't exist / isn't writable) must not raise -- it
   should fall back to stdout-only logging and leave the rest of __init__
   (in particular self.project_dir) intact.
2. main()'s guard: any exception raised by RepairCycleRunner(args)
   construction must be logged and exit the process with code 2, WITHOUT
   ever calling run() -- this is the actual fix for the crash signature
   above (exit 1, no Redis result -> exit 2, logged reason).
"""
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from pipeline.repair_cycle_runner import RepairCycleRunner
import pipeline.repair_cycle_runner as repair_cycle_runner_module


def _args(project="my-project", context=None):
    return SimpleNamespace(project=project, context=context)


@pytest.fixture
def isolate_root_logger():
    """RepairCycleRunner.__init__ adds a FileHandler to the root logger as a
    side effect on success. Without cleanup, handlers (and their open file
    descriptors) accumulate across tests."""
    root = logging.getLogger()
    before = list(root.handlers)
    yield
    for handler in list(root.handlers):
        if handler not in before:
            root.removeHandler(handler)
            handler.close()


class TestInitFileHandlerGuard:
    def test_unwritable_project_dir_falls_back_without_raising(
        self, tmp_path, caplog, monkeypatch, isolate_root_logger
    ):
        """The exact scenario from the incident: project_dir (an epic worktree
        mounted in by project_monitor.py) doesn't exist / isn't writable at
        container startup."""
        monkeypatch.setattr(repair_cycle_runner_module.signal, "signal", lambda *a, **k: None)

        context_file = tmp_path / "context.json"
        nonexistent_dir = tmp_path / "does-not-exist" / "worktree"
        context_file.write_text(json.dumps({"project_dir": str(nonexistent_dir)}))

        with caplog.at_level(logging.WARNING):
            runner = RepairCycleRunner(_args(context=str(context_file)))

        # __init__ completed -- did not raise -- and project_dir is still
        # correctly resolved to the (nonexistent) epic worktree path.
        assert runner.project_dir == nonexistent_dir
        assert any(
            "Could not open repair-cycle log file" in r.message
            and "stdout-only logging" in r.message
            for r in caplog.records
        )

    def test_writable_project_dir_creates_log_file_normally(
        self, tmp_path, caplog, monkeypatch, isolate_root_logger
    ):
        """Companion happy-path: the success branch through this same guard
        (previously also untested) still creates the log file and logs no
        warning."""
        monkeypatch.setattr(repair_cycle_runner_module.signal, "signal", lambda *a, **k: None)

        context_file = tmp_path / "context.json"
        project_dir = tmp_path / "worktree"
        project_dir.mkdir()
        context_file.write_text(json.dumps({"project_dir": str(project_dir)}))

        with caplog.at_level(logging.WARNING):
            runner = RepairCycleRunner(_args(context=str(context_file)))

        assert runner.project_dir == project_dir
        assert (project_dir / ".repair_cycle.log").exists()
        assert not any("Could not open repair-cycle log file" in r.message for r in caplog.records)


class TestMainExitsCleanlyOnInitFailure:
    def _patched_args(self, monkeypatch, context=None):
        monkeypatch.setattr(
            repair_cycle_runner_module,
            "parse_args",
            lambda: SimpleNamespace(
                project="my-project",
                issue=1,
                pipeline_run_id="run-1",
                stage="Testing",
                context=context,
                debug=False,
            ),
        )

    def test_init_failure_exits_2_and_never_calls_run(self, monkeypatch, caplog):
        self._patched_args(monkeypatch)

        run_called = []

        class ExplodingRunner:
            def __init__(self, args):
                raise RuntimeError("boom: could not construct runner")

            def run(self):
                run_called.append(True)
                return 0

        monkeypatch.setattr(repair_cycle_runner_module, "RepairCycleRunner", ExplodingRunner)

        with caplog.at_level(logging.ERROR):
            with pytest.raises(SystemExit) as exc_info:
                repair_cycle_runner_module.main()

        assert exc_info.value.code == 2
        assert run_called == []  # run() must never be reached
        assert any("Failed to initialize RepairCycleRunner" in r.message for r in caplog.records)

    def test_successful_init_still_calls_run_and_exits_its_code(self, monkeypatch):
        self._patched_args(monkeypatch)

        class FakeRunner:
            def __init__(self, args):
                self.args = args

            def run(self):
                return 0

        monkeypatch.setattr(repair_cycle_runner_module, "RepairCycleRunner", FakeRunner)

        with pytest.raises(SystemExit) as exc_info:
            repair_cycle_runner_module.main()

        assert exc_info.value.code == 0

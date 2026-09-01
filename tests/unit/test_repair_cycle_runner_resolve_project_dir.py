"""
Tests for pipeline/repair_cycle_runner.py's RepairCycleRunner._resolve_initial_project_dir()
(final whole-PR review pass on #87, addressed while unblocking #52).

Found: RepairCycleRunner.__init__ hardcoded self.project_dir = Path(f"/workspace/{args.project}")
unconditionally, ignoring the 'project_dir' the orchestrator actually saved into
context.json before launching this container (which may be an isolated epic
worktree for 'issues'-workspace repair cycles -- see #46/#48). This runs BEFORE
self.context is populated by the later load_context() call (needed immediately
for the log file handler), so it re-reads context.json directly rather than
reusing load_context()'s result.

_resolve_initial_project_dir is a @staticmethod taking only `args` (an object
with .project and .context attributes, matching argparse's Namespace) -- tested
directly here without instantiating RepairCycleRunner, to avoid that class's
__init__ side effects (log file handler, signal handlers).
"""

import json
from pathlib import Path
from types import SimpleNamespace

from pipeline.repair_cycle_runner import RepairCycleRunner


def _args(project="my-project", context=None):
    return SimpleNamespace(project=project, context=context)


class TestResolveInitialProjectDir:
    def test_no_context_file_falls_back_to_base_clone_default(self):
        result = RepairCycleRunner._resolve_initial_project_dir(_args(context=None))
        assert result == Path("/workspace/my-project")

    def test_context_file_with_project_dir_is_used(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text(json.dumps({
            "project_dir": "/workspace/.orchestrator/worktrees/my-project/42",
        }))

        result = RepairCycleRunner._resolve_initial_project_dir(
            _args(context=str(context_file))
        )

        assert result == Path("/workspace/.orchestrator/worktrees/my-project/42")

    def test_context_file_missing_project_dir_key_falls_back_to_default(self, tmp_path):
        """Pre-#48 context files won't have 'project_dir' at all -- must fall back,
        not crash or resolve to None."""
        context_file = tmp_path / "context.json"
        context_file.write_text(json.dumps({"board": "Dev"}))

        result = RepairCycleRunner._resolve_initial_project_dir(
            _args(context=str(context_file))
        )

        assert result == Path("/workspace/my-project")

    def test_unreadable_context_file_falls_back_without_raising(self, tmp_path):
        missing_file = tmp_path / "does_not_exist.json"

        result = RepairCycleRunner._resolve_initial_project_dir(
            _args(context=str(missing_file))
        )

        assert result == Path("/workspace/my-project")

    def test_malformed_json_falls_back_without_raising(self, tmp_path):
        context_file = tmp_path / "context.json"
        context_file.write_text("{not valid json")

        result = RepairCycleRunner._resolve_initial_project_dir(
            _args(context=str(context_file))
        )

        assert result == Path("/workspace/my-project")

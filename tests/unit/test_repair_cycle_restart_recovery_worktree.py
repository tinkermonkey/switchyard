"""
Tests for two real, previously-undiscovered gaps in the repair-cycle restart-
recovery path (services/agent_container_recovery.py), found while investigating
what still blocks issue #52's pilot rollout:

1. reconnect_repair_cycle_container() -- used when a repair-cycle container
   survives an orchestrator restart and is STILL RUNNING (the most common
   recovery case) -- loaded context.json but never extracted project_dir
   from it, so the reconnected monitor thread's eventual auto-commit resolved
   the wrong directory instead of the one the container was actually using,
   silently finding "nothing to commit" and losing the fix. Updated for issue
   #123 (WI-D of #119): project_dir replaces the epic_id/branch_name fields
   this path originally threaded through -- commit_agent_changes() no longer
   re-derives a directory from those itself.

2. _process_completed_repair_cycle()'s auto-advance step only checked
   overall_success (did tests pass), never whether the auto-commit itself
   actually succeeded -- so a repair cycle that passed its tests but failed to
   commit (e.g. due to gap #1 above) still advanced the issue as if the fix had
   landed, making a broken run look identical to a clean one.

Both functions build their context_file path from a hardcoded absolute string
and do `from pathlib import Path` as a LOCAL import inside the method body --
patching `<module>.Path` has no effect on that (the local import always binds
the real pathlib.Path fresh). Tests here patch `pathlib.Path.exists` and
`builtins.open` directly (methods on the actual Path class / builtin), which
works regardless of how Path was imported.
"""

import json
from unittest.mock import patch, MagicMock, mock_open

from services.agent_container_recovery import AgentContainerRecovery


def _make_recovery():
    return AgentContainerRecovery(redis_client=MagicMock())


class TestReconnectThreadsProjectDir:
    """reconnect_repair_cycle_container() must extract project_dir from
    context.json and pass it through to _monitor_repair_cycle_container(), so
    the eventual auto-commit resolves the same directory the container is
    actually mounted from."""

    def _run(self, saved_context_json):
        recovery = _make_recovery()

        mock_project_config = MagicMock()
        mock_pipeline = MagicMock()
        mock_pipeline.board_name = "Dev"
        mock_pipeline.workflow = "dev_workflow"
        mock_project_config.pipelines = [mock_pipeline]
        mock_project_config.github = {"repo": "my-repo"}

        with patch("pathlib.Path.exists", return_value=bool(saved_context_json)), \
             patch("builtins.open", mock_open(read_data=saved_context_json or "")), \
             patch("config.manager.ConfigManager") as mock_config_manager_cls, \
             patch("services.project_monitor.ProjectMonitor") as mock_project_monitor_cls, \
             patch("task_queue.task_manager.TaskQueue"):

            mock_config_manager = mock_config_manager_cls.return_value
            mock_config_manager.get_project_config.return_value = mock_project_config
            mock_config_manager.get_workflow_template.return_value = MagicMock()
            mock_monitor = mock_project_monitor_cls.return_value

            recovery.reconnect_repair_cycle_container(
                container_name="repair-cycle-my-project-100-abc12345",
                project="my-project",
                issue_number=100,
                run_id="run-abc12345",
            )

            return mock_monitor

    def test_project_dir_threaded_through_from_context_file(self):
        saved_context = json.dumps({
            "board": "Dev",
            "pipeline_run_id": "run-abc12345",
            "agent_name": "senior_software_engineer",
            "project_dir": "/workspace/.orchestrator/worktrees/my-project/42",
        })

        mock_monitor = self._run(saved_context)

        mock_monitor._monitor_repair_cycle_container.assert_called_once()
        call_kwargs = mock_monitor._monitor_repair_cycle_container.call_args.kwargs
        assert call_kwargs["project_dir"] == "/workspace/.orchestrator/worktrees/my-project/42"
        assert call_kwargs["board_name"] == "Dev"

    def test_missing_context_file_falls_back_to_none_not_a_crash(self):
        """No context.json (e.g. a container from before this field existed) must
        not raise -- project_dir falls back to None. Heuristic board search takes
        over ('SDLC'/'dev' in board name)."""
        mock_monitor = self._run(None)

        mock_monitor._monitor_repair_cycle_container.assert_called_once()
        call_kwargs = mock_monitor._monitor_repair_cycle_container.call_args.kwargs
        assert call_kwargs["project_dir"] is None


class TestAutoAdvanceGatedOnCommitSuccess:
    """_process_completed_repair_cycle() must not auto-advance an issue whose
    repair cycle passed its tests but whose fix was never actually committed."""

    def _run(self, commit_returns):
        recovery = _make_recovery()

        saved_context = json.dumps({
            "board": "Dev",
            "repository": "my-repo",
            "column": "Testing",
            "pipeline_run_id": "run-1234",
            "epic_id": "42",
            "branch_name": "feature/issue-42-epic",
            "project_dir": "/workspace/.orchestrator/worktrees/my-project/42",
            "workspace_type": "issues",
        })
        result = {"overall_success": True, "total_agent_calls": 3, "duration_seconds": 12.0}

        mock_project_config = MagicMock()
        mock_project_config.github = {"org": "my-org"}

        active_run = MagicMock()
        active_run.id = "run-1234"

        async def _fake_commit(*a, **kw):
            return commit_returns

        with patch("pathlib.Path.exists", return_value=True), \
             patch("builtins.open", mock_open(read_data=saved_context)), \
             patch("services.pipeline_run.PipelineRunManager") as mock_prm_cls, \
             patch("config.manager.ConfigManager") as mock_config_manager_cls, \
             patch("services.github_integration.GitHubIntegration") as mock_github_cls, \
             patch("services.auto_commit.auto_commit_service") as mock_auto_commit, \
             patch("services.pipeline_run.get_pipeline_run_manager") as mock_get_prm, \
             patch("subprocess.run"):

            # A bare MagicMock's .get() returns a truthy Mock, not None -- would
            # make the code think a comment was already posted and skip the block
            # that does `import threading`, breaking the LATER (unrelated)
            # threading.Thread(...) use in the auto-commit section a few lines
            # down (that use relies on the earlier block's import already having
            # run -- a real, if fragile, ordering dependency in the production
            # code, not something these tests should paper over).
            recovery.redis.get.return_value = None

            # Two SEPARATE PipelineRunManager access patterns exist in this
            # method: an early one via the class constructor directly
            # (PipelineRunManager(), used for the orphaned-result safety check)
            # and a later one via the singleton accessor (get_pipeline_run_manager(),
            # used for the actual end/mark_failed decision this test cares about).
            # Both must return the SAME matching active_run for the run-id
            # validation each one independently performs to pass through.
            mock_prm_cls.return_value.get_active_pipeline_run.return_value = active_run
            mock_config_manager_cls.return_value.get_project_config.return_value = mock_project_config
            mock_github_cls.return_value.post_agent_output = _fake_commit  # any awaitable is fine here
            mock_auto_commit.commit_agent_changes = _fake_commit

            mock_prm = mock_get_prm.return_value
            mock_prm.get_active_pipeline_run.return_value = active_run
            mock_prm.mark_failed.return_value = True
            mock_prm.end_pipeline_run.return_value = True

            recovery._process_completed_repair_cycle(
                container_name="repair-cycle-my-project-100-run1234",
                container_id="abc123",
                project="my-project",
                issue_number=100,
                result=result,
            )

            return mock_prm

    def test_commit_failure_marks_pipeline_run_failed_instead_of_advancing(self):
        mock_prm = self._run(commit_returns=False)

        mock_prm.mark_failed.assert_called_once_with(
            project="my-project",
            board="Dev",
            issue_number=100,
            reason="Repair cycle passed but its fix was not committed",
        )
        mock_prm.end_pipeline_run.assert_not_called()

    def test_commit_success_advances_normally(self):
        mock_prm = self._run(commit_returns=True)

        mock_prm.mark_failed.assert_not_called()
        mock_prm.end_pipeline_run.assert_called_once()

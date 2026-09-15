"""
Unit tests for DockerAgentRunner's repair_test tool-denial wiring
(claude/docker_runner.py's _execute_in_container).

The repair cycle's test-runner agent (execution_type="repair_test") is meant
to run one test command in the foreground and block until it exits -- a
production incident showed the agent backgrounding the run instead, using
Monitor/ScheduleWakeup to poll it and repeatedly returning a "still waiting"
status update rather than the required result. See
DockerAgentRunner._should_deny_backgrounding_tools's docstring for the full
rationale. _execute_in_container() now passes --disallowedTools for
execution_type == "repair_test" invocations to remove that specific
wait/poll path rather than relying on prompt text alone.

Covers:
- _resolve_execution_type() reads the nested task_context first, falls back
  to the top-level context, and mirrors (without calling into) the inline
  lookups elsewhere in this file.
- _should_deny_backgrounding_tools() is true only for "repair_test".
- _execute_in_container() actually puts --disallowedTools into the command
  it executes for repair_test, and omits it otherwise -- exercising the
  wiring end-to-end rather than just the two helpers in isolation, which
  would not have caught the previously-dead 'claude_cmd' list this file's
  suite of helper-only tests passed against.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from claude.docker_runner import DockerAgentRunner
from config.manager import ConfigurationError


class TestResolveExecutionType:
    def test_reads_nested_task_context_first(self):
        context = {"context": {"execution_type": "repair_test"}, "execution_type": "standard"}
        assert DockerAgentRunner._resolve_execution_type(context) == "repair_test"

    def test_falls_back_to_top_level_context(self):
        context = {"context": {}, "execution_type": "review_cycle"}
        assert DockerAgentRunner._resolve_execution_type(context) == "review_cycle"

    def test_falls_back_when_nested_context_missing_entirely(self):
        context = {"execution_type": "conversational"}
        assert DockerAgentRunner._resolve_execution_type(context) == "conversational"

    def test_empty_string_when_neither_set(self):
        context = {"context": {}}
        assert DockerAgentRunner._resolve_execution_type(context) == ""

    def test_nested_empty_string_falls_through_to_top_level(self):
        # An explicit '' on the nested task_context is falsy, so `or` correctly
        # falls through to the top-level value instead of stopping at ''.
        context = {"context": {"execution_type": ""}, "execution_type": "repair_test"}
        assert DockerAgentRunner._resolve_execution_type(context) == "repair_test"


class TestShouldDenyBackgroundingTools:
    def test_true_for_repair_test(self):
        assert DockerAgentRunner._should_deny_backgrounding_tools("repair_test") is True

    def test_false_for_standard(self):
        assert DockerAgentRunner._should_deny_backgrounding_tools("standard") is False

    def test_false_for_empty_string(self):
        assert DockerAgentRunner._should_deny_backgrounding_tools("") is False

    def test_false_for_other_known_execution_types(self):
        for execution_type in ("review_cycle", "conversational", "repair_fix"):
            assert DockerAgentRunner._should_deny_backgrounding_tools(execution_type) is False


class TestDisallowedToolsConstant:
    def test_names_monitor_and_schedule_wakeup(self):
        # The two tools implicated in the incident: Monitor (used to watch a
        # backgrounded Bash process) and ScheduleWakeup (used to defer to a
        # later turn that never comes in a one-shot --print container).
        tools = DockerAgentRunner._REPAIR_TEST_DISALLOWED_TOOLS.split(",")
        assert tools == ["Monitor", "ScheduleWakeup"]


def _minimal_execute_in_container_context(execution_type: str) -> dict:
    """A context dict just complete enough for _execute_in_container() to
    reach the subprocess.run() that launches the container -- filesystem
    write verification is skipped by making get_project_agent_config() raise
    (see the test below), which is the same fallback path a project with no
    matching agent config takes in production."""
    return {
        "agent": "senior_software_engineer",
        "task_id": "task-1",
        "project": "phone-home",
        "context": {"execution_type": execution_type},
    }


class TestExecuteInContainerDisallowedTools:
    """Drives _execute_in_container() itself, rather than just the two pure
    helpers, so a regression in the wiring between them (e.g. the flag being
    appended to a command list that's never actually executed) fails a test
    instead of passing silently."""

    @staticmethod
    def _run_and_capture_launched_command(execution_type: str, tmp_path: Path) -> str:
        """Runs _execute_in_container() far enough to build the real launch
        command, then aborts it via subprocess.run raising, and returns that
        command joined into one string to search. filesystem_write_allowed
        is forced False by making get_project_agent_config() raise
        ConfigurationError with no 'agent_config' in context (docker_runner.py
        falls back to False in that case), which skips
        _verify_container_write_access() entirely -- the only thing standing
        between context construction and the subprocess.run() call this test
        intercepts.
        """
        runner = DockerAgentRunner()
        mock_subprocess_run = MagicMock(side_effect=RuntimeError("stop-test-here"))

        with patch(
            "config.manager.config_manager.get_project_agent_config",
            side_effect=ConfigurationError("no agent config for this test"),
        ), patch("claude.docker_runner.subprocess.run", mock_subprocess_run):
            with pytest.raises(RuntimeError, match="stop-test-here"):
                import asyncio

                asyncio.run(
                    runner._execute_in_container(
                        docker_cmd=["docker", "run", "-i", "--name", "test-container", "test-image"],
                        prompt="run the tests",
                        container_name="test-container",
                        stream_callback=None,
                        context=_minimal_execute_in_container_context(execution_type),
                        project_dir=tmp_path,
                        image_name="test-image",
                    )
                )

        # The RuntimeError propagates into _execute_in_container's own cleanup
        # path, which calls subprocess.run again ('docker rm -f ...') to tear
        # down the container it believes it just launched -- so more than one
        # call is expected here. The launch command (the one this test cares
        # about) is always the first.
        assert mock_subprocess_run.call_count >= 1
        full_cmd = mock_subprocess_run.call_args_list[0][0][0]
        return " ".join(full_cmd)

    def test_repair_test_execution_denies_monitor_and_schedule_wakeup(self, tmp_path):
        launched = self._run_and_capture_launched_command("repair_test", tmp_path)
        assert "--disallowedTools" in launched
        assert DockerAgentRunner._REPAIR_TEST_DISALLOWED_TOOLS in launched

    def test_standard_execution_does_not_deny_any_tools(self, tmp_path):
        launched = self._run_and_capture_launched_command("standard", tmp_path)
        assert "--disallowedTools" not in launched

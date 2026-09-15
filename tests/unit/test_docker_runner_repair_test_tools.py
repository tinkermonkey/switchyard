"""
Unit tests for DockerAgentRunner's repair_test tool-denial wiring
(claude/docker_runner.py's _execute_in_container).

A 2026-09-14 heimdall run (pipeline_run_id 4963b5b7) showed the repair
cycle's test-runner agent ignoring its prompt's explicit "do not background
the test run" instructions: it used Monitor/ScheduleWakeup to watch a
backgrounded Playwright run and repeatedly returned a "still waiting" status
update instead of the required JSON result, burning ~70 minutes across
retries. _execute_in_container() now passes --disallowedTools for
execution_type == "repair_test" invocations to remove that option
structurally rather than relying on prompt text alone.

Covers:
- _resolve_execution_type() reads the nested task_context first, falls back
  to the top-level context, and mirrors _build_docker_command()'s own
  label-resolution behavior for other execution types.
- _should_deny_backgrounding_tools() is true only for "repair_test".
"""

from claude.docker_runner import DockerAgentRunner


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

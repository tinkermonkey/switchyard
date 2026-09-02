"""
Tests for the process-global ProjectMonitor accessor (get_project_monitor /
register_project_monitor).

This singleton exists so components outside the main poll loop (currently:
pipeline_watchdog.py's zombie/self-heal redispatch) can reach ProjectMonitor.
trigger_agent_for_status() -- the one canonical dispatch entry point -- rather
than re-implementing dispatch logic. Unlike get_pipeline_lock_manager()/
get_pipeline_run_manager(), it does NOT lazily construct one itself (Project
Monitor needs a TaskQueue/ConfigManager at construction time); it only
returns whatever main.py registered.
"""
import services.project_monitor as project_monitor_module
from services.project_monitor import get_project_monitor, register_project_monitor


class TestProjectMonitorSingleton:
    def setup_method(self):
        # Module-level global state -- must not leak between tests (or from
        # a prior import elsewhere in the same test process).
        self._saved = project_monitor_module._project_monitor_instance
        project_monitor_module._project_monitor_instance = None

    def teardown_method(self):
        project_monitor_module._project_monitor_instance = self._saved

    def test_returns_none_before_registration(self):
        assert get_project_monitor() is None

    def test_returns_the_registered_instance(self):
        sentinel = object()
        register_project_monitor(sentinel)
        assert get_project_monitor() is sentinel

    def test_registering_again_replaces_the_previous_instance(self):
        first = object()
        second = object()
        register_project_monitor(first)
        register_project_monitor(second)
        assert get_project_monitor() is second

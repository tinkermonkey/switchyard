"""
Regression test for the repair-cycle "steal the lock" bug found across three
rounds of PR #35 review: ProjectMonitor._start_repair_cycle_for_issue's logic
for stealing the pipeline lock from another issue (repair cycles have
priority over ordinary Development items) never checked whether that lock was
durably retained due to a failed run before releasing and recreating it —
silently erasing the retained_reason/retained_at record and handing the lock
to an unrelated issue.

This test exercises the fix directly: a retained lock held by a different
issue must cause _start_repair_cycle_for_issue to refuse and return None,
without ever calling release_lock() or _create_lock() (both of which are
mocked here specifically so a call to either fails the test immediately).

IMPORTANT — this test previously did NOT reliably exercise the fix at all: in
any environment without a live Redis reachable at the `redis` hostname (e.g.
a bare `pytest`/`docker run` invocation, per this repo's own CLAUDE.md
convention of "No Redis/ES locally: tests handle gracefully via mocks or
skips"), the unmocked self.pipeline_run_manager.get_or_create_pipeline_run()
call raised a connection error BEFORE the lock-steal logic ever ran, was
swallowed by _start_repair_cycle_for_issue's own outer exception handler, and
returned None regardless of whether the fix was present or reverted — making
all three assertions pass vacuously. pipeline_run_manager and the `gh` CLI
subprocess call (also unmocked, also real) are both explicitly mocked below
so this test is deterministic in any environment, matching the pattern
already used by every sibling test in this directory
(test_agent_routing.py, test_retained_lock_gate.py, etc., which all mock
services.pipeline_run.get_pipeline_run_manager).
"""

import pytest
from unittest.mock import Mock, MagicMock, patch

from tests.unit.orchestrator.conftest import create_test_issue


class TestRepairCycleLockSteal:
    def test_refuses_to_steal_a_retained_lock(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
    ):
        """The core regression test: a repair cycle must never steal a
        retained lock out from under a different, failed issue."""
        # A different issue (999) holds a retained lock. _start_repair_cycle_for_issue
        # reads this via get_lock_fail_closed() (fail-closed, returns a
        # (lock, reads_healthy) tuple) at both of its lock-check points, not
        # the plain get_lock().
        retained_lock = Mock()
        retained_lock.locked_by_issue = 999
        retained_lock.retained_reason = "Repair cycle failed: simulated"
        mock_pipeline_lock_manager_auto.get_lock.return_value = retained_lock
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (retained_lock, True)

        # No existing repair-cycle container for issue 100, and no OTHER
        # repair cycle running for issue 999 either — otherwise the function's
        # earlier "another repair cycle is already running" guard fires first
        # (based on this Redis key) and the test wouldn't actually reach the
        # lock-steal logic being tested here at all.
        mock_task_queue.redis_client.get.return_value = None

        create_test_issue(mock_github, 100, 'Testing')

        mock_run = Mock()
        mock_run.id = 'run-repair-100'

        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('services.pipeline_lock_manager.get_pipeline_lock_manager', return_value=mock_pipeline_lock_manager_auto), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr, \
             patch('services.project_monitor.subprocess.run') as mock_subprocess:

            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)
            # Real `gh` CLI calls (e.g. fetching issue comments for previous-
            # stage context) would otherwise actually shell out and take
            # 15+ seconds to fail against a nonexistent test-org/test-repo —
            # deterministic failure, matching what the code already handles
            # gracefully (returns "" and continues).
            import subprocess as _subprocess
            mock_subprocess.side_effect = _subprocess.CalledProcessError(1, ['gh'])

            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)

            project_config = mock_config_manager.get_project_config('test-project')
            # test_configs building requires a real dict here (project_config is
            # otherwise a Mock) — without at least one test type, the function
            # returns None via the "no test configurations found" branch before
            # ever reaching the lock-steal logic this test targets.
            project_config.testing = {'types': [{'type': 'unit'}]}
            pipeline_config = project_config.pipelines[0]
            workflow_template = mock_config_manager.get_workflow_template('test-workflow')
            column = MagicMock()
            column.type = 'standard'
            stage_config = MagicMock()

            result = monitor._start_repair_cycle_for_issue(
                project_name='test-project',
                board_name='dev',
                issue_number=100,
                status='Testing',
                repository='test-repo',
                project_config=project_config,
                pipeline_config=pipeline_config,
                workflow_template=workflow_template,
                column=column,
                stage_config=stage_config,
            )

        assert result is None
        mock_pipeline_lock_manager_auto.release_lock.assert_not_called()
        mock_pipeline_lock_manager_auto._create_lock.assert_not_called()

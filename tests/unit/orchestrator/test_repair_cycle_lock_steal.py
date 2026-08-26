"""
Regression test for the repair-cycle "steal the lock" bug found across three
rounds of PR #35 review: ProjectMonitor._start_repair_cycle_for_issue's logic
for stealing the pipeline lock from another issue (repair cycles have
priority over ordinary Development items) never checked whether that lock was
durably retained due to a failed run before releasing and recreating it —
silently erasing the retained_reason/retained_at record and handing the lock
to an unrelated issue.

This logic now lives entirely inside PipelineLockManager.steal_lock() (a
later review round centralized it there specifically so this exact bug class
— the retained_reason check and the release+create call drifting apart —
can't recur at new call sites). This test exercises the fix at the
_start_repair_cycle_for_issue call site: it must call steal_lock() and, when
steal_lock() reports refusal (a retained lock held by a different issue),
return None without proceeding.

IMPORTANT — this test previously did NOT reliably exercise the fix at all,
twice, for two different reasons:

1. In any environment without a live Redis reachable at the `redis` hostname
   (e.g. a bare `pytest`/`docker run` invocation, per this repo's own
   CLAUDE.md convention of "No Redis/ES locally: tests handle gracefully via
   mocks or skips"), the unmocked
   self.pipeline_run_manager.get_or_create_pipeline_run() call raised a
   connection error BEFORE the lock-steal logic ever ran, was swallowed by
   _start_repair_cycle_for_issue's own outer exception handler, and returned
   None regardless of whether the fix was present or reverted. Fixed by
   mocking pipeline_run_manager and the `gh` CLI subprocess call below.
2. After the steal_lock() centralization, mock_pipeline_lock_manager_auto
   (a plain, unconfigured Mock standing in for the whole lock manager) left
   .steal_lock() unconfigured — calling it returned another bare Mock, and
   `ok, result = lock_manager.steal_lock(...)` at the call site raised
   TypeError: cannot unpack non-iterable Mock object, which the SAME outer
   exception handler from (1) also swallowed, again returning None regardless
   of whether the refusal logic was present or reverted. Fixed by explicitly
   configuring steal_lock.return_value below and asserting it was actually
   called with the expected arguments, rather than only asserting on
   release_lock/_create_lock (which this call site no longer calls directly
   at all post-centralization, so asserting on them no longer distinguishes
   correct from broken behavior).
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
        # A different issue (999) holds a retained lock. An earlier, separate
        # guard (competing-repair-cycle check) also reads get_lock_fail_closed()
        # before the steal_lock() call this test targets is ever reached — it
        # must see the same lock and let this through (999 has no repair-cycle
        # container registered per mock_task_queue.redis_client.get below, so
        # it isn't treated as a competing repair cycle).
        retained_lock = Mock()
        retained_lock.locked_by_issue = 999
        retained_lock.retained_reason = "Repair cycle failed: simulated"
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (retained_lock, True)
        # steal_lock() is what actually contains the retained_reason check
        # now — configure it to report the refusal it would give here.
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (
            False, "retained:Repair cycle failed: simulated"
        )

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
        mock_pipeline_lock_manager_auto.steal_lock.assert_called_once_with(
            'test-project', 'dev', 100
        )
        # steal_lock() itself owns the release+create sequence now — this call
        # site must not perform them directly, whether or not steal_lock()
        # refuses.
        mock_pipeline_lock_manager_auto.release_lock.assert_not_called()
        mock_pipeline_lock_manager_auto._create_lock.assert_not_called()


class TestRepairCycleLockSteal_HappyPath:
    def test_steals_a_non_retained_lock_and_proceeds(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
    ):
        """Control case for the test above: when steal_lock() reports success
        (the lock wasn't retained), _start_repair_cycle_for_issue must proceed
        past the lock-acquisition step rather than returning None — proving
        the refusal in the test above is actually caused by steal_lock()'s
        result, not by some earlier, unrelated guard."""
        # No lock held at all, so the earlier competing-repair-cycle guard
        # (which also reads get_lock_fail_closed()) passes trivially and this
        # reaches the steal_lock() call further down.
        mock_pipeline_lock_manager_auto.get_lock_fail_closed.return_value = (None, True)
        mock_pipeline_lock_manager_auto.steal_lock.return_value = (True, "stolen")
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
            import subprocess as _subprocess
            mock_subprocess.side_effect = _subprocess.CalledProcessError(1, ['gh'])

            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)

            project_config = mock_config_manager.get_project_config('test-project')
            project_config.testing = {'types': [{'type': 'unit'}]}
            pipeline_config = project_config.pipelines[0]
            workflow_template = mock_config_manager.get_workflow_template('test-workflow')
            column = MagicMock()
            column.type = 'standard'
            stage_config = MagicMock()

            monitor._start_repair_cycle_for_issue(
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

        mock_pipeline_lock_manager_auto.steal_lock.assert_called_once_with(
            'test-project', 'dev', 100
        )
        # It must not fall back to the removed direct release_lock/_create_lock
        # sequence — steal_lock() is the only thing that should touch the lock.
        mock_pipeline_lock_manager_auto.release_lock.assert_not_called()
        mock_pipeline_lock_manager_auto._create_lock.assert_not_called()

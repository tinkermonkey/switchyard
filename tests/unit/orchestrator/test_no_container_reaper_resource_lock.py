"""
Tests for the resource-lock exemption in ProjectMonitor's inline no-container
reaper (services/project_monitor.py, the `work_already_in_progress` branch of
trigger_agent_for_status).

This is the SECOND reaper that reaches "this run is dead" from "marked active,
past the grace period, no agent container carries the issue's Docker label".
services/pipeline_watchdog.py's zombie sweep is the first, and it already
exempts dispatches parked inside a project resource lock (#140 item 9,
tests/unit/services/test_pipeline_watchdog_resource_lock_awareness.py). This one
uses the same probe on the same evidence but on a 60-second fuse instead of a
30-minute one, so without the same exemption the double-execution that fix
closes was still reachable, just 30x sooner:

  project_checkout_lock / dev_container_build_lock waits sit BETWEEN
  record_execution_start()/get_or_create_pipeline_run() and
  docker_runner.run_agent_in_container(), so nothing carries the issue's label
  while a coroutine is parked in one. Ending the run and releasing the board
  lock there flips the state record to 'failure' on the next poll and
  redispatches the issue; when the original coroutine finally acquires the lock
  it launches its container alongside the new one — two concurrent executions of
  one issue against the same shared base clone.
"""

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock, patch

from services import project_checkout_lock
from services.project_checkout_lock import RESOURCE_NAME, _tracked_resource_activity
from tests.unit.orchestrator.conftest import create_test_issue


@pytest.fixture(autouse=True)
def clean_registry():
    """The activity registry is process-global; don't leak between tests."""
    with project_checkout_lock._resource_activity_guard:
        project_checkout_lock._resource_activity.clear()
    yield
    with project_checkout_lock._resource_activity_guard:
        project_checkout_lock._resource_activity.clear()


def _old_active_run(run_id='run-no-container'):
    """A run created comfortably past NO_CONTAINER_GRACE_SECONDS (60s)."""
    run = Mock()
    run.id = run_id
    run.is_active.return_value = True
    run.started_at = (
        datetime.now(timezone.utc) - timedelta(minutes=10)
    ).isoformat().replace('+00:00', 'Z')
    return run


class _ReaperHarness:
    """Drives trigger_agent_for_status into the no-container branch.

    Everything here is setup for ONE decision: with a run marked active, no
    container, and the grace period elapsed, does this reaper end the run and
    release the board lock, or does it leave both alone?
    """

    def run(
        self,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
        mock_pipeline_lock_manager_auto,
        issue_number,
        register_activity=None,
        describe_side_effect=None,
    ):
        create_test_issue(mock_github, issue_number, 'Development')

        active_run = _old_active_run()
        run_manager = Mock()
        run_manager.get_or_create_pipeline_run.return_value = (active_run, False)
        run_manager.get_active_pipeline_run.return_value = active_run
        run_manager.end_pipeline_run = Mock(return_value=True)

        tracker = Mock()
        tracker.was_recent_programmatic_change.return_value = False
        tracker.should_execute_work.return_value = (False, 'work_already_in_progress')
        tracker.is_frozen_by_circuit_breaker.return_value = False

        watchdog = Mock()
        watchdog._check_for_agent_container.return_value = False

        patches = [
            patch('services.project_monitor.ConfigManager', return_value=mock_config_manager),
            patch('config.state_manager.state_manager', mock_state_manager),
            patch(
                'monitoring.observability.get_observability_manager',
                return_value=mock_observability[0],
            ),
            patch('services.work_execution_state.work_execution_tracker', tracker),
            patch('services.pipeline_watchdog.get_pipeline_watchdog', return_value=watchdog),
            patch('services.cleanup_guard.try_claim_cleanup', return_value=True),
        ]
        if describe_side_effect is not None:
            patches.append(
                patch(
                    'services.project_checkout_lock.describe_active_resource_lock_activity',
                    side_effect=describe_side_effect,
                )
            )

        from contextlib import ExitStack

        with ExitStack() as stack:
            for p in patches:
                stack.enter_context(p)
            if register_activity is not None:
                stack.enter_context(
                    _tracked_resource_activity(RESOURCE_NAME, 'test-project', register_activity)
                )

            from services.project_monitor import ProjectMonitor

            monitor = ProjectMonitor(
                task_queue=mock_task_queue, config_manager=mock_config_manager
            )
            monitor.pipeline_run_manager = run_manager
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)

            result = monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=issue_number,
                status='Development',
                repository='test-repo',
            )

        return result, run_manager, mock_pipeline_lock_manager_auto, watchdog


class TestNoContainerReaperSkipsLiveResourceLockWork:
    def test_a_dispatch_waiting_on_a_resource_lock_is_not_reaped(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
    ):
        """The regression: a live project_checkout wait for THIS issue must keep
        the run active and the board lock held, even though no container exists."""
        harness = _ReaperHarness()
        result, run_manager, lock_manager, watchdog = harness.run(
            mock_github,
            mock_config_manager,
            mock_state_manager,
            mock_task_queue,
            mock_observability,
            mock_pipeline_lock_manager_auto,
            issue_number=310,
            register_activity=310,
        )

        assert result is None
        watchdog._check_for_agent_container.assert_called_once()
        run_manager.end_pipeline_run.assert_not_called()
        lock_manager.release_lock.assert_not_called()

    def test_an_activity_for_a_different_issue_does_not_vouch(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
    ):
        """The exemption is keyed on (project, issue): a sibling issue's wait is
        not evidence that THIS issue's dispatch is in flight, so the reaper still
        fires."""
        harness = _ReaperHarness()
        result, run_manager, lock_manager, watchdog = harness.run(
            mock_github,
            mock_config_manager,
            mock_state_manager,
            mock_task_queue,
            mock_observability,
            mock_pipeline_lock_manager_auto,
            issue_number=311,
            register_activity=999,
        )

        assert result is None
        run_manager.end_pipeline_run.assert_called_once()
        lock_manager.release_lock.assert_called_once_with('test-project', 'dev', 311)

    def test_no_activity_at_all_still_reaps(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
    ):
        """Control: with nothing registered, the silently-failed-launch cleanup is
        unchanged — this exemption must not turn the reaper off wholesale."""
        harness = _ReaperHarness()
        result, run_manager, lock_manager, _ = harness.run(
            mock_github,
            mock_config_manager,
            mock_state_manager,
            mock_task_queue,
            mock_observability,
            mock_pipeline_lock_manager_auto,
            issue_number=312,
        )

        assert result is None
        run_manager.end_pipeline_run.assert_called_once()
        lock_manager.release_lock.assert_called_once_with('test-project', 'dev', 312)

    def test_an_unverifiable_registry_fails_safe(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
    ):
        """Same posture as PipelineWatchdog's matching check: a run we cannot
        verify is never killed."""
        harness = _ReaperHarness()
        result, run_manager, lock_manager, _ = harness.run(
            mock_github,
            mock_config_manager,
            mock_state_manager,
            mock_task_queue,
            mock_observability,
            mock_pipeline_lock_manager_auto,
            issue_number=313,
            describe_side_effect=Exception('registry unavailable'),
        )

        assert result is None
        run_manager.end_pipeline_run.assert_not_called()
        lock_manager.release_lock.assert_not_called()

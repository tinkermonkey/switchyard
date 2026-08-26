"""
Integration-level test for the retained-lock dispatch gate in
ProjectMonitor.trigger_agent_for_status (services/project_monitor.py).

This is the gate that fixes the bug found in PR #35 review: a lock durably
marked retained-due-to-failure (PipelineLock.retained_reason) must block
dispatch for EVERY column type an issue can sit in — not just the pipeline's
trigger column. The original version of this gate lived only inside the
`is_trigger_column` branch, so a review-cycle crash (set while the issue sits
in a non-trigger column like "Code Review") could bypass it entirely on the
next poll.

Without this test, reverting the gate back to its pre-fix position (inside
is_trigger_column only) passes the entire rest of the unit suite, because the
shared mock_pipeline_lock_manager_auto fixture (tests/unit/orchestrator/
conftest.py) sets get_retained_reason.return_value = None unconditionally —
every other test is structurally incapable of exercising the "gate blocks
dispatch" branch. This test explicitly overrides that to prove the gate
actually fires, for a non-trigger column specifically (the exact case that was
broken), and a control test proves normal dispatch is unaffected when nothing
is retained.
"""

import pytest
from unittest.mock import patch, Mock

from tests.unit.orchestrator.conftest import create_test_issue


class TestRetainedLockGate:
    def test_retained_lock_blocks_dispatch_on_a_non_trigger_column(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
    ):
        """The core regression test: a retained lock must block dispatch even
        on a column that isn't the pipeline's trigger column (mock_config_manager's
        workflow has pipeline_trigger_columns=None, so no column is ever
        "the trigger column" here — every status is effectively non-trigger,
        matching the review-cycle-crash scenario the original bug reproduced)."""
        mock_pipeline_lock_manager_auto.get_retained_reason.return_value = (
            "Review cycle thread crashed: simulated failure"
        )

        create_test_issue(mock_github, 100, 'Code Review')

        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:

            mock_run = Mock()
            mock_run.id = 'run-123'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)

            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)

            result = monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=100,
                status='Code Review',
                repository='test-repo'
            )

        assert result is None
        # No routing decision should have been emitted — dispatch must never
        # have reached the point of selecting an agent for this issue.
        assert not mock_observability[1].emit_agent_routing_decision.called
        # No task should have been queued.
        assert not mock_task_queue.add_task.called

    def test_no_retained_lock_dispatches_normally(
        self,
        mock_pipeline_lock_manager_auto,
        mock_github,
        mock_config_manager,
        mock_state_manager,
        mock_task_queue,
        mock_observability,
    ):
        """Control case: with nothing retained (the fixture default), dispatch
        proceeds normally — proving the gate is scoped to retained locks
        specifically, not blocking everything."""
        mock_pipeline_lock_manager_auto.get_retained_reason.return_value = None

        create_test_issue(mock_github, 101, 'Code Review')

        with patch('services.project_monitor.ConfigManager', return_value=mock_config_manager), \
             patch('config.state_manager.state_manager', mock_state_manager), \
             patch('monitoring.observability.get_observability_manager', return_value=mock_observability[0]), \
             patch('services.pipeline_run.get_pipeline_run_manager') as mock_pipeline_mgr:

            mock_run = Mock()
            mock_run.id = 'run-456'
            mock_pipeline_mgr.return_value.get_or_create_pipeline_run.return_value = (mock_run, False)

            from services.project_monitor import ProjectMonitor
            monitor = ProjectMonitor(task_queue=mock_task_queue, config_manager=mock_config_manager)
            monitor.decision_events = mock_observability[1]
            monitor.get_issue_details = lambda repo, num, org: mock_github.get_issue(num)

            monitor.trigger_agent_for_status(
                project_name='test-project',
                board_name='dev',
                issue_number=101,
                status='Code Review',
                repository='test-repo'
            )

        assert mock_observability[1].emit_agent_routing_decision.called

"""
Tests for dev container recovery deduplication.

Verifies that queue_dev_environment_setup() is idempotent and that
process_task_integrated() raises NonRetryableAgentError (not generic Exception)
when a task is blocked by dev container validation.
"""

import os
import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock

# agents module requires Docker container environment
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)


@pytest.fixture
def mock_logger():
    """Create a mock logger."""
    logger = Mock()
    logger.log_warning = Mock()
    logger.info = Mock()
    logger.error = Mock()
    return logger


@pytest.fixture
def mock_task():
    """Create a mock task object."""
    task = Mock()
    task.id = "test-task-123"
    task.agent = "senior_software_engineer"
    task.project = "test-project"
    task.context = {
        'board': 'dev_board',
        'issue_number': 42,
    }
    return task


# ---------------------------------------------------------------------------
# queue_dev_environment_setup tests
# ---------------------------------------------------------------------------

class TestQueueDevEnvironmentSetup:
    """Tests for idempotent dev environment setup queuing."""

    def test_skips_when_already_in_progress(self, mock_logger):
        """When status is IN_PROGRESS, should skip queuing entirely."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            asyncio.get_event_loop().run_until_complete(
                queue_dev_environment_setup("test-project", mock_logger)
            )

            # Should NOT set status or enqueue
            mock_state.set_status.assert_not_called()
            mock_queue_instance.enqueue.assert_not_called()
            # Should log the skip
            assert any("skipping duplicate" in str(call) for call in mock_logger.info.call_args_list)

    def test_sets_in_progress_before_queuing(self, mock_logger):
        """When status is UNVERIFIED, should set IN_PROGRESS before enqueuing."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            # Track call order
            call_order = []
            mock_state.set_status.side_effect = lambda *a, **kw: call_order.append('set_status')
            mock_queue_instance.enqueue.side_effect = lambda *a, **kw: call_order.append('enqueue')

            from agents.orchestrator_integration import queue_dev_environment_setup

            asyncio.get_event_loop().run_until_complete(
                queue_dev_environment_setup("test-project", mock_logger)
            )

            # Should set status first, then enqueue
            mock_state.set_status.assert_called_once_with(
                "test-project",
                DevContainerStatus.IN_PROGRESS,
                image_name="test-project-agent:latest"
            )
            mock_queue_instance.enqueue.assert_called_once()
            assert call_order == ['set_status', 'enqueue']

    def test_queues_task_when_unverified(self, mock_logger):
        """When status is UNVERIFIED, should create and enqueue a setup task."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            asyncio.get_event_loop().run_until_complete(
                queue_dev_environment_setup("my-project", mock_logger)
            )

            mock_queue_instance.enqueue.assert_called_once()
            # Verify the enqueued task has the right agent
            enqueued_task = mock_queue_instance.enqueue.call_args[0][0]
            assert enqueued_task.agent == "dev_environment_setup"
            assert enqueued_task.project == "my-project"

    def test_consecutive_calls_only_queue_once(self, mock_logger):
        """First call sets IN_PROGRESS and queues; second call sees IN_PROGRESS and skips."""
        from services.dev_container_state import DevContainerStatus

        with patch('services.dev_container_state.dev_container_state') as mock_state, \
             patch('task_queue.task_manager.TaskQueue') as MockTaskQueue:

            mock_queue_instance = Mock()
            MockTaskQueue.return_value = mock_queue_instance

            from agents.orchestrator_integration import queue_dev_environment_setup

            # First call: UNVERIFIED -> sets IN_PROGRESS and queues
            mock_state.get_status.return_value = DevContainerStatus.UNVERIFIED
            asyncio.get_event_loop().run_until_complete(
                queue_dev_environment_setup("test-project", mock_logger)
            )
            assert mock_queue_instance.enqueue.call_count == 1

            # Second call: now IN_PROGRESS -> skips
            mock_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
            asyncio.get_event_loop().run_until_complete(
                queue_dev_environment_setup("test-project", mock_logger)
            )
            # Still only 1 enqueue total
            assert mock_queue_instance.enqueue.call_count == 1


# ---------------------------------------------------------------------------
# process_task_integrated: NonRetryableAgentError tests
# ---------------------------------------------------------------------------

class TestProcessTaskIntegratedValidation:
    """Tests that process_task_integrated raises NonRetryableAgentError on blocked tasks."""

    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_raises_non_retryable_when_needs_dev_setup(self, mock_task, mock_logger):
        """When validation fails with needs_dev_setup, should raise NonRetryableAgentError."""
        from agents.non_retryable import NonRetryableAgentError
        from agents.orchestrator_integration import process_task_integrated, validate_task_can_run

        with patch('agents.orchestrator_integration.validate_task_can_run', new_callable=AsyncMock) as mock_validate, \
             patch('agents.orchestrator_integration.queue_dev_environment_setup', new_callable=AsyncMock) as mock_queue, \
             patch('config.manager.config_manager') as mock_config, \
             patch('monitoring.observability.get_observability_manager') as mock_obs, \
             patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter_cls:

            mock_validate.return_value = {
                'can_run': False,
                'reason': 'Dev container not yet verified',
                'needs_dev_setup': True
            }
            mock_config.get_project_config.return_value = Mock(pipelines=[])
            mock_emitter_cls.return_value = Mock()

            state_manager = Mock()

            with pytest.raises(NonRetryableAgentError, match="Task blocked"):
                self._run(process_task_integrated(mock_task, state_manager, mock_logger))

            # Should still queue dev setup before raising
            mock_queue.assert_awaited_once_with(mock_task.project, mock_logger)

    def test_raises_non_retryable_when_blocked_no_dev_setup(self, mock_task, mock_logger):
        """When validation fails without needs_dev_setup, should still raise NonRetryableAgentError."""
        from agents.non_retryable import NonRetryableAgentError
        from agents.orchestrator_integration import process_task_integrated

        with patch('agents.orchestrator_integration.validate_task_can_run', new_callable=AsyncMock) as mock_validate, \
             patch('config.manager.config_manager') as mock_config, \
             patch('monitoring.observability.get_observability_manager') as mock_obs, \
             patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter_cls:

            mock_validate.return_value = {
                'can_run': False,
                'reason': 'Dev container setup is blocked',
                'needs_dev_setup': False
            }
            mock_config.get_project_config.return_value = Mock(pipelines=[])
            mock_emitter_cls.return_value = Mock()

            state_manager = Mock()

            with pytest.raises(NonRetryableAgentError, match="Task blocked"):
                self._run(process_task_integrated(mock_task, state_manager, mock_logger))

    def test_non_retryable_error_is_runtime_error(self):
        """NonRetryableAgentError is a RuntimeError so worker_pool can distinguish it."""
        from agents.non_retryable import NonRetryableAgentError
        assert issubclass(NonRetryableAgentError, RuntimeError)
        err = NonRetryableAgentError("test")
        assert isinstance(err, NonRetryableAgentError)
        assert isinstance(err, RuntimeError)

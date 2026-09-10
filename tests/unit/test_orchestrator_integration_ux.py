"""
Unit tests for orchestrator_integration.py UX improvements

Tests the enhanced error messaging for TaskValidationError scenarios
"""
import pytest
import os
from datetime import datetime, timedelta
from unittest.mock import Mock, AsyncMock, patch

# Skip these tests if not running in Docker (agents module requires Docker)
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)


@pytest.fixture
def mock_task():
    """Create a mock task object"""
    task = Mock()
    task.id = "test_task_123"
    task.agent = "senior_software_engineer"
    task.project = "test-project"
    task.context = {
        'issue_number': 123,
        'board': 'Development',
        'column': 'In Progress'
    }
    return task


@pytest.fixture
def mock_logger():
    """Create a mock logger"""
    logger = Mock()
    logger.log_warning = Mock()
    logger.info = Mock()
    logger.error = Mock()
    return logger


def _snapshot_from_stubs(mock_dev_state):
    """Keep a mocked dev_container_state's get_status_and_updated_at() (#171)
    consistent with the per-field stubs these tests set.

    validate_task_can_run() reads the status and its timestamp from ONE
    snapshot, so a staleness decision built from both cannot straddle an
    interleaving write. The tests still state the two separately; this derives
    the snapshot from them rather than making every case say it twice.
    """
    mock_dev_state.get_status_and_updated_at.side_effect = lambda project: (
        mock_dev_state.get_status.return_value,
        mock_dev_state.get_status_updated_at.return_value,
    )


@pytest.mark.asyncio
async def test_validation_error_message_includes_context(mock_task, mock_logger):
    """Test that validation error messages include helpful context"""
    # Import inside test to avoid Docker-only module issues
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state, \
         patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter, \
         patch('monitoring.observability.get_observability_manager'):

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        # Mock unverified status
        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED
        _snapshot_from_stubs(mock_dev_state)

        # Mock decision emitter
        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        # Import and call process_task_integrated
        from agents.orchestrator_integration import process_task_integrated

        from agents.non_retryable import NonRetryableAgentError

        with pytest.raises(NonRetryableAgentError) as exc_info:
            await process_task_integrated(mock_task, Mock(), mock_logger)

        # Verify error message includes helpful context
        assert "blocked" in str(exc_info.value).lower()

        # Verify emit_error_decision was called with enhanced message
        assert mock_decision_emitter.emit_error_decision.called
        call_args = mock_decision_emitter.emit_error_decision.call_args_list[0][1]

        # First call is error_encountered
        error_message = call_args['error_message']
        assert "Agent 'senior_software_engineer'" in error_message
        assert "Docker development environment" in error_message or "not yet verified" in error_message


@pytest.mark.asyncio
async def test_recovery_message_is_actionable(mock_task, mock_logger):
    """Test that recovery success messages are actionable"""
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state, \
         patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter, \
         patch('monitoring.observability.get_observability_manager'), \
         patch('agents.orchestrator_integration.queue_dev_environment_setup') as mock_queue:

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED
        _snapshot_from_stubs(mock_dev_state)

        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        from agents.orchestrator_integration import DevSetupQueueOutcome
        mock_queue.return_value = DevSetupQueueOutcome.QUEUED

        from agents.orchestrator_integration import process_task_integrated
        from agents.non_retryable import NonRetryableAgentError

        with pytest.raises(NonRetryableAgentError):
            await process_task_integrated(mock_task, Mock(), mock_logger)

        # Verify second call is error_recovered with actionable message
        assert mock_decision_emitter.emit_error_decision.call_count >= 2
        recovery_call = mock_decision_emitter.emit_error_decision.call_args_list[1][1]

        recovery_message = recovery_call['error_message']
        assert "setup has been queued" in recovery_message or "will be retried" in recovery_message
        assert recovery_call['success'] is True
        assert recovery_call['recovery_action'] == 'queue_dev_environment_setup'
        assert recovery_call['context']['auto_queued'] is True


@pytest.mark.asyncio
async def test_in_progress_message_is_clear(mock_task, mock_logger):
    """Test that in-progress status has clear messaging"""
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state, \
         patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter, \
         patch('monitoring.observability.get_observability_manager'), \
         patch('task_queue.task_manager.TaskQueue') as mock_task_queue_cls:

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
        _snapshot_from_stubs(mock_dev_state)
        mock_dev_state.get_status_updated_at.return_value = datetime.now()

        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        # Allow re-enqueue to succeed so we can inspect the right decision event
        mock_task_queue_cls.return_value.enqueue = Mock(return_value=True)

        from agents.orchestrator_integration import process_task_integrated
        from agents.non_retryable import NonRetryableAgentError

        with pytest.raises(NonRetryableAgentError) as exc_info:
            await process_task_integrated(mock_task, Mock(), mock_logger)

        # Verify exception message mentions in progress
        assert "in progress" in str(exc_info.value).lower()
        assert mock_task.project in str(exc_info.value)


@pytest.mark.asyncio
async def test_blocked_message_includes_troubleshooting(mock_task, mock_logger):
    """Test that blocked status includes troubleshooting guidance"""
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state, \
         patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter, \
         patch('monitoring.observability.get_observability_manager'):

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.BLOCKED
        _snapshot_from_stubs(mock_dev_state)

        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        from agents.orchestrator_integration import process_task_integrated
        from agents.non_retryable import NonRetryableAgentError

        with pytest.raises(NonRetryableAgentError):
            await process_task_integrated(mock_task, Mock(), mock_logger)

        # Verify error message includes file path for troubleshooting
        call_args = mock_decision_emitter.emit_error_decision.call_args_list[0][1]
        error_message = call_args['error_message']
        assert "state/dev_containers/" in error_message
        assert ".yaml" in error_message


@pytest.mark.asyncio
async def test_validate_task_can_run_messages():
    """Test validate_task_can_run returns user-friendly messages"""
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state:

        from agents.orchestrator_integration import validate_task_can_run
        from services.dev_container_state import DevContainerStatus

        mock_task = Mock()
        mock_task.project = "test-project"
        mock_task.agent = "test_agent"
        mock_logger = Mock()

        # Test VERIFIED status
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config
        mock_dev_state.get_status.return_value = DevContainerStatus.VERIFIED
        _snapshot_from_stubs(mock_dev_state)

        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is True
        assert "ready" in result['reason'].lower() or "verified" in result['reason'].lower()

        # Test UNVERIFIED status
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result['needs_dev_setup'] is True
        assert mock_task.project in result['reason']

        # Test IN_PROGRESS status (recently updated -- still within the staleness window)
        mock_dev_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
        mock_dev_state.get_status_updated_at.return_value = datetime.now()
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result.get('defer') is True
        assert "in progress" in result['reason'].lower()
        assert mock_task.project in result['reason']

        # Test BLOCKED status
        mock_dev_state.get_status.return_value = DevContainerStatus.BLOCKED
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert "blocked" in result['reason'].lower()
        assert "state/dev_containers/" in result['reason']

        # Test CHANGES_NEEDED status: must NOT set needs_dev_setup=True. The repair
        # cycle's env-rebuild sub-cycle owns retrying this project; if this returned
        # needs_dev_setup=True, an unrelated task validating during the brief window
        # before the sub-cycle's own reset could trigger a second, redundant
        # queue_dev_environment_setup() call racing the sub-cycle's own retry.
        mock_dev_state.get_status.return_value = DevContainerStatus.CHANGES_NEEDED
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result['needs_dev_setup'] is False


@pytest.mark.asyncio
async def test_validate_task_can_run_stale_in_progress_triggers_resetup():
    """
    Covers the fix for a permanent deadlock: IN_PROGRESS has no built-in timeout, so a
    setup/verifier task that dies without reaching a terminal status (or one whose output
    couldn't be parsed -- see DevEnvironmentVerifierAgent) used to defer every task for
    that project every 30s, forever, with no error surfaced. A status stuck IN_PROGRESS
    well past the time setup+verification normally takes should be treated as stale and
    trigger a fresh setup, not another silent defer.
    """
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state:

        from agents.orchestrator_integration import validate_task_can_run, STALE_IN_PROGRESS_MINUTES
        from services.dev_container_state import DevContainerStatus

        mock_task = Mock()
        mock_task.project = "test-project"
        mock_task.agent = "test_agent"
        mock_logger = Mock()

        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config
        mock_dev_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
        _snapshot_from_stubs(mock_dev_state)

        # Just under the threshold: still a normal defer.
        mock_dev_state.get_status_updated_at.return_value = (
            datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES - 1)
        )
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result.get('defer') is True
        assert result.get('needs_dev_setup') is not True

        # Well past the threshold: stale, should trigger a fresh setup instead of deferring.
        mock_dev_state.get_status_updated_at.return_value = (
            datetime.now() - timedelta(minutes=STALE_IN_PROGRESS_MINUTES + 5)
        )
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result.get('needs_dev_setup') is True
        assert result.get('defer') is not True
        assert mock_task.project in result['reason']

        # No updated_at recorded at all: fail safe to the normal defer, not a crash.
        mock_dev_state.get_status_updated_at.return_value = None
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result.get('defer') is True


async def _dispatch_and_capture_recovery_event(mock_task, mock_logger, outcome):
    """Drive process_task_integrated()'s needs_dev_setup branch with
    queue_dev_environment_setup() returning `outcome`, and hand back the second
    decision event -- the one that reports what the queue attempt did."""
    with patch('config.manager.config_manager') as mock_config, \
         patch('services.dev_container_state.dev_container_state') as mock_dev_state, \
         patch('monitoring.decision_events.DecisionEventEmitter') as mock_emitter, \
         patch('monitoring.observability.get_observability_manager'), \
         patch('agents.orchestrator_integration.queue_dev_environment_setup') as mock_queue:

        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED
        _snapshot_from_stubs(mock_dev_state)

        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        mock_queue.return_value = outcome

        from agents.orchestrator_integration import process_task_integrated
        from agents.non_retryable import NonRetryableAgentError

        with pytest.raises(NonRetryableAgentError):
            await process_task_integrated(mock_task, Mock(), mock_logger)

        return mock_decision_emitter.emit_error_decision.call_args_list[1][1]


@pytest.mark.asyncio
async def test_a_deferral_is_not_reported_as_a_queue(mock_task, mock_logger):
    """THE #169-review regression. queue_dev_environment_setup() returns
    without raising when it defers to a live holder of the dev_container_build
    lock -- an operator-triggered rebuild mid-`docker build`, say -- and this
    path emitted "setup has been queued ... auto_queued: True" anyway, once per
    30s poll for the whole build window. Everything reading decision events (the
    ES pattern indices, pipeline-recommendations, an operator triaging a stuck
    project) saw a stream of successful recoveries for work never enqueued."""
    from agents.orchestrator_integration import DevSetupQueueOutcome

    recovery_call = await _dispatch_and_capture_recovery_event(
        mock_task, mock_logger, DevSetupQueueOutcome.DEFERRED_BUILD_LOCK_HELD
    )

    assert recovery_call['context']['auto_queued'] is False
    assert recovery_call['context']['queue_outcome'] == 'deferred_build_lock_held'
    assert recovery_call['recovery_action'] == 'deferred_to_existing_dev_setup'
    assert "has been queued" not in recovery_call['error_message']


@pytest.mark.asyncio
async def test_a_failed_mark_is_reported_as_a_failure(mock_task, mock_logger):
    """The other non-queue outcome is not a deferral at all: nothing covers this
    project, and the next board poll has to retry. It must not read as success."""
    from agents.orchestrator_integration import DevSetupQueueOutcome

    recovery_call = await _dispatch_and_capture_recovery_event(
        mock_task, mock_logger, DevSetupQueueOutcome.FAILED_STATUS_WRITE
    )

    assert recovery_call['success'] is False
    assert recovery_call['context']['auto_queued'] is False
    assert recovery_call['context']['queue_outcome'] == 'failed_status_write'
    assert "could NOT be queued" in recovery_call['error_message']

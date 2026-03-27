"""
Unit tests for orchestrator_integration.py UX improvements

Tests the enhanced error messaging for TaskValidationError scenarios
"""
import pytest
import os
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


@pytest.mark.asyncio
async def test_validation_error_message_includes_context(mock_task, mock_logger):
    """Test that validation error messages include helpful context"""
    # Import inside test to avoid Docker-only module issues
    with patch('agents.orchestrator_integration.config_manager') as mock_config, \
         patch('agents.orchestrator_integration.dev_container_state') as mock_dev_state, \
         patch('agents.orchestrator_integration.DecisionEventEmitter') as mock_emitter, \
         patch('agents.orchestrator_integration.get_observability_manager'):

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        # Mock unverified status
        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED

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
    with patch('agents.orchestrator_integration.config_manager') as mock_config, \
         patch('agents.orchestrator_integration.dev_container_state') as mock_dev_state, \
         patch('agents.orchestrator_integration.DecisionEventEmitter') as mock_emitter, \
         patch('agents.orchestrator_integration.get_observability_manager'), \
         patch('agents.orchestrator_integration.queue_dev_environment_setup') as mock_queue:

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED

        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        mock_queue.return_value = AsyncMock()

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


@pytest.mark.asyncio
async def test_in_progress_message_is_clear(mock_task, mock_logger):
    """Test that in-progress status has clear messaging"""
    with patch('agents.orchestrator_integration.config_manager') as mock_config, \
         patch('agents.orchestrator_integration.dev_container_state') as mock_dev_state, \
         patch('agents.orchestrator_integration.DecisionEventEmitter') as mock_emitter, \
         patch('agents.orchestrator_integration.get_observability_manager'):

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.IN_PROGRESS

        mock_decision_emitter = Mock()
        mock_decision_emitter.emit_error_decision = Mock()
        mock_emitter.return_value = mock_decision_emitter

        from agents.orchestrator_integration import process_task_integrated
        from agents.non_retryable import NonRetryableAgentError

        with pytest.raises(NonRetryableAgentError) as exc_info:
            await process_task_integrated(mock_task, Mock(), mock_logger)

        # Verify error message mentions in progress
        call_args = mock_decision_emitter.emit_error_decision.call_args_list[0][1]
        error_message = call_args['error_message']
        assert "in progress" in error_message.lower()
        assert mock_task.project in error_message


@pytest.mark.asyncio
async def test_blocked_message_includes_troubleshooting(mock_task, mock_logger):
    """Test that blocked status includes troubleshooting guidance"""
    with patch('agents.orchestrator_integration.config_manager') as mock_config, \
         patch('agents.orchestrator_integration.dev_container_state') as mock_dev_state, \
         patch('agents.orchestrator_integration.DecisionEventEmitter') as mock_emitter, \
         patch('agents.orchestrator_integration.get_observability_manager'):

        # Setup mocks
        mock_agent_config = Mock()
        mock_agent_config.requires_dev_container = True
        mock_config.get_project_agent_config.return_value = mock_agent_config

        from services.dev_container_state import DevContainerStatus
        mock_dev_state.get_status.return_value = DevContainerStatus.BLOCKED

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

        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is True
        assert "ready" in result['reason'].lower() or "verified" in result['reason'].lower()

        # Test UNVERIFIED status
        mock_dev_state.get_status.return_value = DevContainerStatus.UNVERIFIED
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert result['needs_dev_setup'] is True
        assert mock_task.project in result['reason']

        # Test IN_PROGRESS status
        mock_dev_state.get_status.return_value = DevContainerStatus.IN_PROGRESS
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert "in progress" in result['reason'].lower()
        assert mock_task.project in result['reason']

        # Test BLOCKED status
        mock_dev_state.get_status.return_value = DevContainerStatus.BLOCKED
        result = await validate_task_can_run(mock_task, mock_logger)
        assert result['can_run'] is False
        assert "blocked" in result['reason'].lower()
        assert "state/dev_containers/" in result['reason']

"""
Tests for opaque UUID task IDs and execution_type propagation.

Validates that:
1. execute_agent produces valid UUID4 task_ids
2. execution_type is stored in task_context
3. Container name parsing handles both UUID and old-format task_ids
4. Web UI backward compat: repair detection with both execution_type and task_id prefix
"""

import os
import uuid
import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

if not os.path.isdir('/app'):
    SKIP_DOCKER = True
else:
    SKIP_DOCKER = False


UUID4_REGEX = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
)


@pytest.mark.skipif(SKIP_DOCKER, reason="Requires Docker container environment")
class TestExecuteAgentUUID:
    """Verify execute_agent produces valid UUID task_ids and propagates execution_type."""

    @pytest.fixture
    def executor(self):
        with patch('services.agent_executor.get_observability_manager') as mock_obs, \
             patch('services.agent_executor.PipelineFactory'), \
             patch('services.agent_executor.GitHubIntegration'):
            from services.agent_executor import AgentExecutor
            inst = AgentExecutor()
            inst.obs = mock_obs.return_value
            inst.obs.enabled = True
            inst.obs.emit_task_received = MagicMock()
            inst.obs.emit_agent_initialized = MagicMock(return_value='exec-id-123')
            inst.obs.emit_agent_completed = MagicMock()
            yield inst

    @pytest.mark.asyncio
    async def test_task_id_is_valid_uuid4(self, executor):
        """execute_agent should generate a valid UUID4 as task_id."""
        with patch.object(executor, 'factory') as mock_factory, \
             patch.object(executor, '_build_execution_context') as mock_build, \
             patch.object(executor, '_post_agent_output_to_github', new_callable=AsyncMock):

            mock_agent = MagicMock()
            mock_agent.agent_config = {}
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_factory.create_agent.return_value = mock_agent
            mock_build.return_value = {'use_docker': False}

            await executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context={},
                execution_type='test_type'
            )

            # Check that emit_task_received was called with a UUID task_id
            call_args = executor.obs.emit_task_received.call_args
            task_id = call_args[0][1]  # second positional arg
            assert UUID4_REGEX.match(task_id), f"task_id '{task_id}' is not a valid UUID4"

    @pytest.mark.asyncio
    async def test_execution_type_stored_in_task_context(self, executor):
        """execute_agent should store execution_type in task_context."""
        task_context = {'issue_number': 42}

        with patch.object(executor, 'factory') as mock_factory, \
             patch.object(executor, '_build_execution_context') as mock_build, \
             patch.object(executor, '_post_agent_output_to_github', new_callable=AsyncMock), \
             patch('services.agent_executor.config_manager') as mock_config:

            # Return None so workspace prep skips gracefully (no github config)
            mock_config.get_project_config.return_value = None

            mock_agent = MagicMock()
            mock_agent.agent_config = {}
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_factory.create_agent.return_value = mock_agent
            mock_build.return_value = {'use_docker': False}

            await executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context=task_context,
                execution_type='review_cycle'
            )

            assert task_context['execution_type'] == 'review_cycle'

    @pytest.mark.asyncio
    async def test_execution_type_forwarded_to_observability(self, executor):
        """execution_type should be forwarded to emit_task_received and emit_agent_initialized."""
        with patch.object(executor, 'factory') as mock_factory, \
             patch.object(executor, '_build_execution_context') as mock_build, \
             patch.object(executor, '_post_agent_output_to_github', new_callable=AsyncMock):

            mock_agent = MagicMock()
            mock_agent.agent_config = {}
            mock_agent.run_with_circuit_breaker = AsyncMock(return_value={'status': 'success'})
            mock_factory.create_agent.return_value = mock_agent
            mock_build.return_value = {'use_docker': False}

            await executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context={},
                execution_type='conversational'
            )

            # Check emit_task_received got execution_type
            recv_kwargs = executor.obs.emit_task_received.call_args[1]
            assert recv_kwargs.get('execution_type') == 'conversational'

            # Check emit_agent_initialized got execution_type
            init_kwargs = executor.obs.emit_agent_initialized.call_args[1]
            assert init_kwargs.get('execution_type') == 'conversational'

    @pytest.mark.asyncio
    async def test_old_task_id_prefix_param_raises_type_error(self, executor):
        """Passing the old task_id_prefix parameter should raise TypeError."""
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            await executor.execute_agent(
                agent_name='test_agent',
                project_name='test-project',
                task_context={},
                task_id_prefix="review_cycle"  # Old param name
            )


class TestContainerNameParsing:
    """Verify parse_container_name handles UUID and old-format task_ids."""

    @pytest.fixture
    def recovery(self):
        with patch('redis.Redis') as mock_redis:
            mock_redis.return_value.ping.return_value = True
            from services.agent_container_recovery import AgentContainerRecovery
            return AgentContainerRecovery(redis_client=mock_redis.return_value)

    def test_parse_uuid_task_id_with_known_project(self, recovery):
        """Should parse claude-agent-{project}-{uuid} when project is known."""
        test_uuid = str(uuid.uuid4())
        container_name = f"claude-agent-myproject-{test_uuid}"

        with patch('config.manager.config_manager') as mock_config:
            mock_config.list_projects.return_value = ['myproject']
            result = recovery.parse_container_name(container_name)

        assert result is not None
        assert result['project'] == 'myproject'
        assert result['task_id'] == test_uuid
        assert result['container_name'] == container_name

    def test_parse_uuid_task_id_with_hyphenated_project(self, recovery):
        """Should parse correctly when project name contains hyphens."""
        test_uuid = str(uuid.uuid4())
        container_name = f"claude-agent-my-cool-project-{test_uuid}"

        with patch('config.manager.config_manager') as mock_config:
            mock_config.list_projects.return_value = ['my-cool-project']
            result = recovery.parse_container_name(container_name)

        assert result is not None
        assert result['project'] == 'my-cool-project'
        assert result['task_id'] == test_uuid

    def test_parse_uuid_task_id_fallback_heuristic(self, recovery):
        """Should detect UUID format in fallback heuristic when project matching fails."""
        test_uuid = str(uuid.uuid4())
        container_name = f"claude-agent-unknown-project-{test_uuid}"

        with patch('config.manager.config_manager') as mock_config:
            mock_config.list_projects.return_value = []  # No known projects
            result = recovery.parse_container_name(container_name)

        assert result is not None
        assert result['project'] == 'unknown-project'
        assert result['task_id'] == test_uuid

    def test_parse_old_format_still_works(self, recovery):
        """Old format containers should still parse correctly."""
        container_name = "claude-agent-myproject-review_cycle_sw_eng_1234567890"

        with patch('config.manager.config_manager') as mock_config:
            mock_config.list_projects.return_value = ['myproject']
            result = recovery.parse_container_name(container_name)

        assert result is not None
        assert result['project'] == 'myproject'
        assert result['task_id'] == 'review_cycle_sw_eng_1234567890'

    def test_parse_invalid_name_returns_none(self, recovery):
        """Non-agent container names should return None."""
        result = recovery.parse_container_name("redis-server")
        assert result is None


class TestObservabilityEventExecutionType:
    """Verify execution_type appears in ObservabilityEvent serialization."""

    def test_execution_type_in_event_json(self):
        from monitoring.observability import ObservabilityEvent

        event = ObservabilityEvent(
            timestamp='2025-01-01T00:00:00Z',
            event_id='test-id',
            event_type='agent_initialized',
            agent='test_agent',
            task_id='some-uuid',
            project='test-project',
            data={'key': 'value'},
            execution_type='review_cycle'
        )

        import json
        parsed = json.loads(event.to_json())
        assert parsed['execution_type'] == 'review_cycle'

    def test_execution_type_defaults_to_empty(self):
        from monitoring.observability import ObservabilityEvent

        event = ObservabilityEvent(
            timestamp='2025-01-01T00:00:00Z',
            event_id='test-id',
            event_type='task_received',
            agent='test_agent',
            task_id='some-uuid',
            project='test-project',
            data={}
        )

        import json
        parsed = json.loads(event.to_json())
        assert parsed['execution_type'] == ''

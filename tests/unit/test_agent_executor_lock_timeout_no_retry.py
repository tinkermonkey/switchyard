"""
Regression tests for #148 (from #140 items 10/13): AgentExecutor's retry loop
must treat a project resource-lock timeout as a distinct, non-retryable outcome.

ProjectCheckoutLockTimeoutError / DevContainerBuildLockTimeoutError are raised
only AFTER the lock has already polled for its entire, deliberately generous
timeout (~3h for project_checkout, ~1h for dev_container_build — see
services/project_checkout_lock.py's "Blocking vs failing" section). Retrying
that like an ordinary transient agent bug re-runs the whole wait: at the
default retries=2 a single genuine contention event becomes ~9h of wall clock
before the pipeline finally fails, and the only thing that can change the
outcome in the meantime is a *different* holder finishing — which the next
board poll would pick up for free.

Without the exemption these tests fail by observing run_with_circuit_breaker
called 3 times instead of 1.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, patch, MagicMock

from services.agent_executor import AgentExecutor
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


async def _run_with_agent_error(agent_executor, error, retries=2):
    """
    Drive execute_agent() with an agent whose run_with_circuit_breaker always
    raises `error`, and return that mock so the caller can count attempts.

    No issue_number in task_context on purpose: that skips workspace
    preparation and work-execution-state recording, leaving only the retry
    loop under test. asyncio.sleep is stubbed so the 15s/30s retry backoff
    doesn't make the without-the-fix failure mode take a minute.
    """
    task_context = {'task_type': 'adhoc'}

    with patch('services.agent_executor.config_manager'), \
         patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(agent_executor.obs, 'emit_task_received'), \
         patch.object(agent_executor.obs, 'emit_agent_initialized'), \
         patch.object(agent_executor.obs, 'emit_agent_completed'), \
         patch('asyncio.sleep', new_callable=AsyncMock):

        mock_agent = MagicMock()
        mock_agent.run_with_circuit_breaker = AsyncMock(side_effect=error)
        mock_agent.agent_config = {'retries': retries}
        mock_create_agent.return_value = mock_agent

        with pytest.raises(Exception) as exc_info:
            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

        return mock_agent, exc_info.value


class TestLockTimeoutIsNotRetried:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_lock_timeout_is_attempted_exactly_once(self, agent_executor, error_cls):
        mock_agent, raised = await _run_with_agent_error(
            agent_executor, error_cls("could not acquire lock")
        )

        assert mock_agent.run_with_circuit_breaker.call_count == 1
        assert isinstance(raised, error_cls)

    @pytest.mark.asyncio
    async def test_lock_timeout_type_survives_the_executor(self, agent_executor):
        """The type has to reach the executor's caller intact too — project_monitor
        and the pipeline-run teardown make their own decisions on it."""
        _, raised = await _run_with_agent_error(
            agent_executor, ProjectCheckoutLockTimeoutError("busy")
        )

        assert type(raised) is ProjectCheckoutLockTimeoutError

    @pytest.mark.asyncio
    async def test_wrapped_lock_timeout_is_also_not_retried(self, agent_executor):
        """An agent that re-wraps the timeout (`raise Exception(...) from exc`)
        must not defeat the exemption — see services/resource_lock_errors.py."""
        inner = ProjectCheckoutLockTimeoutError("busy")
        wrapper = Exception(f"Business Analyst execution failed: {inner}")
        wrapper.__cause__ = inner

        mock_agent, _ = await _run_with_agent_error(agent_executor, wrapper)

        assert mock_agent.run_with_circuit_breaker.call_count == 1


class TestOrdinaryFailuresStillRetry:
    """Regression guard: the exemption must be scoped to lock timeouts only."""

    @pytest.mark.asyncio
    async def test_ordinary_agent_error_still_uses_all_attempts(self, agent_executor):
        mock_agent, _ = await _run_with_agent_error(
            agent_executor, RuntimeError("agent produced garbage"), retries=2
        )

        assert mock_agent.run_with_circuit_breaker.call_count == 3

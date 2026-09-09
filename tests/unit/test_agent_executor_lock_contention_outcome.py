"""
Regression tests for #148: AgentExecutor must not record a project resource-lock
timeout as outcome='failure'.

The retry-loop and circuit-breaker exemptions are only half the story. The outer
handler in execute_agent() classifies the exception a second time, and 'failure'
is the input to work_execution_tracker.count_consecutive_failures() — which
project_monitor.py turns into pipeline_run_manager.mark_failed() at
MAX_CONSECUTIVE_DISPATCH_FAILURES = 3. mark_failed() durably RETAINS the board's
pipeline lock, so every sibling issue on that board stops dispatching until an
operator runs scripts/release_lock.py. That is a strictly wider blast radius
than the per-agent circuit breaker the exemption already covers, reached by pure
contention.

'lock_contention' is the exact analogue of the 'frozen' outcome recorded for a
ClaudeCodeRateLimitError: not counted as a failure, still re-dispatchable by the
next board poll.

Without the fix these tests fail by observing outcome='failure' (and, for
dev_environment_setup, a dev container downgraded to UNVERIFIED).
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from services.agent_executor import AgentExecutor
from services.work_execution_state import work_execution_tracker
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError
from services.dev_container_state import DevContainerStatus


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


async def _run(agent_executor, error, agent_name='business_analyst'):
    """
    Drive execute_agent() through its outer failure handler with issue_number
    present (the key that gates outcome recording), and return the patched
    work_execution_tracker plus the dev_container_state module mock.

    skip_workspace_prep keeps epic-worktree resolution out of the picture; the
    handler's classification is the only thing under test.
    """
    task_context = {
        'issue_number': 900,
        'column': 'Development',
        'skip_workspace_prep': True,
    }

    tracker = MagicMock()
    tracker.load_state.return_value = {'execution_history': []}

    dev_state = MagicMock()

    with patch('services.agent_executor.config_manager'), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.dev_container_state.dev_container_state', dev_state), \
         patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(agent_executor.obs, 'emit_task_received'), \
         patch.object(agent_executor.obs, 'emit_agent_initialized'), \
         patch.object(agent_executor.obs, 'emit_agent_completed') as mock_completed, \
         patch('asyncio.sleep', new_callable=AsyncMock):

        mock_agent = MagicMock()
        mock_agent.run_with_circuit_breaker = AsyncMock(side_effect=error)
        mock_agent.agent_config = {'retries': 2}
        mock_create_agent.return_value = mock_agent

        with pytest.raises(Exception):
            await agent_executor.execute_agent(
                agent_name=agent_name,
                project_name='test-project',
                task_context=task_context
            )

        return tracker, dev_state, mock_completed


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


class TestLockTimeoutIsNotRecordedAsFailure:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_records_lock_contention_not_failure(self, agent_executor, error_cls):
        tracker, _, _ = await _run(agent_executor, error_cls("busy"))

        assert _recorded_outcomes(tracker) == ['lock_contention']

    @pytest.mark.asyncio
    async def test_wrapped_lock_timeout_is_also_classified_as_contention(self, agent_executor):
        inner = ProjectCheckoutLockTimeoutError("busy")
        wrapper = Exception(f"Business Analyst execution failed: {inner}")
        wrapper.__cause__ = inner

        tracker, _, _ = await _run(agent_executor, wrapper)

        assert _recorded_outcomes(tracker) == ['lock_contention']

    @pytest.mark.asyncio
    async def test_no_agent_failed_event_is_emitted(self, agent_executor):
        """emit_agent_completed(success=False) writes an AGENT_FAILED event and
        degrades the agent's measured success rate for a run it never attempted."""
        _, _, mock_completed = await _run(
            agent_executor, ProjectCheckoutLockTimeoutError("busy")
        )

        assert mock_completed.call_args_list == []


class TestConsecutiveContentionNeverReachesTheDispatchFailureCeiling:
    def test_lock_contention_does_not_count_as_a_dispatch_failure(self, tmp_path):
        """
        The direct analogue of test_repeated_lock_timeouts_never_open_the_circuit:
        N consecutive contention events must not reach
        project_monitor.MAX_CONSECUTIVE_DISPATCH_FAILURES.
        """
        from services.project_monitor import MAX_CONSECUTIVE_DISPATCH_FAILURES

        history = [
            {'column': 'Development', 'agent': 'business_analyst', 'outcome': 'lock_contention'}
            for _ in range(MAX_CONSECUTIVE_DISPATCH_FAILURES + 2)
        ]

        with patch.object(
            work_execution_tracker, 'load_state',
            return_value={'execution_history': history}
        ):
            count = work_execution_tracker.count_consecutive_failures(
                project_name='test-project', issue_number=900,
                column='Development', agent='business_analyst',
            )

        assert count == 0
        assert count < MAX_CONSECUTIVE_DISPATCH_FAILURES

    def test_ordinary_failures_still_count(self):
        """Regression guard: the exemption must be scoped to contention only."""
        history = [
            {'column': 'Development', 'agent': 'business_analyst', 'outcome': 'failure'}
            for _ in range(3)
        ]

        with patch.object(
            work_execution_tracker, 'load_state',
            return_value={'execution_history': history}
        ):
            count = work_execution_tracker.count_consecutive_failures(
                project_name='test-project', issue_number=900,
                column='Development', agent='business_analyst',
            )

        assert count == 3

    def test_lock_contention_is_still_re_dispatchable(self):
        """It must behave like 'frozen'/'failure' for should_execute_work(): the
        next board poll is the retry point."""
        state = {
            'execution_history': [{
                'column': 'Development',
                'agent': 'business_analyst',
                'outcome': 'lock_contention',
                'timestamp': '2025-01-01T00:00:00+00:00',
            }],
            'status_changes': [],
        }

        with patch.object(work_execution_tracker, 'load_state', return_value=state):
            should, reason = work_execution_tracker.should_execute_work(
                project_name='test-project', issue_number=900,
                column='Development', agent='business_analyst',
                trigger_source='manual_move',
            )

        assert should is True
        assert reason == 'retry_after_lock_contention'


class TestDevContainerStateIsNotDowngradedOnContention:
    @pytest.mark.asyncio
    async def test_dev_container_build_timeout_leaves_status_alone(self, agent_executor):
        """
        A DevContainerBuildLockTimeoutError means the build slot was never
        acquired, so nothing was built and nothing is broken. Writing UNVERIFIED
        would make validate_task_can_run() block EVERY agent with
        requires_dev_container on this project.
        """
        _, dev_state, _ = await _run(
            agent_executor,
            DevContainerBuildLockTimeoutError("build slot busy"),
            agent_name='dev_environment_setup',
        )

        # set_status(IN_PROGRESS) is written before dispatch; the assertion is
        # that no UNVERIFIED downgrade follows.
        statuses = [call.args[1] for call in dev_state.set_status.call_args_list]
        assert DevContainerStatus.UNVERIFIED not in statuses

    @pytest.mark.asyncio
    async def test_ordinary_setup_failure_still_resets_the_status(self, agent_executor):
        """Regression guard for the behavior the exemption is carved out of."""
        _, dev_state, _ = await _run(
            agent_executor,
            RuntimeError("Dockerfile.agent build failed"),
            agent_name='dev_environment_setup',
        )

        statuses = [call.args[1] for call in dev_state.set_status.call_args_list]
        assert DevContainerStatus.UNVERIFIED in statuses

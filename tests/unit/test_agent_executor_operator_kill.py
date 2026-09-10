"""
Regression tests for #160: an operator-initiated kill must not be recorded as an
agent failure.

POST /agents/kill/<container_name> routes through cancel_issue_work(), which
sets the cancellation signal FIRST and only then kills the container. What the
retry loop then sees is whatever the kill produced -- for a Docker-executed
agent, claude/docker_runner.py's NonRetryableAgentError for exit code 137/143.

Until #160 that was covered by accident: the agent wrappers re-wrapped
NonRetryableAgentError into a plain Exception, so the attempt was retried and
the NEXT iteration's top-of-loop cancellation check converted it into a
CancellationError. #160 stopped that re-wrap -- correctly, since retrying a
killed or OOM'd container just relaunches it -- which turns a deliberate stop
into a terminal 'failure' outcome unless the conversion is made explicit.

That matters because 'failure' is the input to
work_execution_tracker.count_consecutive_failures(), which project_monitor.py
turns into pipeline_run_manager.mark_failed() at
MAX_CONSECUTIVE_DISPATCH_FAILURES = 3: the board's pipeline lock is then durably
RETAINED and every sibling issue on it stops dispatching until an operator runs
scripts/release_lock.py. Three uses of the kill switch on one issue should not
cost that.

Without the fix these tests fail by observing outcome='failure' and a
NonRetryableAgentError rather than a CancellationError.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, MagicMock, patch

from agents.non_retryable import NonRetryableAgentError
from services.agent_executor import AgentExecutor
from services.cancellation import CancellationError


@pytest.fixture
def agent_executor():
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        return AgentExecutor()


async def _run(agent_executor, error, cancelled: bool, task_context=None):
    """Drive execute_agent() with run_with_circuit_breaker raising `error`.

    `cancelled` models the operator kill's real timing: the signal is NOT set
    when the attempt starts (so the top-of-loop check lets the agent run, as it
    does in production) and IS set by the time the failure is handled -- the
    kill lands mid-run. A test that set it up front would never reach the
    handler at all, since the top-of-loop check would refuse before dispatch.
    """
    if task_context is None:
        task_context = {
            'issue_number': 900,
            'column': 'Development',
            'skip_workspace_prep': True,
        }

    tracker = MagicMock()
    tracker.load_state.return_value = {'execution_history': []}

    dispatched = []

    def _is_cancelled(project, issue_number):
        if not dispatched:
            return False
        return cancelled

    signal = MagicMock()
    signal.is_cancelled.side_effect = _is_cancelled

    with patch('services.agent_executor.config_manager'), \
         patch('services.work_execution_state.work_execution_tracker', tracker), \
         patch('services.dev_container_state.dev_container_state', MagicMock()), \
         patch('services.cancellation.get_cancellation_signal', return_value=signal), \
         patch.object(agent_executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(agent_executor.obs, 'emit_task_received'), \
         patch.object(agent_executor.obs, 'emit_agent_initialized'), \
         patch.object(agent_executor.obs, 'emit_agent_completed'), \
         patch('asyncio.sleep', new_callable=AsyncMock):

        async def _dispatch(*args, **kwargs):
            dispatched.append(True)
            raise error

        mock_agent = MagicMock()
        mock_agent.run_with_circuit_breaker = AsyncMock(side_effect=_dispatch)
        mock_agent.agent_config = {'retries': 2}
        mock_create_agent.return_value = mock_agent

        with pytest.raises(Exception) as exc_info:
            await agent_executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context=task_context
            )

        return exc_info.value, tracker, mock_agent


def _recorded_outcomes(tracker):
    return [
        call.kwargs.get('outcome')
        for call in tracker.record_execution_outcome.call_args_list
    ]


class TestAKilledContainerOnACancelledIssueIsACancellation:

    @pytest.mark.asyncio
    async def test_a_137_exit_while_cancelled_is_converted(self, agent_executor):
        """The kill switch's own signal is what tells these apart -- the same
        signal the top-of-loop check reads, just consulted on the failure too."""
        raised, tracker, _ = await _run(
            agent_executor,
            NonRetryableAgentError(
                "Agent container was terminated by signal (exit_code=137): killed"
            ),
            cancelled=True,
        )

        assert isinstance(raised, CancellationError)
        assert _recorded_outcomes(tracker) == ['cancelled']

    @pytest.mark.asyncio
    async def test_the_original_failure_is_kept_as_the_cause(self, agent_executor):
        """So an operator reading the log can still see what the kill produced."""
        original = NonRetryableAgentError("exit_code=143")

        raised, _, _ = await _run(agent_executor, original, cancelled=True)

        assert raised.__cause__ is original

    @pytest.mark.asyncio
    async def test_it_is_not_retried(self, agent_executor):
        """The conversion must not cost an extra container launch -- which is the
        very thing dropping the re-wrap was for."""
        _, _, mock_agent = await _run(
            agent_executor, NonRetryableAgentError("exit_code=137"), cancelled=True
        )

        assert mock_agent.run_with_circuit_breaker.call_count == 1

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_while_cancelled_is_also_a_cancellation(
        self, agent_executor
    ):
        """cancel_issue_work() kills the container however it can; the failure
        that surfaces is not always a 137. The signal is the authority, not the
        exception type."""
        raised, tracker, _ = await _run(
            agent_executor, RuntimeError("container vanished"), cancelled=True
        )

        assert isinstance(raised, CancellationError)
        assert _recorded_outcomes(tracker) == ['cancelled']


class TestNothingElseIsReclassified:

    @pytest.mark.asyncio
    async def test_a_137_with_no_cancellation_stays_a_non_retryable_failure(
        self, agent_executor
    ):
        """An OOM kill is not an operator stop: it still terminates the run and
        still counts, it just must not be retried.

        An operator stop cannot arrive this way any more (#160 review): the kill
        endpoint recovers project/issue from the container's Docker labels when
        the Redis tracking hash is gone, and that hash's TTL now outlasts the
        longest configured agent timeout -- see
        tests/unit/test_operator_kill_attribution.py. Nor can docker_runner's own
        grace-period kill, which no longer raises this type at all."""
        original = NonRetryableAgentError(
            "Agent container was terminated by signal (exit_code=137): OOM"
        )

        raised, tracker, mock_agent = await _run(
            agent_executor, original, cancelled=False
        )

        assert raised is original
        assert _recorded_outcomes(tracker) == ['failure']
        assert mock_agent.run_with_circuit_breaker.call_count == 1

    @pytest.mark.asyncio
    async def test_a_project_scoped_dispatch_has_no_signal_to_consult(
        self, agent_executor
    ):
        """No 'issue_number' key at all -- a project-scoped dispatch (see
        normalize_issue_scope()). Key-presence, not a falsy value, is what gates
        the check, exactly as the top-of-loop one does."""
        original = NonRetryableAgentError("exit_code=137")

        raised, tracker, _ = await _run(
            agent_executor,
            original,
            cancelled=True,
            task_context={'column': 'Development', 'skip_workspace_prep': True},
        )

        assert raised is original
        assert _recorded_outcomes(tracker) == []

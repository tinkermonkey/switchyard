"""
The agent retry backoff is a production constant that the test harness zeroes.

Two things can silently break that arrangement, and each costs the suite 45
seconds a test without failing anything:

  1. The retry loop stops reading the module global -- someone inlines the
     number again, or binds it with `from ... import RETRY_BACKOFF_BASE_SECONDS`
     at the top of a function. Reassignment in tests/conftest.py then has no
     effect and the sleeps come back.
  2. The guard itself stops running, or is deleted as apparently pointless.

The other direction matters too: if the zero ever leaks into production, every
agent failure retries instantly, three times, against a circuit breaker that
has had no time to recover.

So this pins both ends -- what the source file declares, and what the process
running these tests actually has.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import ast
import inspect
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import services.agent_executor as agent_executor
from services.agent_executor import AgentExecutor


def _declared_default() -> int:
    """The literal in the source file, read without importing it.

    Reading the attribute would return whatever conftest last assigned, which
    is the thing this is trying to distinguish from.
    """
    source = Path(inspect.getsourcefile(agent_executor)).read_text()
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == 'RETRY_BACKOFF_BASE_SECONDS'
            for t in node.targets
        ):
            return ast.literal_eval(node.value)
    pytest.fail("services/agent_executor.py declares no RETRY_BACKOFF_BASE_SECONDS")


def test_production_still_backs_off():
    """The shipped default is a real wait, not the harness's zero."""
    assert _declared_default() == 15


def test_the_harness_zeroed_it():
    """tests/conftest.py's guard reached this process."""
    assert agent_executor.RETRY_BACKOFF_BASE_SECONDS == 0


@pytest.mark.asyncio
async def test_the_retry_loop_reads_the_global_it_is_given():
    """The load-bearing half: the loop honours a reassigned module global.

    Driven through execute_agent() rather than by reading the source, so
    inlining the number again fails here -- verified by mutation.

    A function-local `from services.agent_executor import
    RETRY_BACKOFF_BASE_SECONDS` passes this, and correctly so: it re-reads the
    module attribute on every call, so it honours the reassignment too. What
    would not is a binding taken once at module scope in a *different* module,
    which is why the value is kept where its only reader is.

    The agent raises a plain RuntimeError, which matches none of the retry
    exemptions, so all three attempts run and two waits happen -- 1 * base and
    2 * base.
    """
    with patch('services.agent_executor.get_observability_manager'), \
         patch('services.agent_executor.PipelineFactory'), \
         patch('services.agent_executor.GitHubIntegration'):
        executor = AgentExecutor()

    slept = []

    async def record(seconds):
        slept.append(seconds)

    with patch.object(agent_executor, 'RETRY_BACKOFF_BASE_SECONDS', 7), \
         patch('services.agent_executor.config_manager'), \
         patch.object(executor.factory, 'create_agent') as mock_create_agent, \
         patch.object(executor.obs, 'emit_task_received'), \
         patch.object(executor.obs, 'emit_agent_initialized'), \
         patch.object(executor.obs, 'emit_agent_completed'), \
         patch('asyncio.sleep', new=record):

        mock_agent = MagicMock()
        mock_agent.run_with_circuit_breaker = AsyncMock(
            side_effect=RuntimeError("agent produced garbage")
        )
        mock_agent.agent_config = {'retries': 2}
        mock_create_agent.return_value = mock_agent

        with pytest.raises(Exception):
            await executor.execute_agent(
                agent_name='business_analyst',
                project_name='test-project',
                task_context={'task_type': 'adhoc'},
            )

    assert slept == [7, 14]

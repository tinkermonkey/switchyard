"""
Regression tests for #160: the agent execute() wrappers must not re-wrap a
NonRetryableAgentError, and an operator-initiated kill must not be counted as an
agent failure.

claude/docker_runner.py raises NonRetryableAgentError for container exit codes
137/143 -- a container that was deliberately terminated (the OOM killer, or an
operator via POST /agents/kill/<container_name>). All three agent wrappers used
to erase that type with their generic `raise Exception(...) from exc`, so
services/agent_executor.py's and services/worker_pool.py's isinstance()
exemptions both went False and the task got its full 3 attempts -- each one
launching another container to be killed again, and an OOM reproducing on every
single one.

The half that needed a decision rather than a rider (see #160's own "decide what
the operator-kill path SHOULD do"): not re-wrapping is right for an OOM, but it
also removes the accidental protection the re-wrap gave the KILL SWITCH. The
kill endpoint sets the cancellation signal before killing the container, and the
re-wrapped exception used to be retried straight into agent_executor's
top-of-loop cancellation check, which converted it. Unwrapped, it would instead
propagate as a terminal failure -- recorded as outcome='failure', three of which
reach MAX_CONSECUTIVE_DISPATCH_FAILURES, mark_failed(), and a durably retained
board lock a human has to clear with scripts/release_lock.py. So the retry-loop
handler now converts ANY failure raised while that issue is cancelled into
CancellationError, explicitly.

Without the fix these tests fail: the raised exception is a plain Exception,
not the original type.

The two sibling riders #160 collects are covered where their own loops already
are: tests/unit/test_repair_cycle_lock_timeout.py for _run_tests(), and
tests/unit/services/test_worker_pool_lock_timeout_no_retry.py for the worker
pool. The operator-kill conversion is covered in
tests/unit/test_agent_executor_operator_kill.py.
"""

import os
import pytest

# agents/__init__.py requires Docker; skip outside that environment.
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, patch

from agents.base_maker_agent import MakerAgent
from agents.code_reviewer_agent import CodeReviewerAgent
from agents.documentation_editor_agent import DocumentationEditorAgent
from agents.non_retryable import NonRetryableAgentError


class _StubMakerAgent(MakerAgent):
    """Minimal concrete MakerAgent -- only execute()'s error handling is under test."""

    def __init__(self):
        super().__init__("business_analyst", agent_config={})

    @property
    def agent_display_name(self) -> str:
        return "Business Analyst"

    @property
    def agent_role_description(self) -> str:
        return "Stub role description."

    @property
    def output_sections(self):
        return ["Summary"]


_SIBLINGS = [
    (CodeReviewerAgent, "agents.code_reviewer_agent"),
    (DocumentationEditorAgent, "agents.documentation_editor_agent"),
]


async def _execute_maker_with_error(error):
    agent = _StubMakerAgent()
    with patch.object(agent._prompt_builder, "build", return_value="prompt"), \
         patch("agents.base_maker_agent.run_claude_code", new=AsyncMock(side_effect=error)):
        with pytest.raises(Exception) as exc_info:
            await agent.execute({"context": {}})
        return exc_info.value


async def _execute_sibling_with_error(agent_cls, module, error):
    agent = agent_cls(agent_config={})
    context = {"context": {
        "direct_prompt": "continue the review",
        "previous_stage_output": "prior stage output",
    }}
    with patch(f"{module}.run_claude_code", new=AsyncMock(side_effect=error)):
        with pytest.raises(Exception) as exc_info:
            await agent.execute(context)
        return exc_info.value


class TestNonRetryableIsPassedThroughUnwrapped:

    @pytest.mark.asyncio
    async def test_the_maker_wrapper_preserves_the_type(self):
        original = NonRetryableAgentError(
            "Agent container was terminated by signal (exit_code=137): OOM"
        )

        raised = await _execute_maker_with_error(original)

        assert raised is original
        assert isinstance(raised, NonRetryableAgentError)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_cls,module", _SIBLINGS)
    async def test_the_sibling_wrappers_preserve_the_type(self, agent_cls, module):
        original = NonRetryableAgentError(
            "Agent container was terminated by signal (exit_code=143): SIGTERM"
        )

        raised = await _execute_sibling_with_error(agent_cls, module, original)

        assert raised is original
        assert isinstance(raised, NonRetryableAgentError)

    @pytest.mark.asyncio
    async def test_an_ordinary_failure_is_still_wrapped(self):
        """The pass-through must stay scoped: an ordinary agent failure keeps the
        agent-identifying wrapper, and keeps getting its retries."""
        original = RuntimeError("claude produced garbage")

        raised = await _execute_maker_with_error(original)

        assert raised is not original
        assert type(raised) is Exception
        assert not isinstance(raised, NonRetryableAgentError)

"""
Regression tests for #148 (from #140 item 25): the agent execute() wrappers must
not re-wrap a project resource-lock timeout, a NonRetryableAgentError, or the
other types the dispatch layers recognise by isinstance().

run_claude_code() acquires the project_checkout and dev_container_build locks
around every agent execution and raises ProjectCheckoutLockTimeoutError /
DevContainerBuildLockTimeoutError when it can't get one. MakerAgent.execute()'s
generic `except Exception as exc: raise Exception(...) from exc` used to erase
that type before agent_executor.py's retry loop or services/circuit_breaker.py
could special-case it — the exact reason CancellationError and
ClaudeCodeRateLimitError are already re-raised unwrapped a few lines above.

All three agent wrappers (MakerAgent and its two PipelineStage siblings,
CodeReviewerAgent and DocumentationEditorAgent) carry the same handler and are
covered here together: services/resource_lock_errors.py can recognise a wrapped
timeout through its `__cause__` chain, but that only holds while every wrapper
remembers to write `from exc` — a two-word suffix no test pinned and neither
sibling's code mentions. NonRetryableAgentError has no such fallback at all:
docker_runner._raise_for_failed_exit_code() raises it for container exit codes
137/143 (OOM kill / SIGTERM), and erasing the type there gets an OOM-killed
container re-run three more times and counted against its circuit breaker.

Without the fix, these tests fail: the raised exception is a plain Exception,
not the original type.
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
from services.cancellation import CancellationError
from services.resource_lock_errors import is_lock_timeout_error
from services.project_checkout_lock import ProjectCheckoutLockTimeoutError
from services.dev_container_build_lock import DevContainerBuildLockTimeoutError


class _StubMakerAgent(MakerAgent):
    """Minimal concrete MakerAgent — only execute()'s error handling is under test."""

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


@pytest.fixture
def agent():
    return _StubMakerAgent()


async def _execute_with_error(agent, error):
    with patch.object(agent._prompt_builder, "build", return_value="prompt"), \
         patch("agents.base_maker_agent.run_claude_code", new=AsyncMock(side_effect=error)):
        with pytest.raises(Exception) as exc_info:
            await agent.execute({"context": {}})
        return exc_info.value


class TestLockTimeoutsArePassedThroughUnwrapped:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_lock_timeout_type_survives_execute(self, agent, error_cls):
        original = error_cls("could not acquire lock within 10900.0s")

        raised = await _execute_with_error(agent, original)

        assert raised is original
        assert type(raised) is error_cls


class TestNonRetryableIsPassedThroughUnwrapped:
    """
    docker_runner._raise_for_failed_exit_code() raises NonRetryableAgentError for
    container exit codes 137/143, and that raise lands directly in this wrapper
    (run_claude_code returns straight out of run_agent_in_container on the Docker
    branch). Unlike a lock timeout it is NOT chained through `from exc` by
    anything downstream, so if the wrapper erases it there is no __cause__ walk to
    recover it: both agent_executor's and worker_pool's isinstance() exemptions go
    False and an OOM-killed container is re-run to be killed again.
    """

    @pytest.mark.asyncio
    async def test_non_retryable_type_survives_execute(self, agent):
        original = NonRetryableAgentError("container OOM-killed exit_code=137")

        raised = await _execute_with_error(agent, original)

        assert raised is original
        assert isinstance(raised, NonRetryableAgentError)


class TestExistingExemptionsUnchanged:
    """Regression guard for the exemptions this one was modelled on."""

    @pytest.mark.asyncio
    async def test_cancellation_error_still_passed_through(self, agent):
        original = CancellationError("cancelled")
        raised = await _execute_with_error(agent, original)
        assert raised is original


class TestOrdinaryFailuresStillWrapped:
    """The pass-through must be scoped to lock timeouts only — an ordinary
    agent failure still gets the agent-identifying wrapper."""

    @pytest.mark.asyncio
    async def test_ordinary_error_is_still_wrapped_with_the_display_name(self, agent):
        original = RuntimeError("claude produced garbage")

        raised = await _execute_with_error(agent, original)

        assert raised is not original
        assert type(raised) is Exception
        assert "Business Analyst execution failed" in str(raised)
        assert raised.__cause__ is original


# ----------------------------------------------------------------------------
# The two sibling wrappers
# ----------------------------------------------------------------------------

_SIBLINGS = [
    (CodeReviewerAgent, "agents.code_reviewer_agent", "Code review failed"),
    (DocumentationEditorAgent, "agents.documentation_editor_agent",
     "Documentation review failed"),
]


async def _execute_sibling_with_error(agent_cls, module, error):
    """
    Drive a sibling agent's execute() with run_claude_code raising `error`.
    direct_prompt short-circuits prompt assembly, leaving only the error handling
    under test.
    """
    agent = agent_cls(agent_config={})
    context = {"context": {
        "direct_prompt": "continue the review",
        # DocumentationEditorAgent refuses to run without it; harmless elsewhere.
        "previous_stage_output": "prior stage output",
    }}

    with patch(f"{module}.run_claude_code", new=AsyncMock(side_effect=error)):
        with pytest.raises(Exception) as exc_info:
            await agent.execute(context)
        return exc_info.value


class TestSiblingWrappersCarryTheSameRule:
    """
    agents/code_reviewer_agent.py and agents/documentation_editor_agent.py carry
    the identical wrapper. Both are exercised through their real execute() rather
    than a hand-built `raise ... from exc`, so the invariant these tests pin is
    the one that matters — recognisability at the dispatch layer — not the
    incidental spelling of the raise statement.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_cls,module,_msg", _SIBLINGS)
    @pytest.mark.parametrize(
        "error_cls", [ProjectCheckoutLockTimeoutError, DevContainerBuildLockTimeoutError]
    )
    async def test_lock_timeout_type_survives(self, agent_cls, module, _msg, error_cls):
        original = error_cls("could not acquire lock within 10900.0s")

        raised = await _execute_sibling_with_error(agent_cls, module, original)

        assert raised is original
        assert is_lock_timeout_error(raised)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_cls,module,_msg", _SIBLINGS)
    async def test_non_retryable_type_survives(self, agent_cls, module, _msg):
        original = NonRetryableAgentError("container OOM-killed exit_code=137")

        raised = await _execute_sibling_with_error(agent_cls, module, original)

        assert raised is original

    @pytest.mark.asyncio
    @pytest.mark.parametrize("agent_cls,module,expected_message", _SIBLINGS)
    async def test_ordinary_error_is_still_wrapped(self, agent_cls, module, expected_message):
        original = RuntimeError("claude produced garbage")

        raised = await _execute_sibling_with_error(agent_cls, module, original)

        assert raised is not original
        assert type(raised) is Exception
        assert expected_message in str(raised)
        assert raised.__cause__ is original

"""
Regression tests for #148 (from #140 item 25): MakerAgent.execute() must not
re-wrap a project resource-lock timeout.

run_claude_code() acquires the project_checkout and dev_container_build locks
around every agent execution and raises ProjectCheckoutLockTimeoutError /
DevContainerBuildLockTimeoutError when it can't get one. MakerAgent.execute()'s
generic `except Exception as exc: raise Exception(...) from exc` used to erase
that type before agent_executor.py's retry loop or services/circuit_breaker.py
could special-case it — the exact reason CancellationError and
ClaudeCodeRateLimitError are already re-raised unwrapped a few lines above.

Without the fix, the first two tests fail: the raised exception is a plain
Exception, not the lock timeout type.
"""

import os
import pytest

# agents/__init__.py requires Docker; skip outside that environment.
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, patch

from agents.base_maker_agent import MakerAgent
from services.cancellation import CancellationError
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

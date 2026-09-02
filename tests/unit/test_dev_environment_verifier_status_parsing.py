"""
Tests for DevEnvironmentVerifierAgent's status-marker parsing.

Covers the bug where a verifier run that didn't produce a parseable
"### Status **APPROVED/BLOCKED**" marker silently left dev_container_state
untouched (whatever dev_environment_setup set it to -- IN_PROGRESS) while
still returning {"status": "success"}. Since IN_PROGRESS has no timeout of
its own, this permanently deadlocked the project's pipeline: every task
needing a dev container deferred itself every 30s, forever, with no error
ever surfaced.

Confirmed in production for both `codetoreum` (stuck >24h) and `phone-home`
(stuck ~45min before being caught) -- both hit this exact silent no-op path.
"""
import os
from unittest.mock import AsyncMock, patch

import pytest

# agents/__init__.py requires Docker; skip outside that environment.
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.dev_container_state import DevContainerStatus


def _task_context(project="test-project"):
    return {
        "context": {
            "project": project,
            "issue": {"title": "T", "body": "B"},
            "previous_stage_output": "setup completed",
        },
        "project": project,
    }


@pytest.mark.asyncio
class TestVerifierStatusParsing:
    async def test_approved_marks_verified(self):
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value="### Status\n**APPROVED**\n\nLooks good."),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            result = await agent.execute(_task_context())

        mock_state.set_status.assert_called_once()
        _, kwargs = mock_state.set_status.call_args
        assert kwargs["status"] == DevContainerStatus.VERIFIED
        assert result["status"] == "success"

    async def test_blocked_marks_blocked_with_issues(self):
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        output = "### Status\n**BLOCKED**\n\n#### Issues Found\nMissing Dockerfile.agent\n### Next"
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value=output),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            await agent.execute(_task_context())

        _, kwargs = mock_state.set_status.call_args
        assert kwargs["status"] == DevContainerStatus.BLOCKED
        assert "Missing Dockerfile.agent" in kwargs["error_message"]

    async def test_unparseable_output_marks_blocked_not_silent_success(self):
        """The bug: output with no status marker at all used to leave
        dev_container_state completely untouched."""
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value="I looked at the environment and it seems fine, no markers here."),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            await agent.execute(_task_context())

        mock_state.set_status.assert_called_once()
        _, kwargs = mock_state.set_status.call_args
        assert kwargs["status"] == DevContainerStatus.BLOCKED
        assert "could not parse" in kwargs["error_message"].lower()

    async def test_unrecognized_status_word_marks_blocked_not_silent_success(self):
        """The other silent-success gap: a status marker present, but with a
        word other than APPROVED/BLOCKED/CHANGES NEEDED."""
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value="### Status\n**PENDING**\n\nStill working on it."),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            await agent.execute(_task_context())

        mock_state.set_status.assert_called_once()
        _, kwargs = mock_state.set_status.call_args
        assert kwargs["status"] == DevContainerStatus.BLOCKED
        assert "PENDING" in kwargs["error_message"]

    async def test_unparseable_final_text_honors_status_agent_already_set(self):
        """The clobbering bug: when the final response text has no marker but
        the agent's own tool calls already resolved dev_container_state
        (e.g. it called set_status(VERIFIED) mid-session, then closed with a
        summary that happened to drop the "### Status" marker), the fallback
        must NOT overwrite that resolution with a forced BLOCKED.

        Regression for incident: phone-home #341 -- verifier confirmed the fix
        and set VERIFIED itself, then this exact fallback clobbered it back to
        BLOCKED because the closing text lacked the marker, forcing manual
        intervention on an already-passing rebuild."""
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value="```\n### Summary\nConfirmed resolved. Dev container state updated to VERIFIED.\n```"),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            mock_state.get_status.side_effect = [
                DevContainerStatus.IN_PROGRESS,  # snapshot taken before the session runs
                DevContainerStatus.VERIFIED,     # what the agent's own tool call already set
            ]
            result = await agent.execute(_task_context())

        mock_state.set_status.assert_not_called()
        assert result["status"] == "success"

    async def test_unparseable_final_text_still_blocks_when_nothing_changed(self):
        """Negative case for the fix above: if the agent never resolved
        dev_container_state itself (status is identical before and after,
        e.g. still IN_PROGRESS), the "could not parse" fallback must still
        force BLOCKED -- otherwise the project deadlocks forever, which is
        the original bug this whole module guards against."""
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value="I looked at the environment and it seems fine, no markers here."),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            mock_state.get_status.side_effect = [
                DevContainerStatus.IN_PROGRESS,
                DevContainerStatus.IN_PROGRESS,
            ]
            await agent.execute(_task_context())

        mock_state.set_status.assert_called_once()
        _, kwargs = mock_state.set_status.call_args
        assert kwargs["status"] == DevContainerStatus.BLOCKED
        assert "could not parse" in kwargs["error_message"].lower()

    async def test_changes_needed_marks_changes_needed_with_reason(self):
        r"""CHANGES NEEDED (used when a REQUIRED FIX could not be independently
        confirmed) must resolve to CHANGES_NEEDED, not fall through to BLOCKED.

        This also regression-tests the regex fix: the old `\*\*(\w+)\*\*` pattern
        could never match a two-word status like "CHANGES NEEDED" (the space isn't
        a \w character), so this status always fell through to the unparseable-output
        branch and was silently marked BLOCKED instead."""
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        output = (
            "### Status\n**CHANGES NEEDED**\n\n#### Issues Found\n"
            "Could not reproduce the failing test's exact build context.\n### Next"
        )
        with patch(
            "agents.dev_environment_verifier_agent.run_claude_code",
            new=AsyncMock(return_value=output),
        ), patch("agents.dev_environment_verifier_agent.dev_container_state") as mock_state:
            await agent.execute(_task_context())

        mock_state.set_status.assert_called_once()
        _, kwargs = mock_state.set_status.call_args
        assert kwargs["status"] == DevContainerStatus.CHANGES_NEEDED
        assert "Could not reproduce" in kwargs["error_message"]

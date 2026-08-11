"""
Tests that every agent type honors a caller-supplied task_context['direct_prompt']
override — the mechanism agent_executor.py's frozen-session resume fork (see
_apply_frozen_session_resume) relies on to send a short continuation prompt
instead of rebuilding the agent's full prompt from scratch when resuming a
Claude Code session with --resume.

Background: pr_code_reviewer and requirements_verifier have always checked
this manually in their own hand-written execute() overrides. Most other agents
(anything using MakerAgent.execute() unmodified, e.g. senior_software_engineer)
already get this for free via PromptContext.from_task_context() +
PromptBuilder.build()'s "if ctx.direct_prompt: return ctx.direct_prompt" check
(see test_prompt_builder.py::test_direct_prompt_passthrough for that layer).

The real gap was three agents that extend PipelineStage directly and construct
their own PromptContext + call a different PromptBuilder method
(build_reviewer_prompt / build_verifier_prompt) that never checked
direct_prompt at all: code_reviewer, documentation_editor,
dev_environment_verifier. This file proves those three are now fixed, and
includes a regression-guard confirming an already-correct MakerAgent-based
agent (senior_software_engineer) still works too.
"""
import os
from unittest.mock import AsyncMock, patch

import pytest

# agents/__init__.py requires Docker; skip outside that environment.
if not os.path.exists('/app/state/dev_containers'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)


CONTINUATION_PROMPT = "Please continue exactly where you left off."


@pytest.mark.asyncio
class TestMakerAgentAlreadyHonorsDirectPrompt:
    """Regression guard: confirms the baseline (already-working) mechanism
    for MakerAgent-derived agents that don't override execute() — proves the
    fix below didn't need to touch these, and that they keep working."""

    async def test_senior_software_engineer_honors_direct_prompt(self):
        from agents.senior_software_engineer_agent import SeniorSoftwareEngineerAgent

        agent = SeniorSoftwareEngineerAgent(agent_config={'agent_config': None})
        context = {
            'context': {
                'direct_prompt': CONTINUATION_PROMPT,
                'issue': {'title': 'Test', 'body': 'Test body'},
                'project': 'proj',
            },
            'completed_work': [],
        }

        with patch(
            'agents.base_maker_agent.run_claude_code',
            new=AsyncMock(return_value={'result': 'ok', 'session_id': None}),
        ) as mock_run:
            await agent.execute(context)

        sent_prompt = mock_run.call_args.args[0]
        assert sent_prompt == CONTINUATION_PROMPT


@pytest.mark.asyncio
class TestCodeReviewerAgentDirectPrompt:
    async def test_honors_direct_prompt(self):
        from agents.code_reviewer_agent import CodeReviewerAgent

        agent = CodeReviewerAgent()
        context = {
            'context': {
                'direct_prompt': CONTINUATION_PROMPT,
                'issue': {'title': 'Test', 'body': 'Test body'},
                'project': 'proj',
            },
        }

        with patch(
            'agents.code_reviewer_agent.run_claude_code',
            new=AsyncMock(return_value={'result': 'ok'}),
        ) as mock_run:
            await agent.execute(context)

        sent_prompt = mock_run.call_args.args[0]
        assert sent_prompt == CONTINUATION_PROMPT

    async def test_builds_full_prompt_when_no_direct_prompt(self):
        """Normal (non-resume) path must still build the full reviewer prompt."""
        from agents.code_reviewer_agent import CodeReviewerAgent

        agent = CodeReviewerAgent()
        context = {
            'context': {
                'issue': {'title': 'Test issue', 'body': 'Test body'},
                'project': 'proj',
                'change_manifest': 'diff --git a/foo.py b/foo.py',
            },
        }

        with patch(
            'agents.code_reviewer_agent.run_claude_code',
            new=AsyncMock(return_value={'result': 'ok'}),
        ) as mock_run:
            await agent.execute(context)

        sent_prompt = mock_run.call_args.args[0]
        assert sent_prompt != CONTINUATION_PROMPT
        assert len(sent_prompt) > len(CONTINUATION_PROMPT)


@pytest.mark.asyncio
class TestDocumentationEditorAgentDirectPrompt:
    async def test_honors_direct_prompt(self):
        from agents.documentation_editor_agent import DocumentationEditorAgent

        agent = DocumentationEditorAgent()
        context = {
            'context': {
                'direct_prompt': CONTINUATION_PROMPT,
                'issue': {'title': 'Test', 'body': 'Test body'},
                'project': 'proj',
                'previous_stage_output': 'Some docs to review',
            },
        }

        with patch(
            'agents.documentation_editor_agent.run_claude_code',
            new=AsyncMock(return_value={'result': 'ok'}),
        ) as mock_run:
            await agent.execute(context)

        sent_prompt = mock_run.call_args.args[0]
        assert sent_prompt == CONTINUATION_PROMPT

    async def test_still_requires_previous_stage_output_even_with_direct_prompt(self):
        """The required-field validation is about whether the task itself is
        well-formed, independent of which prompt gets sent — must still raise
        when previous_stage_output is missing, direct_prompt or not."""
        from agents.documentation_editor_agent import DocumentationEditorAgent

        agent = DocumentationEditorAgent()
        context = {
            'context': {
                'direct_prompt': CONTINUATION_PROMPT,
                'issue': {'title': 'Test', 'body': 'Test body'},
                'project': 'proj',
            },
        }

        with pytest.raises(Exception, match="previous stage output"):
            await agent.execute(context)


@pytest.mark.asyncio
class TestDevEnvironmentVerifierAgentDirectPrompt:
    async def test_honors_direct_prompt(self):
        from agents.dev_environment_verifier_agent import DevEnvironmentVerifierAgent

        agent = DevEnvironmentVerifierAgent()
        context = {
            'context': {
                'direct_prompt': CONTINUATION_PROMPT,
                'issue': {'title': 'Test', 'body': 'Test body'},
                'project': 'proj',
                'previous_stage_output': 'setup completed',
            },
            'project': 'proj',
        }

        with patch(
            'agents.dev_environment_verifier_agent.run_claude_code',
            new=AsyncMock(return_value="### Status\n**APPROVED**\n\nLooks good."),
        ) as mock_run, patch('agents.dev_environment_verifier_agent.dev_container_state'):
            await agent.execute(context)

        sent_prompt = mock_run.call_args.args[0]
        assert sent_prompt == CONTINUATION_PROMPT

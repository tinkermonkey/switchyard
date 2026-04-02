"""
PR Code Reviewer Agent

Uses pr-review-toolkit skill to review PR code quality.
Runs in Docker with project code mounted.

Prompt template lives in:
  prompts/content/agents/pr_code_reviewer/main_prompt.md
"""

from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
from claude.claude_integration import run_claude_code
from prompts import PromptBuilder, PromptContext
import logging

logger = logging.getLogger(__name__)


class PRCodeReviewerAgent(AnalysisAgent):
    """Review PR code quality using pr-review-toolkit skill."""

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("pr_code_reviewer", agent_config=agent_config)
        self._prompt_builder = PromptBuilder()

    @property
    def agent_display_name(self) -> str:
        return "PR Code Reviewer"

    @property
    def agent_role_description(self) -> str:
        return "I review PR code quality using automated analysis tools."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Critical Issues",
            "High Priority Issues",
            "Medium Priority Issues",
            "Low Priority / Nice-to-Have",
        ]

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task_context = context.get("context", {})
        pr_url = task_context.get("pr_url")
        direct_prompt = task_context.get("direct_prompt")

        if not pr_url:
            raise ValueError("pr_url is required for PR code review")

        if direct_prompt:
            prompt = direct_prompt
        else:
            ctx = PromptContext(
                mode="initial",
                agent_name="pr_code_reviewer",
                agent_display_name=self.agent_display_name,
                agent_role_description=self.agent_role_description,
                output_sections=self.output_sections,
                pr_url=pr_url,
            )
            prompt = self._prompt_builder.build_from_template(ctx)

        logger.info("Running PR code review for %s", pr_url)
        result = await run_claude_code(prompt, context)

        if isinstance(result, dict):
            result_text = result.get("result", "")
            if result.get("output_posted"):
                context["output_posted"] = True
        else:
            result_text = str(result)

        context["agent_output"] = result_text
        return context

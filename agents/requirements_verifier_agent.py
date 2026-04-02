"""
Requirements Verifier Agent

Verifies PR implementation against requirements and context.
Runs in Docker with project code mounted.

Prompt template lives in:
  prompts/content/agents/requirements_verifier/main_prompt.md
"""

from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
from claude.claude_integration import run_claude_code
from prompts import PromptBuilder, PromptContext
import logging

logger = logging.getLogger(__name__)


class RequirementsVerifierAgent(AnalysisAgent):
    """Verify PR against specific requirements context."""

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("requirements_verifier", agent_config=agent_config)
        self._prompt_builder = PromptBuilder()

    @property
    def agent_display_name(self) -> str:
        return "Requirements Verifier"

    @property
    def agent_role_description(self) -> str:
        return "I verify PR implementation against requirements and design specifications."

    @property
    def output_sections(self) -> List[str]:
        return ["Gaps Found", "Deviations", "Verified"]

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task_context = context.get("context", {})
        pr_url = task_context.get("pr_url")
        check_name = task_context.get("check_name")
        check_content = task_context.get("check_content")
        direct_prompt = task_context.get("direct_prompt")

        if not all([pr_url, check_name, check_content]):
            raise ValueError("pr_url, check_name, and check_content are required")

        if direct_prompt:
            prompt = direct_prompt
        else:
            ctx = PromptContext(
                mode="initial",
                agent_name="requirements_verifier",
                agent_display_name=self.agent_display_name,
                agent_role_description=self.agent_role_description,
                output_sections=self.output_sections,
                pr_url=pr_url,
                check_name=check_name,
                check_content=check_content,
            )
            prompt = self._prompt_builder.build_from_template(ctx)

        logger.info("Verifying PR against %s", check_name)
        result = await run_claude_code(prompt, context)

        if isinstance(result, dict):
            result_text = result.get("result", "")
            if result.get("output_posted"):
                context["output_posted"] = True
        else:
            result_text = str(result)

        context["agent_output"] = result_text
        return context

from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from prompts import PromptBuilder, PromptContext, IssueContext, ReviewCycleContext
import logging

logger = logging.getLogger(__name__)


class DocumentationEditorAgent(PipelineStage):
    """
    Senior Documentation Editor conducting comprehensive documentation review.

    Prompt content lives in:
      prompts/content/agents/documentation_editor/review_task.md
      prompts/content/agents/documentation_editor/format_initial.md
      prompts/content/agents/documentation_editor/format_rereviewing.md
      prompts/content/review_cycle/reviewer_*.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("documentation_editor", agent_config=agent_config)
        self._prompt_builder = PromptBuilder()

    async def _get_filter_instructions(self) -> str:
        """Load learned review filters (≥75% confidence) to inject into prompt."""
        try:
            from services.review_filter_manager import get_review_filter_manager
            filter_manager = get_review_filter_manager()
            filters = await filter_manager.get_agent_filters(
                agent_name="documentation_editor",
                min_confidence=0.75,
                active_only=True,
            )
            return filter_manager.build_filter_instructions(filters) if filters else ""
        except Exception as exc:
            logger.warning("Failed to load review filters (non-critical): %s", exc)
            return ""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task_context = context.get("context", {})
        previous_stage = task_context.get("previous_stage_output", "")
        if not previous_stage:
            raise Exception("Documentation Editor needs previous stage output from Technical Writer")

        issue_raw = task_context.get("issue", {})
        review_cycle_raw = task_context.get("review_cycle", {})

        review_cycle = None
        if review_cycle_raw:
            review_cycle = ReviewCycleContext(
                iteration=review_cycle_raw.get("iteration", 0),
                max_iterations=review_cycle_raw.get("max_iterations", 3),
                maker_agent=review_cycle_raw.get("maker_agent", ""),
                reviewer_agent=review_cycle_raw.get("reviewer_agent", ""),
                is_rereviewing=review_cycle_raw.get("is_rereviewing", False),
                post_human_feedback=review_cycle_raw.get("post_human_feedback", False),
                previous_review_feedback=review_cycle_raw.get("previous_review_feedback") or "",
                context_dir=task_context.get("review_cycle_context_dir"),
            )

        ctx = PromptContext(
            mode="initial",
            agent_name="documentation_editor",
            agent_display_name="Senior Documentation Editor",
            agent_role_description="",
            output_sections=[],
            project=task_context.get("project", ""),
            issue=IssueContext(
                title=issue_raw.get("title", "No title"),
                body=issue_raw.get("body", "No description"),
            ),
            # Embed the documentation to review directly in the context section
            previous_stage=previous_stage,
            review_cycle_context_dir=task_context.get("review_cycle_context_dir"),
            review_cycle=review_cycle,
        )

        filter_instructions = await self._get_filter_instructions()

        prompt = self._prompt_builder.build_reviewer_prompt(
            ctx,
            reviewer_title="Senior Documentation Editor",
            review_domain="documentation",
            filter_instructions=filter_instructions,
        )

        try:
            enhanced_context = context.copy()
            if self.agent_config and "agent_config" in self.agent_config:
                enhanced_context["agent_config"] = self.agent_config["agent_config"]
            if self.agent_config and "mcp_servers" in self.agent_config:
                enhanced_context["mcp_servers"] = self.agent_config["mcp_servers"]

            result = await run_claude_code(prompt, enhanced_context)

            if isinstance(result, dict):
                markdown_output = result.get("result", "")
                if result.get("output_posted"):
                    context["output_posted"] = True
            else:
                markdown_output = result if isinstance(result, str) else str(result)

            context["agent_output"] = markdown_output
            logger.info("Documentation review completed, output length: %d", len(markdown_output))
            return context

        except Exception as exc:
            raise Exception(f"Documentation review failed: {exc}") from exc

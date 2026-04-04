"""
Base Maker Agent Class

Provides a unified execution pattern for all maker agents (agents that create
or produce output).  Prompt assembly is fully delegated to PromptBuilder;
agent subclasses declare only their identity and optional capability flags.

Three execution modes are selected automatically from task context:
  initial   — first-time creation from requirements
  question  — conversational reply to a thread question
  revision  — update based on reviewer or human feedback
"""

from typing import Dict, Any, List
from abc import ABC, abstractmethod
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from prompts import PromptBuilder, PromptContext
import logging

logger = logging.getLogger(__name__)


class MakerAgent(PipelineStage, ABC):
    """
    Base class for all maker agents.

    Subclasses implement the four abstract properties and nothing else —
    all prompt logic lives in PromptBuilder.
    """

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None):
        super().__init__(agent_name, agent_config=agent_config)
        self.agent_name = agent_name
        self._prompt_builder = PromptBuilder()

    # ── Abstract properties — each subclass must provide these ───────────────

    @property
    @abstractmethod
    def agent_display_name(self) -> str:
        """Human-readable label, e.g. 'Business Analyst'."""

    @property
    def agent_role_description(self) -> str:
        """One-sentence role description injected at the top of every prompt.

        Loaded from prompts/content/agents/{agent_name}/role_description.md.
        """
        return self._prompt_builder._loader.agent_role_description(self.agent_name)

    @property
    @abstractmethod
    def output_sections(self) -> List[str]:
        """Ordered section names used in initial and revision prompts."""

    # ── Optional overrides ───────────────────────────────────────────────────

    @property
    def prompt_variant(self) -> str:
        """
        Template variant key.
          'standard'       — analysis/planning framing (default)
          'implementation' — code implementation framing
        """
        return "standard"

    @property
    def include_sub_issue_format(self) -> bool:
        """Set to True to append the sub-issue JSON format block (WorkBreakdownAgent)."""
        return False

    # ── Capability flags — override or set via agent_config ──────────────────

    def _capability_flags(self) -> Dict[str, bool]:
        """Return makes_code_changes and filesystem_write_allowed from config."""
        makes_code_changes = False
        filesystem_write_allowed = True
        if self.agent_config:
            if isinstance(self.agent_config, dict):
                makes_code_changes = self.agent_config.get("makes_code_changes", False)
                filesystem_write_allowed = self.agent_config.get("filesystem_write_allowed", True)
            elif hasattr(self.agent_config, "get"):
                cfg = self.agent_config.get("agent_config", {})
                makes_code_changes = cfg.get("makes_code_changes", False)
                filesystem_write_allowed = cfg.get("filesystem_write_allowed", True)
        return {
            "makes_code_changes": makes_code_changes,
            "filesystem_write_allowed": filesystem_write_allowed,
        }

    # ── Prompt context construction ───────────────────────────────────────────

    def _build_prompt_context(self, task_context: Dict[str, Any]) -> PromptContext:
        """
        Build the PromptContext for this invocation.

        Subclasses may override to add agent-specific fields (e.g.
        WorkBreakdownAgent sets sub-issue extras).
        """
        flags = self._capability_flags()
        return PromptContext.from_task_context(
            task_context,
            agent_name=self.agent_name,
            agent_display_name=self.agent_display_name,
            agent_role_description=self.agent_role_description,
            output_sections=self.output_sections,
            makes_code_changes=flags["makes_code_changes"],
            filesystem_write_allowed=flags["filesystem_write_allowed"],
            prompt_variant=self.prompt_variant,
            include_sub_issue_format=self.include_sub_issue_format,
        )

    # ── Main execution ────────────────────────────────────────────────────────

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute agent with automatic mode detection.

        Called by the orchestrator.  Builds a PromptContext, delegates prompt
        assembly to PromptBuilder, and runs the prompt via Claude Code SDK.
        """
        task_context = context.get("context", {})
        prompt_ctx = self._build_prompt_context(task_context)
        context["execution_mode"] = prompt_ctx.mode

        logger.info(
            "Agent %s executing in %s mode (variant=%s)",
            self.agent_name, prompt_ctx.mode, prompt_ctx.prompt_variant,
        )

        prompt = self._prompt_builder.build(prompt_ctx)

        try:
            enhanced_context = context.copy()
            if self.agent_config and "agent_config" in self.agent_config:
                enhanced_context["agent_config"] = self.agent_config["agent_config"]
            if self.agent_config and "mcp_servers" in self.agent_config:
                enhanced_context["mcp_servers"] = self.agent_config["mcp_servers"]
                logger.info(
                    "Added %d MCP servers to context", len(enhanced_context["mcp_servers"])
                )

            result = await run_claude_code(prompt, enhanced_context)

            if isinstance(result, dict):
                analysis_text = result.get("result", "")
                if result.get("output_posted"):
                    context["output_posted"] = True
                session_id = result.get("session_id")
                if session_id:
                    context["claude_session_id"] = session_id
                    logger.info("Stored Claude Code session_id: %s", session_id)
            else:
                analysis_text = result if isinstance(result, str) else str(result)

            context["agent_output"] = analysis_text
            context["completed_work"] = context.get("completed_work", []) + [
                f"{self.agent_display_name} analysis completed"
            ]
            return context

        except Exception as exc:
            raise Exception(f"{self.agent_display_name} execution failed: {exc}") from exc

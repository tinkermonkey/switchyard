from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class TechnicalWriterAgent(MakerAgent):
    """
    Technical Writer agent for documentation creation.

    Prompt content lives in:
      prompts/content/agents/technical_writer/guidelines.md
      prompts/content/agents/technical_writer/quality_standards.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("technical_writer", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Technical Writer"

    @property
    def agent_role_description(self) -> str:
        return (
            "I create clear, accurate technical documentation including API docs, user guides, "
            "tutorials, and knowledge base content following documentation best practices "
            "for clarity and completeness."
        )

    @property
    def output_sections(self) -> List[str]:
        return [
            "API Documentation",
            "User Documentation",
            "Developer Documentation",
            "System Documentation",
            "Operations Documentation",
        ]

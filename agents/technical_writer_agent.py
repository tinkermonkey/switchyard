from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class TechnicalWriterAgent(MakerAgent):
    """
    Technical Writer agent for documentation creation.

    Creates clear, accurate technical documentation following best practices.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("technical_writer", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Technical Writer"

    @property
    def agent_role_description(self) -> str:
        return "I create clear, accurate technical documentation including API docs, user guides, tutorials, and knowledge base content following documentation best practices for clarity and completeness."

    @property
    def output_sections(self) -> List[str]:
        return [
            "API Documentation",
            "User Documentation",
            "Developer Documentation",
            "System Documentation",
            "Operations Documentation"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Documentation is clear, accurate, and complete
- API documentation follows OpenAPI/Swagger standards
- User guides include step-by-step tutorials
- Code examples are functional and well-explained
- Documentation is maintained and version-controlled
"""

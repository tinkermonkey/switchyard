from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class SoftwareArchitectAgent(MakerAgent):
    """
    Software Architect agent for system architecture design.

    Focuses on scalability, maintainability, performance, and security.
    Creates Architecture Decision Records (ADRs) with trade-off analyses.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("software_architect", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Software Architect"

    @property
    def agent_role_description(self) -> str:
        return "I design system architectures considering scalability, maintainability, performance, and security. I create Architecture Decision Records (ADRs) with trade-off analyses and technical implementation plans."

    @property
    def output_sections(self) -> List[str]:
        return [
            "System Architecture",
            "Scalability Design",
            "Performance Architecture",
            "Security Architecture",
            "Maintainability Design",
            "Technology Decisions",
            "Architecture Decision Records (ADRs)",
            "Implementation Plan"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Architecture patterns are appropriate for the problem domain
- Scalability considerations are clearly defined
- Security best practices are incorporated
- Technology choices are justified with ADRs
- Design supports maintainability and testability
"""

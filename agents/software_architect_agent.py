from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
import logging

logger = logging.getLogger(__name__)


class SoftwareArchitectAgent(AnalysisAgent):
    """
    Software Architect agent for system architecture design.

    Prompt content lives in:
      prompts/content/agents/software_architect/guidelines.md
      prompts/content/agents/software_architect/quality_standards.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("software_architect", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Software Architect"

    @property
    def agent_role_description(self) -> str:
        return (
            "I design system architectures considering scalability, maintainability, "
            "performance, and security. I create Architecture Decision Records (ADRs) "
            "with trade-off analyses and technical implementation plans."
        )

    @property
    def output_sections(self) -> List[str]:
        return [
            "System Architecture",
            "Scalability Design",
            "Established Patterns",
            "Component Reuse",
            "Implementation Plan",
        ]

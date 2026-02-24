from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
import logging

logger = logging.getLogger(__name__)


class SoftwareArchitectAgent(AnalysisAgent):
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
            "Established Patterns",
            "Component Reuse",
            "Implementation Plan"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        """Override to provide architecture-specific guidelines"""
        return """
**Project-Specific Expert Agents**:
Check `/workspace/CLAUDE.md` for a "Specialized Sub-Agents" section. If any listed agent
matches your task domain (e.g., architect for project-specific architectural patterns,
guardian for boundary and antipattern enforcement), you MUST consult it via the Task tool
before producing your design. Do not design from general knowledge when a project-specific
agent exists for your task.
"""

    def get_quality_standards(self) -> str:
        return """
- Architecture patterns are appropriate for the problem domain and the application
- Scalability considerations are clearly defined
- Security best practices are incorporated
- Technology choices are justified with ADRs
- Design supports maintainability and testability
- No unnecessary complexity is introduced
- No over-engineering is present
- No new design patterns that are not important to the project
"""

from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class IdeaResearcherAgent(MakerAgent):
    """
    Idea Researcher agent for technical research and concept analysis.

    Explores solution landscapes, prior art, and architectural implications.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("idea_researcher", agent_config=agent_config)
        self.agent_config = agent_config or {}

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Idea Researcher"

    @property
    def agent_role_description(self) -> str:
        return "I conduct technical research and concept analysis, exploring solution landscapes, prior art, and architectural implications."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Problem Abstraction",
            "Solution Landscape Research",
            "Prior Art and Examples",
            "Capability Impact Analysis",
            "Technical Considerations"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """
## Research Guidelines

- Break down ideas into core abstract problems
- Research common solutions and approaches in the industry
- Find and document prior art (open source, papers, blog posts)
- Analyze how this would change or extend the current architecture
- Identify novel aspects vs. established patterns
- Document trade-offs between different solution strategies

Focus on understanding the landscape before implementation decisions.
"""

    def get_quality_standards(self) -> str:
        return """
- Research is thorough and covers multiple solution approaches
- Prior art is documented with specific examples and references
- Technical analysis is grounded in concrete examples
- Trade-offs are clearly articulated
- Capability impact is clearly defined
"""

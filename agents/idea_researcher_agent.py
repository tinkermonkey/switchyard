from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
import logging

logger = logging.getLogger(__name__)


class IdeaResearcherAgent(AnalysisAgent):
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
            "Executive Summary",
            "References and Prior Art",
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

**Important:** Your reports should returned as markdown content, don't create any files. Provide a succinct, insightful summary and analyses that demonstrate a progression of the idea.

"""

    def get_quality_standards(self) -> str:
        return """
- The idea is built out and progressed with research
"""

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
        return "I conduct business research and concept analysis, exploring solution landscapes, prior art, and architectural implications."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Executive Summary",
            "Idea Exploration",
            "Potential Directions"
            "References and Prior Art",
            "Technical Considerations"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """

Please explore and build out the idea through thorough research and analysis so that they can be better communicated and evaluated.

Please don't build requirements of designs yet, focus on research and analysis and enriching the ideas in the ticket.

**Important:** Your reports should returned as markdown content, don't create any files. Provide a succinct, insightful summary and analyses that demonstrate a progression of the idea.

"""

    def get_quality_standards(self) -> str:
        return """
- The idea is built out and progressed with research
"""

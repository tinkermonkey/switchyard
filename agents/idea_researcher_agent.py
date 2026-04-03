from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
import logging

logger = logging.getLogger(__name__)


class IdeaResearcherAgent(AnalysisAgent):
    """
    Idea Researcher agent for technical research and concept analysis.

    Prompt content lives in:
      prompts/content/agents/idea_researcher/guidelines.md
      prompts/content/agents/idea_researcher/quality_standards.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("idea_researcher", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Idea Researcher"

    @property
    def output_sections(self) -> List[str]:
        return [
            "Executive Summary",
            "Idea Exploration",
            "Potential Directions",
            "References and Prior Art",
            "Technical Considerations",
        ]

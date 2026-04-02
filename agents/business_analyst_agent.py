from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
import logging

logger = logging.getLogger(__name__)


class BusinessAnalystAgent(AnalysisAgent):
    """
    Business Analyst agent for requirements analysis and user story creation.

    Prompt content lives in:
      prompts/content/agents/business_analyst/guidelines.md
      prompts/content/agents/business_analyst/quality_standards.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("business_analyst", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Business Analyst"

    @property
    def agent_role_description(self) -> str:
        return "I analyse business requirements, create user stories, and ensure requirements are clear, complete, and testable."

    @property
    def output_sections(self) -> List[str]:
        return ["Executive Summary", "Functional Requirements", "User Stories"]

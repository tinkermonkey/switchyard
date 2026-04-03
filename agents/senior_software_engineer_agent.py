from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class SeniorSoftwareEngineerAgent(MakerAgent):
    """
    Senior Software Engineer agent for code implementation.

    Uses the 'implementation' prompt variant — a focused prompt framing
    with issue title + description rather than the analysis scaffold.

    Prompt content lives in:
      prompts/content/agents/senior_software_engineer/guidelines.md
      prompts/content/agents/senior_software_engineer/quality_standards.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("senior_software_engineer", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Senior Software Engineer"

    @property
    def output_sections(self) -> List[str]:
        return ["Implementation"]

    @property
    def prompt_variant(self) -> str:
        return "implementation"

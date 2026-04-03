from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class DevEnvironmentSetupAgent(MakerAgent):
    """
    Development Environment Setup agent for configuring development environments.

    Prompt content lives in:
      prompts/content/agents/dev_environment_setup/guidelines.md
      (no quality_standards file — not applicable for this agent)
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("dev_environment_setup", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Dev Environment Setup Specialist"

    @property
    def output_sections(self) -> List[str]:
        return [
            "Problem Analysis",
            "Files Modified",
            "Changes Made",
            "Testing & Verification",
            "Next Steps",
        ]

from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class DevEnvironmentSetupAgent(MakerAgent):
    """
    Development Environment Setup agent for configuring development environments.

    Creates setup scripts, configuration files, and documentation for dev environments.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("dev_environment_setup", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Dev Environment Setup Specialist"

    @property
    def agent_role_description(self) -> str:
        return "I create comprehensive development environment setup instructions, scripts, and configurations to ensure consistent, reproducible development environments."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Environment Requirements",
            "Setup Instructions",
            "Configuration Files",
            "Troubleshooting Guide",
            "Verification Steps"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Setup instructions are clear and complete
- All dependencies are documented
- Configuration is automated where possible
- Cross-platform compatibility is considered
- Troubleshooting steps cover common issues
"""

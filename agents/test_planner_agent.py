from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class TestPlannerAgent(MakerAgent):
    """
    Test Planner agent for test strategy development.

    Creates comprehensive test plans covering unit, integration, system, and acceptance testing.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("test_planner", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Test Planner"

    @property
    def agent_role_description(self) -> str:
        return "I develop comprehensive test strategies covering unit, integration, system, and acceptance testing, using equivalence partitioning, boundary analysis, and risk-based testing approaches."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Test Strategy Overview",
            "Test Case Design",
            "Test Automation Strategy",
            "Test Environment Planning",
            "Performance Testing Plan",
            "Security Testing Strategy"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Test coverage includes unit, integration, system, and acceptance levels
- Test cases use equivalence partitioning and boundary value analysis
- Risk-based testing prioritizes critical paths
- Automation strategy is defined for regression testing
- Performance and security testing are included
"""

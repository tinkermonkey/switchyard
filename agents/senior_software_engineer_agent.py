from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class SeniorSoftwareEngineerAgent(MakerAgent):
    """
    Senior Software Engineer agent for code implementation.

    Follows SOLID principles, DRY, KISS, and YAGNI with comprehensive test coverage.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("senior_software_engineer", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Senior Software Engineer"

    @property
    def agent_role_description(self) -> str:
        return "I implement clean code following SOLID principles, DRY, KISS, and YAGNI, with comprehensive test coverage (>80%), proper error handling, and maintainable architecture."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Core Implementation",
            "Code Quality",
            "Testing Implementation",
            "Documentation",
            "Performance Considerations"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Code follows SOLID principles (Single Responsibility, Open/Closed, Liskov Substitution, Interface Segregation, Dependency Inversion)
- Test coverage >80% with unit, integration, and edge case tests
- Proper error handling and logging
- Clear variable/function naming and code documentation
- Performance optimized for the use case
"""

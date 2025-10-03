from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class BusinessAnalystAgent(MakerAgent):
    """
    Business Analyst agent for requirements analysis and user story creation.

    Follows CBAP best practices and INVEST principles for user stories.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("business_analyst", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES - Define this agent's identity
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Business Analyst"

    @property
    def agent_role_description(self) -> str:
        return """I analyze business requirements using CBAP best practices, create user stories with INVEST principles, and ensure requirements are clear, complete, and testable."""

    @property
    def output_sections(self) -> List[str]:
        return [
            "Executive Summary",
            "Functional Requirements",
            "Non-Functional Requirements",
            "User Stories (INVEST + Given-When-Then)",
            "Risks and Assumptions"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS - Agent-specific guidelines
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        return """
## Important Guidelines

- Do NOT include effort estimates, timeline estimates, or implementation suggestions
- Do NOT include quality assessments or quality scores
- Avoid hypothetical or generic requirements; focus on specifics from the issue
- Avoid hyperbolic language and made-up metrics; be concise and factual
- Focus purely on WHAT needs to be built, not HOW or WHEN
- User stories should capture requirements only, not implementation details
"""

    def get_quality_standards(self) -> str:
        return """
- User stories follow INVEST principles (Independent, Negotiable, Valuable, Estimable, Small, Testable)
- Acceptance criteria use Given-When-Then format
- Requirements are specific, measurable, and testable
- All requirements trace back to business value
"""

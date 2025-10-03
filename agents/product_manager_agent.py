from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class ProductManagerAgent(MakerAgent):
    """
    Product Manager agent for strategic product planning.

    Uses RICE framework (Reach, Impact, Confidence, Effort) for feature prioritization.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("product_manager", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Product Manager"

    @property
    def agent_role_description(self) -> str:
        return "I provide strategic product planning using the RICE framework (Reach, Impact, Confidence, Effort) to prioritize features and align product strategy with market needs and stakeholder value."

    @property
    def output_sections(self) -> List[str]:
        return [
            "RICE Framework Analysis",
            "Feature Prioritization",
            "Market Alignment",
            "Stakeholder Impact",
            "Strategic Recommendations",
            "Requirements Review"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- RICE scores are calculated with clear rationale
- Feature prioritization aligns with business goals
- Market analysis is data-driven
- Stakeholder needs are balanced
- Strategic recommendations are actionable
"""

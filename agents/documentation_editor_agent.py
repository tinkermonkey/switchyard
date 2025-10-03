from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class DocumentationEditorAgent(MakerAgent):
    """
    Documentation Editor agent for refining and improving documentation.

    Ensures clarity, consistency, and completeness in all documentation.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("documentation_editor", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Documentation Editor"

    @property
    def agent_role_description(self) -> str:
        return "I review and refine documentation for clarity, accuracy, consistency, and completeness, ensuring it meets quality standards and serves the target audience effectively."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Editorial Review",
            "Clarity Improvements",
            "Consistency Check",
            "Completeness Assessment",
            "Final Recommendations"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Documentation is clear and concise
- Technical accuracy is verified
- Terminology is consistent throughout
- All sections are complete
- Target audience needs are met
"""

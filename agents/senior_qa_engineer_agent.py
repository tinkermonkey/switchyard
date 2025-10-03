from typing import Dict, Any, List
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class SeniorQAEngineerAgent(MakerAgent):
    """
    Senior QA Engineer agent for quality assurance execution.

    Performs comprehensive testing and production readiness assessment.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("senior_qa_engineer", agent_config=agent_config)

    # ==================================================================================
    # REQUIRED PROPERTIES
    # ==================================================================================

    @property
    def agent_display_name(self) -> str:
        return "Senior QA Engineer"

    @property
    def agent_role_description(self) -> str:
        return "I execute comprehensive quality assurance including integration testing, performance testing, end-to-end validation, and production readiness assessment."

    @property
    def output_sections(self) -> List[str]:
        return [
            "End-to-End Testing",
            "Integration Testing",
            "Performance Testing",
            "Security Testing",
            "Production Readiness Assessment"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- All critical user flows are tested end-to-end
- Integration points are validated
- Performance benchmarks are met
- Security vulnerabilities are identified and addressed
- Production deployment checklist is complete
"""

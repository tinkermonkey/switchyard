"""
Agent implementations for the Claude Code Orchestrator.

This module provides all the specialized agents that handle different phases
of the Software Development Lifecycle (SDLC) based on the vision document.
"""

# Import all agent classes for easy access
from .dev_environment_setup_agent import DevEnvironmentSetupAgent
from .idea_researcher_agent import IdeaResearcherAgent
from .business_analyst_agent import BusinessAnalystAgent
from .product_manager_agent import ProductManagerAgent
from .requirements_reviewer_agent import RequirementsReviewerAgent
from .software_architect_agent import SoftwareArchitectAgent
from .design_reviewer_agent import DesignReviewerAgent
from .test_planner_agent import TestPlannerAgent
from .test_reviewer_agent import TestReviewerAgent
from .work_breakdown_agent import WorkBreakdownAgent
from .senior_software_engineer_agent import SeniorSoftwareEngineerAgent
from .code_reviewer_agent import CodeReviewerAgent
from .senior_qa_engineer_agent import SeniorQAEngineerAgent
from .qa_reviewer_agent import QAReviewerAgent
from .technical_writer_agent import TechnicalWriterAgent
from .documentation_editor_agent import DocumentationEditorAgent

# Agent registry mapping agent names to classes
AGENT_REGISTRY = {
    "dev_environment_setup": DevEnvironmentSetupAgent,
    "idea_researcher": IdeaResearcherAgent,
    "business_analyst": BusinessAnalystAgent,
    "product_manager": ProductManagerAgent,
    "requirements_reviewer": RequirementsReviewerAgent,
    "software_architect": SoftwareArchitectAgent,
    "design_reviewer": DesignReviewerAgent,
    "test_planner": TestPlannerAgent,
    "test_reviewer": TestReviewerAgent,
    "work_breakdown_agent": WorkBreakdownAgent,
    "senior_software_engineer": SeniorSoftwareEngineerAgent,
    "code_reviewer": CodeReviewerAgent,
    "senior_qa_engineer": SeniorQAEngineerAgent,
    "qa_reviewer": QAReviewerAgent,
    "technical_writer": TechnicalWriterAgent,
    "documentation_editor": DocumentationEditorAgent,
}

# Export all agents and registry
__all__ = [
    "DevEnvironmentSetupAgent",
    "IdeaResearcherAgent",
    "BusinessAnalystAgent",
    "ProductManagerAgent",
    "RequirementsReviewerAgent",
    "SoftwareArchitectAgent",
    "DesignReviewerAgent",
    "TestPlannerAgent",
    "TestReviewerAgent",
    "WorkBreakdownAgent",
    "SeniorSoftwareEngineerAgent",
    "CodeReviewerAgent",
    "SeniorQAEngineerAgent",
    "QAReviewerAgent",
    "TechnicalWriterAgent",
    "DocumentationEditorAgent",
    "AGENT_REGISTRY",
]

def get_agent_class(agent_name: str):
    """
    Get agent class by name.

    Args:
        agent_name: The name of the agent (e.g., 'business_analyst')

    Returns:
        The agent class, or None if not found
    """
    return AGENT_REGISTRY.get(agent_name)

def list_available_agents():
    """
    Get list of all available agent names.

    Returns:
        List of agent names
    """
    return list(AGENT_REGISTRY.keys())
"""
Agent implementations for the Claude Code Orchestrator.

This module provides all the specialized agents that handle different phases
of the Software Development Lifecycle (SDLC) based on the vision document.
"""

# Import base classes
from .base_maker_agent import MakerAgent
from .base_analysis_agent import AnalysisAgent

# Import all agent classes for easy access
from .dev_environment_setup_agent import DevEnvironmentSetupAgent
from .dev_environment_verifier_agent import DevEnvironmentVerifierAgent
from .idea_researcher_agent import IdeaResearcherAgent
from .business_analyst_agent import BusinessAnalystAgent
from .requirements_reviewer_agent import RequirementsReviewerAgent
from .software_architect_agent import SoftwareArchitectAgent
from .work_breakdown_agent import WorkBreakdownAgent
from .senior_software_engineer_agent import SeniorSoftwareEngineerAgent
from .code_reviewer_agent import CodeReviewerAgent
from .technical_writer_agent import TechnicalWriterAgent
from .documentation_editor_agent import DocumentationEditorAgent

# Agent registry mapping agent names to classes
AGENT_REGISTRY = {
    "dev_environment_setup": DevEnvironmentSetupAgent,
    "dev_environment_verifier": DevEnvironmentVerifierAgent,
    "idea_researcher": IdeaResearcherAgent,
    "business_analyst": BusinessAnalystAgent,
    "requirements_reviewer": RequirementsReviewerAgent,
    "software_architect": SoftwareArchitectAgent,
    "work_breakdown_agent": WorkBreakdownAgent,
    "senior_software_engineer": SeniorSoftwareEngineerAgent,
    "code_reviewer": CodeReviewerAgent,
    "technical_writer": TechnicalWriterAgent,
    "documentation_editor": DocumentationEditorAgent,
}

# Export all agents and registry
__all__ = [
    "MakerAgent",
    "AnalysisAgent",
    "DevEnvironmentSetupAgent",
    "DevEnvironmentVerifierAgent",
    "IdeaResearcherAgent",
    "BusinessAnalystAgent",
    "RequirementsReviewerAgent",
    "SoftwareArchitectAgent",
    "WorkBreakdownAgent",
    "SeniorSoftwareEngineerAgent",
    "CodeReviewerAgent",
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
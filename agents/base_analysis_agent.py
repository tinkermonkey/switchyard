"""
Base Analysis Agent Class

Specialised base for maker agents that produce analysis, documentation, or
planning output but do NOT modify files in the workspace.

Key differences from MakerAgent:
  - makes_code_changes defaults to False
  - filesystem_write_allowed defaults to False
  - PromptBuilder automatically selects the restrictive output instructions

Agents that should inherit from AnalysisAgent:
  - business_analyst
  - idea_researcher
  - software_architect
  - work_breakdown_agent
  - pr_code_reviewer (reviews only)
  - requirements_verifier

Agents that should inherit from MakerAgent (they write files):
  - senior_software_engineer
  - technical_writer
  - dev_environment_setup
"""

from typing import Dict, Any
from agents.base_maker_agent import MakerAgent
import logging

logger = logging.getLogger(__name__)


class AnalysisAgent(MakerAgent):
    """
    Base class for agents that produce analysis/documentation without modifying files.

    Output is pure markdown posted to GitHub discussions/issues.
    Constructor enforces makes_code_changes=False and filesystem_write_allowed=False
    unless the agent config explicitly overrides them.
    """

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None):
        super().__init__(agent_name, agent_config=agent_config)

        # Enforce analysis-only defaults into the config dict so _capability_flags()
        # returns the correct values without requiring each subclass to set them.
        if agent_config:
            if isinstance(agent_config, dict):
                agent_config.setdefault("makes_code_changes", False)
                agent_config.setdefault("filesystem_write_allowed", False)
            elif "agent_config" in agent_config:
                inner = agent_config["agent_config"]
                if isinstance(inner, dict):
                    inner.setdefault("makes_code_changes", False)
                    inner.setdefault("filesystem_write_allowed", False)

"""
Base Analysis Agent Class

Specialized base class for maker agents that produce analysis, documentation, or planning
outputs but do NOT modify files in the workspace.

This class inherits from MakerAgent and provides:
- Consistent output formatting for analysis-only agents
- Default configuration indicating no filesystem writes
- Clear semantic distinction between code-writing and analysis agents

Agents that should inherit from AnalysisAgent:
- business_analyst
- idea_researcher  
- software_architect
- work_breakdown_agent
- documentation_editor (reviews only, doesn't write)

Agents that should inherit directly from MakerAgent (they write files):
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
    Base class for maker agents that produce analysis/documentation but don't modify files.

    These agents output markdown content that gets posted to GitHub discussions/issues
    but never create or modify files in the workspace.
    
    Key characteristics:
    - makes_code_changes: false (by default, can be overridden in agent config)
    - filesystem_write_allowed: false (by default, can be overridden in agent config)
    - Output is pure markdown text for GitHub posting
    - Must follow strict formatting guidelines (no preambles, no summary sections)
    """

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None):
        super().__init__(agent_name, agent_config=agent_config)
        
        # Set default configuration for analysis agents if not already set
        if agent_config:
            # Ensure these defaults are set if not explicitly configured
            if isinstance(agent_config, dict):
                agent_config.setdefault('makes_code_changes', False)
                agent_config.setdefault('filesystem_write_allowed', False)
            elif 'agent_config' in agent_config:
                # Handle nested agent_config structure
                inner_config = agent_config['agent_config']
                if isinstance(inner_config, dict):
                    inner_config.setdefault('makes_code_changes', False)
                    inner_config.setdefault('filesystem_write_allowed', False)

    def _get_output_instructions(self, mode: str = 'initial') -> str:
        """
        Override output instructions to be specific to analysis agents.
        
        Analysis agents always output markdown for GitHub comments and never write files.
        This method is called by the base MakerAgent's prompt builders.
        """
        if mode == 'question':
            return """
**IMPORTANT - OUTPUT FORMAT**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first.
- Output your answer as markdown text directly
- DO NOT create any files
- Use proper markdown formatting (headers, lists, code blocks)
- **NO INTERNAL DIALOG**: Do not include planning statements like "Let me research...", "I'll examine...". Just provide the answer.
"""

        return """

**IMPORTANT - OUTPUT FORMAT FOR ANALYSIS**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first. The project's CLAUDE.md file defines project-specific conventions and documentation requirements that take precedence over these general instructions.
- Output your analysis as markdown text directly in your response
- DO NOT create any files - this will be posted to GitHub as a comment
- DO NOT include project name, feature name, or date headers (this info is already in the discussion)
- **START IMMEDIATELY** with your first section heading (e.g., "## Executive Summary" or "## Problem Abstraction")
- **NO CONVERSATIONAL PREAMBLES**: Do NOT include statements like "Ok, I'll build...", "I'll analyze...", "Let me create...", etc.
- **NO SUMMARY SECTIONS**: Do NOT create a "Summary for GitHub Comment" section at the end - your entire output IS the comment
- **NO INTERNAL DIALOG**: Do NOT include planning statements like "Let me research...", "I'll examine...", "Now let me check..."
- **NO TOOL USAGE COMMENTARY**: Do not narrate what tools you're using or what you're searching for
- Focus on WHAT needs to be done, not HOW or WHEN
- Be specific and factual, avoid hypotheticals and hyperbole
- Use proper markdown formatting (headers, lists, code blocks)
"""

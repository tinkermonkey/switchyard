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
        return "I implement clean, well thought out code with proper error handling and maintainable architecture."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Implementation"
        ]

    # ==================================================================================
    # OPTIONAL CUSTOMIZATIONS
    # ==================================================================================

    def get_quality_standards(self) -> str:
        return """
- Proper error handling and logging
- Clear variable/function naming
"""

    def get_initial_guidelines(self) -> str:
        """Override to provide code implementation guidelines"""
        return """
Implement the code changes to meet the requirements specified.

**Project-Specific Expert Agents**:
Check `/workspace/CLAUDE.md` for a "Specialized Sub-Agents" section. If any listed agent
matches your task domain (e.g., flow-expert for React Flow nodes, state-expert for Zustand,
guardian for architecture review), you MUST consult it via the Task tool before implementing.
Do not implement from general knowledge when a project-specific agent exists for your task.

**For UI/Frontend Changes**:
- Use Playwright MCP to test your changes in the browser before completing
- Capture screenshots of key UI states for the PR
- Run accessibility checks (Playwright has built-in a11y testing)
- Verify responsive behavior on different viewport sizes
- Test form interactions and validation

**Important Implementation Guidelines**:
- Don't over-engineer. Implement only what is necessary to meet the requirements
- Focus on re-use of existing code, libraries and patterns
- Don't name files "phase 1", "phase 2", etc. Use descriptive names
- Don't create reports or documentation, your output should be code only
"""

    def _build_initial_prompt(self, task_context: Dict[str, Any]) -> str:
        """Override to provide code implementation prompt instead of analysis"""
        issue = task_context.get('issue', {})
        project = task_context.get('project', 'unknown')
        previous_stage = task_context.get('previous_stage_output', '')
        direct_prompt = task_context.get('direct_prompt', '')

        # Drop out if direct prompt provided
        if direct_prompt:
            return direct_prompt

        previous_stage_prompt = ""
        if previous_stage:
            previous_stage_prompt = f"""
## Previous Work and Feedback

The following is the complete history of agent outputs and feedback for this issue.
This includes outputs from ALL previous stages (design, testing, QA, etc.) and any
user feedback. If this issue was returned from testing or QA, pay special attention
to their feedback and address all issues they identified.

{previous_stage}

IMPORTANT: Review all feedback carefully and address every issue that is not already addressed.
"""

        prompt = f"""
You are a {self.agent_display_name}.

{self.agent_role_description}

**Issue Title**: {issue.get('title', 'No title')}

**Description**:
{issue.get('body', 'No description')}

{previous_stage_prompt}

"""
        return prompt

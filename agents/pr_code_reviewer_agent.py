"""
PR Code Reviewer Agent

Uses pr-review-toolkit skill to review PR code quality.
Runs in Docker with project code mounted.
"""

from typing import Dict, Any, List
from agents.base_analysis_agent import AnalysisAgent
from claude.claude_integration import run_claude_code
import logging

logger = logging.getLogger(__name__)


class PRCodeReviewerAgent(AnalysisAgent):
    """
    Review PR code quality using pr-review-toolkit skill.

    This agent runs in Docker with project code mounted.
    The pr-review-toolkit skill needs access to the local repository
    to fetch PR diffs and analyze code.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("pr_code_reviewer", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "PR Code Reviewer"

    @property
    def agent_role_description(self) -> str:
        return "I review PR code quality using automated analysis tools."

    @property
    def output_sections(self) -> List[str]:
        return [
            "Critical Issues",
            "High Priority Issues",
            "Medium Priority Issues",
            "Low Priority / Nice-to-Have",
        ]

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute PR code review using pr-review-toolkit skill.

        Expects task_context to contain:
        - pr_url: URL of the PR to review
        - direct_prompt: Pre-built prompt with instructions
        """
        task_context = context.get('context', {})
        pr_url = task_context.get('pr_url')
        direct_prompt = task_context.get('direct_prompt')

        if not pr_url:
            raise ValueError("pr_url is required for PR code review")

        # Use provided prompt or build default
        if not direct_prompt:
            direct_prompt = self._build_default_prompt(pr_url)

        logger.info(f"Running PR code review for {pr_url}")

        # Make ONE call to run_claude_code
        # This runs in Docker with project code mounted
        result = await run_claude_code(direct_prompt, context)

        # Extract result
        result_text = result.get('result', '') if isinstance(result, dict) else str(result)

        context['markdown_analysis'] = result_text
        context['raw_analysis_result'] = result_text

        return context

    def _build_default_prompt(self, pr_url: str) -> str:
        """Build default PR review prompt"""
        return f"""
You are a PR Code Reviewer. Review this pull request for code quality issues.

**CRITICAL**: Use the /pr-review-toolkit:review-pr skill for this task.

PR to review: {pr_url}

## Output Format

Structure your findings by severity:

### Critical Issues
- **[Finding Title]**: [Description]

### High Priority Issues
- **[Finding Title]**: [Description]

### Medium Priority Issues
- **[Finding Title]**: [Description]

### Low Priority / Nice-to-Have
- **[Finding Title]**: [Description]

If no issues at a severity level, write "None found".
"""

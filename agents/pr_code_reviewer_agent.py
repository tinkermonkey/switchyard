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
        if isinstance(result, dict):
            result_text = result.get('result', '')
            if result.get('output_posted'):
                context['output_posted'] = True
        else:
            result_text = str(result)

        context['markdown_analysis'] = result_text
        context['raw_analysis_result'] = result_text

        return context

    def _build_default_prompt(self, pr_url: str) -> str:
        """Build default PR review prompt"""
        return f"""
You are a PR Code Reviewer. Review this pull request for code quality issues.

**CRITICAL**: Use the /pr-review-toolkit:review-pr skill for this task.

PR to review: {pr_url}

## Instructions for Using pr-review-toolkit

**IMPORTANT - Task Tool Execution:**
- When invoking the pr-review-toolkit skill, it will launch specialized review agents
- **DO NOT** set `run_in_background: true` on ANY Task tool calls
- Each Task tool call should **BLOCK** until the subagent completes
- This ensures you receive the actual review results, not just a "task queued" confirmation
- You MUST wait for ALL review agents to complete before aggregating results
- The skill supports both sequential and parallel review modes internally

**Sequential Review (RECOMMENDED for this orchestrator context):**
- Launch agents one at a time, waiting for each to complete
- This ensures you collect all results before exiting
- Pattern: Task() blocks → collect result → Task() blocks → collect result → aggregate all

**If you accidentally use parallel/background tasks:**
- You MUST use the TaskOutput tool to retrieve results
- Example: `TaskOutput(task_id=<task_id>, block=true)` for each background task
- Only exit after collecting ALL TaskOutput results

**Expected workflow:**
1. Invoke the /pr-review-toolkit:review-pr skill
2. The skill will coordinate multiple specialized review agents
3. Wait for all agents to complete their analysis
4. Aggregate all findings from the specialized agents
5. Return consolidated review with all findings organized by severity

## Output Format

Structure your findings by severity:

### Critical Issues
- **[Finding Title]**: [Description]
  - Location: `file.py:line`
  - Recommendation: [Specific fix]

### High Priority Issues
- **[Finding Title]**: [Description]
  - Location: `file.py:line`
  - Recommendation: [Specific fix]

### Medium Priority Issues
- **[Finding Title]**: [Description]
  - Location: `file.py:line`
  - Recommendation: [Specific fix]

### Low Priority / Nice-to-Have
- **[Finding Title]**: [Description]
  - Location: `file.py:line`
  - Recommendation: [Specific fix]

If no issues at a severity level, write "None found".

**REMINDER**: Do not exit until you have aggregated results from ALL specialized review agents.
"""

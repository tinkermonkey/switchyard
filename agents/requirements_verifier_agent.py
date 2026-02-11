"""
Requirements Verifier Agent

Verifies PR implementation against requirements and context.
Runs in Docker with project code mounted.
"""

from typing import Dict, Any
from agents.base_analysis_agent import AnalysisAgent
from claude.claude_integration import run_claude_code
import logging

logger = logging.getLogger(__name__)


class RequirementsVerifierAgent(AnalysisAgent):
    """
    Verify PR against specific requirements context.

    This agent runs in Docker with project code mounted.
    It needs access to the repository to fetch PR diffs.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("requirements_verifier", agent_config=agent_config)

    @property
    def agent_display_name(self) -> str:
        return "Requirements Verifier"

    @property
    def agent_role_description(self) -> str:
        return "I verify PR implementation against requirements and design specifications."

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Verify PR against requirements context.

        Expects task_context to contain:
        - pr_url: URL of the PR
        - check_name: Name of the verification (e.g., "Parent Issue Requirements")
        - check_content: The requirements/context to verify against
        - direct_prompt: Pre-built prompt with instructions
        """
        task_context = context.get('context', {})
        pr_url = task_context.get('pr_url')
        check_name = task_context.get('check_name')
        check_content = task_context.get('check_content')
        direct_prompt = task_context.get('direct_prompt')

        if not all([pr_url, check_name, check_content]):
            raise ValueError("pr_url, check_name, and check_content are required")

        # Use provided prompt or build default
        if not direct_prompt:
            direct_prompt = self._build_default_prompt(pr_url, check_name, check_content)

        logger.info(f"Verifying PR against {check_name}")

        # Make ONE call to run_claude_code
        # This runs in Docker with project code mounted
        result = await run_claude_code(direct_prompt, context)

        # Extract result
        result_text = result.get('result', '') if isinstance(result, dict) else str(result)

        context['markdown_analysis'] = result_text
        context['raw_analysis_result'] = result_text

        return context

    def _build_default_prompt(self, pr_url: str, check_name: str, check_content: str) -> str:
        """Build default verification prompt"""
        # Truncate very long context
        max_len = 15000
        if len(check_content) > max_len:
            check_content = check_content[:max_len] + "\n\n[... truncated ...]"

        return f"""
You are a Requirements Verification Specialist.

## PR to Verify
{pr_url}

Review the PR diff to understand what was implemented.

## Context Source: {check_name}

The following is the original context that should be addressed by the PR:

---
{check_content}
---

## Your Task

1. Read the PR diff carefully
2. Compare against the context above
3. Identify any gaps or deviations

## Output Format

### Gaps Found
- **[Gap Title]**: [What was specified vs what was implemented or missing]

### Deviations
- **[Deviation Title]**: [What was specified vs what was actually done]

### Verified
- [Requirements that were correctly implemented]

Under "Gaps Found" and "Deviations", write "None found" if there are none.
"""

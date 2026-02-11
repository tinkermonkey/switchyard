from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class CodeReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("code_reviewer", agent_config=agent_config)

    async def _get_filter_instructions(self) -> str:
        """
        Get learned filter instructions to inject into review prompt.

        Returns filter guidance based on historical review outcomes.
        """
        try:
            from services.review_filter_manager import get_review_filter_manager

            filter_manager = get_review_filter_manager()

            # Get active filters for this agent with high confidence
            filters = await filter_manager.get_agent_filters(
                agent_name='code_reviewer',
                min_confidence=0.75,  # 75%+ confidence
                active_only=True
            )

            if not filters:
                return ""

            # Build filter instructions using manager's formatter
            filter_text = filter_manager.build_filter_instructions(filters)

            return filter_text

        except Exception as e:
            logger.warning(f"Failed to load review filters (non-critical): {e}")
            return ""

    def _get_output_format_instructions(self, is_rereviewing: bool = False) -> str:
        """Get output format instructions"""
        if is_rereviewing:
            resolved_section = """
### Previous Issues Status

**IMPORTANT**: Start by listing each issue from your previous review and its status:
- ✅ **[Previous Issue Title]** - RESOLVED: [Brief note on how it was addressed]
- ⚠️ **[Previous Issue Title]** - PARTIALLY RESOLVED: [What's still missing]
- ❌ **[Previous Issue Title]** - NOT RESOLVED: [What still needs to be done]

This section is **MANDATORY** in re-reviews. It shows you're tracking progress.

### New Issues Found (if any)

Only list NEW issues discovered in THIS revision that are:
- Critical problems introduced by the changes
- Directly related to how previous issues were addressed
- NOT just additional nice-to-have improvements
"""
        else:
            resolved_section = "### Issues Found"

        return f"""
**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

{resolved_section}

#### Critical (Must Fix)
**IMPORTANT**: Only use this category for issues that:
- Have critical security vulnerabilities (OWASP Top 10)
- Will cause data loss or corruption
- Break core functionality completely
- Violate fundamental requirements

Most code quality issues should be **High Priority**, not Critical.

List critical issues here, or write "None" if no critical security/data issues found.

#### High Priority (Should Fix)
- **[Issue Title]**: [Description and recommendation]

List important issues that must be addressed but are not critical security vulnerabilities.

**IMPORTANT**: Do not waste time on issues that are not critical or high priority.

### Summary
Brief summary of overall code quality and next steps
```

**Decision Criteria**:
- APPROVED: Code meets quality standards, no significant issues, ready for testing
- CHANGES NEEDED: Issues found that developer can address in revision
- BLOCKED: Critical security vulnerabilities or fundamental issues requiring human intervention

**Use CHANGES NEEDED unless there are truly un-addressable critical issues that need human decisions.**

REQUIRED: Include "**Status**: X" at the top for automation parsing."""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive code review with security, performance, and quality analysis"""

        # Extract from nested task context
        task_context = context.get('context', {})

        # Get the previous stage output (from maker agent)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            raise Exception("Code Reviewer needs previous stage output from maker agent")

        implementation = previous_stage
        review_cycle = task_context.get('review_cycle', {})
        issue = task_context.get('issue', {})

        # Get scoped git diff if available (for issues workspace)
        scoped_diff = task_context.get('scoped_git_diff', '')

        # Build iteration context for re-reviews
        iteration_context = ""
        is_rereviewing = False

        if review_cycle:
            iteration = review_cycle.get('iteration', 0)
            max_iterations = review_cycle.get('max_iterations', 3)
            maker_agent = review_cycle.get('maker_agent', 'unknown')
            is_rereviewing = review_cycle.get('is_rereviewing', False)
            post_human_feedback = review_cycle.get('post_human_feedback', False)

            if post_human_feedback:
                iteration_context = f"""

## Post-Escalation Review Update

You previously escalated this review due to **blocking issues** that required human intervention.

**The human has now responded with feedback.** Your task is to:

1. **Read the human feedback** in the discussion/issue context
2. **Incorporate their guidance** into your review assessment
3. **Update your review** based on their corrections, clarifications, or directions
4. **Post your UPDATED review** that reflects the human's input

**Important Guidelines**:
- If the human corrected your assessment, update your review accordingly
- If the human provided additional context, incorporate it into your evaluation
- Your updated review should be a **complete, standalone review** (not just changes)
- Set the appropriate status: APPROVED, CHANGES NEEDED, or BLOCKED (if still unresolved)

**Current Iteration**: {iteration}/{max_iterations}

"""
            elif is_rereviewing:
                iteration_context = f"""

## Review Cycle Context - Re-Review Mode

This is **Re-Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent.replace('_', ' ').title()} has revised their code based on your previous feedback.

**IMPORTANT - Review Scope**:
- You are reviewing ONLY the changes made by {maker_agent.replace('_', ' ').title()} in response to feedback
- DO NOT re-review code that was previously approved by other review cycles
- Focus on the commits made by {maker_agent.replace('_', ' ').title()} since the last review

**Your Task**: Verify previous issues are resolved. Be concise.

**Review Approach**:
1. Check if your PREVIOUS feedback items were addressed (don't re-raise if fixed)
2. Note any NEW issues discovered in the maker's changes
3. Make your decision

**Keep Feedback CONCISE**:
- 1-2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY it's important (developer already knows)
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

**Common Issues**:
- Adding capabilities that were not requested -> These need to be removed
- Markdown files with developer notes and implementation details -> These need to be removed
- Test and debug scripts that are not in the test folder tree -> These need to be evaluated and cleaned up
- Leaving in or commenting out code that was meant to be replaced or removed -> These need to be cleaned up
- Code with names including "Phase X" or "Step Y" -> These need to be renamed to meaningful names

**Escalation**: After {max_iterations} iterations, unresolved issues will escalate to human review.
"""
            else:
                iteration_context = f"""

## Review Cycle Context - Initial Review

This is **Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent.replace('_', ' ').title()} has implemented the code.

**Your Task**: Conduct a comprehensive code review of {maker_agent.replace('_', ' ').title()}'s work.

**After Review**: If issues found, maker will revise. Up to {max_iterations} review cycles.
"""

        # Get output format instructions
        format_instructions = self._get_output_format_instructions(is_rereviewing)

        # Inject learned review filters
        filter_instructions = await self._get_filter_instructions()

        # Build git diff section if available
        git_diff_section = ""
        if scoped_diff:
            git_diff_section = f"""

## Code Changes (Git Diff)

**IMPORTANT**: These are the ONLY changes you should review. Do not review unchanged code.

```diff
{scoped_diff}
```

**Review Focus**: Analyze ONLY the lines marked with `+` (additions) and `-` (deletions) in the diff above.
"""

        prompt = f"""
You are a **Senior Software Engineer** conducting comprehensive code review.

{iteration_context}

{filter_instructions}

## Original Requirements

**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}
{git_diff_section}
## Implementation to Review

{implementation}

## Your Review Task

Conduct a comprehensive code review covering:

**Code Quality Assessment**:
- Clean code practices (DRY, KISS, YAGNI)
- Code readability and maintainability
- Naming conventions and structure -> No "Phase X" of "Enhanced" or "Improved" etc
- Error handling completeness
- Removing commented-out or dead code
- Following project coding standards and norms
- Re-using existing libraries and modules where appropriate
- Avoiding unnecessary complexity
- Making new code consistent with existing code style

{format_instructions}

**IMPORTANT**:
- Output your review as **markdown text** directly in your response
- DO NOT create any files - this review will be posted to GitHub as a comment
- DO NOT include project name, feature name, or date headers
- Start directly with "### Status"
- Be specific and actionable in your feedback
- Categorize issues by severity correctly (most issues are High Priority, not Critical)
"""

        try:
            # Enhance context with agent config
            enhanced_context = context.copy()

            if self.agent_config and 'agent_config' in self.agent_config:
                enhanced_context['agent_config'] = self.agent_config['agent_config']
            if self.agent_config and 'mcp_servers' in self.agent_config:
                enhanced_context['mcp_servers'] = self.agent_config['mcp_servers']
                logger.info(f"Added {len(enhanced_context['mcp_servers'])} MCP servers to context")

            result = await run_claude_code(prompt, enhanced_context)

            # Store result as markdown
            # Handle both dict format (with tools_used metadata) and legacy string format
            if isinstance(result, dict):
                markdown_output = result.get('result', '')
            else:
                markdown_output = result if isinstance(result, str) else str(result)
            context['markdown_review'] = markdown_output
            context['raw_review_result'] = markdown_output

            logger.info(f"Code review completed, output length: {len(markdown_output)}")

            return context

        except Exception as e:
            raise Exception(f"Code review failed: {str(e)}")

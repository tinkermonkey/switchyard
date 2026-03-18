from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DocumentationEditorAgent(PipelineStage):
    """
    Documentation Editor agent for comprehensive documentation review.

    Reviews documentation for clarity, accuracy, consistency, and completeness
    with sophisticated filtering and revision tracking similar to code reviewer.
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("documentation_editor", agent_config=agent_config)

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
                agent_name='documentation_editor',
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
- Critical factual errors or dangerous examples
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
- Contain factually incorrect information that will mislead users
- Include broken links to critical resources (setup, API docs, security)
- Provide dangerous examples (security vulnerabilities, data loss)
- Fundamentally misrepresent how the system works

Most documentation issues should be **High Priority**, not Critical.

List critical issues here, or write "None" if no critical factual/safety issues found.

#### High Priority (Should Fix)
- **[Issue Title]**: [1-2 sentences describing what's wrong and what needs to change]

List important issues that must be addressed but are not critical safety/factual errors.

**IMPORTANT**: Keep feedback CONCISE (1-2 sentences per issue maximum).

### Summary
Brief summary of overall documentation quality and next steps
```

**Decision Criteria**:
- APPROVED: Documentation meets quality standards, no significant issues, ready for publication
- CHANGES NEEDED: Issues found that technical writer can address in revision
- BLOCKED: Critical factual errors or fundamental issues requiring human intervention

**Use CHANGES NEEDED unless there are truly un-addressable critical issues that need human decisions.**

REQUIRED: Include "**Status**: X" at the top for automation parsing."""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive documentation review"""

        # Extract from nested task context
        task_context = context.get('context', {})

        # Get the previous stage output (from technical writer)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            raise Exception("Documentation Editor needs previous stage output from Technical Writer")

        documentation = previous_stage
        review_cycle = task_context.get('review_cycle', {})
        issue = task_context.get('issue', {})

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
                previous_review_feedback = review_cycle.get('previous_review_feedback') or ''
                prior_feedback_section = f"""
**Your Previous Review Feedback**:
<previous_feedback>
{previous_review_feedback}
</previous_feedback>

""" if previous_review_feedback else ""
                iteration_context = f"""

## Review Cycle Context - Re-Review Mode

This is **Re-Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent.replace('_', ' ').title()} has revised their documentation based on your previous feedback.

{prior_feedback_section}**IMPORTANT - Review Scope**:
- You are reviewing ONLY the changes made by {maker_agent.replace('_', ' ').title()} in response to feedback
- DO NOT re-review documentation that was previously approved by other review cycles
- Focus on the sections revised by {maker_agent.replace('_', ' ').title()} since the last review

**Your Task**: Verify previous issues are resolved. Be concise.

**Review Approach**:
1. Check if your PREVIOUS feedback items (listed above) were addressed (don't re-raise if fixed)
2. Note any NEW issues discovered in the maker's changes
3. Make your decision

**Keep Feedback CONCISE**:
- 1-2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY it's important (writer already knows)
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

**Common Documentation Issues**:
- Placeholder content ("TBD", "Coming soon") -> Must be removed or completed
- Code examples that don't work when copy-pasted -> Must be tested and fixed
- Vague descriptions without concrete details -> Must add specifics
- Marketing fluff instead of technical substance -> Must be rewritten objectively
- Sections duplicating existing documentation -> Must be removed or linked
- Missing error handling/troubleshooting examples -> Must be added
- Broken links or incorrect cross-references -> Must be verified and fixed

**Escalation**: After {max_iterations} iterations, unresolved issues will escalate to human review.
"""
            else:
                iteration_context = f"""

## Review Cycle Context - Initial Review

This is **Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent.replace('_', ' ').title()} has created the documentation.

**Your Task**: Conduct a comprehensive documentation review of {maker_agent.replace('_', ' ').title()}'s work.

**After Review**: If issues found, maker will revise. Up to {max_iterations} review cycles.
"""

        # Get output format instructions
        format_instructions = self._get_output_format_instructions(is_rereviewing)

        # Inject learned review filters
        filter_instructions = await self._get_filter_instructions()

        prompt = f"""
You are a **Senior Documentation Editor** conducting comprehensive documentation review.

{iteration_context}

{filter_instructions}

## Original Requirements

**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}

## Documentation to Review

{documentation}

## Your Review Task

Conduct a comprehensive documentation review covering:

**Content Quality Assessment**:
- Factual accuracy and technical correctness
- Clarity and readability for target audience
- Completeness (all required sections present)
- Code examples are runnable and include expected output
- Error cases and troubleshooting guidance included
- Consistency in terminology and structure
- Active voice and concise sentences (under 25 words)
- No placeholder content ("TBD", "Coming soon")
- No marketing language ("revolutionary", "seamless")
- Proper cross-references (links verified, not broken)

**Structure Assessment**:
- Logical information flow (most important first)
- Descriptive section names (not generic "Overview", "Details")
- One concept per section
- Inline examples (not separate "Examples" section)

**Common Anti-Patterns to Check**:
- Explaining obvious concepts ("Git is a version control system")
- Speculative future sections without current content
- Duplicating content that exists elsewhere (should link instead)
- Documenting implementation details users don't need
- Generic introductions that don't add value

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
                if result.get('output_posted'):
                    context['output_posted'] = True
            else:
                markdown_output = result if isinstance(result, str) else str(result)
            context['markdown_review'] = markdown_output
            context['raw_review_result'] = markdown_output

            logger.info(f"Documentation review completed, output length: {len(markdown_output)}")

            return context

        except Exception as e:
            raise Exception(f"Documentation review failed: {str(e)}")

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

List critical issues here, or write "None" if no critical issues found.

#### High Priority (Should Fix)
- **[Issue Title]**: [Description and recommendation]

List important issues that are in scope for this PR and must be addressed by the developer.
Write "None" if no high-priority issues found.

#### Advisory (Out of Scope / FYI)
- **[Issue Title]**: [Brief note — pre-existing, future work, or cosmetic]

Use this tier for observations that are real but do NOT need to be fixed in this PR:
pre-existing gaps, future enhancements, minor cosmetic preferences, or issues that are
explicitly out of scope per the requirements. Do NOT escalate these to High Priority just
because they exist. Write "None" if nothing to note.

### Summary
Brief summary of overall code quality and next steps
```

**Status Decision Rules** (enforced strictly):
- **APPROVED**: No Critical items AND no High Priority items. Advisory items alone do not block approval.
- **CHANGES NEEDED**: One or more Critical or High Priority items exist that the developer must address.
- **BLOCKED**: Issues exist that cannot be resolved by the developer alone (security escalation, fundamental requirement conflict, needs human decision).

**CRITICAL RULE**: If you list ANY item under "High Priority (Should Fix)", you MUST set status to **CHANGES NEEDED**.
An APPROVED status with High Priority items is contradictory and invalid.
If an issue is real but out of scope for this PR, put it under **Advisory** — not High Priority.

**Use CHANGES NEEDED unless there are truly un-addressable critical issues that need human decisions.**

REQUIRED: Include "### Status" followed by the bold status on the next line for automation parsing."""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute comprehensive code review with security, performance, and quality analysis"""

        # Extract from nested task context
        task_context = context.get('context', {})

        review_cycle = task_context.get('review_cycle', {})
        issue = task_context.get('issue', {})

        # Get change manifest if available (for issues workspace)
        change_manifest = task_context.get('change_manifest', '')

        # File-based context dir (mounted into container at /workspace/review_cycle_context)
        context_dir = task_context.get('review_cycle_context_dir')

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
                # When file-based context is available, point to the previous feedback file
                # instead of embedding the full text in the prompt
                if context_dir and iteration and iteration > 1:
                    prev_feedback_file = f'review_feedback_{iteration - 1}.md'
                    prior_feedback_section = f"""
**Your Previous Review Feedback**: read `/workspace/review_cycle_context/{prev_feedback_file}`

"""
                else:
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

**Maker Agent**: {maker_agent.replace('_', ' ').title()} has revised their code based on your previous feedback.

{prior_feedback_section}**IMPORTANT - Review Scope**:
- You are reviewing ONLY the changes made by {maker_agent.replace('_', ' ').title()} in response to feedback
- DO NOT re-review code that was previously approved by other review cycles
- Focus on the commits made by {maker_agent.replace('_', ' ').title()} since the last review

**Your Task**: Verify previous issues are resolved. Be concise.

**Review Approach**:
1. Check if your PREVIOUS feedback items (listed above) were addressed (don't re-raise if fixed)
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

        # Build change manifest / context section
        if context_dir and review_cycle:
            # File-based context: point reviewer to the mounted directory
            maker_file = f'maker_output_{iteration}.md' if iteration else 'maker_output_1.md'
            prev_feedback_note = ''
            if is_rereviewing and iteration and iteration > 1:
                prev_feedback_file = f'review_feedback_{iteration - 1}.md'
                prev_feedback_note = (
                    f'- **`{prev_feedback_file}`** — your previous feedback; '
                    f'verify those issues are now resolved\n'
                )
            context_files_section = f"""

## Review Cycle Context Files

All context files are at `/workspace/review_cycle_context/`:
- **`current_diff.md`** — git changes to review (stat + commits) ← run `git diff` from those commits
- **`{maker_file}`** — current implementation to review
- `initial_request.md` — original requirements to verify against
{prev_feedback_note}- Earlier numbered files show the full iteration history

**Review focus**: Read `current_diff.md` for the list of changed files, then use
`git diff <base_commit> HEAD -- <file>` to examine the actual changes. Review ONLY
additions (`+`) and deletions (`-`). Do not review unchanged code.
"""
            git_diff_section = context_files_section
            requirements_section = f"""
## Original Requirements

**Title**: {issue.get('title', 'No title')}
(Full requirements in `/workspace/review_cycle_context/initial_request.md`)
"""
        else:
            # Legacy fallback: embed context directly in prompt
            git_diff_section = ""
            if change_manifest:
                git_diff_section = f"""

## Code Changes

{change_manifest}

**Review focus**: Use the `git diff` commands listed above to fetch and examine the actual
changes before reviewing. Review ONLY additions (`+`) and deletions (`-`) in the diff output.
Do not review unchanged code.
"""
            requirements_section = f"""
## Original Requirements

**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}
"""

        prompt = f"""
You are a **Senior Software Engineer** conducting comprehensive code review.

{iteration_context}

{filter_instructions}
{requirements_section}
{git_diff_section}
## Project-Specific Expert Agents

Check `/workspace/CLAUDE.md` for a "Specialized Sub-Agents" section. If any listed agent
matches your review domain (e.g., guardian for boundary violations and antipattern enforcement,
flow-expert for React Flow node patterns, state-expert for state management conventions),
you MUST consult it via the Task tool before completing your review. Do not assess
project-specific patterns from general knowledge when a project expert agent exists.

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
                if result.get('output_posted'):
                    context['output_posted'] = True
            else:
                markdown_output = result if isinstance(result, str) else str(result)
            context['markdown_review'] = markdown_output
            context['raw_review_result'] = markdown_output

            logger.info(f"Code review completed, output length: {len(markdown_output)}")

            return context

        except Exception as e:
            raise Exception(f"Code review failed: {str(e)}")

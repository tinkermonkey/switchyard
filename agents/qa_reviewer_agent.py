from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class QAReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("qa_reviewer", agent_config=agent_config)

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
                agent_name='qa_reviewer',
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
- New test failures
- Tests that are now passing but with concerning implementation
- Critical problems introduced by the changes
"""
        else:
            resolved_section = "### Issues Found"

        return f"""
**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

{resolved_section}

#### Critical (BLOCKING - Must Fix Before Approval)
**IMPORTANT**: Use this category for:
- ANY test failures (unit, integration, e2e)
- New/changed code coverage below 80%
- Missing integration or e2e tests
- Critical security vulnerabilities found during testing
- Production readiness blockers

List critical issues here, or write "None" if all tests pass and coverage is adequate.

#### High Priority (Should Fix)
- **[Issue Title]**: [Description and recommendation]

List important issues in test implementation, test quality, or code changes made during QA work.

#### Medium Priority (Consider)
- **[Issue Title]**: [Description and recommendation]

#### Low Priority / Suggestions
- **[Issue Title]**: [Description and recommendation]

### Test Results Summary
- **Unit Tests**: X/X passed
- **Integration Tests**: X/X passed
- **Test Coverage**: X%
- **Overall Status**: PASS/FAIL

### Summary
Brief summary of QA work quality and next steps
```

**Decision Criteria**:
- APPROVED: ALL tests passing, new/changed code coverage ≥80%, integration/e2e tests written, no critical issues, ready for deployment
- CHANGES NEEDED: Test failures exist OR new/changed code coverage <80% OR missing integration/e2e tests OR quality issues that can be fixed
- BLOCKED: Fundamental issues requiring human intervention or architectural changes

**CRITICAL RULE**: You MUST use CHANGES NEEDED (not APPROVED) if ANY tests are failing.

REQUIRED: Include "**Status**: X" at the top for automation parsing."""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute QA review focusing on test execution results and quality"""

        # Extract from nested task context
        task_context = context.get('context', {})

        # Get the previous stage output (from senior_qa_engineer)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            raise Exception("QA Reviewer needs previous stage output from Senior QA Engineer")

        qa_output = previous_stage
        review_cycle = task_context.get('review_cycle', {})
        issue = task_context.get('issue', {})

        # Get scoped git diff if available (for code changes made during QA)
        scoped_diff = task_context.get('scoped_git_diff', '')

        # Build iteration context for re-reviews
        iteration_context = ""
        is_rereviewing = False

        if review_cycle:
            iteration = review_cycle.get('iteration', 0)
            max_iterations = review_cycle.get('max_iterations', 3)
            maker_agent = review_cycle.get('maker_agent', 'senior_qa_engineer')
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

**QA Engineer**: {maker_agent.replace('_', ' ').title()} has revised their work based on your previous feedback.

**Your Task**: Verify previous issues are resolved, especially test failures.

**Review Approach**:
1. Check if ALL tests are now passing (critical requirement)
2. Verify test coverage is ≥80%
3. Check if your PREVIOUS feedback items were addressed
4. Note any NEW issues discovered

**Keep Feedback CONCISE**:
- 1-2 sentences per issue maximum
- Focus on WHAT is wrong and WHAT needs to be done
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

**Common Issues**:
- Markdown files with developer notes and explanations -> These need to be removed
- Test and debug scripts that are not in the test folder tree -> These need to be evaluated and cleaned up
- Leaving in or commenting out code that was meant to be replaced or removed -> These need to be cleaned up
- Code with names including "Phase X" or "Step Y" -> These need to be renamed to meaningful names

**CRITICAL**: You MUST mark as CHANGES NEEDED if any tests are still failing.

**Escalation**: After {max_iterations} iterations, unresolved issues will escalate to human review.
"""
            else:
                iteration_context = f"""

## Review Cycle Context - Initial Review

This is **Review Iteration {iteration} of {max_iterations}**.

**QA Engineer**: {maker_agent.replace('_', ' ').title()} has executed testing and quality assurance.

**Your Task**: Validate test execution results and QA work quality.

**After Review**: If issues found, QA Engineer will revise. Up to {max_iterations} review cycles.
"""

        # Get output format instructions
        format_instructions = self._get_output_format_instructions(is_rereviewing)

        # Build git diff section if available
        git_diff_section = ""
        if scoped_diff:
            git_diff_section = f"""

## Code Changes Made During QA Work (Git Diff)

**Note**: These are code changes made by the QA Engineer (e.g., test fixes, bug fixes).

```diff
{scoped_diff}
```

**Review Focus**: Analyze changes for quality and correctness.
"""

        # Inject learned review filters
        filter_instructions = await self._get_filter_instructions()

        prompt = f"""
You are a **QA Reviewer** validating test execution results and QA work quality.

Your expertise includes: test strategy validation, test execution analysis, quality metrics assessment, and production readiness evaluation.
{iteration_context}
{filter_instructions}

## Original Requirements

**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}
{git_diff_section}
## QA Engineer's Output to Review

{qa_output}

## Your Review Task

**CRITICAL VALIDATION CHECKLIST**:

1. **Test Writing Validation** (BLOCKING if not met):
   - ✅ Did the QA Engineer WRITE integration tests?
   - ✅ Did the QA Engineer WRITE end-to-end tests?
   - ✅ Are the new tests comprehensive and meaningful?

   **If integration/e2e tests are missing, you MUST mark as CHANGES NEEDED with Critical priority.**

2. **Test Execution Validation** (BLOCKING if not met):
   - ✅ Did the QA Engineer actually RUN ALL of the tests (not just analyze)?
   - ✅ Are ALL unit tests passing?
   - ✅ Are ALL integration tests passing?
   - ✅ Are ALL e2e tests passing?
   - ✅ Is new/changed code coverage ≥80%?

   **If ANY test is failing, you MUST mark as CHANGES NEEDED with Critical priority.**

3. **Test Results Analysis**:
   - For any test failures: Are root causes identified?
   - For any test failures: Are fix recommendations specific and actionable?
   - Are test results properly categorized (unit, integration, e2e)?

4. **Test Coverage Assessment**:
   - Is overall coverage percentage reported?
   - Is new/changed code coverage ≥80%?
   - Are new/changed files with <80% coverage identified?

5. **Code Changes Review** (if QA Engineer made code changes):
   - Are bug fixes correct and well-tested?
   - Are test improvements appropriate?
   - Is code quality maintained?

6. **Production Readiness**:
   - Are all production readiness checklist items addressed?
   - Are performance benchmarks validated?
   - Are security checks completed?
   - Is error handling comprehensive?

7. **Quality of QA Work**:
   - Is the output complete and well-organized?
   - Are recommendations clear and actionable?
   - Is the analysis thorough and professional?

{format_instructions}

**IMPORTANT**:
- Output your review as **markdown text** directly in your response
- DO NOT create any files - this review will be posted to GitHub as a comment
- DO NOT include project name, feature name, or date headers
- Start directly with "### Status"
- Be specific and actionable in your feedback
- **YOU MUST FAIL THIS REVIEW (CHANGES NEEDED) IF:**
  - ANY tests are failing
  - Integration or e2e tests are missing
  - New/changed code coverage <80%
- Only approve if ALL tests pass AND integration/e2e tests written AND new/changed code coverage ≥80% AND no critical issues
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
            markdown_output = result if isinstance(result, str) else str(result)
            context['markdown_review'] = markdown_output
            context['raw_review_result'] = markdown_output

            logger.info(f"QA review completed, output length: {len(markdown_output)}")

            return context

        except Exception as e:
            raise Exception(f"QA review failed: {str(e)}")

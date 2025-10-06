from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class TestReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("test_reviewer", agent_config=agent_config)

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
                agent_name='test_reviewer',
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

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute test plan review focusing on completeness, feasibility, and quality"""

        # Extract from nested task context
        task_context = context.get('context', {})

        # Get the previous stage output (from test_planner)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            raise Exception("Test Reviewer needs previous stage output from Test Planner")

        test_plan_content = previous_stage

        # Check for review cycle context
        review_cycle = task_context.get('review_cycle', {})
        issue = task_context.get('issue', {})

        # Build iteration context similar to requirements_reviewer
        iteration_context = ""
        if review_cycle:
            iteration = review_cycle.get('iteration', 0)
            max_iterations = review_cycle.get('max_iterations', 3)
            maker_agent = review_cycle.get('maker_agent', 'test_planner')
            is_rereviewing = review_cycle.get('is_rereviewing', False)
            post_human_feedback = review_cycle.get('post_human_feedback', False)

            if post_human_feedback:
                iteration_context = f"""

## Post-Escalation Review Update

You previously escalated this review due to **blocking issues** that required human intervention.

**The human has now responded with feedback.** Your task is to:

1. **Read the human feedback** in the discussion context below
2. **Incorporate their guidance** into your review assessment
3. **Update your review** based on their corrections, clarifications, or directions
4. **Post your UPDATED review** that reflects the human's input

**Current Iteration**: {iteration}/{max_iterations}

"""
            elif is_rereviewing:
                iteration_context = f"""

## Review Cycle Context - Re-Review Mode

This is **Re-Review Iteration {iteration} of {max_iterations}**.

**Maker**: {maker_agent.replace('_', ' ').title()} has revised their work based on your previous feedback.

**Your Task**: Verify previous issues are resolved. Be concise.

**Keep Feedback CONCISE**:
- 1-2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY
- Only include items that genuinely need fixing

After {max_iterations} iterations, escalates to human review.

"""
            else:
                iteration_context = f"""

## Review Cycle Context - Initial Review

This is **Initial Review (Iteration {iteration} of {max_iterations})**.

**Your Task**: Identify issues that need fixing. Be specific and concise.

**Keep Feedback CONCISE**:
- State WHAT is wrong and HOW to fix it
- Don't explain WHY (maker understands quality standards)
- 1-2 sentences per issue

"""

        # Inject learned review filters
        filter_instructions = await self._get_filter_instructions()

        prompt = f"""
Review the test plan provided by the Test Planner.
{iteration_context}
{filter_instructions}

Original Issue:
Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}

Test Plan to Review:
{test_plan_content}

IMPORTANT: Output your review as markdown text directly in your response. DO NOT create any files. This review will be posted to GitHub as a comment.

Provide a comprehensive test plan review addressing:

1. **Test Coverage Analysis**: Functional, non-functional, edge cases, risk-based testing
2. **Test Strategy Evaluation**: Testing pyramid, test level distribution, quality gates
3. **Automation Assessment**: Framework selection, coverage targets, CI/CD integration
4. **Performance Testing Review**: Baselines, load scenarios, scalability, monitoring
5. **Test Environment Planning**: Environment requirements, data management, provisioning

**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED**

### Issues Found

#### Critical (BLOCKING)
List blocking issues or "None"

#### High Priority
- **[Issue Title]**: [Description and recommendation]

#### Medium Priority
- **[Issue Title]**: [Description and recommendation]

#### Low Priority / Suggestions
- **[Issue Title]**: [Description and recommendation]

### Summary
Brief summary of overall assessment and next steps
```

**Decision Criteria**:
- APPROVED: No critical gaps, work is adequate for next stage
- CHANGES NEEDED: Specific fixable issues exist
- BLOCKED: Critical issues OR maker ignored previous feedback

REQUIRED: Include "**Status**: X" for automation parsing.
"""

        try:
            result = await run_claude_code(prompt, context)

            # Result is the review in markdown format
            review_text = result if isinstance(result, str) else str(result)

            # Store the markdown output for GitHub comment
            context['markdown_review'] = review_text
            context['review_completed'] = True

            logger.info(f"Test plan review completed (length: {len(review_text)} chars)")
            return context

        except Exception as e:
            raise Exception(f"Test plan review failed: {str(e)}")

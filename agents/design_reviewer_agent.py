from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import logging

logger = logging.getLogger(__name__)


class DesignReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("design_reviewer", agent_config=agent_config)

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
                agent_name='design_reviewer',
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
- Critical technical problems introduced by the changes
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

#### Critical (BLOCKING)
**IMPORTANT**: Only use this category for issues that:
- Cannot be addressed by the architect in a revision
- Require fundamental changes to requirements or business decisions
- Have no clear implementation path without human/stakeholder input

Most technical issues (missing validations, unclear patterns, performance concerns) should be **High Priority**, not BLOCKING.

List blocking issues here, or write "None" if all issues can be addressed by architect revision.

#### High Priority
- **[Issue Title]**: [Description and recommendation]

List important technical issues that must be addressed but CAN be fixed by the architect.

#### Medium Priority
- **[Issue Title]**: [Description and recommendation]

#### Low Priority / Suggestions
- **[Issue Title]**: [Description and recommendation]

### Summary
Brief summary of overall assessment and next steps
```

**Decision Criteria**:
- APPROVED: Design meets all requirements, no significant issues
- CHANGES NEEDED: Issues found that architect can address in revision
- BLOCKED: True blocking issues that require human intervention

**Use CHANGES NEEDED unless there are truly un-addressable issues that need human decisions.**

REQUIRED: Include "**Status**: X" at the top for automation parsing."""

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """Execute architecture design review focusing on patterns, security, and scalability"""

        # Extract from nested task context
        task_context = context.get('context', {})

        # Get the previous stage output (from software_architect)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            raise Exception("Design Reviewer needs previous stage output from Software Architect")

        architecture_design = previous_stage
        review_cycle = task_context.get('review_cycle', {})
        issue = task_context.get('issue', {})

        # Extract focus areas for review
        focus_areas = context.get('focus_areas', ['security', 'scalability', 'patterns', 'performance'])

        # Build iteration context similar to other reviewers
        iteration_context = ""
        is_rereviewing = False
        if review_cycle:
            iteration = review_cycle.get('iteration', 0)
            max_iterations = review_cycle.get('max_iterations', 3)
            maker_agent = review_cycle.get('maker_agent', 'software_architect')
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
Review the architecture design provided by the Software Architect.
{iteration_context}
{filter_instructions}

Original Issue:
Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}

Architecture Design to Review:
{architecture_design}

Focus Areas: {', '.join(focus_areas)}

IMPORTANT: Output your review as markdown text directly in your response. DO NOT create any files. This review will be posted to GitHub as a comment.

**Review Philosophy**: This is an iterative maker-checker process. Reserve BLOCKING status only for issues that truly cannot be addressed through iteration. Most technical gaps should be High Priority issues that trigger another iteration, not blockers.

{self._get_output_format_instructions(is_rereviewing)}

Provide comprehensive architecture review covering:

1. **Design Pattern Analysis**: Architectural patterns, SOLID principles, anti-patterns, consistency
2. **Security Assessment**: OWASP compliance, auth/authz, data protection, API security
3. **Scalability Review**: Horizontal/vertical scaling, bottlenecks, load balancing, caching
4. **Performance Analysis**: Targets feasibility, resource efficiency, monitoring, optimization
5. **Maintainability**: Code organization, dependencies, deployment, documentation
6. **Technical Risk**: Implementation risks, technology choices, integration complexity
7. **Compliance**: Industry standards, best practices, quality attributes
"""

        try:
            # Enhance context with MCP server data if available
            enhanced_context = context.copy()

            # Add agent_config for security enforcement (requires_docker check)
            if self.agent_config and 'agent_config' in self.agent_config:
                enhanced_context['agent_config'] = self.agent_config['agent_config']

            # Add MCP server configuration to context for Claude Code
            if hasattr(self, 'mcp_integration') and self.mcp_integration:
                enhanced_context['mcp_servers'] = []
                for name, server in self.mcp_integration.servers.items():
                    enhanced_context['mcp_servers'].append({
                        'name': name,
                        'url': server.url,
                        'capabilities': server.capabilities
                    })

                try:
                    # Use Serena to find similar design review patterns
                    search_results = await self.mcp_integration.serena_search(
                        "architecture review security scalability patterns OWASP",
                        file_types=['md', 'txt', 'py']
                    )
                    if search_results:
                        enhanced_context['review_patterns'] = search_results[:2]

                    logger.info(f"Enhanced review context with {len(search_results)} review patterns from Serena")
                except Exception as e:
                    logger.warning(f"Serena search failed: {e}")

            result = await run_claude_code(prompt, enhanced_context)

            # Result is the review in markdown format
            review_text = result if isinstance(result, str) else str(result)

            # Store the markdown output for GitHub comment
            context['markdown_review'] = review_text
            context['review_completed'] = True

            logger.info(f"Architecture design review completed (length: {len(review_text)} chars)")
            return context

        except Exception as e:
            raise Exception(f"Architecture design review failed: {str(e)}")
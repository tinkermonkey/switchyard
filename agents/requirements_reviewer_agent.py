from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from datetime import datetime
import json
import logging
from services.review_parser import ReviewStatus

logger = logging.getLogger(__name__)

class RequirementsReviewerAgent(PipelineStage):
    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("requirements_reviewer", agent_config=agent_config)

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
                agent_name='requirements_reviewer',
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
        """Execute requirements review on the Business Analyst's output"""

        # Extract from nested task context
        task_context = context.get('context', {})
        issue = task_context.get('issue', {})

        # Debug logging
        logger.debug(f"Context keys: {list(context.keys())}")
        if 'previous_stage_output' in task_context:
            prev_out = task_context.get('previous_stage_output', '')
            logger.debug(f"previous_stage_output length: {len(prev_out)}")

        # Get the previous stage output (from business_analyst or revision)
        previous_stage = task_context.get('previous_stage_output', '')

        if not previous_stage:
            logger.error(f"No previous_stage_output found. Task context: {json.dumps(task_context, indent=2)[:500]}")
            raise Exception("Requirements Reviewer needs previous stage output from Business Analyst")

        # Check for review cycle context
        review_cycle = task_context.get('review_cycle', {})
        iteration_context = ""

        if review_cycle:
            iteration = review_cycle.get('iteration', 0)
            max_iterations = review_cycle.get('max_iterations', 3)
            maker_agent = review_cycle.get('maker_agent', 'unknown')
            is_rereviewing = review_cycle.get('is_rereviewing', False)
            post_human_feedback = review_cycle.get('post_human_feedback', False)
            human_feedback = review_cycle.get('human_feedback', '')

            if post_human_feedback:
                iteration_context = f"""

## Post-Escalation Review Update

You previously escalated this review due to **blocking issues** that required human intervention.

**The human has now responded with feedback.** Your task is to:

1. **Read the human feedback** in the discussion context below
2. **Incorporate their guidance** into your review assessment
3. **Update your review** based on their corrections, clarifications, or directions
4. **Post your UPDATED review** that reflects the human's input

**Important Guidelines**:
- If the human corrected your assessment, update your review accordingly
- If the human provided additional context, incorporate it into your evaluation
- If the human gave directions to the business analyst, those will be handled separately
- Your updated review should be a **complete, standalone review** (not just changes)
- Set the appropriate status: APPROVED, CHANGES NEEDED, or BLOCKED (if still unresolved)

**Current Iteration**: {iteration}/{max_iterations}

"""
            elif is_rereviewing:
                iteration_context = f"""

## Review Cycle Context - Re-Review Mode

This is **Re-Review Iteration {iteration} of {max_iterations}**.

**Maker**: {maker_agent.replace('_', ' ').title()} has revised their work based on your previous feedback.

**Your Task**: Verify previous issues are resolved. Be concise.

**Review Approach**:
1. Check if your PREVIOUS feedback items were addressed (don't re-raise if fixed)
2. Note any NEW issues discovered
3. Make your decision

**Keep Feedback CONCISE**:
- 1-2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY it's important (maker already knows)
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

**Status Decision**:
- **APPROVED**: All critical issues resolved (minor issues OK)
- **CHANGES NEEDED**: Specific fixable issues remain
- **BLOCKED**: Critical issues persist OR maker didn't address previous feedback

After {max_iterations} iterations, escalates to human review.

"""
            else:
                iteration_context = f"""

## Review Cycle Context - Initial Review

This is **Initial Review (Iteration {iteration} of {max_iterations})**.

**Your Task**: Identify issues that need fixing. Be specific and concise.

**Review Approach**:
- Focus on CRITICAL issues (requirements gaps, ambiguities, contradictions)
- Be selective - only raise issues that genuinely need fixing
- Keep feedback brief: 1-2 sentences per issue
- Provide clear, actionable guidance

**Keep Feedback CONCISE**:
- State WHAT is wrong
- State HOW to fix it
- Don't explain WHY (maker understands quality standards)
- Don't praise what's good (focus on issues only)

**Status Decision**:
- **APPROVED**: No critical issues, work is adequate
- **CHANGES NEEDED**: Specific issues that can be fixed
- **BLOCKED**: Critical issues that might need human input

"""

        # Check if this is manual feedback (not review cycle)
        feedback_data = task_context.get('feedback')
        previous_output = task_context.get('previous_output')
        feedback_prompt = ""

        if feedback_data and previous_output:
            feedback_prompt = f"""

YOUR PREVIOUS REVIEW:
{previous_output}

HUMAN FEEDBACK RECEIVED:
{feedback_data.get('formatted_text', '')}

IMPORTANT: Review your previous review and refine it based on the feedback.
Do NOT start from scratch - update and improve your existing review.

CRITICAL: Output the COMPLETE, UPDATED review with all changes incorporated.
Do NOT output just the changes - the next agent needs the full review document.
"""
        elif feedback_data:
            feedback_prompt = f"""

HUMAN FEEDBACK:
{feedback_data.get('formatted_text', '')}

Please incorporate this feedback into your review.
"""

        # Inject learned review filters
        filter_instructions = await self._get_filter_instructions()

        prompt = f"""
Review the requirements analysis provided by the Business Analyst.
{iteration_context}
{filter_instructions}

Original Issue:
Title: {issue.get('title', 'No title')}
Description: {issue.get('body', 'No description')}

Business Analyst's Output:
{previous_stage}
{feedback_prompt}

**IMPORTANT - OUTPUT FORMAT**:
- Output your review as text directly in your response
- DO NOT create any files - this review will be posted to GitHub as a comment
- **START IMMEDIATELY** with "### Status" - no preambles or introductory text
- **NO CONVERSATIONAL DIALOG**: Do NOT include planning statements like "I'll review...", "Let me check...", "Now let me...", etc.
- **NO TOOL USAGE COMMENTARY**: Do not narrate what tools you're using or what you're searching for
- Your response should begin directly with the review format shown below

Provide a comprehensive review focusing on:
1. **Simplicity**: Do the requirements avoid unnecessary complexity?
2. **Clarity**: Are the requirements clearly and unambiguously stated?
3. **Completeness**: Are all aspects of the issue addressed?
4. **Context awareness**: Do the requirements consider the broader project context? Do they align with project goals and constraints? Do they reuse existing components where possible?

Use the web search tool and any MCP servers if needed to validate requirements as well as your feedback.

IMPORTANT GUIDELINES - BE CONCISE:
- Keep your review SHORT and FOCUSED (aim for 200-400 words total)
- Only raise issues that genuinely need fixing
- Each issue: 1-2 sentences stating WHAT is wrong and HOW to fix it
- Don't explain WHY issues matter (maker understands quality standards)
- Don't praise what's good (focus only on problems)
- Don't assign quality scores or metrics
- Don't include effort/timeline estimates
- Don't suggest implementation details

**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Issues Found

#### Critical (BLOCKING)
- [Only genuinely blocking issues - be selective]

#### High Priority
- [Issues that should be fixed]

#### Questions
- [Only if unclear - be specific]

### Summary
Brief summary of overall assessment and next steps
```

**Decision Criteria**:
- APPROVED: No critical gaps, work is adequate for next stage
- CHANGES NEEDED: Specific fixable issues exist
- BLOCKED: Critical issues OR maker ignored previous feedback

Before submitting, verify each issue is:
1. Accurate (actually a problem)
2. Specific (clear what to fix)
3. Actionable (maker can fix it)

REQUIRED: Include "**Status**: X" at the top for automation parsing.
"""

        # PROMPT DEBUG LOGGING
        logger.info(f"PROMPT DEBUG - Prompt length: {len(prompt)}")
        logger.info(f"PROMPT DEBUG - Prompt starts with: {prompt[:200]}")
        logger.info(f"PROMPT DEBUG - 'Business Analyst' in prompt: {'Business Analyst' in prompt}")
        logger.info(f"PROMPT DEBUG - previous_stage was {len(previous_stage)} chars")

        try:
            # Enhance context with MCP server data if available
            enhanced_context = context.copy()

            # Add agent_config for security enforcement (requires_docker check)
            if self.agent_config and 'agent_config' in self.agent_config:
                enhanced_context['agent_config'] = self.agent_config['agent_config']

            # Add MCP server configuration from agent_config to context for Claude Code
            if self.agent_config and 'mcp_servers' in self.agent_config:
                enhanced_context['mcp_servers'] = self.agent_config['mcp_servers']
                logger.info(f"Added {len(enhanced_context['mcp_servers'])} MCP servers to context")

            result = await run_claude_code(prompt, enhanced_context)

            # Result is the review in markdown format
            review_text = result if isinstance(result, str) else str(result)

            # Store the markdown output for GitHub comment
            context['markdown_review'] = review_text
            context['review_completed'] = True

            # Check if review indicates issues that need addressing
            # If "NEEDS REVISION" or critical issues found, trigger feedback loop to maker agent
            needs_revision = "NEEDS REVISION" in review_text or "Critical Issues" in review_text or "CRITICAL" in review_text.upper()

            # Check if we're in a review cycle (review cycle executor handles maker invocation)
            task_context_data = context.get('context', {})
            is_review_cycle = task_context_data.get('trigger') == 'review_cycle'

            if needs_revision and not is_review_cycle:
                # Only trigger feedback if NOT in review cycle
                # (Review cycle executor handles the maker-checker loop)
                logger.info("Review found issues - determining maker agent for feedback loop")
                maker_agent = await self._get_maker_agent_for_review(context)
                if maker_agent:
                    logger.info(f"Triggering feedback loop to {maker_agent}")
                    await self._trigger_maker_feedback(context, review_text, maker_agent)
                else:
                    logger.warning("Could not determine maker agent for feedback - skipping feedback loop")
            elif needs_revision and is_review_cycle:
                logger.info("Review found issues but in review_cycle mode - review cycle executor will handle maker invocation")
            else:
                logger.info("Review approved - no feedback loop needed")
                # Check if this is a discussion workspace - if so, finalize to issue
                await self._check_and_finalize_to_issue(context)

            return context

        except Exception as e:
            raise Exception(f"Requirements review failed: {str(e)}")

    async def _get_maker_agent_for_review(self, context):
        """Determine which maker agent this reviewer is reviewing for"""
        try:
            from config.manager import config_manager

            task_context_data = context.get('context', {})
            project = context.get('project')
            board = task_context_data.get('board')
            current_column = task_context_data.get('column')

            if not project or not board:
                return None

            # Get project config
            project_config = config_manager.get_project_config(project)

            # Find the pipeline for this board
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return None

            # Get the workflow template
            workflow_template = config_manager.get_workflow_template(pipeline_config.workflow)

            # Find current column in workflow
            column_names = [col.name for col in workflow_template.columns]
            if current_column not in column_names:
                return None

            current_index = column_names.index(current_column)
            if current_index == 0:
                return None  # No previous stage

            # Look backwards for the previous maker agent
            for i in range(current_index - 1, -1, -1):
                prev_column = workflow_template.columns[i]
                if prev_column.agent and prev_column.agent != 'null':
                    # Check if this is a reviewer or maker
                    # Reviewers typically have "review" in their name
                    if 'reviewer' not in prev_column.agent:
                        return prev_column.agent

            return None

        except Exception as e:
            logger.error(f"Error determining maker agent: {e}")
            return None

    async def _trigger_maker_feedback(self, context, review_text, maker_agent):
        """Create a feedback task for the maker agent to address review findings"""
        try:
            from task_queue.task_manager import TaskQueue, Task, TaskPriority
            from datetime import datetime
            import time

            task_context_data = context.get('context', {})
            issue_number = task_context_data.get('issue_number')
            project = context.get('project')
            repository = task_context_data.get('repository')
            board = task_context_data.get('board')

            if not issue_number or not project:
                logger.warning("Cannot trigger feedback loop - missing issue_number or project")
                return

            # Get the business_analyst's previous output
            previous_stage = task_context_data.get('previous_stage_output', '')

            # Create feedback task for business_analyst
            feedback_task_context = {
                'project': project,
                'board': board,
                'pipeline': task_context_data.get('pipeline'),
                'repository': repository,
                'issue_number': issue_number,
                'issue': task_context_data.get('issue'),
                'column': 'feedback',
                'trigger': 'reviewer_feedback',
                'workspace_type': task_context_data.get('workspace_type', 'issues'),  # Preserve workspace
                'discussion_id': task_context_data.get('discussion_id'),  # Preserve discussion ID
                'feedback': {
                    'comments': [{
                        'author': 'requirements_reviewer',
                        'body': review_text,
                        'created_at': datetime.now().isoformat()
                    }],
                    'formatted_text': f"**Feedback from Requirements Reviewer:**\n{review_text}"
                },
                'previous_output': previous_stage,  # The BA's previous work to refine
                'timestamp': datetime.now().isoformat()
            }

            task_queue = TaskQueue(use_redis=True)
            feedback_task = Task(
                id=f"{maker_agent}_feedback_{project}_{board}_{issue_number}_{int(time.time())}",
                agent=maker_agent,
                project=project,
                priority=TaskPriority.HIGH,  # Reviewer feedback gets high priority
                context=feedback_task_context,
                created_at=datetime.now().isoformat()
            )

            task_queue.enqueue(feedback_task)
            logger.info(f"Created feedback task for {maker_agent} on issue #{issue_number}")

        except Exception as e:
            logger.error(f"Failed to trigger maker feedback: {e}")
            # Don't fail the review if feedback task creation fails

    async def _check_and_finalize_to_issue(self, context):
        """Check if requirements should be finalized to issue (for discussion workspaces)"""
        try:
            from config.manager import config_manager
            from config.state_manager import state_manager

            task_context_data = context.get('context', {})
            project = context.get('project')
            board = task_context_data.get('board')
            issue_number = task_context_data.get('issue_number')
            repository = task_context_data.get('repository')
            workspace_type = task_context_data.get('workspace_type', 'issues')

            if not project or not board or not issue_number:
                return

            # Only finalize if using discussions workspace
            if workspace_type != 'discussions':
                logger.debug(f"Not finalizing - workspace is {workspace_type}")
                return

            # Check if pipeline is configured for auto-finalization
            project_config = config_manager.get_project_config(project)
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                return

            # Check if update_issue_on_completion is enabled (default: true)
            should_finalize = getattr(pipeline_config, 'update_issue_on_completion', True)
            if not should_finalize:
                logger.info(f"Auto-finalization disabled for {project}/{board}")
                return

            # Get discussion ID from state
            discussion_id = state_manager.get_discussion_for_issue(project, issue_number)
            if not discussion_id:
                logger.warning(f"No discussion found for issue #{issue_number} - cannot finalize")
                return

            logger.info(f"Requirements approved - triggering finalization for issue #{issue_number}")

            # Import ProjectMonitor to access finalization method
            from services.project_monitor import ProjectMonitor
            from task_queue.task_manager import TaskQueue

            # Create a ProjectMonitor instance (it needs task_queue but we won't use it)
            task_queue = TaskQueue(use_redis=True)
            monitor = ProjectMonitor(task_queue, config_manager)

            # Call finalization method
            monitor.finalize_requirements_to_issue(
                project_name=project,
                board_name=board,
                issue_number=issue_number,
                repository=repository,
                discussion_id=discussion_id
            )

            logger.info(f"Finalization triggered successfully for issue #{issue_number}")

        except Exception as e:
            logger.error(f"Error checking/finalizing to issue: {e}")
            import traceback
            logger.error(traceback.format_exc())
            # Don't fail the review if finalization fails
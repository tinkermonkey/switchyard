"""
Base Maker Agent Class

Provides a unified pattern for all maker agents (create/produce output) with three execution modes:
1. Initial - First-time analysis/creation
2. Question - Answer human questions about previous output (conversational)
3. Revision - Update previous output based on feedback

All maker agents inherit from this base class and implement agent-specific properties.
"""

from typing import Dict, Any, List
from abc import ABC, abstractmethod
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
import logging

logger = logging.getLogger(__name__)


class MakerAgent(PipelineStage, ABC):
    """
    Base class for all maker agents (agents that create/produce output).

    Maker agents operate in three modes:
    - Initial: Create output from requirements
    - Question: Answer human questions about previous output (conversational)
    - Revision: Update output based on feedback (from reviewer or human)
    """

    def __init__(self, agent_name: str, agent_config: Dict[str, Any] = None):
        super().__init__(agent_name, agent_config=agent_config)

    # ==================================================================================
    # ABSTRACT PROPERTIES - Each agent must implement these
    # ==================================================================================

    @property
    @abstractmethod
    def agent_display_name(self) -> str:
        """Human-readable agent name (e.g., 'Business Analyst')"""
        pass

    @property
    @abstractmethod
    def agent_role_description(self) -> str:
        """Brief description of agent's role and expertise"""
        pass

    @property
    @abstractmethod
    def output_sections(self) -> List[str]:
        """List of section names in agent's output (for revision prompts)"""
        pass

    # ==================================================================================
    # OPTIONAL PROPERTIES - Agents can override for customization
    # ==================================================================================

    def get_initial_guidelines(self) -> str:
        """
        Agent-specific guidelines for initial analysis mode.
        Override to add domain-specific instructions.
        """
        return ""

    def get_quality_standards(self) -> str:
        """
        Quality standards and best practices for this agent type.
        Override to add domain-specific standards.
        """
        return ""

    # ==================================================================================
    # MODE DETECTION - Determines which execution mode to use
    # ==================================================================================

    def _determine_execution_mode(self, task_context: Dict[str, Any]) -> str:
        """
        Determine execution mode from task context.

        Returns:
            'question' - Conversational mode (threaded Q&A)
            'revision' - Revision mode (review cycle or feedback)
            'initial' - Initial analysis mode
        """
        # Check for conversational mode (threaded Q&A)
        is_conversational = (
            task_context.get('trigger') == 'feedback_loop' and
            task_context.get('conversation_mode') == 'threaded' and
            len(task_context.get('thread_history', [])) > 0
        )

        if is_conversational:
            logger.info("Using QUESTION mode (threaded conversational)")
            return 'question'

        # Check for revision mode (review cycle or feedback)
        is_revision = (
            task_context.get('trigger') in ['review_cycle_revision', 'feedback_loop'] or
            'revision' in task_context or
            'feedback' in task_context
        )

        if is_revision:
            logger.info("Using REVISION mode (update based on feedback)")
            return 'revision'

        # Default to initial mode
        logger.info("Using INITIAL mode (first-time analysis)")
        return 'initial'

    # ==================================================================================
    # OUTPUT INSTRUCTION BUILDER - Conditional based on agent capabilities
    # ==================================================================================

    def _get_output_instructions(self) -> str:
        """
        Build output instructions based on agent configuration.

        Agents that make code changes get different instructions than
        agents that only produce analysis/reviews.
        """
        from config.manager import config_manager

        # Check if agent makes code changes
        makes_code_changes = False
        filesystem_write_allowed = True

        if self.agent_config:
            # Try to get from agent_config dict
            if isinstance(self.agent_config, dict):
                makes_code_changes = self.agent_config.get('makes_code_changes', False)
                filesystem_write_allowed = self.agent_config.get('filesystem_write_allowed', True)
            # Try to get from agent_config object attributes
            elif hasattr(self.agent_config, 'get'):
                agent_cfg = self.agent_config.get('agent_config', {})
                makes_code_changes = agent_cfg.get('makes_code_changes', False)
                filesystem_write_allowed = agent_cfg.get('filesystem_write_allowed', True)

        # Agents that modify files get permissive instructions
        if makes_code_changes or filesystem_write_allowed:
            return """

**IMPORTANT**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first. The project's CLAUDE.md file defines project-specific conventions, file organization, and documentation requirements that take precedence over these general instructions.
- You may create, edit, or modify files as needed to complete your task
- Use the Write, Edit, and other file manipulation tools
- Your changes will be auto-committed to git
- Also provide a summary of your work as markdown for the GitHub comment
- Use proper markdown formatting (headers, lists, code blocks)
- **DO NOT include internal planning dialog**: Do not include statements like "Let me research...", "I'll examine...", "Now let me check...", etc. in your GitHub comment summary. Only include the final summary of what you did.
"""

        # Agents that only analyze/review get restrictive instructions
        else:
            return """

**IMPORTANT - OUTPUT FORMAT**:
- **PROJECT-SPECIFIC CONVENTIONS OVERRIDE**: Read `/workspace/CLAUDE.md` first. The project's CLAUDE.md file defines project-specific conventions and documentation requirements that take precedence over these general instructions.
- Output your analysis as markdown text directly in your response
- DO NOT create any files - this will be posted to GitHub as a comment
- DO NOT include project name, feature name, or date headers (this info is already in the discussion)
- **START IMMEDIATELY** with your first section heading (e.g., "## Executive Summary" or "## Problem Abstraction")
- **NO CONVERSATIONAL PREAMBLES**: Do NOT include statements like "Ok, I'll build...", "I'll analyze...", "Let me create...", etc.
- **NO SUMMARY SECTIONS**: Do NOT create a "Summary for GitHub Comment" section at the end - your entire output IS the comment
- **NO INTERNAL DIALOG**: Do NOT include planning statements like "Let me research...", "I'll examine...", "Now let me check..."
- Focus on WHAT needs to be done, not HOW or WHEN
- Be specific and factual, avoid hypotheticals and hyperbole
- Use proper markdown formatting (headers, lists, code blocks)
"""

    # ==================================================================================
    # PROMPT BUILDERS - One for each mode
    # ==================================================================================

    def _build_initial_prompt(self, task_context: Dict[str, Any]) -> str:
        """Build prompt for initial analysis/creation"""
        issue = task_context.get('issue', {})
        project = task_context.get('project', 'unknown')
        previous_stage = task_context.get('previous_stage_output', '')

        previous_stage_prompt = ""
        if previous_stage:
            previous_stage_prompt = f"""
## Previous Stage Output
{previous_stage}

Build upon this previous analysis in your work.
"""

        guidelines = self.get_initial_guidelines()
        guidelines_section = f"\n{guidelines}" if guidelines else ""

        quality_standards = self.get_quality_standards()
        quality_section = f"""
## Quality Standards
{quality_standards}
""" if quality_standards else ""

        # Build output instructions based on agent capabilities
        output_instructions = self._get_output_instructions()

        prompt = f"""
You are a {self.agent_display_name}.

{self.agent_role_description}

## Task: Initial Analysis

Analyze the following requirement for project {project}:

**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}
**Labels**: {issue.get('labels', [])}
{previous_stage_prompt}{quality_section}
## Output Format

Provide a comprehensive analysis with the following sections:
{chr(10).join(f'- {section}' for section in self.output_sections)}
{guidelines_section}
{output_instructions}
"""
        return prompt

    def _build_question_prompt(self, task_context: Dict[str, Any]) -> str:
        """Build prompt for answering questions (conversational mode)"""
        thread_history = task_context.get('thread_history', [])
        current_question = task_context.get('feedback', {}).get('formatted_text', '')
        issue = task_context.get('issue', {})

        # Format conversation history
        formatted_history = self._format_thread_history(thread_history)

        # Include initial guidelines so agent knows its capabilities
        guidelines = self.get_initial_guidelines()
        guidelines_section = f"\n{guidelines}" if guidelines else ""

        # Include output instructions so agent knows it can take action
        output_instructions = self._get_output_instructions()

        prompt = f"""
You are the {self.agent_display_name} continuing a conversation.

{self.agent_role_description}

## Original Context
**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}
{guidelines_section}
## Conversation History
{formatted_history}

## Latest Question
{current_question}

## Response Guidelines

You are in **conversational mode**:

1. **Take Action When Requested**: If the user is asking you to proceed, DO IT - don't ask for permission again
2. **Be Direct & Concise**: 200-500 words unless the question needs more
3. **Reference Prior Discussion**: Build on what's been said
4. **Natural Tone**: Professional but approachable ("I", "you")
5. **Stay Focused**: Answer the specific question
6. **Clarify if Needed**: Ask follow-up questions if unclear
7. **NO Internal Planning Dialog**: Do not include statements like "Let me research...", "I'll examine...", "Now let me check...". Just provide the findings directly.

**Response Format**:
- Use markdown for clarity (bold, lists, code blocks)
- Start directly with your answer (no formal headers)
- End naturally (no signatures)

**Common Scenarios**:
- "Expand on X?" → 2-3 focused paragraphs on X
- "What about Y?" → Explain Y, connect to previous points
- "Compare X and Y?" → Direct comparison with key differences
- "Confused about Z" → Clarify with simpler explanation/examples
- "Yes, do it" / "Please proceed" → TAKE ACTION immediately without asking again
{output_instructions}

Your response will be posted as a threaded reply.
"""
        return prompt

    def _build_revision_prompt(self, task_context: Dict[str, Any]) -> str:
        """Build prompt for revision based on feedback"""
        # Get revision context
        revision_data = task_context.get('revision', {})
        previous_output = revision_data.get('previous_output', task_context.get('previous_output', ''))
        feedback = revision_data.get('feedback', task_context.get('feedback', {}).get('formatted_text', ''))

        # Check if this is a review cycle
        review_cycle = task_context.get('review_cycle', {})
        iteration = review_cycle.get('iteration', 0)
        max_iterations = review_cycle.get('max_iterations', 3)
        reviewer = review_cycle.get('reviewer_agent', 'reviewer')
        is_review_cycle = task_context.get('trigger') == 'review_cycle_revision'

        issue = task_context.get('issue', {})

        # Build cycle context section
        if is_review_cycle:
            cycle_context = f"""
## Review Cycle - Revision {iteration} of {max_iterations}

The {reviewer.replace('_', ' ').title()} has reviewed your work and identified issues to address.

**Your Task**: REVISE your previous output to address the feedback. Don't start from scratch.

After {max_iterations} iterations, unresolved work escalates for human review.
"""
        else:
            cycle_context = """
## Feedback Context

User feedback has been provided on your previous work. Incorporate their suggestions.
"""

        prompt = f"""
You are the {self.agent_display_name} revising your work based on feedback.

{self.agent_role_description}
{cycle_context}
## Original Context
**Title**: {issue.get('title', 'No title')}
**Description**: {issue.get('body', 'No description')}

## Your Previous Output (to be revised)
{previous_output}

## Feedback to Address
{feedback}

## Revision Guidelines

**CRITICAL - How to Revise**:
1. **Read feedback systematically**: List each distinct issue raised
2. **Address EVERY feedback point**: Don't leave any issues unresolved
3. **Make TARGETED changes**: Modify only what was criticized
4. **Keep working content**: Don't rewrite sections that weren't criticized
5. **Stay focused**: Don't add new content unless specifically requested

**Required Output Structure**:

**MUST START WITH**:
```
## Revision Notes
- ✅ [Issue 1 Title]: [Brief description of what you changed]
- ✅ [Issue 2 Title]: [Brief description of what you changed]
- ✅ [Issue 3 Title]: [Brief description of what you changed]
...
```

This checklist is **CRITICAL** - it helps the reviewer see you addressed each point.

**Then provide your COMPLETE, REVISED document**:
- All sections: {', '.join(self.output_sections)}
- Full content (not just changes)
- DO NOT include project name, feature name, or date headers (already in discussion)

**Important Don'ts**:
- ❌ Start from scratch (this is a REVISION, not complete rewrite)
- ❌ Skip any feedback point without addressing it
- ❌ Remove content that wasn't criticized
- ❌ Add new sections unless specifically requested
- ❌ Make changes to sections that weren't mentioned in feedback
- ❌ Ignore subtle feedback ("clarify X" means "add more detail about X")

**Format**: Markdown text for GitHub posting.
"""
        return prompt

    # ==================================================================================
    # HELPER METHODS
    # ==================================================================================

    def _format_thread_history(self, history: List[Dict]) -> str:
        """Format thread history for conversational prompts"""
        if not history:
            return ""

        formatted = []
        for msg in history:
            role = msg.get('role', 'user')
            author = msg.get('author', 'unknown')

            # Handle body being either string or dict (e.g., {'formatted_text': '...'})
            body_raw = msg.get('body', '')
            if isinstance(body_raw, dict):
                # Extract text from dict (common patterns)
                body = body_raw.get('formatted_text', '') or body_raw.get('text', '') or str(body_raw)
            else:
                body = str(body_raw)

            body = body.strip()

            if role == 'agent':
                formatted.append(f"**You** ({author}):\n{body}\n")
            else:
                formatted.append(f"**@{author}**:\n{body}\n")

        return "\n".join(formatted)

    # ==================================================================================
    # MAIN EXECUTION - Delegates to mode-specific prompt builder
    # ==================================================================================

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        """
        Execute agent with automatic mode detection.

        This is the main entry point called by the orchestrator.
        Mode is detected automatically and delegated to appropriate prompt builder.
        """
        # Extract task context
        task_context = context.get('context', {})

        # Determine execution mode
        mode = self._determine_execution_mode(task_context)

        # Build prompt based on mode
        if mode == 'question':
            prompt = self._build_question_prompt(task_context)
        elif mode == 'revision':
            prompt = self._build_revision_prompt(task_context)
        else:  # initial
            prompt = self._build_initial_prompt(task_context)

        try:
            # Enhance context with agent config
            enhanced_context = context.copy()

            if self.agent_config and 'agent_config' in self.agent_config:
                enhanced_context['agent_config'] = self.agent_config['agent_config']

            # Execute with Claude Code SDK
            result = await run_claude_code(prompt, enhanced_context)

            # Process result (handle both old string format and new dict format)
            if isinstance(result, dict):
                analysis_text = result.get('result', '')
                session_id = result.get('session_id')

                # Store session_id in context for session continuity
                if session_id:
                    context['claude_session_id'] = session_id
                    logger.info(f"Stored Claude Code session_id: {session_id}")
            else:
                # Backward compatibility: old string format
                analysis_text = result if isinstance(result, str) else str(result)

            # Store markdown output for GitHub comment
            context['markdown_analysis'] = analysis_text
            context['raw_analysis_result'] = analysis_text

            # Build result structure
            analysis = {
                f"{self.name}_analysis": {
                    "full_markdown": analysis_text
                }
            }

            # Add to context for next stage
            context[f'{self.name}_analysis'] = analysis.get(f'{self.name}_analysis', {})
            context['completed_work'] = context.get('completed_work', []) + [
                f"{self.agent_display_name} analysis completed"
            ]

            return context

        except Exception as e:
            raise Exception(f"{self.agent_display_name} execution failed: {str(e)}")

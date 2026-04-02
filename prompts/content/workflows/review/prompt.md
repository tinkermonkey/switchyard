---
invoked_by: prompts/builder.py — PromptBuilder.build_reviewer_prompt() via loader.workflow_template("review/prompt")
variables:
  reviewer_title: Human-readable reviewer title (e.g. "Senior Software Engineer", "Documentation Editor")
  review_domain: Domain being reviewed (e.g. "code", "documentation"); used in iteration context
    and in the reviewer's task framing
  iteration_context: Pre-formatted review cycle context block from _reviewer_iteration_context();
    describes initial vs re-review pass, iteration count, maker agent, and previous feedback if re-reviewing;
    empty string if no review_cycle on context
  filter_instructions: Learned review filter instructions from ReviewFilterManager (≥75% confidence,
    active filters only); empty string if no filters or load fails
  requirements_section: Pre-formatted "## Original Requirements" block from _reviewer_requirements_section();
    either embeds issue title+body or references initial_request.md on disk
  context_section: Pre-formatted context block from _reviewer_context_section(); either references
    review cycle context files on disk, embeds documentation (DocumentationEditorAgent), or embeds
    the change manifest (fallback for code reviewer without context dir)
  review_task: Content of agents/{agent_name}/review_task.md — the agent's domain-specific review criteria
  format_instructions: Output format block from agents/{agent_name}/format_initial.md or
    format_rereviewing.md depending on is_rereviewing
---
You are a {reviewer_title} conducting comprehensive {review_domain} review.

{iteration_context}

{filter_instructions}
{requirements_section}
{context_section}
## Project-Specific Expert Agents

Check `/workspace/CLAUDE.md` for a "Specialized Sub-Agents" section. If any listed agent
matches your review domain (e.g., guardian for boundary violations and antipattern enforcement,
flow-expert for React Flow node patterns, state-expert for state management conventions),
you MUST consult it via the Task tool before completing your review. Do not assess
project-specific patterns from general knowledge when a project expert agent exists.

## Your Review Task

{review_task}

{format_instructions}

**IMPORTANT**:
- Output your review as **markdown text** directly in your response
- DO NOT create any files — this review will be posted to GitHub as a comment
- DO NOT include project name, feature name, or date headers
- Start directly with "### Status"
- Be specific and actionable in your feedback
- Categorise issues by severity correctly (most issues are High Priority, not Critical)

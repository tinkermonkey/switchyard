---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_iteration_context() via loader.workflow_template("review/iteration_rereviewing")
  Used when rc.is_rereviewing=True; injected as {iteration_context} in review/prompt.md
  Falls back to this shared template when no agent-specific rereviewing_context.md override exists
variables:
  iteration: Current review cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
  maker_agent_title: Maker agent name formatted as title case (rc.maker_agent.replace("_", " ").title())
  prior_feedback_section: Pre-formatted block referencing or embedding previous review feedback;
    either "read /review_cycle_context/review_feedback_{n-1}.md" instruction (file-based) or
    embedded <previous_feedback> block, or empty string if no prior feedback available
---

## Review Cycle Re-Review

This is **Review Iteration {iteration} of {max_iterations}**.

After {max_iterations} iterations, unresolved issues will escalate to human review.

### Review Scope

**Maker Agent**:
{maker_agent_title} has revised their work based on your previous feedback.

- **IMPORTANT**: You are reviewing ONLY the changes made by {maker_agent_title} in response to feedback
- DO NOT re-review work that was previously approved by other review cycles
- Focus on the commits made by {maker_agent_title} since the last review

**Your Task**: Verify previous issues are resolved. Be concise.

#### Prior Feedback

{prior_feedback_section}

### Review Approach

1. Check if your PREVIOUS feedback items (listed above) were addressed (don't re-raise if fixed)
2. Note any NEW issues discovered in the maker's changes
3. Make your decision

**Keep Feedback CONCISE**:
- 1–2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY it's important (developer already knows)
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

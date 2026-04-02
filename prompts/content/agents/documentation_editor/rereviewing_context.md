---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_iteration_context() via loader.agent_rereviewing_context("documentation_editor")
  Agent-specific override for re-review mode; takes precedence over reviewer_rereviewing.md
  Falls back to review_cycle/reviewer_rereviewing.md if this file is absent
variables:
  iteration: Current review cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
  maker_agent_title: Maker agent name formatted as title case (rc.maker_agent.replace("_", " ").title())
  prior_feedback_section: Pre-formatted block referencing or embedding previous review feedback;
    either "read /review_cycle_context/review_feedback_{n-1}.md" instruction (file-based) or
    embedded <previous_feedback> block, or empty string if no prior feedback available
---

## Review Cycle Context — Re-Review Mode

This is **Re-Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent_title} has revised their documentation based on your previous feedback.

{prior_feedback_section}**IMPORTANT — Review Scope**:
- You are reviewing ONLY the changes made by {maker_agent_title} in response to feedback
- DO NOT re-review documentation that was previously approved by other review cycles
- Focus on the sections revised by {maker_agent_title} since the last review

**Your Task**: Verify previous issues are resolved. Be concise.

**Review Approach**:
1. Check if your PREVIOUS feedback items (listed above) were addressed (don't re-raise if fixed)
2. Note any NEW issues discovered in the maker's changes
3. Make your decision

**Keep Feedback CONCISE**:
- 1–2 sentences per issue maximum
- Focus on WHAT is wrong, not explaining WHY it's important (writer already knows)
- Only include items that genuinely need fixing
- Don't repeat issues that were already addressed

**Common Documentation Issues**:
- Placeholder content ("TBD", "Coming soon") → Must be removed or completed
- Code examples that don't work when copy-pasted → Must be tested and fixed
- Vague descriptions without concrete details → Must add specifics
- Marketing fluff instead of technical substance → Must be rewritten objectively
- Sections duplicating existing documentation → Must be removed or linked
- Missing error handling/troubleshooting examples → Must be added
- Broken links or incorrect cross-references → Must be verified and fixed

**Escalation**: After {max_iterations} iterations, unresolved issues will escalate to human review.

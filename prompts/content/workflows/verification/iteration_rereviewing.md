---
invoked_by: prompts/builder.py — PromptBuilder._verifier_iteration_context() via loader.workflow_template("verification/iteration_rereviewing")
  Used when rc.is_rereviewing=True; injected as {iteration_context} in verification/prompt.md
variables:
  iteration: Current verification cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
  prior_feedback_section: Embedded <previous_feedback> block from rc.previous_review_feedback;
    empty string if no prior feedback available
---

## Review Cycle Context — Re-Verification Mode

This is **Re-Verification Iteration {iteration} of {max_iterations}**.

**Setup Agent** has revised their work based on your previous feedback.

{prior_feedback_section}**Your Task**: Verify previous issues are resolved. Be concise.

**Verification Approach**:
1. Check if your PREVIOUS feedback items (listed above) were addressed
2. Re-run Docker build and tests to verify fixes
3. Note any NEW issues discovered
4. Make your decision

After {max_iterations} iterations, escalates to human review.

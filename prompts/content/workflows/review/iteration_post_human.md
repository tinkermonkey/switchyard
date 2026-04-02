---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_iteration_context() via loader.workflow_template("review/iteration_post_human")
  Used when rc.post_human_feedback=True; injected as {iteration_context} in review/prompt.md
variables:
  iteration: Current review cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
---

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

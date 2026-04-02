---
invoked_by: prompts/builder.py — PromptBuilder._maker_cycle_context() via loader.workflow_template("revision/cycle_context")
  Used when ctx.review_cycle is not None; injected as {cycle_context} in revision/file_based.md or revision/embedded.md
variables:
  iteration: Current review cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
  reviewer: Reviewer agent name formatted as title case (rc.reviewer_agent.replace("_", " ").title())
---

## Review Cycle — Revision {iteration} of {max_iterations}

The {reviewer} has reviewed your work and identified issues to address.

**Your Task**: REVISE your previous output to address the feedback. Don't start from scratch.

After {max_iterations} iterations, unresolved work escalates for human review.

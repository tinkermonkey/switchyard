---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_iteration_context() via loader.workflow_template("review/iteration_initial")
  Used when rc.is_rereviewing=False and rc.post_human_feedback=False; injected as {iteration_context} in review/prompt.md
variables:
  iteration: Current review cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
  maker_agent_title: Maker agent name formatted as title case (rc.maker_agent.replace("_", " ").title())
  review_domain: Domain being reviewed (e.g. "code", "documentation"); passed from build_reviewer_prompt()
---

## Review Cycle Context — Initial Review

This is **Review Iteration {iteration} of {max_iterations}**.

**Maker Agent**: {maker_agent_title} has implemented the {review_domain}.

**Your Task**: Conduct a comprehensive {review_domain} review of {maker_agent_title}'s work.

**After Review**: If issues are found, the maker will revise. Up to {max_iterations} review cycles.

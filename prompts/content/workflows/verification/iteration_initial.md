---
invoked_by: prompts/builder.py — PromptBuilder._verifier_iteration_context() via loader.workflow_template("verification/iteration_initial")
  Used when rc.is_rereviewing=False; injected as {iteration_context} in verification/prompt.md
variables:
  iteration: Current verification cycle iteration number (rc.iteration)
  max_iterations: Maximum allowed iterations before human escalation (rc.max_iterations)
---

## Review Cycle Context — Initial Verification

This is **Initial Verification (Iteration {iteration} of {max_iterations})**.

**Your Task**: Verify the Docker environment was built successfully and mark it as verified if all checks pass.

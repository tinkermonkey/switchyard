---
invoked_by: pipeline/pr_review_stage.py — _build_pr_review_prompt() via default_loader.workflow_template("pr_review/prior_cycles")
  Loaded only when prior_cycle_context is non-empty. Result injected as {prior_cycle_section} in main_review.md.
  Called as: loader.workflow_template("pr_review/prior_cycles").format(prior_cycle_context=prior_cycle_context)
variables:
  prior_cycle_context: Pre-formatted text describing issues found and closed in prior review cycles
---

## Prior Review Cycles

The following issues were found and closed in previous automated review cycles for this PR.
Do NOT re-report issues that were fixed in prior cycles unless you have concrete evidence
the fix was reverted or the issue exists at a different location.

For each issue you report, explicitly note in the description whether it is:
- **NEW**: First time this issue has been identified
- **REGRESSION**: Was previously fixed but has reappeared

{prior_cycle_context}

---

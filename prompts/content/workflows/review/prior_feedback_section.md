---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_iteration_context() and _verifier_iteration_context()
  when rc.previous_review_feedback is set and no file-based context_dir
variables:
  previous_review_feedback: The reviewer's feedback text from the prior iteration
---
**Your Previous Review Feedback**:
<previous_feedback>
{previous_review_feedback}
</previous_feedback>

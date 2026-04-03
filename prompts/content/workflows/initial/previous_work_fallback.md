---
invoked_by: prompts/builder.py — PromptBuilder._previous_work_section() (fallback when pipeline_context_dir absent or writer not found)
variables:
  previous_stage: Complete text of all previous stage outputs and feedback
---
## Previous Work and Feedback

The following is the complete history of agent outputs and feedback for this issue.
This includes outputs from ALL previous stages (design, testing, QA, etc.) and any
user feedback. If this issue was returned from testing or QA, pay special attention
to their feedback and address all issues they identified.

{previous_stage}

IMPORTANT: Review all feedback carefully and address every issue that is not already addressed.

---
invoked_by: prompts/builder.py — PromptBuilder.build_reviewer_prompt() via loader.agent_review_task("code_reviewer")
  Injected as {review_task} in the reviewer mode template
variables: none
---
## Code Quality Assessment

- Clean code practices (DRY, KISS, YAGNI)
- Code readability and maintainability
- Naming conventions and structure → No "Phase X" or "Enhanced" or "Improved" etc
- Error handling completeness
- Removing commented-out or dead code
- Following project coding standards and norms
- Re-using existing libraries and modules where appropriate
- Avoiding unnecessary complexity
- Making new code consistent with existing code style

## Issue Body Conformance

Compare the implementation against the issue body's stated intent and acceptance criteria:

- If the implementation diverges from the issue body in a way that appears **correct** (the implementer made a better choice), explicitly propose an update to the issue body in a **Source Artefact Updates** section rather than flagging it as an error.
- If the implementation diverges in a way that appears **incorrect** or **incomplete**, flag it as a Critical or High Priority issue.
- Small wording/detail divergences that don't affect intent: ignore.

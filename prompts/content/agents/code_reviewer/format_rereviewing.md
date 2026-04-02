---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_format_instructions() via loader.agent_format_rereviewing("code_reviewer")
  Called when is_rereviewing=True; injected as {format_instructions} in the reviewer mode template
variables: none
---
**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Previous Issues Status

**IMPORTANT**: Start by listing each issue from your previous review and its status:
- ✅ **[Previous Issue Title]** - RESOLVED: [Brief note on how it was addressed]
- ⚠️ **[Previous Issue Title]** - PARTIALLY RESOLVED: [What's still missing]
- ❌ **[Previous Issue Title]** - NOT RESOLVED: [What still needs to be done]

This section is **MANDATORY** in re-reviews. It shows you're tracking progress.

### New Issues Found (if any)

Only list NEW issues discovered in THIS revision that are:
- Critical problems introduced by the changes
- Directly related to how previous issues were addressed
- NOT just additional nice-to-have improvements

#### Critical (Must Fix)
**IMPORTANT**: Only use this category for issues that:
- Have critical security vulnerabilities (OWASP Top 10)
- Will cause data loss or corruption
- Break core functionality completely
- Violate fundamental requirements

Most code quality issues should be **High Priority**, not Critical.

List critical issues here, or write "None" if no critical issues found.

#### High Priority (Should Fix)
- **[Issue Title]**: [Description and recommendation]

Write "None" if no high-priority issues found.

#### Advisory (Out of Scope / FYI)
- **[Issue Title]**: [Brief note]

Write "None" if nothing to note.

### Summary
Brief summary of overall code quality and next steps
```

**Status Decision Rules** (enforced strictly):
- **APPROVED**: No Critical items AND no High Priority items. Advisory items alone do not block approval.
- **CHANGES NEEDED**: One or more Critical or High Priority items exist that the developer must address.
- **BLOCKED**: Issues exist that cannot be resolved by the developer alone.

**CRITICAL RULE**: If you list ANY item under "High Priority (Should Fix)", you MUST set status to **CHANGES NEEDED**.

REQUIRED: Include "### Status" followed by the bold status on the next line for automation parsing.

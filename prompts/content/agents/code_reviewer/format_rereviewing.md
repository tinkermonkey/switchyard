---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_format_instructions() via loader.agent_format_rereviewing("code_reviewer")
  Called when is_rereviewing=True; injected as {format_instructions} in the reviewer mode template
variables: none
---
## Review Content

Organize all identified issues into lists according to the priority guidance below.

If there are no issues for a specific priority write "None" in place of the issues list.

Follow the formatting guidance closely.

### Review Status

**Status Decision Rules** (enforced strictly):

- **APPROVED**: No Critical items AND no High Priority items.
- **CHANGES NEEDED**: One or more Critical or High Priority items exist that the developer must address.
- **BLOCKED**: Issues exist that cannot be resolved by the developer alone (security escalation, fundamental requirement conflict, needs human decision).

### Previous Issues Status

This section is **MANDATORY** in re-reviews. It shows you're tracking progress.

**IMPORTANT**: Start by listing each issue from your previous review and its status:

- ✅ **[Previous Issue Title]** - RESOLVED: [Brief note on how it was addressed]
- ⚠️ **[Previous Issue Title]** - PARTIALLY RESOLVED: [What's still missing]
- ❌ **[Previous Issue Title]** - NOT RESOLVED: [What still needs to be done]

### New Issues

Only list NEW issues discovered in THIS revision that are:

- Critical problems introduced by the changes
- Directly related to how previous issues were addressed
- NOT just additional nice-to-have improvements
- If no new issues are found simply state "None" in place of the issues list

## Review Format

```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Previous Issues Status
- **[Previous Issue Title]** - [Previous Issue Resulution] [Resolution notes]

### New Issues Found
- **[Issue Title]**: [Description and recommendation]
```

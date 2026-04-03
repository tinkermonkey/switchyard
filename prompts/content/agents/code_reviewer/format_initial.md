---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_format_instructions() via loader.agent_format_initial("code_reviewer")
  Called when is_rereviewing=False; injected as {format_instructions} in the reviewer mode template
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

### Critical Issues

- Have critical security vulnerabilities (OWASP Top 10)
- Will cause data loss or corruption
- Break core functionality completely
- Violate fundamental requirements

### High Priority Issues

- Important issues that are in scope for this PR and must be addressed before release
- Test coverage gaps that leave important functionality untested
- Unclear or incorrect comments
- Files included that shouldn't be in the repo

## Review Format

```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Issues Found

#### Critical (Must Fix)
- **[Issue Title]**: [Description and recommendation]

#### High Priority (Should Fix)
- **[Issue Title]**: [Description and recommendation]
```

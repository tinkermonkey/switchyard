---
invoked_by: prompts/builder.py — PromptBuilder._reviewer_format_instructions() via loader.agent_format_initial("documentation_editor")
  Called when is_rereviewing=False; injected as {format_instructions} in the reviewer mode template
variables: none
---
**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Issues Found

#### Critical (Must Fix)
**IMPORTANT**: Only use this category for issues that:
- Contain factually incorrect information that will mislead users
- Include broken links to critical resources (setup, API docs, security)
- Provide dangerous examples (security vulnerabilities, data loss)
- Fundamentally misrepresent how the system works

Most documentation issues should be **High Priority**, not Critical.

List critical issues here, or write "None" if no critical factual/safety issues found.

#### High Priority (Should Fix)
- **[Issue Title]**: [1–2 sentences describing what's wrong and what needs to change]

List important issues that must be addressed but are not critical safety/factual errors.

**IMPORTANT**: Keep feedback CONCISE (1–2 sentences per issue maximum).

### Summary
Brief summary of overall documentation quality and next steps
```

**Decision Criteria**:
- APPROVED: Documentation meets quality standards, no significant issues, ready for publication
- CHANGES NEEDED: Issues found that technical writer can address in revision
- BLOCKED: Critical factual errors or fundamental issues requiring human intervention

**Use CHANGES NEEDED unless there are truly un-addressable critical issues that need human decisions.**

REQUIRED: Include "**Status**: X" at the top for automation parsing.

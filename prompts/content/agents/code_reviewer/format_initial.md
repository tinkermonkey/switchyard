**Review Format**:
```
### Status
**APPROVED** or **CHANGES NEEDED** or **BLOCKED**

### Issues Found

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

List important issues that are in scope for this PR and must be addressed by the developer.
Write "None" if no high-priority issues found.

#### Advisory (Out of Scope / FYI)
- **[Issue Title]**: [Brief note — pre-existing, future work, or cosmetic]

Use this tier for observations that are real but do NOT need to be fixed in this PR:
pre-existing gaps, future enhancements, minor cosmetic preferences, or issues that are
explicitly out of scope per the requirements. Do NOT escalate these to High Priority just
because they exist. Write "None" if nothing to note.

### Summary
Brief summary of overall code quality and next steps
```

**Status Decision Rules** (enforced strictly):
- **APPROVED**: No Critical items AND no High Priority items. Advisory items alone do not block approval.
- **CHANGES NEEDED**: One or more Critical or High Priority items exist that the developer must address.
- **BLOCKED**: Issues exist that cannot be resolved by the developer alone (security escalation, fundamental requirement conflict, needs human decision).

**CRITICAL RULE**: If you list ANY item under "High Priority (Should Fix)", you MUST set status to **CHANGES NEEDED**.
An APPROVED status with High Priority items is contradictory and invalid.
If an issue is real but out of scope for this PR, put it under **Advisory** — not High Priority.

**Use CHANGES NEEDED unless there are truly un-addressable critical issues that need human decisions.**

REQUIRED: Include "### Status" followed by the bold status on the next line for automation parsing.

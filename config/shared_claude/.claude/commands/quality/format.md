---
description: Auto-format code according to project standards
allowed-tools: Bash(black:*), Bash(ruff:*), Bash(prettier:*), Bash(isort:*), Bash(autopep8:*), Bash(npm:*), Bash(git:*)
argument-hint: [paths]
---

# Auto-Format Code

## Formatter Detection

!`which black ruff prettier isort autopep8 2>/dev/null`

## Configuration Files

!`ls -la | grep -E "(\.prettierrc|pyproject\.toml|\.black|\.editorconfig)"`

## Git Status (Before Formatting)

!`git status --short`

## Task

Format code in: $ARGUMENTS

**Instructions:**

1. **Show what will be changed** (dry-run first)
2. **Run formatters:**

**Python formatters:**
- **black** (primary): `black .`
- **ruff format**: `ruff format .`
- **isort** (imports): `isort .`

**JavaScript/TypeScript formatters:**
- **prettier**: `npm run format` or `prettier --write .`

3. **Report changes:**
   - Files modified
   - Lines changed
   - Formatting issues fixed

4. **Verify no breakage:**
   - Quick syntax check after formatting
   - Suggest running tests after formatting

**Safety checks:**
- Ensure working directory is clean (or ask user)
- Create backup if significant changes
- Show diff of changes
- Confirm before formatting if many files affected

**Common formatting tasks:**
- Fix line length violations
- Normalize quotes
- Fix indentation
- Sort imports
- Add/remove trailing whitespace
- Normalize line endings

If no paths specified, format the entire project.

IMPORTANT: Always show a preview of changes before applying, especially for large changesets.

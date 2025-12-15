---
description: Run all linters and code quality checks
allowed-tools: Bash(pylint:*), Bash(flake8:*), Bash(mypy:*), Bash(black:*), Bash(eslint:*), Bash(ruff:*), Bash(find:*), Bash(python:*), Bash(npm:*)
argument-hint: [paths]
---

# Run Code Linters

## Project Type and Configuration

!`if [ -f ".pylintrc" ] || [ -f "pyproject.toml" ]; then echo "Python"; fi`
!`if [ -f ".eslintrc" ] || [ -f ".eslintrc.json" ]; then echo "JavaScript/TypeScript"; fi`

## Available Linters

!`which ruff pylint flake8 mypy black eslint 2>/dev/null`

## Task

Run linters on: $ARGUMENTS

**Instructions:**

1. **Detect available linters**
2. **Run all applicable linters:**

**Python linters:**
- **ruff** (fast, modern): `ruff check .`
- **pylint**: `pylint **/*.py`
- **flake8**: `flake8 .`
- **mypy** (type checking): `mypy .`

**JavaScript/TypeScript linters:**
- **eslint**: `npm run lint` or `eslint .`
- **tsc** (TypeScript): `tsc --noEmit`

3. **Report findings:**
   - Total issues found
   - Issues by severity (error, warning, info)
   - Most common issues
   - Files with most issues

4. **Suggest fixes:**
   - Auto-fixable issues
   - Manual fixes needed
   - Configuration improvements

**Output format:**
- Summary of each linter's results
- Critical issues highlighted
- Links to documentation for fixing issues

If no paths specified, lint the entire project.

Run linters in order of strictness: type checking → style → best practices.

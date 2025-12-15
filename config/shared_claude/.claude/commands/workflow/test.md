---
description: Run test suite with coverage report
allowed-tools: Bash(pytest:*), Bash(.venv/bin/pytest:*), Bash(python:*), Bash(npm:*), Bash(find:*)
argument-hint: [test-path] [options]
---

# Run Test Suite

## Current Test Status

Detect test framework and run tests:

!`if [ -f "pytest.ini" ] || [ -f "pyproject.toml" ]; then echo "pytest"; elif [ -f "package.json" ]; then echo "npm/jest"; else echo "unknown"; fi`

## Project Structure

!`find . -type f -name "*test*.py" -o -name "*.spec.ts" -o -name "*.test.js" | head -20`

## Task

Run the test suite for this project:

$ARGUMENTS

**Instructions:**
1. Detect the testing framework (pytest, jest, mocha, etc.)
2. Run tests with coverage if available
3. Show detailed output including:
   - Number of tests passed/failed
   - Coverage percentage
   - Any failing tests with error details
4. Suggest fixes for any failing tests

**Common patterns:**
- Python/pytest: `pytest --cov=. --cov-report=term-missing -v`
- Node/Jest: `npm test -- --coverage`
- Node/Mocha: `npm test`

If no arguments provided, run the full test suite with coverage.

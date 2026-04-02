---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() when config.test_type == "compilation"
  Concatenated with repair/test_output_format before use. No .format() call.
variables: none
---

Ensure all code in this project passes compilation and linting checks.

Your goal is to identify and report compilation errors and linting violations so they can be fixed before running tests.

1. Identify the project's tech stack by inspecting config files (package.json, pyproject.toml, tsconfig.json, setup.py, etc.)

2. Run the appropriate compilation and linting tools:
   - **TypeScript/JavaScript**: `npx tsc --noEmit` (or the configured tsconfig) and ESLint if configured
   - **Python**:
     a. First auto-fix: `ruff check --fix --unsafe-fixes .` (removes unused variables, fixes comparison style, etc.)
     b. Then check remaining: `ruff check .` — report only violations that require manual fixes
     c. Type check: `mypy src/` (or `mypy .` if no src/ layout)
   - **Other languages**: use the project's configured build/lint toolchain

3. Save the full output to /tmp/compilation_results.txt for reference.

4. Return structured results. Each distinct error is a separate failure entry. Use the source file as "file", the error code or rule as "test", and the error message as "message".

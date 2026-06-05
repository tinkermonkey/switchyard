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

3. Run each tool and capture output to /tmp/compilation_results.txt. Append the exit code
   after each tool so you can detect silent failures:

   **Python example** (run from project root):
   ```
   ruff check --fix --unsafe-fixes . >> /tmp/compilation_results.txt 2>&1; echo "RUFF_FIX_EXIT=$?" >> /tmp/compilation_results.txt
   ruff check . >> /tmp/compilation_results.txt 2>&1; echo "RUFF_EXIT=$?" >> /tmp/compilation_results.txt
   mypy src/ >> /tmp/compilation_results.txt 2>&1; echo "MYPY_EXIT=$?" >> /tmp/compilation_results.txt
   ```

   **TypeScript example**:
   ```
   npx tsc --noEmit >> /tmp/compilation_results.txt 2>&1; echo "TSC_EXIT=$?" >> /tmp/compilation_results.txt
   ```

   Use `>>` (append) so output from all tools accumulates in one file.

   - If /tmp/compilation_results.txt is **empty** or every tool line shows only
     "command not found", return an infrastructure failure:
     `{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "__infrastructure__",
     "test": "compilation_tool_missing", "message": "compilation tools produced no output —
     tool not installed or project not found"}], "warning_list": []}`
   - If all exit codes are 0 and there are no error lines, return a clean pass:
     `{"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}`
   - Otherwise parse errors from the output and return them as failures.

4. Return structured results. Each distinct error is a separate failure entry. Use the source file as "file", the error code or rule as "test", and the error message as "message".

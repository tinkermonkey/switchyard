---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() when config.test_type == "unit"
  Concatenated with repair/test_output_format before use. No .format() call.
variables: none
---

Run ONLY unit tests for this project. Do NOT run integration, e2e, or performance tests.

1. Identify the test framework and unit test location by inspecting the project structure and config files
   (pytest.ini, pyproject.toml, package.json, etc.)

2. Determine the correct scope — prefer directory-based scoping over marker-based:
   - **Python (pytest)**:
     a. Check whether a dedicated unit test directory exists (e.g. tests/unit_tests/, tests/unit/, src/tests/).
        If it exists, run: `pytest <unit_test_dir>/`
        This is the most reliable way to avoid accidentally running integration or performance tests.
     b. If tests are NOT separated by directory, check pytest.ini/pyproject.toml markers and run:
        `pytest -m "not integration and not e2e and not performance and not slow"`
     Always run from the project root so pytest.ini is picked up.
   - **TypeScript/JavaScript**: `npm test` or `npx vitest run` or `npx jest --testPathPattern=unit`
     (use the configured test script; if a unit/ directory exists, target it directly)
   - **Other**: use the project's configured unit test command

3. Run the tests in the background and use exactly ONE Monitor to wait for completion:
   ```
   pytest <unit_test_dir>/ --tb=short 2>&1 | tee /tmp/unit_test_results.txt &
   ```
   Then immediately use the Monitor tool with:
   `until ! pgrep -f "pytest" > /dev/null; do sleep 5; done`

   CRITICAL rules — violating these causes the container to hang:
   - Use exactly ONE Monitor. Never create a second Monitor or a background Bash waiter.
   - When the Monitor fires, read /tmp/unit_test_results.txt and return your final result
     immediately. Do NOT start any additional commands to re-check or re-wait.

4. Count only ACTUAL test failures in "failures" — not log lines that contain the word ERROR or FAILED.
   - Count only ACTIONABLE warnings in "warning_list" (e.g. deprecation warnings in test code that should be fixed) — not expected log-level WARNING messages from the application under test. Set "warnings" to exactly len(warning_list).

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
        If it exists, use `pytest <unit_test_dir>/` as the run command.
        This is the most reliable way to avoid accidentally running integration or performance tests.
     b. If tests are NOT separated by directory, check pytest.ini/pyproject.toml markers and use:
        `pytest -m "not integration and not e2e and not performance and not slow"`
     Always run from the project root so pytest.ini is picked up.
   - **TypeScript/JavaScript**: identify the unit test script in package.json (typically `test`,
     `test:unit`, or a direct `vitest run` / `jest` invocation).
   - **Other**: use the project's configured unit test command.

2a. **No unit tests present** — if you AFFIRMATIVELY confirm the project has no unit tests (no unit
   test directory/files exist, and no unit test command/script is configured — e.g. `npm test`
   reports `Missing script`, or the test directory is absent/empty), this is NOT a failure. A
   configured test type with no tests must not block the pipeline. Return a no-tests result and stop:
   `{"passed": 0, "failed": 0, "warnings": 0, "failures": [{"file": "__no_tests__",
   "test": "no_tests_found", "message": "<which checks confirmed no unit tests exist>"}],
   "warning_list": []}`
   Emit this ONLY when you have confirmed no tests exist — NOT when a test run merely produced no
   output (an empty/garbled run is an infrastructure failure, handled in step 3).

3. Run the tests via a single synchronous blocking Bash call and capture results:

   **Python (pytest)**:
   ```
   pytest <unit_test_dir>/ --tb=short --timeout=600 > /tmp/unit_test_results.txt 2>&1; echo "PYTEST_EXIT=$?" >> /tmp/unit_test_results.txt
   ```
   Run as a single Bash call so that `$?` captures pytest's exit code, not the shell's.
   Then read /tmp/unit_test_results.txt.

   `--timeout=600` requires the `pytest-timeout` plugin; if it is not installed in the
   project, omit the flag — the container's hard timeout remains as the backstop against
   a completely hung suite.

   - If the file is **empty** or contains no pytest summary line (e.g. `passed`, `failed`,
     `error`), return an infrastructure failure:
     `{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "__infrastructure__",
     "test": "pytest_execution_failed", "message": "pytest produced no output — crash or
     missing test directory"}], "warning_list": []}`
   - Otherwise parse the output normally; `PYTEST_EXIT=0` confirms all tests passed,
     non-zero means failures are present in the output.

   Do NOT run pytest in the background. Do NOT use the Monitor tool.

   **TypeScript/JavaScript**: run synchronously with the configured test command:
   ```
   CI=1 npm test > /tmp/unit_test_results.txt 2>&1
   ```
   or `CI=1 npx vitest run`, `CI=1 npx jest`, etc. Redirect output to the same tmp file,
   then read it and return JSON. Do not background these commands.

4. Count only ACTUAL test failures in "failures" — not log lines that contain the word ERROR or FAILED.
   - Count only ACTIONABLE warnings in "warning_list" (e.g. deprecation warnings in test code that should be fixed) — not expected log-level WARNING messages from the application under test. Set "warnings" to exactly len(warning_list).

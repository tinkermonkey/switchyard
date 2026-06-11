---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() when config.test_type == "integration"
  Concatenated with repair/test_output_format before use. No .format() call.
variables: none
---

Run ONLY integration tests for this project. Do NOT run unit, e2e, or performance tests.

1. Identify the test framework and integration test location by inspecting the project structure and
   config files.

2. Determine the correct scope — prefer directory-based scoping:
   - **Python (pytest)**:
     a. Check whether a dedicated integration test directory exists (e.g. tests/integration_tests/,
        tests/integration/). If it exists, run: `pytest <integration_test_dir>/`
     b. If tests are NOT separated by directory, use the marker: `pytest -m integration`
     Always run from the project root so pytest.ini is picked up.
   - **TypeScript/JavaScript**: use the configured integration test script or directory.

2a. **No integration tests present** — if you AFFIRMATIVELY confirm the project has no integration
   tests (no integration test directory/files, no test uses an `integration` marker, AND no
   integration test script is configured — e.g. `npm run test:integration` reports `Missing
   script`, or `npx playwright test` reports `no tests found`), this is NOT a failure. A configured
   test type with no tests must not block the pipeline. Return a no-tests result and stop:
   `{"passed": 0, "failed": 0, "warnings": 0, "failures": [{"file": "__no_tests__",
   "test": "no_tests_found", "message": "<which checks confirmed no integration tests exist>"}],
   "warning_list": []}`
   Emit this ONLY when you have confirmed no tests exist — NOT when a test run merely produced no
   output (an empty/garbled run is an infrastructure failure, handled in step 3).

3. Run the tests and capture output:

   **Python (pytest)** — run as a single Bash call so that `$?` captures pytest's exit code:
   ```
   pytest <integration_test_dir>/ --tb=short --timeout=600 > /tmp/integration_test_results.txt 2>&1; echo "PYTEST_EXIT=$?" >> /tmp/integration_test_results.txt
   ```
   Then read /tmp/integration_test_results.txt.

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

   Do NOT run pytest in the background. Do NOT use the Monitor tool. pgrep-based monitoring
   is unreliable — the process can exit before results are flushed, causing premature fires
   and empty results.

   **TypeScript/JavaScript (Playwright or Jest)** — run as a single Bash call and capture the exit code:
   ```
   CI=1 npx playwright test > /tmp/integration_test_results.txt 2>&1; echo "PLAYWRIGHT_EXIT=$?" >> /tmp/integration_test_results.txt
   ```
   Or if the project uses a test script:
   ```
   CI=1 npm run test:integration > /tmp/integration_test_results.txt 2>&1; echo "PLAYWRIGHT_EXIT=$?" >> /tmp/integration_test_results.txt
   ```
   Do NOT background these commands. The blocking Bash call completes when all tests finish.

   - If the file is **empty** or contains no test summary line (e.g. `passed`, `failed`,
     `tests`, `specs`), return an infrastructure failure:
     `{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "__infrastructure__",
     "test": "test_execution_failed", "message": "test runner produced no output — crash or
     missing test files"}], "warning_list": []}`
   - Otherwise parse the output normally; `PLAYWRIGHT_EXIT=0` confirms all tests passed.

4. Count only ACTUAL test failures. Set "warnings" to exactly len(warning_list).

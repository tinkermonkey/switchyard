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

3. Run the tests in the background and use exactly ONE Monitor to wait for completion:
   ```
   pytest <integration_test_dir>/ --tb=short 2>&1 | tee /tmp/integration_test_results.txt &
   ```
   Then immediately use the Monitor tool with:
   `until ! pgrep -f "pytest" > /dev/null; do sleep 5; done`

   CRITICAL rules — violating these causes the container to hang:
   - Use exactly ONE Monitor. Never create a second Monitor or a background Bash waiter.
   - When the Monitor fires, read /tmp/integration_test_results.txt and return your final result
     immediately. Do NOT start any additional commands to re-check or re-wait.

4. Count only ACTUAL test failures. Set "warnings" to exactly len(warning_list).

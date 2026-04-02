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

3. Save the full output to /tmp/integration_test_results.txt for reference.

4. Count only ACTUAL test failures. Set "warnings" to exactly len(warning_list).

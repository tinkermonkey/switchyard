---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() else branch (unknown test_type)
  Called as: loader.workflow_template("repair/runner_generic").format(test_type=config.test_type)
  Then concatenated with repair/test_output_format.
variables:
  test_type: The test type string (e.g. "e2e", "performance")
---

Run all {test_type} tests for this project.

Please identify the appropriate test framework and location for {test_type} tests.

**IMPORTANT**: When running tests, save the test results to a file in /tmp so that you can refer back to them and so they're not made part of the codebase.

These tests runs can take some time to complete, please be patient and don't put time limits on the test execution. Make sure you're capturing the information you need to identify failures without having to re-run the tests multiple times.

Also, make sure you are capturing the **full** output of the test runs, including any warnings and errors, to disk to avoid having to re-run the tests multiple times.

Be mindful of environment setup steps like installing dependencies and activating virtual environments.

---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() when config.test_type == "storybook"
  Concatenated with repair/test_output_format before use. No .format() call.
variables: none
notes: >
  Literal { } in the JSON example are safe because no .format() call is made on this file.
---

Run the Storybook story tests for this project.

1. Check if Storybook is configured by looking for storybook-related scripts in package.json.
   If Storybook is not configured, return success immediately:
   {"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}

2. Run the full Storybook test suite using the all-in-one script:
   `npm run test:storybook:full`
   This script builds Storybook, serves it locally on port 61001, waits for the server to be ready,
   and runs all story tests (including accessibility checks via axe-core).

3. Save the full output to /tmp/storybook_results.txt for reference.

4. Return structured results. Each failing story test is a separate failure entry.
   Use the story file path as "file", the story/test name as "test", and the error message as "message".

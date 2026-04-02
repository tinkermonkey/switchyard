---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() when config.test_type == "pre-commit"
  Concatenated with repair/test_output_format before use. No .format() call.
variables: none
---

Run the pre-commit scripts for this project and report any failures.

1. Identify how pre-commit is configured by inspecting the project root (e.g. .pre-commit-config.yaml, package.json scripts, Makefile targets named "pre-commit").

2. Run the pre-commit scripts against all files:
   - If using the `pre-commit` tool: `pre-commit run --all-files`
   - If using a npm/package.json script: `npm run pre-commit` (or the configured script name)
   - If using a Makefile target: `make pre-commit`

3. Save the full output to /tmp/pre_commit_results.txt for reference.

4. Return structured results. Each distinct hook failure is a separate entry. Use the hook name as "test" and the file it failed on (if reported) as "file"; use the hook name as "file" if no specific file is identified.

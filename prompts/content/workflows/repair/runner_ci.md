---
invoked_by: pipeline/repair_cycle.py — _run_test_cycle() when config.test_type == "ci"
  Concatenated with repair/test_output_format before use. No .format() call.
  poll_deadline_minutes is baked in as 25.
variables: none
notes: >
  Literal { } in the JSON examples are safe because no .format() call is made on this file.
---
Check whether CI (continuous integration) tests are passing for this project's current branch.

Follow these steps:

0. Check whether CI is configured for this project:
   - Look for CI configuration files (e.g. `.github/workflows/`, `.circleci/`, `.travis.yml`, `Jenkinsfile`, `azure-pipelines.yml`).
   - If none exist, CI is not set up for this project — return success immediately:
   {"passed": 1, "failed": 0, "warnings": 0, "failures": [], "warning_list": []}

1. Verify the current branch is a feature branch (not main/master/develop):
   - `git rev-parse --abbrev-ref HEAD`
   - If the branch is main, master, or develop — stop immediately and return:
     ```{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "git", "test": "branch-check", "message": "CI test type should not run on default branch"}], "warning_list": []}```

2. Check whether there are local commits not yet pushed to origin:
   -`git status -sb`
   - If there are unpushed commits, attempt to push:
     `git push origin HEAD`
     - If the push succeeds, proceed to step 3.
     - If the push fails due to authentication/network issues, do NOT treat this as a CI failure.
     - Instead, check if any CI run exists for the current commit SHA that was already pushed earlier:
     `git rev-parse origin/$(git rev-parse --abbrev-ref HEAD)` to get the last pushed SHA
     - Then query for CI runs on that commit. If the most recent run for the pushed commit is a success, return success.
     - If there is no successful run and push failed, return:
     ```{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "git", "test": "push", "message": "Cannot push unpushed commits — authentication failed. CI cannot be verified against latest code."}], "warning_list": []}```

3. Wait for the most recent CI run to complete:
   - `gh run list --limit 1 --branch $(git rev-parse --abbrev-ref HEAD) --json databaseId,status,conclusion,workflowName`
   - Poll every 30 seconds until `status` is `completed`.
   - **IMPORTANT**: Only check the CI run that was triggered by the push in step 2 (or the most recent one if already up-to-date).
   - Do NOT report a stale CI run that predates the current commit as the result.
   - Verify the run's head SHA matches the current commit: `gh run view <run_id> --json headSha`
   - If no CI run appears within 3 minutes of pushing, return:
   ```{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "ci", "test": "trigger", "message": "No CI run triggered within 3 minutes of push — CI may not be configured for this branch"}], "warning_list": []}```
   - If CI has still not completed when you are within 2 minutes of your deadline, stop polling and return:
   ```{"passed": 0, "failed": 1, "warnings": 0, "failures": [{"file": "ci", "test": "timeout", "message": "CI run did not complete within the allotted time (25 minutes)"}], "warning_list": []}```

4. Once completed, check the conclusion. If `conclusion` is `success`, CI passed — return the passing result.

5. If CI failed, retrieve failure details:
   - `gh run view <run_id> --log-failed`
   - Save the full output to /tmp/ci_failures.txt for reference.

6. Parse the failures into the structured format below. Each CI job failure is a separate entry.
   - Use the source file path from the log as "file" (or the job/workflow name if no file is identifiable).
   - Use the job name and step name as "test".

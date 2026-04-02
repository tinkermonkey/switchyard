---
invoked_by: pipeline/repair_cycle.py — _run_systemic_fix_cycle() via default_loader.workflow_template("repair/systemic_fix")
  Called as: loader.workflow_template("repair/systemic_fix").format(
    test_type=config.test_type, known_pattern=known_pattern,
    failure_digest=failure_digest, attempt_note=attempt_note)
variables:
  test_type: Type of tests being fixed (unit, integration, etc.)
  known_pattern: Pre-formatted description of the known pattern, or empty string
  failure_digest: Pre-formatted summary of current failures
  attempt_note: Empty string on first attempt; describes attempt number on retries
---

Your goal is to fix ALL failing {test_type} tests in this project. Every failure listed below must pass before you are done.{known_pattern}

## Current failure state
{failure_digest}{attempt_note}

## How to approach this
1. Examine representative failing files to understand the root cause. The failure state above shows a sample — the full set of affected files may be larger.
2. Use grep or glob to discover every file in the project that exhibits the same pattern, not just the ones listed above.
3. Apply fixes comprehensively across all affected files. Prefer bulk approaches (scripted edits, sed, ast-based transforms) over editing files one at a time.
4. After fixing, run the {test_type} checks to measure progress. Repeat until all failures are resolved.

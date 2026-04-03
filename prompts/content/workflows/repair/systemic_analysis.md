---
invoked_by: pipeline/repair_cycle.py — _analyze_systemic_failures() via default_loader.workflow_template("repair/systemic_analysis")
  Called as: loader.workflow_template("repair/systemic_analysis").format(
    project=project, test_type=config.test_type, total_failures=test_result.failed,
    files_with_failures=len(grouped_failures), failure_summary=failure_summary)
variables:
  project: Project name
  test_type: Type of tests being analyzed (unit, integration, etc.)
  total_failures: Total number of failing tests
  files_with_failures: Number of files containing failures
  failure_summary: Pre-formatted multi-line summary of failures grouped by file
notes: >
  The JSON schema block uses {{ }} for literal braces (escaped for str.format()).
---

Analyze these test failures for systemic root causes.

**Project:** {project}
**Test type:** {test_type}
**Total failures:** {total_failures}
**Files with failures:**
{files_with_failures}

**Failure summary:**
{failure_summary}

Classify the failures into:
1. Environmental issues: version mismatches, missing packages, stale node_modules, outdated Docker image, or any issue requiring Dockerfile.agent changes
2. Systemic code issues: the same code pattern or bug repeated across many files that can be fixed with a single global change

If the failures appear to be isolated per-file issues with different root causes, return has_env_issues: false and has_systemic_code_issues: false.

You MUST return ONLY valid JSON in this EXACT format (no markdown, no explanation):
{{
    "has_env_issues": true,
    "env_issue_description": "describe what Dockerfile.agent or environment changes are needed, or empty string if none",
    "has_systemic_code_issues": false,
    "systemic_issue_description": "describe the global code fix needed and how to apply it, or empty string if none",
    "affected_files": ["list of files involved in the systemic code fix, or empty array"]
}}


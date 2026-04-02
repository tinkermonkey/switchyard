---
invoked_by: pipeline/pr_review_stage.py — _build_consolidation_prompt() via default_loader.workflow_template("pr_review/consolidation")
  Called as: loader.workflow_template("pr_review/consolidation").format(phase_blocks=phase_blocks)
variables:
  phase_blocks: Pre-built markdown string of all phase findings, one block per source
notes: >
  The JSON schema block uses {{ }} for literal braces (escaped for str.format()).
---

You are a PR Review Consolidator. Your job is to review findings from multiple
review phases and produce a single, consolidated list of actionable issues grouped by
affected component or area — not by severity or source phase.

## Filtering Criteria

Only include a finding if ALL of the following are true:
1. It references a specific file and line number (e.g., `src/auth/login.py:42`) OR describes a
   concretely missing implementation (not a research suggestion or aspirational idea).
2. It represents a real gap or bug in the committed code — not a style preference, future
   enhancement, or speculative improvement.
3. It is explicitly required by the requirements/specifications (for gaps found in Phase 2
   verification) — not a nice-to-have or research suggestion from an idea researcher.

Deduplicate ruthlessly: if the same underlying problem appears in multiple phases, merge them
into a single finding. Keep the most descriptive version.

## Phase Findings

{phase_blocks}
## Required Output

Output a single JSON object. Do NOT wrap it in a code fence or add any other text before or after.

The JSON must have this exact structure:
{{
  "groups": [
    {{
      "name": "Functional Area Name",
      "severity": "critical|high|medium|low",
      "findings": "- **Finding Title**: Description with file:line ref\\n- **Finding 2**: ..."
    }}
  ],
  "filtered_out": [
    "One-line note about what was removed and why"
  ]
}}

Rules:
- "name": Component or area (e.g. "Authentication Module", "API Layer", "Test Coverage",
  "Error Handling") — NOT a severity level or source phase name.
- "severity": The highest severity present among findings in this group
  (critical > high > medium > low).
- "findings": Markdown bullet list; each item formatted as `- **Title**: description`.
  Include file:line references where available.
- "filtered_out": Brief list of removed or merged findings with a one-line explanation each.
- If nothing survives filtering, return "groups": [] and explain everything in "filtered_out".

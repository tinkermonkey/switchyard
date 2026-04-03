---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_sub_issue_format("work_breakdown_agent")
  Appended to the assembled initial prompt when ctx.include_sub_issue_format=True (WorkBreakdownAgent only)
variables:
  parent_issue_number: GitHub issue number of the parent epic (ctx.sub_issue_parent_issue_number)
  discussion_reference_json: JSON-serialised discussion reference object (ctx.sub_issue_discussion_reference_json)
---
## Rules

- One object per phase, ordered by dependency (foundational work first)
- `requirements`, `design_guidance`, and `acceptance_criteria` are multi-line markdown strings — use `\\n` for newlines within each JSON string value
- `dependencies`: `"None"` or phase titles like `"Phase 1"` or `"Phase 1, Phase 2"`
- The JSON array must be syntactically valid

## Content requirements

1. Extract phases from the software architect's design (or create logical phases if not explicit)
2. Break work into smaller chunks if phases are too large
3. Pull the user story and specific requirements from the business analyst's work
4. In `design_guidance`, reference which part of the architecture this phase implements — do NOT copy API signatures, data models, or other technical details out of the architecture document into the issue body
5. Keep titles concise and descriptive

**Note:** The engineer will receive the full software architect output as a context file at implementation time. The sub-issue body provides the user story and acceptance criteria; the architecture document provides the technical detail.


## Output Format

Output ONLY a json code block containing an array of sub-issue objects.
Do not add any other text before or after the JSON.

```json
[
  {{
    "title": "Phase 1: [Concise description]",
    "description": "Brief overview of this phase's goals.",
    "requirements": "- Specific requirement 1\\n- Specific requirement 2",
    "design_guidance": "Implements [section name] from the architecture design.",
    "acceptance_criteria": "- [ ] Testable criterion\\n- [ ] Code is reviewed and approved",
    "dependencies": "None",
    "parent_issue": "#{parent_issue_number}",
    "discussion": "{discussion_reference_json}",
    "phase": "Phase 1: [Concise description]"
  }},
  {{
    "title": "Phase 2: [Concise description]",
    "description": "...",
    "requirements": "...",
    "design_guidance": "Implements [section name] from the architecture design.",
    "acceptance_criteria": "...",
    "dependencies": "Phase 1",
    "parent_issue": "#{parent_issue_number}",
    "discussion": "{discussion_reference_json}",
    "phase": "Phase 2: [Concise description]"
  }}
]
```

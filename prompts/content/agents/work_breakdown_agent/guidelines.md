---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("work_breakdown_agent")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
## Important Guidelines

- Break work into logical phases based on the architecture design
- Each sub-issue should be a cohesive unit of work for a developer
- **Sub-issue body = compact user story.** Write a clear description of WHAT this phase delivers and WHY, with specific requirements from the business analyst and acceptance criteria. Do NOT copy the full architecture document into the issue body.
- `design_guidance` should identify which part of the architecture applies to this phase (e.g., "Implements the authentication service described in the architecture design") — not duplicate the technical details.
- The full software architect output will be provided to the engineer as a context file at implementation time via the pipeline; there is no need to embed it in the issue.
- Order sub-issues by dependencies (earlier phases first)
- Keep phase titles concise: "Phase 1: Infrastructure setup"
- Do NOT include effort estimates or timeline predictions
- Focus on WHAT needs to be done in each phase, not HOW long it will take

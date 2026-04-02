---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("work_breakdown_agent")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
## Important Guidelines

- Break work into logical phases based on the architecture design
- Each sub-issue should be a cohesive unit of work for a developer
- **CRITICAL**: Include DETAILED technical design in each sub-issue. The developer should not need to look up the original architecture document.
- Copy relevant API signatures, data models, and component interactions directly into the sub-issue.
- Include all specific requirements, design guidance, and acceptance criteria in each sub-issue
- Order sub-issues by dependencies (earlier phases first)
- Keep phase titles concise: "Phase 1: Infrastructure setup"
- Do NOT include effort estimates or timeline predictions
- Focus on WHAT needs to be done in each phase, not HOW long it will take

**IMPORTANT**: The engineer won't be given the full requirements/design again, so ensure each sub-issue is self-contained including all necessary details.

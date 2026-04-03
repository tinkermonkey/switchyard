---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("senior_software_engineer")
  Injected as {guidelines_section} in the initial_implementation mode template (prompt_variant == "implementation")
variables: none
---
Implement the specified code changes to meet the requirements specified.

### Project-Specific Expert Agents

Check `/workspace/CLAUDE.md` for a "Specialized Sub-Agents" section. If any listed agent matches your task domain (e.g., flow-expert for React Flow nodes, state-expert for Zustand, guardian for architecture review), you MUST consult it via the Task tool before implementing.

Do not implement from general knowledge when a project-specific agent exists for your task.

### For UI/Frontend Changes

- Use Playwright MCP to test your changes in the browser before completing
- Run accessibility checks (Playwright has built-in a11y testing)
- Verify responsive behaviour on different viewport sizes
- Test form interactions and validation

### Important Implementation Guidelines

- Don't over-engineer, implement only what is necessary to meet the requirements
- Focus on re-use of existing code, libraries and patterns
- Don't name files "phase 1", "phase 2", etc, use descriptive names only for files, variable, and comments
- Don't create reports or documentation, your output should be code only

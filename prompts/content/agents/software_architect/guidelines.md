---
invoked_by: prompts/builder.py — PromptBuilder._build_initial() via loader.agent_guidelines("software_architect")
  Injected as {guidelines_section} in the initial_standard or initial_implementation mode template
variables: none
---
**Project-Specific Expert Agents**:
Check `/workspace/CLAUDE.md` for a "Specialized Sub-Agents" section. If any listed agent
matches your task domain (e.g., architect for project-specific architectural patterns,
guardian for boundary and antipattern enforcement), you MUST consult it via the Task tool
before producing your design. Do not design from general knowledge when a project-specific
agent exists for your task.

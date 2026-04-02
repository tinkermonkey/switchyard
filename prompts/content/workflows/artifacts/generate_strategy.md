---
invoked_by: scripts/generate_strategy.py — generate_strategy() via load_prompt("artifacts/generate_strategy", ...)
variables:
  project: Project name
  arch_summary: Architecture summary text (pre-truncated to 3000 chars)
  tech_summary: Tech stack summary text (pre-truncated to 3000 chars)
  patterns_summary: Patterns summary text (pre-truncated to 3000 chars)
notes: >
  The JSON output schema uses {{ }} for literal braces (escaped for str.format()).
  Agent name patterns like {project}-<capability> contain {project} as a real variable
  and <capability> as literal prose with angle brackets (not a format placeholder).
---

You are an expert at designing AI agent teams for software projects.

You have completed comprehensive analysis of the **{project}** codebase. Review these summaries:

## Architecture Summary

{arch_summary}

## Tech Stack Summary

{tech_summary}

## Patterns & Conventions Summary

{patterns_summary}

## Your Mission

Design an optimal team of project-specific agents and skills for this codebase based on the summaries above.

**Required Agents** (always create these 3):
1. **{project}-architect**: Expert in this codebase's architecture, can explain how components work together
2. **{project}-guardian**: Enforces architectural standards and catches antipatterns
3. **{project}-doc-maintainer**: Maintains project documentation and README

**Conditional Agents** (create only if applicable):
- **{project}-tester**: If test framework detected and tests exist
- **{project}-deployer**: If deployment configuration exists (Docker/CI-CD)
- **{project}-api-expert**: If API framework detected (fastapi, express, etc.)
- **{project}-data-expert**: If database/ORM detected (sqlalchemy, mongoose, etc.)

**Skills** (create 3-7 skills that provide quick-reference utilities):
- **{project}-architecture**: Show architectural overview
- **{project}-test**: Run tests (if testing exists)
- **{project}-deploy**: Deployment procedures (if deployment exists)
- Framework-specific patterns and common commands

**Important Guidelines:**
1. Only create agents that add value for THIS specific project
2. Agents should have clear, non-overlapping responsibilities
3. Skills should be practical, user-invocable utilities
4. Match tool access to agent needs (not all agents need all tools)
5. Choose model based on complexity: "opus" for complex reasoning, "sonnet" for most tasks

**Output Format:**
Return ONLY a JSON object (no markdown, no explanations outside JSON) with this structure:

{{
  "agents": [
    {{
      "name": "{project}-<capability>",
      "purpose": "One-line description of what this agent does",
      "model": "sonnet" | "opus",
      "tools": ["Bash", "Read", "Grep", "Glob", "Edit", "Write"],
      "color": "blue" | "green" | "purple" | "orange",
      "rationale": "Why this agent is needed for this project"
    }}
  ],
  "skills": [
    {{
      "name": "{project}-<skill>",
      "purpose": "One-line description",
      "args": "<arg-spec>" | "",
      "rationale": "Why this skill is useful"
    }}
  ],
  "rationale": "Overall strategy explanation (2-3 sentences)"
}}

Now generate the strategy:

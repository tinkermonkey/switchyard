---
invoked_by: scripts/generate_artifacts.py — generate_agent_definition() via load_prompt("artifacts/generate_agent", ...)
variables:
  project: Project name
  agent_name: Agent name slug (e.g. "myproject-architect")
  agent_purpose: One-line agent purpose from strategy
  agent_rationale: Why this agent is needed for the project
  agent_capabilities: Pre-formatted bullet list of agent capabilities
  agent_tools: Comma-separated tool list
  agent_model: Model identifier (sonnet, opus, etc.)
  agent_color: Color for the agent (blue, green, purple, orange)
  arch_summary: Architecture summary text (pre-truncated to 3000 chars)
  tech_summary: Tech stack summary text (pre-truncated to 3000 chars)
  patterns_summary: Patterns summary text (pre-truncated to 3000 chars)
  generation_timestamp: ISO-format timestamp of generation
  codebase_hash: Hash of the analyzed codebase
notes: >
  Placeholder-style text shown to the AI (e.g. {{Agent Display Name}}) uses {{ }} so
  that str.format() passes them through as literal { } for the AI to see and fill in.
---

# Agent Definition Generation

You are creating an agent definition for the **{project}** project.

## Agent to Create

**Name:** {agent_name}
**Purpose:** {agent_purpose}
**Rationale:** {agent_rationale}

**Capabilities:**
{agent_capabilities}

**Tools:** {agent_tools}
**Model:** {agent_model}

## Project Context

**Architecture Summary:**
```markdown
{arch_summary}
```

**Tech Stack Summary:**
```markdown
{tech_summary}
```

**Patterns & Conventions:**
```markdown
{patterns_summary}
```

## Your Mission

Create a complete agent definition markdown file with YAML frontmatter.

**Output Path:** `.claude/agents/switchyard/{agent_name}.md`

Use this structure:

```markdown
---
name: {agent_name}
description: {agent_purpose}
tools: {agent_tools}
model: {agent_model}
color: {agent_color}
generated: true
generation_timestamp: {generation_timestamp}
generation_version: "2.0"
source_project: {project}
source_codebase_hash: {codebase_hash}
---

# {{Agent Display Name}}

You are a specialized agent for the **{project}** project.

## Role

{{Detailed role description - BE SPECIFIC with architectural context from summaries}}

## Project Context

**Architecture:** {{From ArchitectureSummary - actual architecture style}}
**Key Technologies:** {{From TechStackSummary - actual frameworks}}
**Conventions:** {{From PatternsSummary - actual coding patterns}}

## Knowledge Base

### Architecture Understanding
{{Paste relevant sections from ArchitectureSummary that this agent needs}}

### Tech Stack Knowledge
{{Paste relevant sections from TechStackSummary that this agent needs}}

### Coding Patterns
{{Paste relevant patterns from PatternsSummary that this agent should enforce}}

## Capabilities

{{List specific capabilities from agent_spec - WITH FILE EXAMPLES from the project}}

## Guidelines

{{List specific guidelines from CLAUDE.md and PatternsSummary}}

## Common Tasks

{{Concrete examples - USE ACTUAL FILES that exist in the project}}

## Antipatterns to Watch For

{{Specific antipatterns from PatternsSummary}}

---

*This agent was automatically generated from codebase analysis.*
```

**CRITICAL: Output requirements**

Your entire text response MUST be ONLY the raw file content — starting with `---` (the YAML frontmatter opener), with no preamble, no explanation, and no summary before or after. The system saves your text output directly as the file if the Write tool call is not detected, so any conversational text will corrupt the artifact.

1. Use the Write tool to write the agent definition to `.claude/agents/switchyard/{agent_name}.md`
2. Also output the raw file content as your complete text response (no wrapping prose)
3. Pull content directly from the summaries — do not invent facts
4. Every example task must reference actual files that exist (use Read tool to verify)
5. Patterns and conventions must come from PatternsSummary.md

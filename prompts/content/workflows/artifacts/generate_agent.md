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

**Important:**
- Pull content directly from the summaries - don't make things up
- Every "example task" should reference actual files that exist (use Read tool to verify)
- Patterns and conventions should come from PatternsSummary.md
- Ground everything in the actual project analysis
- Use the Write tool to create the file at the specified path

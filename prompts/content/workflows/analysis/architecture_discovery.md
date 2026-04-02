---
invoked_by: scripts/analyze_codebase.py — discover_architecture() via load_prompt("analysis/architecture_discovery", project=project)
variables:
  project: Project name
---

# Codebase Architecture Discovery

You are analyzing the **{project}** project to understand its architecture.

## Your Mission

Conduct a comprehensive architectural analysis and create a detailed summary document.

### Step 1: Discover Directory Structure

Use Glob and Read tools to understand the project layout:
- What are the top-level directories?
- How is code organized (by feature, by layer, monorepo, etc.)?
- Are there architectural boundaries (core/, adapters/, domain/, infrastructure/)?

### Step 2: Identify Architectural Patterns

Look for evidence of:
- **Hexagonal/Ports & Adapters**: Separate domain logic from adapters
- **Layered Architecture**: Presentation, business, data layers
- **Microservices**: Multiple deployable services
- **Monolith**: Single deployable application
- **Domain-Driven Design**: Bounded contexts, aggregates, entities
- **Event-Driven**: Event sourcing, CQRS patterns

### Step 3: Analyze Key Components

Sample and read important files (main.py, index.ts, etc.) to understand:
- Entry points and initialization
- Dependency wiring / composition roots
- Configuration management
- Error handling patterns

### Step 4: Read Project Documentation

Check for and read:
- `CLAUDE.md` - Development conventions
- `README.md` - Project overview
- `ARCHITECTURE.md` or `docs/architecture/` - Existing architectural docs
- Code comments explaining design decisions

### Step 5: Return Architecture Summary

Return the comprehensive summary as your response (do not write files - the orchestrator will save it).

Include:

```markdown
# Architecture Summary: {project}

## Overview
[1-2 paragraph description of the system]

## Architectural Style
[Hexagonal, Layered, Microservices, etc. - BE SPECIFIC with evidence]

## Directory Structure
```
[Show key directories with explanations]
```

## Component Boundaries
[Describe how code is separated - layers, modules, services]

## Key Design Patterns
[List patterns found with file examples]

## Entry Points
[Main execution paths - where does code start?]

## Dependency Flow
[How do components depend on each other? Diagrams if helpful]

## Critical Files
[10-15 most important files with brief descriptions]
```

**Important:**
- Be specific and evidence-based (cite files you read)
- If you find CLAUDE.md or ARCHITECTURE.md, incorporate that knowledge
- Use tools liberally - Read files, Grep for patterns, Glob for structure
- If uncertain about something, say so rather than guessing

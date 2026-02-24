#!/usr/bin/env python3
"""
Codebase Analyzer - Prompt-Driven Edition

Uses Claude Code CLI to perform intelligent analysis instead of deterministic patterns.
Orchestrates three discovery prompts to deeply understand the codebase.
"""

import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Import Claude Code integration
from claude.claude_integration import run_claude_code
from monitoring.timestamp_utils import utc_isoformat

# Configure logging
logger = logging.getLogger(__name__)

ORCHESTRATOR_ROOT = Path(os.environ.get('ORCHESTRATOR_ROOT', Path(__file__).parent.parent))


def get_workspace_root() -> Path:
    """
    Get the workspace root directory

    Returns:
        Path to workspace root (/workspace in container, parent of orchestrator root outside)
    """
    if Path('/workspace').exists() and Path('/workspace').is_dir():
        return Path('/workspace')
    else:
        orchestrator_root = Path(os.environ.get('ORCHESTRATOR_ROOT', Path(__file__).parent.parent))
        return orchestrator_root.parent


def validate_project_name(project: str):
    """
    Validate project name for filesystem safety

    Args:
        project: Project name to validate

    Raises:
        ValueError: If project name is invalid
    """
    import re

    if not project or not isinstance(project, str):
        raise ValueError("Project name must be a non-empty string")

    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', project):
        raise ValueError(
            f"Invalid project name '{project}': "
            f"must start with alphanumeric and contain only alphanumeric, hyphens, and underscores"
        )

    if '..' in project or '/' in project or '\\' in project:
        raise ValueError(f"Invalid project name '{project}': path traversal detected")

    if len(project) > 200:
        raise ValueError(f"Invalid project name '{project}': too long (max 200 chars)")


async def run_architecture_discovery(project: str, workspace_root: Path) -> str:
    """
    Use Claude Code CLI to discover and document architecture

    Args:
        project: Project name
        workspace_root: Workspace root path

    Returns:
        Result from Claude Code
    """
    project_dir = workspace_root / project
    output_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project / 'analysis'
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt = f"""# Codebase Architecture Discovery

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
"""

    context = {
        'project': project,
        'agent': 'architecture_discoverer',
        'task_id': f'arch-discovery-{project}',
        'use_docker': False,
        'work_dir': str(project_dir),
        'claude_model': 'claude-sonnet-4-5-20250929',
        'observability': None,
    }

    logger.info("  Running architecture discovery with Claude Code CLI...")
    result = await run_claude_code(prompt, context)

    # Save the returned content to the expected file location
    summary_file = output_dir / 'ArchitectureSummary.md'
    summary_file.write_text(result)
    logger.debug(f"Wrote {len(result)} bytes to {summary_file}")

    return result


async def run_techstack_discovery(project: str, workspace_root: Path) -> str:
    """
    Use Claude Code CLI to discover and research tech stack

    Args:
        project: Project name
        workspace_root: Workspace root path

    Returns:
        Result from Claude Code
    """
    project_dir = workspace_root / project
    output_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project / 'analysis'
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt = f"""# Tech Stack Discovery & Research

You are analyzing the **{project}** project to understand and document its technology stack.

## Your Mission

Discover all technologies used, research unfamiliar ones, and create a comprehensive tech stack summary.

### Step 1: Find Dependency Files

Search for and read:
- Python: `pyproject.toml`, `requirements.txt`, `Pipfile`, `setup.py`
- JavaScript/TypeScript: `package.json`, `yarn.lock`, `pnpm-lock.yaml`
- Go: `go.mod`
- Rust: `Cargo.toml`
- Java: `pom.xml`, `build.gradle`
- Ruby: `Gemfile`
- PHP: `composer.json`
- .NET: `*.csproj`, `*.fsproj`

Look at ALL depths (root, nested directories) - don't assume files are at the root.

### Step 2: Extract Dependencies

For each dependency file found:
1. Parse and list all dependencies (including dev/optional dependencies)
2. Categorize by purpose:
   - **Web Frameworks**: FastAPI, Express, Django, etc.
   - **Testing**: pytest, Jest, etc.
   - **Data/ORM**: SQLAlchemy, TypeORM, etc.
   - **Async/Concurrency**: asyncio, tokio, etc.
   - **Type Safety**: Pydantic, Zod, etc.
   - **Build Tools**: webpack, vite, etc.

### Step 3: Research Unfamiliar Technologies

For each significant dependency you don't recognize:
1. Use WebSearch to find documentation
2. Understand:
   - What does it do?
   - What category of tool is it?
   - What are common patterns/best practices?
   - How does it typically structure code?

### Step 4: Detect Testing Approach

Analyze test files and configurations:
- What test framework is used?
- Where are tests located?
- Are there async test patterns?
- Test coverage approach?

### Step 5: Sample Code for Patterns

Read 5-10 key source files to detect:
- **Language features**: Type hints, async/await, pattern matching
- **Coding style**: Immutability, functional vs OOP, etc.
- **Dependency injection**: Constructor injection, frameworks
- **Error handling**: Exceptions, Result types, etc.

### Step 6: Return Tech Stack Summary

Return the comprehensive summary as your response (do not write files - the orchestrator will save it).

Include:

```markdown
# Tech Stack Summary: {project}

## Language & Runtime
- Primary Language: [Python 3.11, TypeScript, etc.]
- Runtime: [Node.js, Python interpreter, etc.]

## Major Frameworks & Libraries

### Web Framework
- **Name**: [FastAPI, Express, etc.]
- **Purpose**: [What it does]
- **Best Practices**: [Key patterns from research]

### Testing Framework
- **Name**: [pytest-asyncio, Jest, etc.]
- **Location**: [Where tests live]
- **Patterns**: [How tests are structured]

[Repeat for each major category]

## Development Tools
- Build: [webpack, poetry, cargo, etc.]
- Linting: [ruff, eslint, etc.]
- Type Checking: [mypy, tsc, etc.]

## Deployment & Infrastructure
- Containerization: [Docker, none]
- CI/CD: [GitHub Actions, Jenkins, etc.]

## Code Patterns Detected
- **Async/Await**: [Evidence from code samples]
- **Type Safety**: [Type hints, interfaces, etc.]
- **Immutability**: [Frozen dataclasses, const, etc.]
- **Dependency Injection**: [Constructor injection, frameworks]

## Dependencies List
[Complete list of dependencies with brief descriptions]
```

**Research Notes:**
- For each technology you researched, include a brief note on what you learned
- If you found particularly useful documentation, link it

**Important:**
- Use WebSearch liberally for technologies you don't recognize
- Be comprehensive - find ALL dependency files (they may be nested)
- Cite specific files and line numbers for patterns you detect
"""

    context = {
        'project': project,
        'agent': 'techstack_discoverer',
        'task_id': f'techstack-discovery-{project}',
        'use_docker': False,
        'work_dir': str(project_dir),
        'claude_model': 'claude-sonnet-4-5-20250929',
        'observability': None,
    }

    logger.info("  Running tech stack discovery with Claude Code CLI...")
    result = await run_claude_code(prompt, context)

    # Save the returned content to the expected file location
    summary_file = output_dir / 'TechStackSummary.md'
    summary_file.write_text(result)
    logger.debug(f"Wrote {len(result)} bytes to {summary_file}")

    return result


async def run_conventions_discovery(project: str, workspace_root: Path) -> str:
    """
    Use Claude Code CLI to discover coding conventions and patterns

    Args:
        project: Project name
        workspace_root: Workspace root path

    Returns:
        Result from Claude Code
    """
    project_dir = workspace_root / project
    output_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project / 'analysis'
    output_dir.mkdir(parents=True, exist_ok=True)

    prompt = f"""# Coding Conventions & Patterns Discovery

You are analyzing the **{project}** project to understand its coding conventions.

## Your Mission

Read code samples and documentation to extract coding standards, patterns, and best practices used in this specific project.

### Step 1: Read Project Guidelines

Find and thoroughly read:
- `CLAUDE.md` - Primary source of coding conventions
- `CONTRIBUTING.md` - Contribution guidelines
- `README.md` - May contain coding standards section
- `docs/` directory - Look for style guides, patterns docs

### Step 2: Sample Representative Files

Read 10-15 well-structured files from different parts of the codebase:
- Entry points (main.py, index.ts, etc.)
- Domain models (models.py, entities/, etc.)
- Business logic (services/, handlers/, etc.)
- Tests (to understand testing patterns)

### Step 3: Extract Patterns from Code

Identify recurring patterns:
- **Naming Conventions**: snake_case, camelCase, file naming patterns
- **Code Organization**: How are files structured? Imports organized?
- **Type Annotations**: Comprehensive, partial, or absent?
- **Error Handling**: Exceptions, Result types, error propagation
- **Async Patterns**: Consistent async/await usage, callback patterns
- **Immutability**: Frozen dataclasses, readonly, const usage
- **Documentation**: Docstring style, comment patterns
- **Configuration**: How is config managed and passed around?

### Step 4: Identify Antipatterns to Avoid

From CLAUDE.md or code analysis, note:
- What patterns are explicitly discouraged?
- What architectural boundaries must not be crossed?
- What common mistakes should be avoided?

### Step 5: Return Patterns Summary

Return the comprehensive summary as your response (do not write files - the orchestrator will save it).

```markdown
# Coding Patterns & Conventions: {project}

## Conventions from CLAUDE.md
[Extract key guidelines from CLAUDE.md if it exists]

## Naming Conventions
- Files: [Pattern and examples]
- Classes: [Pattern and examples]
- Functions: [Pattern and examples]
- Variables: [Pattern and examples]

## Code Organization
- Import order: [How imports are organized]
- File structure: [Typical file layout]
- Module boundaries: [How code is separated]

## Type Safety & Annotations
- Style: [Comprehensive type hints? TypeScript strict mode?]
- Examples: [Show typical type usage]

## Error Handling
- Pattern: [Exceptions, Result types, etc.]
- Examples: [From actual code]

## Testing Conventions
- File naming: [test_*.py, *.test.ts, etc.]
- Test structure: [AAA pattern, fixtures, etc.]
- Async testing: [How async tests are handled]

## Common Patterns
[List 5-10 patterns found across multiple files]

## Antipatterns to Avoid
[List what NOT to do, from CLAUDE.md or code review]

## Best Practices Specific to This Project
[Unique conventions not found in other codebases]
```

**Important:**
- Prioritize CLAUDE.md - it's the authoritative source
- Be specific with examples (file:line references)
- Note both what TO do and what NOT to do
"""

    context = {
        'project': project,
        'agent': 'conventions_discoverer',
        'task_id': f'conventions-discovery-{project}',
        'use_docker': False,
        'work_dir': str(project_dir),
        'claude_model': 'claude-sonnet-4-5-20250929',
        'observability': None,
    }

    logger.info("  Running conventions discovery with Claude Code CLI...")
    result = await run_claude_code(prompt, context)

    # Save the returned content to the expected file location
    summary_file = output_dir / 'PatternsSummary.md'
    summary_file.write_text(result)
    logger.debug(f"Wrote {len(result)} bytes to {summary_file}")

    return result


async def run_codebase_analysis(project: str, workspace_root: Path) -> Dict[str, Any]:
    """
    Orchestrate Claude Code CLI to perform comprehensive analysis

    Args:
        project: Project name
        workspace_root: Workspace root path

    Returns:
        Analysis summary with paths to generated markdown files
    """
    validate_project_name(project)
    logger.info(f"Running codebase analysis for {project}...")

    output_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project / 'analysis'
    output_dir.mkdir(parents=True, exist_ok=True)

    # Phase 1: Architecture discovery (Claude Code CLI)
    logger.info("Phase 1: Discovering architecture...")
    try:
        await run_architecture_discovery(project, workspace_root)
        logger.info("  ✓ Created: ArchitectureSummary.md")
    except Exception as e:
        logger.error(f"  ✗ Architecture discovery failed: {e}")
        raise

    # Phase 2: Tech stack discovery (Claude Code CLI)
    logger.info("Phase 2: Discovering tech stack...")
    try:
        await run_techstack_discovery(project, workspace_root)
        logger.info("  ✓ Created: TechStackSummary.md")
    except Exception as e:
        logger.error(f"  ✗ Tech stack discovery failed: {e}")
        raise

    # Phase 3: Conventions discovery (Claude Code CLI)
    logger.info("Phase 3: Discovering patterns & conventions...")
    try:
        await run_conventions_discovery(project, workspace_root)
        logger.info("  ✓ Created: PatternsSummary.md")
    except Exception as e:
        logger.error(f"  ✗ Conventions discovery failed: {e}")
        raise

    # Read generated summaries
    arch_summary_path = output_dir / 'ArchitectureSummary.md'
    tech_summary_path = output_dir / 'TechStackSummary.md'
    patterns_summary_path = output_dir / 'PatternsSummary.md'

    # Verify files exist
    if not arch_summary_path.exists():
        raise FileNotFoundError(f"Architecture summary not created: {arch_summary_path}")
    if not tech_summary_path.exists():
        raise FileNotFoundError(f"Tech stack summary not created: {tech_summary_path}")
    if not patterns_summary_path.exists():
        raise FileNotFoundError(f"Patterns summary not created: {patterns_summary_path}")

    arch_summary = arch_summary_path.read_text()
    tech_summary = tech_summary_path.read_text()
    patterns_summary = patterns_summary_path.read_text()

    # Extract basic metadata for backwards compatibility
    # (Some callers expect tech_stacks, key_files fields)
    analysis = {
        'project': project,
        'timestamp': utc_isoformat(),
        'architecture_summary': arch_summary,
        'techstack_summary': tech_summary,
        'patterns_summary': patterns_summary,
        'summary_files': {
            'architecture': str(arch_summary_path),
            'techstack': str(tech_summary_path),
            'patterns': str(patterns_summary_path),
        },
        # Legacy fields for backwards compatibility
        'tech_stacks': extract_tech_stacks_from_summary(tech_summary),
        'key_files': [],  # No longer sampled by deterministic code
    }

    # Save analysis metadata
    orchestrator_root = Path(os.environ.get('ORCHESTRATOR_ROOT', '.'))
    state_output_dir = orchestrator_root / 'state' / 'projects' / project
    state_output_dir.mkdir(parents=True, exist_ok=True)
    output_file = state_output_dir / 'codebase_analysis.json'

    with open(output_file, 'w') as f:
        json.dump(analysis, f, indent=2)

    logger.info(f"  ✓ Analysis saved to: {output_file}")

    return analysis


def extract_tech_stacks_from_summary(tech_summary: str) -> Dict[str, Any]:
    """
    Extract basic tech stack info from summary for backwards compatibility

    Args:
        tech_summary: Tech stack summary markdown

    Returns:
        Basic tech stack structure
    """
    # Simple extraction - look for "Primary Language:" line
    languages = []
    frameworks = []
    primary_language = 'unknown'

    for line in tech_summary.split('\n'):
        if 'Primary Language:' in line:
            # Extract language after colon
            lang = line.split(':')[-1].strip()
            primary_language = lang.lower().split()[0] if lang else 'unknown'
            languages = [primary_language]
        elif '### ' in line and any(fw in line.lower() for fw in ['framework', 'testing', 'web']):
            # Try to extract framework name from next line
            continue

    return {
        'languages': languages,
        'frameworks': frameworks,
        'primary_language': primary_language,
    }


async def main():
    """CLI entry point for testing"""
    import argparse

    parser = argparse.ArgumentParser(description="Analyze project codebase using Claude Code CLI")
    parser.add_argument('project', help='Project name to analyze')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Get workspace root
    workspace_root = get_workspace_root()

    try:
        import asyncio
        analysis = await run_codebase_analysis(args.project, workspace_root)
        print(f"\n✓ Analysis complete for {args.project}")
        print(f"  Summaries created:")
        print(f"    - ArchitectureSummary.md")
        print(f"    - TechStackSummary.md")
        print(f"    - PatternsSummary.md")
    except Exception as e:
        logger.error(f"✗ Analysis failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())

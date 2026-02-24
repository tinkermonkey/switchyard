#!/usr/bin/env python3
"""
Artifact Generator

Generates agent and skill markdown files using Claude Code CLI prompts.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, Any, List

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

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
    # Inside container: /workspace
    # Outside container: Parent of orchestrator root (e.g., /home/user/workspace/orchestrator)
    if Path('/workspace').exists() and Path('/workspace').is_dir():
        return Path('/workspace')
    else:
        # Outside container: parent of ORCHESTRATOR_ROOT
        orchestrator_root = Path(os.environ.get('ORCHESTRATOR_ROOT', Path(__file__).parent.parent))
        return orchestrator_root.parent


def get_project_claude_dir(project: str) -> Path:
    """
    Get .claude directory for a specific project

    Args:
        project: Project name

    Returns:
        Path to project's .claude directory
    """
    workspace_root = get_workspace_root()
    project_dir = workspace_root / project
    return project_dir / '.claude'


def validate_artifact_name(name: str, artifact_type: str):
    """
    Validate that an artifact name is safe for filesystem use

    Args:
        name: Artifact name to validate
        artifact_type: Type of artifact ('agent' or 'skill')

    Raises:
        ValueError: If name is invalid
    """
    # Names must:
    # - Start with alphanumeric
    # - Contain only alphanumeric, hyphens, and underscores
    # - Not be empty
    # - Not contain path traversal sequences
    if not name:
        raise ValueError(f"Empty {artifact_type} name")

    if not re.match(r'^[a-zA-Z0-9][a-zA-Z0-9_-]*$', name):
        raise ValueError(
            f"Invalid {artifact_type} name '{name}': "
            f"must start with alphanumeric and contain only alphanumeric, hyphens, and underscores"
        )

    if '..' in name or '/' in name or '\\' in name:
        raise ValueError(f"Invalid {artifact_type} name '{name}': path traversal detected")

    if len(name) > 200:
        raise ValueError(f"Invalid {artifact_type} name '{name}': too long (max 200 chars)")


async def generate_agent(
    project: str,
    agent_spec: Dict[str, Any],
    analysis: Dict[str, Any],
    strategy: Dict[str, Any],
    codebase_hash: str,
    dry_run: bool = False
) -> Path:
    """
    Generate agent markdown file using Claude Code CLI

    Args:
        project: Project name
        agent_spec: Agent specification from strategy
        analysis: Codebase analysis
        strategy: Full strategy
        codebase_hash: Hash of codebase
        dry_run: If True, don't write files

    Returns:
        Path to generated agent file
    """
    # Validate agent name for filesystem safety
    validate_artifact_name(agent_spec['name'], 'agent')

    # Get project-specific .claude directory
    workspace_root = get_workspace_root()
    claude_dir = get_project_claude_dir(project)
    agents_dir = claude_dir / 'agents' / 'clauditoreum'
    agent_file = agents_dir / f"{agent_spec['name']}.md"

    if dry_run:
        logger.info(f"[DRY RUN] Would write: {agent_file}")
        return agent_file

    # Read summaries
    summaries_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project / 'analysis'
    arch_summary = (summaries_dir / 'ArchitectureSummary.md').read_text()
    tech_summary = (summaries_dir / 'TechStackSummary.md').read_text()
    patterns_summary = (summaries_dir / 'PatternsSummary.md').read_text()

    # Build prompt for Claude Code CLI
    prompt = f"""# Agent Definition Generation

You are creating an agent definition for the **{project}** project.

## Agent to Create

**Name:** {agent_spec['name']}
**Purpose:** {agent_spec.get('purpose', 'Not specified')}
**Rationale:** {agent_spec.get('rationale', 'Not specified')}

**Capabilities:**
{chr(10).join('- ' + cap for cap in agent_spec.get('capabilities', []))}

**Tools:** {', '.join(agent_spec.get('tools', []))}
**Model:** {agent_spec.get('model', 'sonnet')}

## Project Context

**Architecture Summary:**
```markdown
{arch_summary[:3000]}
```

**Tech Stack Summary:**
```markdown
{tech_summary[:3000]}
```

**Patterns & Conventions:**
```markdown
{patterns_summary[:3000]}
```

## Your Mission

Create a complete agent definition markdown file with YAML frontmatter.

**Output Path:** `.claude/agents/clauditoreum/{agent_spec['name']}.md`

Use this structure:

```markdown
---
name: {agent_spec['name']}
description: {agent_spec.get('purpose', 'Agent description')}
tools: {agent_spec.get('tools', ['Read', 'Grep', 'Glob'])}
model: {agent_spec.get('model', 'sonnet')}
color: {agent_spec.get('color', 'blue')}
generated: true
generation_timestamp: {utc_isoformat()}
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
"""

    # Run Claude Code CLI to generate agent
    context = {
        'project': project,
        'work_dir': str(workspace_root / project),
        'agent': 'artifact_generator',
        'use_docker': False
    }

    try:
        result = await run_claude_code(prompt, context)

        # Claude should have created the file, but verify it exists
        if not agent_file.exists():
            # If Claude didn't create it, write the returned content
            agent_file.parent.mkdir(parents=True, exist_ok=True)
            agent_file.write_text(result)

        logger.info(f"  ✓ Generated agent: {agent_file.name}")
        return agent_file

    except Exception as e:
        logger.error(f"  ✗ Failed to generate agent {agent_spec['name']}: {e}")
        raise


async def generate_skill(
    project: str,
    skill_spec: Dict[str, Any],
    analysis: Dict[str, Any],
    strategy: Dict[str, Any],
    codebase_hash: str,
    dry_run: bool = False
) -> Path:
    """
    Generate skill markdown file using Claude Code CLI

    Args:
        project: Project name
        skill_spec: Skill specification from strategy
        analysis: Codebase analysis
        strategy: Full strategy
        codebase_hash: Hash of codebase
        dry_run: If True, don't write files

    Returns:
        Path to generated skill directory
    """
    # Validate skill name for filesystem safety
    validate_artifact_name(skill_spec['name'], 'skill')

    # Get project-specific .claude directory
    workspace_root = get_workspace_root()
    claude_dir = get_project_claude_dir(project)
    skills_dir = claude_dir / 'skills'
    skill_dir = skills_dir / skill_spec['name']
    skill_file = skill_dir / 'SKILL.md'

    if dry_run:
        logger.info(f"[DRY RUN] Would write: {skill_file}")
        return skill_dir

    # Read summaries
    summaries_dir = ORCHESTRATOR_ROOT / 'state' / 'projects' / project / 'analysis'
    arch_summary = (summaries_dir / 'ArchitectureSummary.md').read_text()
    tech_summary = (summaries_dir / 'TechStackSummary.md').read_text()
    patterns_summary = (summaries_dir / 'PatternsSummary.md').read_text()

    # Build prompt for Claude Code CLI
    prompt = f"""# Skill Definition Generation

You are creating a skill definition for the **{project}** project.

## Skill to Create

**Name:** {skill_spec['name']}
**Purpose:** {skill_spec.get('purpose', 'Not specified')}
**Implementation:** {skill_spec.get('implementation', 'Not specified')}
**Args:** {skill_spec.get('args', [])}

## Project Context

**Architecture Summary:**
```markdown
{arch_summary[:2000]}
```

**Tech Stack Summary:**
```markdown
{tech_summary[:2000]}
```

**Patterns & Conventions:**
```markdown
{patterns_summary[:2000]}
```

## Your Mission

Create a complete skill definition markdown file with YAML frontmatter.

**Output Path:** `.claude/skills/{skill_spec['name']}/SKILL.md`

Use this structure:

```markdown
---
name: {skill_spec['name']}
description: {skill_spec.get('purpose', 'Skill description')}
user_invocable: true
args: {skill_spec.get('args', [])}
generated: true
generation_timestamp: {utc_isoformat()}
generation_version: "2.0"
source_project: {project}
source_codebase_hash: {codebase_hash}
---

# {{Skill Display Name}}

Quick-reference skill for **{project}**.

## Usage

```bash
/{skill_spec['name']} {{args}}
```

## Purpose

{{Detailed purpose - BE SPECIFIC with project context}}

## Implementation

{{Actual commands/operations to perform - USE ACTUAL PROJECT FILES AND COMMANDS}}

For example:
- If this is a test skill, use the actual test framework command from TechStackSummary
- If this is an architecture skill, reference actual directories from ArchitectureSummary
- If this is a patterns skill, cite actual files from PatternsSummary

## Examples

{{Concrete usage examples with actual project context}}

---

*This skill was automatically generated.*
```

**Important:**
- Use actual commands from the project (from TechStackSummary - test framework, build tools, etc.)
- Reference actual files and directories from ArchitectureSummary
- Don't use generic placeholders - be specific to THIS project
- Use the Write tool to create the file at the specified path
"""

    # Run Claude Code CLI to generate skill
    context = {
        'project': project,
        'work_dir': str(workspace_root / project),
        'agent': 'artifact_generator',
        'use_docker': False
    }

    try:
        result = await run_claude_code(prompt, context)

        # Claude should have created the file, but verify it exists
        if not skill_file.exists():
            # If Claude didn't create it, write the returned content
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(result)

        logger.info(f"  ✓ Generated skill: {skill_dir.name}")
        return skill_dir

    except Exception as e:
        logger.error(f"  ✗ Failed to generate skill {skill_spec['name']}: {e}")
        raise


CLAUDE_MD_SECTION_START = "<!-- generated-agents-section -->"
CLAUDE_MD_SECTION_END = "<!-- /generated-agents-section -->"


def render_claude_md_section(strategy: Dict[str, Any]) -> str:
    """Build the auto-generated CLAUDE.md block from a strategy dict."""
    agents = strategy.get('agents', [])
    skills = strategy.get('skills', [])

    lines = [CLAUDE_MD_SECTION_START]

    if agents:
        lines.extend([
            "",
            "## Specialized Sub-Agents",
            "",
            "**MANDATORY**: Before implementing, identify which specialist agent applies to your "
            "task and consult it via the `Task` tool. Do not proceed with implementation until "
            "you have consulted the relevant agent. These agents have deep project-specific context "
            "that general knowledge cannot replicate.",
            "",
            "| Agent | When to use |",
            "|---|---|",
        ])
        for agent in agents:
            rationale = agent.get('rationale', agent.get('purpose', ''))
            when_to_use = rationale.split('.')[0].rstrip('.,;')
            lines.append(f"| `{agent['name']}` | {when_to_use} |")

        lines.extend(["", "```"])
        for agent in agents[:3]:
            purpose = agent.get('purpose', agent['name'])
            lines.append(
                f'Task(subagent_type="{agent["name"]}", prompt="<your question about {purpose}>")'
            )
        lines.append("```")

    if skills:
        lines.extend([
            "",
            "## Skills",
            "",
            "| Skill | What it does |",
            "|---|---|",
        ])
        for skill in skills:
            lines.append(f"| `/{skill['name']}` | {skill.get('purpose', '')} |")

    lines.extend(["", CLAUDE_MD_SECTION_END])
    return "\n".join(lines)


def update_project_claude_md(project: str, strategy: Dict[str, Any], dry_run: bool = False) -> bool:
    """
    Write or replace the generated agent/skills section in the project root CLAUDE.md.

    Replaces content between sentinel comments on re-runs; appends on first run.
    Targets the project root CLAUDE.md (not .claude/CLAUDE.md).
    """
    workspace_root = get_workspace_root()
    claude_md_path = workspace_root / project / 'CLAUDE.md'

    new_section = render_claude_md_section(strategy)

    if dry_run:
        logger.info(f"[DRY RUN] Would update {claude_md_path} with generated agent/skills section")
        return True

    if claude_md_path.exists():
        existing = claude_md_path.read_text(encoding='utf-8')
        if CLAUDE_MD_SECTION_START in existing and CLAUDE_MD_SECTION_END in existing:
            before = existing[:existing.index(CLAUDE_MD_SECTION_START)]
            after = existing[existing.index(CLAUDE_MD_SECTION_END) + len(CLAUDE_MD_SECTION_END):]
            updated = before.rstrip('\n') + '\n\n' + new_section + after.rstrip('\n') + '\n'
        else:
            updated = existing.rstrip('\n') + '\n\n' + new_section + '\n'
    else:
        updated = f"# {project}\n\n{new_section}\n"

    claude_md_path.write_text(updated, encoding='utf-8')
    logger.info(
        f"  ✓ Updated {claude_md_path} with "
        f"{len(strategy.get('agents', []))} agents, {len(strategy.get('skills', []))} skills"
    )
    return True


async def generate_all_artifacts(
    project: str,
    strategy: Dict[str, Any],
    analysis: Dict[str, Any],
    codebase_hash: str,
    dry_run: bool = False
) -> Dict[str, List[Path]]:
    """
    Generate all agents and skills for a project using Claude Code CLI

    Args:
        project: Project name
        strategy: Generation strategy
        analysis: Codebase analysis
        codebase_hash: Hash of codebase
        dry_run: If True, don't write files

    Returns:
        Dict with 'agents' and 'skills' lists of created paths
    """
    created_artifacts = {
        'agents': [],
        'skills': []
    }

    # Generate agents
    logger.info(f"  Generating {len(strategy['agents'])} agent(s)...")
    for agent_spec in strategy['agents']:
        try:
            agent_path = await generate_agent(project, agent_spec, analysis, strategy, codebase_hash, dry_run)
            created_artifacts['agents'].append(agent_path)
        except Exception as e:
            logger.error(f"  ✗ Failed to generate agent {agent_spec['name']}: {e}")

    # Generate skills
    logger.info(f"  Generating {len(strategy['skills'])} skill(s)...")
    for skill_spec in strategy['skills']:
        try:
            skill_path = await generate_skill(project, skill_spec, analysis, strategy, codebase_hash, dry_run)
            created_artifacts['skills'].append(skill_path)
        except Exception as e:
            logger.error(f"  ✗ Failed to generate skill {skill_spec['name']}: {e}")

    # Update project CLAUDE.md with generated agent/skills section
    logger.info("  Updating project CLAUDE.md...")
    update_project_claude_md(project, strategy, dry_run=dry_run)

    return created_artifacts


async def review_artifacts_quality(
    project: str,
    created_artifacts: Dict[str, List[Path]],
    dry_run: bool = False
) -> Dict[str, Any]:
    """
    Use Claude Code CLI to review and refine generated artifacts

    Args:
        project: Project name
        created_artifacts: Dict with 'agents' and 'skills' lists of paths
        dry_run: If True, don't make changes

    Returns:
        Dict with review results
    """
    workspace_root = get_workspace_root()

    # Build list of artifacts for review
    agent_files = [p.name for p in created_artifacts['agents']]
    skill_files = [p.parent.name + '/SKILL.md' for p in created_artifacts['skills']]

    logger.info(f"  Reviewing {len(agent_files)} agent(s) and {len(skill_files)} skill(s)...")

    # Build comprehensive review prompt
    prompt = f"""# Agent Team Quality Review

You are reviewing the generated agent and skill definitions for **{project}**.

## Artifacts to Review

**Agents ({len(agent_files)}):**
{chr(10).join(f'- .claude/agents/clauditoreum/{name}' for name in agent_files)}

**Skills ({len(skill_files)}):**
{chr(10).join(f'- .claude/skills/{name}' for name in skill_files)}

## Your Mission

Review ALL generated artifacts and fix any issues found. Focus on quality, accuracy, and polish.

### Review Criteria

For each artifact:

1. **Placeholder Removal** (Critical):
   - Find and fix unfilled placeholders like `{{e}}`, `{{trace_id}}`, `{{store_error}}`, etc.
   - Replace with realistic variable names or complete the code examples
   - Common placeholders to fix:
     - Exception variables: `{{e}}` → `e` or specific name
     - IDs: `{{trace_id}}` → `trace_id` or example value
     - Errors: `{{store_error}}` → `store_error`
     - Lists: `{{invalid_services}}` → `invalid_services`

2. **Code Example Quality**:
   - Ensure all code examples are complete and runnable
   - Use actual file paths from the project
   - Follow project conventions (async/await, type hints, etc.)
   - Remove any template artifacts or incomplete snippets

3. **Consistency**:
   - Verify YAML frontmatter is complete and valid
   - Check that tone and formatting are consistent across all artifacts
   - Ensure descriptions are clear and actionable

4. **Accuracy**:
   - Verify file paths and line numbers are correct (use Read/Grep to check)
   - Confirm port interfaces and method signatures match actual code
   - Test that example commands will actually work

5. **Optimization**:
   - Remove redundancy and verbosity
   - Clarify ambiguous instructions
   - Add missing context where needed
   - Improve examples to be more practical

6. **Formatting**:
   - Ensure proper markdown formatting
   - Code blocks have correct language tags
   - Lists and sections are well-structured

## Process

1. Read each artifact file using the Read tool
2. Identify issues based on criteria above
3. Use the Edit tool to fix issues (preserve existing content structure)
4. Focus on surgical edits - don't rewrite unnecessarily
5. After reviewing all files, provide a summary of changes made

## Important Guidelines

- Make targeted fixes, not wholesale rewrites
- Preserve the core content and knowledge base
- Use Edit tool for changes (not Write - we want to preserve content)
- If you find a file path reference, verify it exists before keeping it
- If unsure about something, leave it rather than guessing

## Expected Output

After reviewing and fixing all artifacts, provide a summary:

```markdown
# Quality Review Summary

## Changes Made

### Agents
- agent-name.md: Fixed X placeholders, improved Y examples
- ...

### Skills
- skill-name/SKILL.md: Fixed X issues, clarified Y
- ...

## Statistics
- Total artifacts reviewed: X
- Issues found: Y
- Issues fixed: Z
- Artifacts modified: N

## Validation Ready
All artifacts should now pass validation without warnings.
```
"""

    if dry_run:
        logger.info(f"[DRY RUN] Would review {len(agent_files) + len(skill_files)} artifacts")
        return {'status': 'dry_run', 'reviewed': 0, 'modified': 0}

    # Run Claude Code CLI for review
    context = {
        'project': project,
        'work_dir': str(workspace_root / project),
        'agent': 'quality_reviewer',
        'use_docker': False
    }

    try:
        result = await run_claude_code(prompt, context)

        logger.info(f"  ✓ Quality review complete")

        return {
            'status': 'completed',
            'reviewed': len(agent_files) + len(skill_files),
            'summary': result
        }

    except Exception as e:
        logger.error(f"  ✗ Quality review failed: {e}")
        return {
            'status': 'error',
            'error': str(e)
        }


async def _run_generation(args):
    """Async wrapper for generation"""
    import json

    # Load strategy
    if args.strategy:
        strategy_file = Path(args.strategy)
    else:
        strategy_file = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / 'state' / 'projects' / args.project / 'generation_strategy.json'

    if not strategy_file.exists():
        logger.error(f"Strategy file not found: {strategy_file}")
        sys.exit(1)

    with open(strategy_file, 'r') as f:
        strategy = json.load(f)

    # Load analysis
    if args.analysis:
        analysis_file = Path(args.analysis)
    else:
        analysis_file = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / 'state' / 'projects' / args.project / 'codebase_analysis.json'

    if not analysis_file.exists():
        logger.error(f"Analysis file not found: {analysis_file}")
        sys.exit(1)

    with open(analysis_file, 'r') as f:
        analysis = json.load(f)

    # Get codebase hash from analysis or calculate
    from scripts.maintain_agent_team import calculate_codebase_hash
    codebase_hash = calculate_codebase_hash(args.project)

    # Generate artifacts
    created_artifacts = await generate_all_artifacts(
        args.project,
        strategy,
        analysis,
        codebase_hash,
        dry_run=args.dry_run
    )

    print(f"\n✓ Generation complete for {args.project}")
    print(f"  Agents: {len(created_artifacts['agents'])}")
    print(f"  Skills: {len(created_artifacts['skills'])}")


def main():
    """CLI entry point for testing"""
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Generate agents and skills")
    parser.add_argument('project', help='Project name')
    parser.add_argument('--strategy', help='Path to strategy JSON (default: auto-detect)')
    parser.add_argument('--analysis', help='Path to analysis JSON (default: auto-detect)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    # Run async generation
    try:
        asyncio.run(_run_generation(args))
    except Exception as e:
        logger.error(f"✗ Generation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

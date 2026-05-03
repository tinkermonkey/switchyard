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
from scripts._prompt_loader import load_prompt

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



def _extract_artifact_content(result: str) -> str:
    """
    Extract artifact file content from Claude's text response.

    Claude may wrap the file content in a fenced code block with any language tag.
    Finds the first such block whose content starts with YAML frontmatter (---).
    Falls back to the raw result so _validate_artifact_content can produce a clear error.
    """
    content = result.strip()

    # Match any fenced code block regardless of language tag
    for block in re.findall(r'```[^\n]*\n(.*?)\n```', content, re.DOTALL):
        stripped = block.strip()
        if stripped.startswith('---'):
            return stripped

    # Content may start directly with frontmatter (no code block)
    if content.startswith('---'):
        return content

    # Return as-is so validation produces a clear error
    return content


def _parse_frontmatter(content: str):
    """
    Parse YAML frontmatter from a string using PyYAML.

    Returns (frontmatter_dict, body_str). The closing --- must be a line by itself.
    Raises ValueError on structural or parse errors.
    """
    import yaml

    lines = content.split('\n')
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            end_idx = i
            break

    if end_idx is None:
        raise ValueError("YAML frontmatter is not closed — no closing '---' line found")

    frontmatter_str = '\n'.join(lines[1:end_idx])
    body = '\n'.join(lines[end_idx + 1:]).strip()

    try:
        frontmatter = yaml.safe_load(frontmatter_str) or {}
    except yaml.YAMLError as e:
        raise ValueError(f"YAML frontmatter is invalid: {e}") from e

    if not isinstance(frontmatter, dict):
        raise ValueError(
            f"YAML frontmatter parsed as {type(frontmatter).__name__}, expected a mapping"
        )

    return frontmatter, body


def _validate_artifact_content(content: str, artifact_type: str, name: str) -> None:
    """
    Validate that content is a proper agent/skill definition with YAML frontmatter.

    Raises ValueError when content looks like Claude's conversational output
    rather than a structured file definition.
    """
    if not content:
        raise ValueError(f"{artifact_type} '{name}': empty content")

    if not content.startswith('---'):
        raise ValueError(
            f"{artifact_type} '{name}': content does not start with YAML frontmatter (---).\n"
            f"Claude returned conversational text instead of the file definition.\n"
            f"Got: {content[:200]!r}"
        )

    try:
        frontmatter, body = _parse_frontmatter(content)
    except ValueError as e:
        raise ValueError(f"{artifact_type} '{name}': {e}") from e

    for field in ('name', 'description'):
        if not frontmatter.get(field):
            raise ValueError(
                f"{artifact_type} '{name}': frontmatter missing required '{field}' field"
            )

    actual_name = str(frontmatter['name'])
    if actual_name != name:
        raise ValueError(
            f"{artifact_type} '{name}': frontmatter name '{actual_name}' "
            f"does not match expected name '{name}'"
        )

    if len(body) < 200:
        raise ValueError(
            f"{artifact_type} '{name}': body too short ({len(body)} chars) — "
            f"a useful system prompt needs at least 200 characters"
        )


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
    agents_dir = claude_dir / 'agents' / 'switchyard'
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
    prompt = load_prompt(
        "artifacts/generate_agent",
        project=project,
        agent_name=agent_spec['name'],
        agent_purpose=agent_spec.get('purpose', 'Not specified'),
        agent_rationale=agent_spec.get('rationale', 'Not specified'),
        agent_capabilities="\n".join('- ' + cap for cap in agent_spec.get('capabilities', [])),
        agent_tools=', '.join(agent_spec.get('tools', [])),
        agent_model=agent_spec.get('model', 'sonnet'),
        agent_color=agent_spec.get('color', 'blue'),
        arch_summary=arch_summary[:3000],
        tech_summary=tech_summary[:3000],
        patterns_summary=patterns_summary[:3000],
        generation_timestamp=utc_isoformat(),
        codebase_hash=codebase_hash,
    )

    # Run Claude Code CLI to generate agent
    context = {
        'project': project,
        'work_dir': str(workspace_root / project),
        'agent': 'artifact_generator',
        'use_docker': False
    }

    try:
        result = await run_claude_code(prompt, context)

        if agent_file.exists():
            _validate_artifact_content(agent_file.read_text(), 'agent', agent_spec['name'])
        else:
            content = _extract_artifact_content(result)
            _validate_artifact_content(content, 'agent', agent_spec['name'])
            agent_file.parent.mkdir(parents=True, exist_ok=True)
            agent_file.write_text(content)

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
    prompt = load_prompt(
        "artifacts/generate_skill",
        project=project,
        skill_name=skill_spec['name'],
        skill_purpose=skill_spec.get('purpose', 'Not specified'),
        skill_implementation=skill_spec.get('implementation', 'Not specified'),
        skill_args=str(skill_spec.get('args', [])),
        arch_summary=arch_summary[:2000],
        tech_summary=tech_summary[:2000],
        patterns_summary=patterns_summary[:2000],
        generation_timestamp=utc_isoformat(),
        codebase_hash=codebase_hash,
    )

    # Run Claude Code CLI to generate skill
    context = {
        'project': project,
        'work_dir': str(workspace_root / project),
        'agent': 'artifact_generator',
        'use_docker': False
    }

    try:
        result = await run_claude_code(prompt, context)

        if skill_file.exists():
            _validate_artifact_content(skill_file.read_text(), 'skill', skill_spec['name'])
        else:
            content = _extract_artifact_content(result)
            _validate_artifact_content(content, 'skill', skill_spec['name'])
            skill_file.parent.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(content)

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
    agent_files_list = "\n".join(f'- .claude/agents/switchyard/{name}' for name in agent_files)
    skill_files_list = "\n".join(f'- .claude/skills/{name}' for name in skill_files)
    prompt = load_prompt(
        "artifacts/review_artifacts",
        project=project,
        agent_count=str(len(agent_files)),
        agent_files=agent_files_list,
        skill_count=str(len(skill_files)),
        skill_files=skill_files_list,
    )

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

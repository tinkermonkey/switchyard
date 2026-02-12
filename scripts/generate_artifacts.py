#!/usr/bin/env python3
"""
Artifact Generator

Generates agent and skill markdown files from templates and strategy.
"""

import logging
import os
import re
import sys
from pathlib import Path
from typing import Dict, Any, List

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.template_engine import (
    load_template,
    populate_template,
    build_agent_context,
    build_skill_context
)

# Configure logging
logger = logging.getLogger(__name__)

# Constants
CLAUDE_DIR = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / '.claude'
AGENTS_DIR = CLAUDE_DIR / 'agents'
SKILLS_DIR = CLAUDE_DIR / 'skills'


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


def generate_agent(
    project: str,
    agent_spec: Dict[str, Any],
    analysis: Dict[str, Any],
    strategy: Dict[str, Any],
    codebase_hash: str,
    dry_run: bool = False
) -> Path:
    """
    Generate agent markdown file

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

    # Load template
    template = load_template('agent_template.md')

    # Build context
    context = build_agent_context(project, agent_spec, analysis, strategy, codebase_hash)

    # Populate template
    content = populate_template(template, context)

    # Determine output path
    agent_file = AGENTS_DIR / f"{agent_spec['name']}.md"

    if dry_run:
        logger.info(f"[DRY RUN] Would write: {agent_file}")
        return agent_file

    # Write file
    agent_file.parent.mkdir(parents=True, exist_ok=True)
    with open(agent_file, 'w') as f:
        f.write(content)

    logger.info(f"  ✓ Generated agent: {agent_file.name}")
    return agent_file


def generate_skill(
    project: str,
    skill_spec: Dict[str, Any],
    analysis: Dict[str, Any],
    strategy: Dict[str, Any],
    codebase_hash: str,
    dry_run: bool = False
) -> Path:
    """
    Generate skill markdown file

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

    # Load template
    template = load_template('skill_template.md')

    # Build context
    context = build_skill_context(project, skill_spec, analysis, strategy, codebase_hash)

    # Populate template
    content = populate_template(template, context)

    # Determine output path
    skill_dir = SKILLS_DIR / skill_spec['name']
    skill_file = skill_dir / 'SKILL.md'

    if dry_run:
        logger.info(f"[DRY RUN] Would write: {skill_file}")
        return skill_dir

    # Write file
    skill_dir.mkdir(parents=True, exist_ok=True)
    with open(skill_file, 'w') as f:
        f.write(content)

    logger.info(f"  ✓ Generated skill: {skill_dir.name}")
    return skill_dir


def generate_all_artifacts(
    project: str,
    strategy: Dict[str, Any],
    analysis: Dict[str, Any],
    codebase_hash: str,
    dry_run: bool = False
) -> Dict[str, List[Path]]:
    """
    Generate all agents and skills for a project

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
            agent_path = generate_agent(project, agent_spec, analysis, strategy, codebase_hash, dry_run)
            created_artifacts['agents'].append(agent_path)
        except Exception as e:
            logger.error(f"  ✗ Failed to generate agent {agent_spec['name']}: {e}")

    # Generate skills
    logger.info(f"  Generating {len(strategy['skills'])} skill(s)...")
    for skill_spec in strategy['skills']:
        try:
            skill_path = generate_skill(project, skill_spec, analysis, strategy, codebase_hash, dry_run)
            created_artifacts['skills'].append(skill_path)
        except Exception as e:
            logger.error(f"  ✗ Failed to generate skill {skill_spec['name']}: {e}")

    return created_artifacts


def main():
    """CLI entry point for testing"""
    import argparse
    import json

    parser = argparse.ArgumentParser(description="Generate agents and skills")
    parser.add_argument('project', help='Project name')
    parser.add_argument('--strategy', help='Path to strategy JSON (default: auto-detect)')
    parser.add_argument('--analysis', help='Path to analysis JSON (default: auto-detect)')
    parser.add_argument('--dry-run', action='store_true', help='Preview without writing files')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

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

    # Generate artifacts
    try:
        # Get codebase hash from analysis or calculate
        from scripts.maintain_agent_team import calculate_codebase_hash
        codebase_hash = calculate_codebase_hash(args.project)

        created_artifacts = generate_all_artifacts(
            args.project,
            strategy,
            analysis,
            codebase_hash,
            dry_run=args.dry_run
        )

        print(f"\n✓ Generation complete for {args.project}")
        print(f"  Agents: {len(created_artifacts['agents'])}")
        print(f"  Skills: {len(created_artifacts['skills'])}")

    except Exception as e:
        logger.error(f"✗ Generation failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

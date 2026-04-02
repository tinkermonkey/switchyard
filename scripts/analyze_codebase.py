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

    prompt = load_prompt("analysis/architecture_discovery", project=project)

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

    prompt = load_prompt("analysis/techstack_discovery", project=project)

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

    prompt = load_prompt("analysis/conventions_discovery", project=project)

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

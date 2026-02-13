#!/usr/bin/env python3
"""
Agent Team Maintainer

Generates and maintains project-specific agents and skills for managed projects.
Analyzes project codebases and creates tailored AI agents that understand the
unique architecture, tech stack, and patterns of each project.

Usage:
    python scripts/maintain_agent_team.py [options]

Options:
    --project PROJECT       Generate agents/skills for specific project only
    --dry-run              Show what would be done without executing
    --auto-approve         Skip interactive strategy approval prompts
    --cleanup              Remove outdated generated artifacts
    --rebuild-images       Rebuild Docker images after generation
    -h, --help             Show this help message

Examples:
    # Generate for all projects
    python scripts/maintain_agent_team.py

    # Generate for specific project
    python scripts/maintain_agent_team.py --project context-studio

    # Preview changes without executing
    python scripts/maintain_agent_team.py --dry-run

    # Full workflow with auto-approval
    python scripts/maintain_agent_team.py --auto-approve --cleanup

    # Generate and rebuild Docker images
    python scripts/maintain_agent_team.py --auto-approve --rebuild-images
"""

import argparse
import hashlib
import logging
import os
import sys
import yaml
from datetime import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set ORCHESTRATOR_ROOT for running outside Docker container
# Inside container: /app (set by container environment)
# Outside container: Current directory (clauditoreum/)
if 'ORCHESTRATOR_ROOT' not in os.environ:
    os.environ['ORCHESTRATOR_ROOT'] = str(Path(__file__).parent.parent.resolve())

from config.manager import config_manager
from monitoring.timestamp_utils import utc_isoformat

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Constants
CLAUDE_DIR = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / '.claude'
GENERATED_DIR = CLAUDE_DIR / 'generated'
AGENTS_DIR = CLAUDE_DIR / 'agents'
SKILLS_DIR = CLAUDE_DIR / 'skills'
MANIFEST_FILE = GENERATED_DIR / 'manifest.yaml'
STATE_DIR = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / 'state' / 'projects'


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


def ensure_directories():
    """Ensure required directories exist"""
    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    AGENTS_DIR.mkdir(parents=True, exist_ok=True)
    SKILLS_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Ensured directories: {GENERATED_DIR}, {AGENTS_DIR}, {SKILLS_DIR}, {STATE_DIR}")


def load_manifest() -> Dict[str, Any]:
    """
    Load the generation manifest

    Returns:
        Manifest data or empty structure if not exists
    """
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load manifest: {e}")
            return {}

    # Return empty structure
    return {
        'version': '1.0',
        'last_updated': utc_isoformat(),
        'projects': {}
    }


def save_manifest(manifest: Dict[str, Any]):
    """
    Save the generation manifest

    Args:
        manifest: Manifest data to save
    """
    manifest['last_updated'] = utc_isoformat()

    try:
        with open(MANIFEST_FILE, 'w') as f:
            yaml.safe_dump(manifest, f, default_flow_style=False, sort_keys=False)
        logger.debug(f"Saved manifest to {MANIFEST_FILE}")
    except Exception as e:
        logger.error(f"Failed to save manifest: {e}")
        raise


def load_project_state(project: str) -> Dict[str, Any]:
    """
    Load agent generation state for a project

    Args:
        project: Project name

    Returns:
        State data or empty structure if not exists
    """
    state_file = STATE_DIR / project / 'agent_generation_state.yaml'

    if state_file.exists():
        try:
            with open(state_file, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load state for {project}: {e}")
            return {}

    # Return empty structure
    return {
        'version': '1.0',
        'project': project,
        'last_updated': None,
        'codebase': {
            'analysis_hash': None,
            'analysis_timestamp': None,
            'tech_stack': {}
        },
        'generations': [],
        'artifacts': {
            'agents': [],
            'skills': []
        },
        'maintenance': {
            'next_analysis': None,
            'auto_regenerate': False
        }
    }


def save_project_state(project: str, state: Dict[str, Any]):
    """
    Save agent generation state for a project

    Args:
        project: Project name
        state: State data to save
    """
    state_file = STATE_DIR / project / 'agent_generation_state.yaml'
    state_file.parent.mkdir(parents=True, exist_ok=True)

    state['last_updated'] = utc_isoformat()

    try:
        with open(state_file, 'w') as f:
            yaml.safe_dump(state, f, default_flow_style=False, sort_keys=False)
        logger.debug(f"Saved state for {project} to {state_file}")
    except Exception as e:
        logger.error(f"Failed to save state for {project}: {e}")
        raise


def calculate_codebase_hash(project: str) -> str:
    """
    Calculate hash of critical codebase files to detect changes

    Args:
        project: Project name

    Returns:
        SHA256 hash of combined critical files
    """
    workspace_root = get_workspace_root()
    project_dir = workspace_root / project

    if not project_dir.exists():
        logger.warning(f"Project directory does not exist: {project_dir}")
        return "missing"

    # Files to hash (in order of importance)
    critical_files = [
        'requirements.txt',
        'package.json',
        'pyproject.toml',
        'Cargo.toml',
        'go.mod',
        '.claude/CLAUDE.md',
        'README.md',
        'Dockerfile',
        'Dockerfile.agent',
        'docker-compose.yml'
    ]

    hasher = hashlib.sha256()
    files_found = []

    for file_name in critical_files:
        file_path = project_dir / file_name
        if file_path.exists() and file_path.is_file():
            try:
                with open(file_path, 'rb') as f:
                    hasher.update(f.read())
                files_found.append(file_name)
            except Exception as e:
                logger.warning(f"Failed to read {file_name} for {project}: {e}")

    # Also hash directory structure (top 2 levels)
    try:
        dir_structure = []
        for item in sorted(project_dir.iterdir()):
            if item.is_dir() and not item.name.startswith('.'):
                dir_structure.append(f"dir:{item.name}")
                # Add one level down
                for subitem in sorted(item.iterdir()):
                    if subitem.is_dir():
                        dir_structure.append(f"dir:{item.name}/{subitem.name}")

        hasher.update('\n'.join(dir_structure).encode('utf-8'))
    except Exception as e:
        logger.warning(f"Failed to hash directory structure for {project}: {e}")

    hash_value = hasher.hexdigest()[:16]  # Use first 16 chars
    logger.debug(f"Calculated hash for {project}: {hash_value} (from {len(files_found)} files)")

    return hash_value


def detect_codebase_changes(project: str) -> Dict[str, Any]:
    """
    Detect if codebase has changed since last generation

    Args:
        project: Project name

    Returns:
        Dict with keys:
        - changed: bool - Whether codebase changed
        - hash: str - Current hash
        - previous_hash: str | None - Previous hash
        - impact: Dict - What changed and impact level
    """
    current_hash = calculate_codebase_hash(project)
    state = load_project_state(project)
    previous_hash = state.get('codebase', {}).get('analysis_hash')

    changed = (previous_hash is None) or (current_hash != previous_hash)

    result = {
        'changed': changed,
        'hash': current_hash,
        'previous_hash': previous_hash,
        'impact': {
            'level': 'high' if previous_hash is None else ('medium' if changed else 'none'),
            'reason': 'Initial generation' if previous_hash is None else (
                'Codebase modified' if changed else 'No changes detected'
            )
        }
    }

    logger.info(f"Change detection for {project}: {result['impact']['reason']} (hash: {current_hash})")

    return result


def discover_projects_for_generation(project_filter: str = None) -> List[str]:
    """
    Discover projects that should have agent teams generated

    Args:
        project_filter: Optional specific project name to filter to

    Returns:
        List of project names
    """
    workspace_root = get_workspace_root()

    # Get all visible projects
    if project_filter:
        projects = [project_filter]
    else:
        projects = config_manager.list_visible_projects()

    # Filter to projects that exist and aren't clauditoreum itself
    valid_projects = []
    for project in projects:
        if project == 'clauditoreum':
            logger.debug(f"Skipping {project}: This is the orchestrator itself")
            continue

        project_dir = workspace_root / project
        if project_dir.exists() and project_dir.is_dir():
            valid_projects.append(project)
        else:
            logger.debug(f"Skipping {project}: Directory not found at {project_dir}")

    return valid_projects


def run_generation_workflow(project: str, args) -> Dict[str, Any]:
    """
    Main generation workflow for a project

    Args:
        project: Project name
        args: Command-line arguments

    Returns:
        Dict with generation results
    """
    logger.info(f"\n{'='*60}")
    logger.info(f"Agent Team Generation: {project}")
    logger.info(f"{'='*60}\n")

    # Phase 1: Detect changes
    logger.info("Phase 1: Detecting codebase changes...")
    changes = detect_codebase_changes(project)

    if not changes['changed'] and not args.cleanup:
        logger.info(f"✓ No changes detected for {project}")
        logger.info(f"  Previous hash: {changes['previous_hash']}")
        logger.info(f"  Current hash: {changes['hash']}")
        logger.info(f"\n  Use --cleanup to review generated artifacts anyway\n")
        return {'status': 'skipped', 'reason': 'no_changes'}

    logger.info(f"  Change level: {changes['impact']['level']}")
    logger.info(f"  Reason: {changes['impact']['reason']}")

    if args.dry_run:
        logger.info("\n[DRY RUN] Would proceed with generation workflow:")
        logger.info("  Phase 2: Analyze codebase")
        logger.info("  Phase 3: Generate strategy")
        logger.info("  Phase 4: User review (unless --auto-approve)")
        logger.info("  Phase 5: Generate artifacts")
        logger.info("  Phase 6: Validate artifacts")
        logger.info("  Phase 7: Deploy to .claude/")
        logger.info("  Phase 8: Update manifest and state")
        return {'status': 'dry_run'}

    # Phase 2: Analyze codebase
    logger.info("\nPhase 2: Analyzing codebase...")
    from scripts.analyze_codebase import run_codebase_analysis

    try:
        analysis = run_codebase_analysis(project, get_workspace_root())
        logger.info(f"  ✓ Analysis complete ({len(analysis['key_files'])} key files sampled)")
    except Exception as e:
        logger.error(f"  ✗ Analysis failed: {e}")
        return {'status': 'error', 'phase': 'analysis', 'error': str(e)}

    # Phase 3: Generate strategy
    logger.info("\nPhase 3: Generating strategy...")
    from scripts.generate_strategy import generate_strategy_with_llm, display_strategy, confirm_strategy
    import asyncio

    try:
        config = config_manager.get_project_config(project)

        # SIMPLIFIED: Just use asyncio.run() - we're always in sync context
        # (maintain_agent_team.py main() is not async)
        strategy = asyncio.run(generate_strategy_with_llm(project, analysis, config))

        logger.info(f"  ✓ Strategy generated ({len(strategy['agents'])} agents, {len(strategy['skills'])} skills)")
    except Exception as e:
        logger.error(f"  ✗ Strategy generation failed: {e}")
        return {'status': 'error', 'phase': 'strategy', 'error': str(e)}

    # Phase 4: User review
    if not args.auto_approve:
        logger.info("\nPhase 4: Review strategy...")
        display_strategy(project, strategy)

        if not confirm_strategy():
            logger.info("  ⊗ Strategy rejected by user")
            return {'status': 'aborted_by_user'}

        logger.info("  ✓ Strategy approved")
    else:
        logger.info("\nPhase 4: Strategy auto-approved (--auto-approve flag)")

    # Phase 5: Generate artifacts
    logger.info("\nPhase 5: Generating artifacts...")
    from scripts.generate_artifacts import generate_all_artifacts

    try:
        created_artifacts = generate_all_artifacts(
            project,
            strategy,
            analysis,
            changes['hash'],
            dry_run=False  # Already handled dry-run earlier
        )
        logger.info(f"  ✓ Generated {len(created_artifacts['agents'])} agents, {len(created_artifacts['skills'])} skills")
    except Exception as e:
        logger.error(f"  ✗ Generation failed: {e}")
        return {'status': 'error', 'phase': 'generation', 'error': str(e)}

    # Phase 6: Validate artifacts
    logger.info("\nPhase 6: Validating artifacts...")
    from scripts.validate_artifacts import validate_all_generated_artifacts

    validation_results = validate_all_generated_artifacts(project)

    if validation_results['failed'] > 0:
        logger.warning(f"  ⚠ {validation_results['failed']} artifact(s) failed validation")
        for result in validation_results['files']:
            if not result['passed']:
                logger.error(f"    ✗ {Path(result['file']).name}: {'; '.join(result['errors'])}")
    else:
        logger.info(f"  ✓ All {validation_results['passed']} artifact(s) validated")

    # Phase 7: Artifacts deployed (implicit - already written in Phase 5)
    logger.info("\nPhase 7: Artifacts deployed to .claude/")

    # Phase 8: Update manifest and state
    logger.info("\nPhase 8: Updating manifest and state...")
    update_manifest_with_artifacts(project, created_artifacts, strategy)
    update_state_with_generation(project, changes, analysis, created_artifacts)
    logger.info("  ✓ Manifest and state updated")

    # Cleanup phase (if --cleanup flag)
    if args.cleanup:
        logger.info("\nCleanup: Removing outdated artifacts...")
        from scripts.cleanup_artifacts import cleanup_project_artifacts
        cleanup_project_artifacts(project, dry_run=False, force=args.auto_approve)

    # Final summary
    logger.info(f"\n{'='*60}")
    logger.info("Generation Complete")
    logger.info(f"{'='*60}")
    logger.info(f"  Agents: {len(created_artifacts['agents'])}")
    logger.info(f"  Skills: {len(created_artifacts['skills'])}")
    logger.info(f"  Validation: {validation_results['passed']}/{validation_results['total']} passed")

    return {
        'status': 'completed',
        'artifacts': created_artifacts,
        'validation': validation_results
    }


def update_manifest_with_artifacts(project: str, artifacts: Dict, strategy: Dict):
    """
    Update manifest with newly created artifacts

    Args:
        project: Project name
        artifacts: Created artifacts dict
        strategy: Generation strategy
    """
    manifest = load_manifest()

    if 'projects' not in manifest:
        manifest['projects'] = {}

    manifest['projects'][project] = {
        'last_generation': utc_isoformat(),
        'generation_hash': calculate_codebase_hash(project),
        'agents': [
            {
                'name': a.stem,
                'file': f'agents/{a.name}',
                'purpose': next((ag['purpose'] for ag in strategy['agents'] if ag['name'] == a.stem), '')
            }
            for a in artifacts['agents']
        ],
        'skills': [
            {
                'name': s.name,
                'directory': f'skills/{s.name}/',
                'purpose': next((sk['purpose'] for sk in strategy['skills'] if sk['name'] == s.name), '')
            }
            for s in artifacts['skills']
        ]
    }

    save_manifest(manifest)
    logger.debug(f"  Updated manifest for {project}")


def update_state_with_generation(project: str, changes: Dict, analysis: Dict, artifacts: Dict):
    """
    Update project state with generation details

    Args:
        project: Project name
        changes: Change detection results
        analysis: Codebase analysis
        artifacts: Created artifacts
    """
    state = load_project_state(project)

    # Update codebase section
    state['codebase']['analysis_hash'] = changes['hash']
    state['codebase']['analysis_timestamp'] = utc_isoformat()
    state['codebase']['tech_stack'] = analysis['tech_stacks']

    # Add generation record
    from datetime import datetime, timezone
    timestamp_now = datetime.now(timezone.utc)
    generation_record = {
        'id': f"gen-{timestamp_now.strftime('%Y%m%d-%H%M%S')}",
        'timestamp': utc_isoformat(),
        'trigger': 'manual',
        'mode': 'incremental' if changes['previous_hash'] else 'initial',
        'artifacts_created': len(artifacts['agents']) + len(artifacts['skills']),
        'artifacts_updated': 0,
        'success': True
    }
    state['generations'].append(generation_record)

    # Update artifacts section
    state['artifacts']['agents'] = [a.stem for a in artifacts['agents']]
    state['artifacts']['skills'] = [s.name for s in artifacts['skills']]

    save_project_state(project, state)
    logger.debug(f"  Updated state for {project}")


def generate_for_projects(args):
    """
    Main function to generate agent teams for projects

    Args:
        args: Parsed command-line arguments
    """
    # Ensure directory structure exists
    ensure_directories()

    # Discover projects
    logger.info("Discovering projects for agent generation...")
    projects = discover_projects_for_generation(args.project)

    if not projects:
        if args.project:
            logger.error(f"Project '{args.project}' not found or not visible")
        else:
            logger.warning("No projects found for agent generation")
        return

    logger.info(f"Found {len(projects)} project(s) for agent generation:")
    for project in projects:
        logger.info(f"  - {project}")

    if args.dry_run:
        logger.info("\n[DRY RUN MODE] No changes will be made\n")

    # Load manifest
    manifest = load_manifest()

    # Process each project
    results = {}
    for project in projects:
        try:
            result = run_generation_workflow(project, args)
            results[project] = result
        except KeyboardInterrupt:
            logger.info("\n\nInterrupted by user")
            raise
        except Exception as e:
            logger.error(f"✗ Failed to generate for {project}: {e}", exc_info=True)
            results[project] = {'status': 'error', 'error': str(e)}

    # Report summary
    logger.info("\n" + "="*60)
    logger.info("Generation Summary")
    logger.info("="*60)
    logger.info(f"Total: {len(results)} projects")

    for status in ['completed', 'skipped', 'dry_run', 'pending_implementation', 'error']:
        count = sum(1 for r in results.values() if r.get('status') == status)
        if count > 0:
            status_label = status.replace('_', ' ').title()
            logger.info(f"{status_label}: {count}")

            if status == 'error':
                failed_projects = [p for p, r in results.items() if r.get('status') == status]
                for project in failed_projects:
                    logger.info(f"  - {project}: {results[project].get('error', 'Unknown error')}")

    logger.info("")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Generate and maintain project-specific agents and skills",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Generate for all projects
  python scripts/maintain_agent_team.py

  # Generate for specific project
  python scripts/maintain_agent_team.py --project context-studio

  # Preview changes without executing
  python scripts/maintain_agent_team.py --dry-run

  # Full workflow with auto-approval
  python scripts/maintain_agent_team.py --auto-approve --cleanup

  # Generate and rebuild Docker images
  python scripts/maintain_agent_team.py --auto-approve --rebuild-images
        """
    )

    parser.add_argument(
        '--project',
        type=str,
        help='Specific project to generate for (default: all visible projects)'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    parser.add_argument(
        '--auto-approve',
        action='store_true',
        help='Skip interactive strategy approval prompts'
    )

    parser.add_argument(
        '--cleanup',
        action='store_true',
        help='Remove outdated generated artifacts'
    )

    parser.add_argument(
        '--rebuild-images',
        action='store_true',
        help='Rebuild Docker images after generation (requires orchestrator running)'
    )

    args = parser.parse_args()

    # Run generation
    try:
        generate_for_projects(args)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

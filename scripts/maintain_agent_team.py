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
    --rebuild-images       Rebuild Docker images after generation (requires container)
    -h, --help             Show this help message

Examples:
    # Generate for all projects (can run outside container)
    python scripts/maintain_agent_team.py

    # Generate for specific project (can run outside container)
    python scripts/maintain_agent_team.py --project context-studio

    # Preview changes without executing
    python scripts/maintain_agent_team.py --dry-run

    # Full workflow with auto-approval
    python scripts/maintain_agent_team.py --auto-approve --cleanup

    # Generate and rebuild Docker images (MUST run inside orchestrator container)
    docker-compose exec orchestrator python scripts/maintain_agent_team.py --auto-approve --rebuild-images --project rounds
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

# Constants for orchestrator-level directories (per-project state only)
ORCHESTRATOR_ROOT = Path(os.environ.get('ORCHESTRATOR_ROOT', '.'))
STATE_DIR = ORCHESTRATOR_ROOT / 'state' / 'projects'


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


def ensure_directories():
    """Ensure required orchestrator-level directories exist"""
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logger.debug(f"Ensured directory: {STATE_DIR}")
    # Note: Project-specific .claude directories are created by generate_artifacts.py as needed


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


def trigger_docker_rebuild(project: str, dry_run: bool = False) -> bool:
    """
    Trigger Docker image rebuild for a project by calling rebuild script.

    Note: This requires running inside the orchestrator Docker container
    because the rebuild script needs access to the Docker socket.

    Args:
        project: Project name
        dry_run: If True, show what would be done without executing

    Returns:
        True if rebuild succeeded, False otherwise
    """
    import subprocess
    import sys

    try:
        logger.info(f"  Triggering Docker rebuild for {project}...")

        if dry_run:
            logger.info(f"  [DRY RUN] Would execute: python scripts/rebuild_project_images.py --project {project} --update-state")
            return True

        # Call rebuild script as subprocess
        # This properly isolates the Docker-dependent rebuild script
        rebuild_script = Path(__file__).parent / 'rebuild_project_images.py'

        cmd = [
            sys.executable,  # Use same Python interpreter
            str(rebuild_script),
            '--project', project,
            '--update-state'  # Update dev container state after successful rebuild
        ]

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout (same as rebuild script)
        )

        if result.returncode == 0:
            logger.info(f"  ✓ Docker rebuild completed successfully")
            # Log last few lines of output for confirmation
            if result.stdout:
                output_lines = result.stdout.strip().split('\n')
                for line in output_lines[-5:]:
                    if line.strip():
                        logger.info(f"    {line}")
            return True
        else:
            logger.error(f"  ✗ Docker rebuild failed (exit code {result.returncode})")
            if result.stderr:
                error_lines = result.stderr.strip().split('\n')
                logger.error(f"  Error output:")
                for line in error_lines[-10:]:
                    if line.strip():
                        logger.error(f"    {line}")

            # Check for common error: Docker not available
            if 'docker' in result.stderr.lower() and ('not found' in result.stderr.lower() or 'permission denied' in result.stderr.lower()):
                logger.error(
                    f"\n  Hint: Docker rebuild requires running inside the orchestrator container.\n"
                    f"  Run: docker-compose exec orchestrator python scripts/maintain_agent_team.py --project {project} --rebuild-images"
                )

            return False

    except subprocess.TimeoutExpired:
        logger.error(f"  ✗ Docker rebuild timeout (exceeded 30 minutes)")
        return False
    except Exception as e:
        logger.error(f"  ✗ Failed to trigger Docker rebuild: {e}")
        logger.error(
            f"  Hint: Ensure you're running inside the orchestrator container or omit --rebuild-images flag"
        )
        return False


async def _run_async_phases(project: str, config: Dict[str, Any]) -> tuple:
    """
    Run async phases (analysis and strategy) in a single event loop

    Args:
        project: Project name
        config: Project configuration

    Returns:
        Tuple of (analysis, strategy)
    """
    from scripts.analyze_codebase import run_codebase_analysis
    from scripts.generate_strategy import generate_strategy_with_llm

    # Phase 2: Analyze codebase
    analysis = await run_codebase_analysis(project, get_workspace_root())

    # Phase 3: Generate strategy
    strategy = await generate_strategy_with_llm(project, analysis, config)

    return analysis, strategy


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
        logger.info("  Phase 8: Update state")
        if args.rebuild_images:
            logger.info("  Phase 9: Rebuild Docker image (--rebuild-images)")
        if args.cleanup:
            logger.info("  Cleanup: Remove outdated artifacts (--cleanup)")
        return {'status': 'dry_run'}

    # Phase 2 & 3: Run async phases (analysis and strategy) in single event loop
    logger.info("\nPhase 2: Analyzing codebase...")
    import asyncio
    from scripts.generate_strategy import display_strategy, confirm_strategy

    try:
        config = config_manager.get_project_config(project)

        # Run both async phases in a single event loop to avoid event loop conflicts
        analysis, strategy = asyncio.run(_run_async_phases(project, config))

        logger.info(f"  ✓ Analysis complete (summaries created)")
        logger.info("\nPhase 3: Generating strategy...")
        logger.info(f"  ✓ Strategy generated ({len(strategy['agents'])} agents, {len(strategy['skills'])} skills)")
    except Exception as e:
        logger.error(f"  ✗ Async phases failed: {e}")
        return {'status': 'error', 'phase': 'async_phases', 'error': str(e)}

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
    logger.info(f"\nPhase 7: Artifacts deployed to {project}/.claude/")

    # Phase 8: Update state
    logger.info("\nPhase 8: Updating state...")
    update_state_with_generation(project, changes, analysis, created_artifacts)
    logger.info("  ✓ State updated")

    # Phase 9: Rebuild Docker images (if --rebuild-images flag and Dockerfile.agent exists)
    rebuild_triggered = False
    if args.rebuild_images:
        workspace_root = get_workspace_root()
        dockerfile_path = workspace_root / project / 'Dockerfile.agent'

        if dockerfile_path.exists():
            logger.info(f"\nPhase 9: Rebuilding Docker image for {project}...")
            # Note: dry_run is always False here (early return at line 428 if True)
            # But pass it anyway as defense-in-depth
            rebuild_success = trigger_docker_rebuild(project, dry_run=False)

            if rebuild_success:
                logger.info(f"  ✓ Docker image rebuild completed")
                rebuild_triggered = True
            else:
                logger.warning(f"  ⚠ Docker image rebuild failed (see logs above)")
        else:
            logger.info(f"\nPhase 9: Skipping Docker rebuild (no Dockerfile.agent for {project})")

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
    if args.rebuild_images:
        if rebuild_triggered:
            logger.info(f"  Docker Rebuild: ✓ Success")
        else:
            logger.info(f"  Docker Rebuild: Skipped (no Dockerfile.agent)")

    return {
        'status': 'completed',
        'artifacts': created_artifacts,
        'validation': validation_results,
        'rebuild_triggered': rebuild_triggered if args.rebuild_images else None
    }


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
  # Generate for all projects (can run outside container)
  python scripts/maintain_agent_team.py

  # Generate for specific project (can run outside container)
  python scripts/maintain_agent_team.py --project context-studio

  # Preview changes without executing
  python scripts/maintain_agent_team.py --dry-run

  # Full workflow with auto-approval
  python scripts/maintain_agent_team.py --auto-approve --cleanup

  # Generate and rebuild Docker images (MUST run inside orchestrator container)
  docker-compose exec orchestrator python scripts/maintain_agent_team.py --auto-approve --rebuild-images --project rounds
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
        help='Rebuild Docker images after generation (requires orchestrator container: docker-compose exec orchestrator ...)'
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

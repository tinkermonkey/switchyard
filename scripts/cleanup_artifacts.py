#!/usr/bin/env python3
"""
Artifact Cleanup Manager

Safely removes outdated generated artifacts with archiving.
"""

import logging
import os
import shutil
import sys
import yaml
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, List, Optional

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from scripts.validate_artifacts import validate_artifact, validate_yaml_frontmatter

# Configure logging
logger = logging.getLogger(__name__)

# Constants for orchestrator-level directories
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
    validate_project_name(project)
    workspace_root = get_workspace_root()
    project_dir = workspace_root / project
    return project_dir / '.claude'


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


def load_project_state(project: str) -> Dict[str, Any]:
    """
    Load agent generation state for a project

    Args:
        project: Project name

    Returns:
        State data or empty structure if not exists
    """
    validate_project_name(project)
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
        'artifacts': {
            'agents': [],
            'skills': []
        }
    }


def identify_outdated_artifacts(project: str) -> List[Dict[str, Any]]:
    """
    Identify artifacts eligible for cleanup by checking against project state

    Args:
        project: Project name

    Returns:
        List of outdated artifacts with metadata
    """
    # Load per-project state
    state = load_project_state(project)

    # Check for validation failures in registered artifacts
    outdated = []

    # Get project-specific directories
    claude_dir = get_project_claude_dir(project)
    agents_dir = claude_dir / 'agents' / 'switchyard'
    skills_dir = claude_dir / 'skills'

    # Check registered agents for validation failures
    for agent_name in state.get('artifacts', {}).get('agents', []):
        agent_file_path = agents_dir / f"{agent_name}.md"

        if not agent_file_path.exists():
            # Registered but missing - mark for cleanup from state
            outdated.append({
                'type': 'agent',
                'name': agent_name,
                'file': str(agent_file_path),
                'reason': 'missing_file'
            })
            continue

        # Check validation
        validation = validate_artifact(agent_file_path)
        if not validation['passed']:
            outdated.append({
                'type': 'agent',
                'name': agent_name,
                'file': str(agent_file_path),
                'reason': 'validation_failure',
                'errors': validation['errors']
            })

    # Check registered skills for validation failures
    for skill_name in state.get('artifacts', {}).get('skills', []):
        skill_dir_path = skills_dir / skill_name
        skill_file_path = skill_dir_path / 'SKILL.md'

        if not skill_file_path.exists():
            # Registered but missing
            outdated.append({
                'type': 'skill',
                'name': skill_name,
                'file': str(skill_file_path),
                'directory': str(skill_dir_path),
                'reason': 'missing_file'
            })
            continue

        # Check validation
        validation = validate_artifact(skill_file_path)
        if not validation['passed']:
            outdated.append({
                'type': 'skill',
                'name': skill_name,
                'file': str(skill_file_path),
                'directory': str(skill_dir_path),
                'reason': 'validation_failure',
                'errors': validation['errors']
            })

    return outdated


def identify_orphaned_artifacts(project: str) -> List[Dict[str, Any]]:
    """
    Find generated artifacts on filesystem NOT in project state

    Args:
        project: Project name

    Returns:
        List of orphaned artifacts
    """
    # Load per-project state
    state = load_project_state(project)
    registered_agents = set(state.get('artifacts', {}).get('agents', []))
    registered_skills = set(state.get('artifacts', {}).get('skills', []))

    orphaned = []

    # Get project-specific directories
    claude_dir = get_project_claude_dir(project)
    agents_dir = claude_dir / 'agents' / 'switchyard'
    skills_dir = claude_dir / 'skills'

    # Check agents on filesystem
    if agents_dir.exists():
        for agent_file in agents_dir.glob('*.md'):
            # Check if generated and not in state
            validation = validate_yaml_frontmatter(agent_file)
            if validation.get('passed') and validation.get('frontmatter', {}).get('generated'):
                if agent_file.stem not in registered_agents:
                    orphaned.append({
                        'type': 'agent',
                        'name': agent_file.stem,
                        'file': str(agent_file),
                        'reason': 'not_in_project_state'
                    })

    # Check skills on filesystem
    if skills_dir.exists():
        for skill_dir in skills_dir.glob(f'{project}-*'):
            if skill_dir.is_dir():
                skill_file = skill_dir / 'SKILL.md'
                if skill_file.exists():
                    validation = validate_yaml_frontmatter(skill_file)
                    if validation.get('passed') and validation.get('frontmatter', {}).get('generated'):
                        if skill_dir.name not in registered_skills:
                            orphaned.append({
                                'type': 'skill',
                                'name': skill_dir.name,
                                'file': str(skill_file),
                                'directory': str(skill_dir),
                                'reason': 'not_in_project_state'
                            })

    return orphaned


def archive_artifact(artifact: Dict[str, Any], project: str) -> Path:
    """
    Archive artifact before deletion

    Args:
        artifact: Artifact metadata
        project: Project name

    Returns:
        Path to archived artifact
    """
    # Create archive directory with timestamp in project's .claude/archives/
    claude_dir = get_project_claude_dir(project)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = claude_dir / 'archives' / timestamp
    archive_dir.mkdir(parents=True, exist_ok=True)

    # Copy artifact to archive
    source = Path(artifact['file'])

    if artifact['type'] == 'agent' and source.is_file():
        dest = archive_dir / source.name
        shutil.copy2(source, dest)
        logger.debug(f"  Archived agent: {dest}")
        return dest

    elif artifact['type'] == 'skill':
        source_dir = Path(artifact.get('directory', source.parent))
        if source_dir.is_dir():
            dest = archive_dir / source_dir.name
            shutil.copytree(source_dir, dest)
            logger.debug(f"  Archived skill: {dest}")
            return dest

    raise ValueError(f"Cannot archive artifact: {artifact}")


def safe_delete_artifact(artifact: Dict[str, Any], project: str, dry_run: bool = False):
    """
    Safely delete artifact with archiving

    Args:
        artifact: Artifact metadata
        project: Project name
        dry_run: If True, don't actually delete
    """
    artifact_path = Path(artifact['file']).resolve()

    # Get project-specific directories for validation
    claude_dir = get_project_claude_dir(project)
    agents_dir = claude_dir / 'agents' / 'switchyard'
    skills_dir = claude_dir / 'skills'

    # 0. Path traversal protection - verify path is within expected directory
    if artifact['type'] == 'agent':
        if not artifact_path.is_relative_to(agents_dir.resolve()):
            raise ValueError(f"Refusing to delete agent path outside agents directory: {artifact_path}")
    elif artifact['type'] == 'skill':
        skill_dir = Path(artifact.get('directory', artifact_path.parent)).resolve()
        if not skill_dir.is_relative_to(skills_dir.resolve()):
            raise ValueError(f"Refusing to delete skill path outside skills directory: {skill_dir}")

    # 1. Verify generated flag (safety check)
    if artifact_path.exists():
        validation = validate_yaml_frontmatter(artifact_path)
        if not validation.get('frontmatter', {}).get('generated', False):
            raise ValueError(f"Cannot delete non-generated artifact: {artifact['name']}")

    if dry_run:
        logger.info(f"[DRY RUN] Would delete: {artifact['file']}")
        return

    # 2. Archive before deletion
    try:
        if artifact_path.exists():
            archive_path = archive_artifact(artifact, project)
            logger.debug(f"  Archived to: {archive_path}")
    except Exception as e:
        logger.error(f"  ✗ Failed to archive {artifact['name']}: {e}")
        raise

    # 3. Delete artifact
    try:
        if artifact['type'] == 'agent':
            if artifact_path.is_file():
                artifact_path.unlink()
                logger.info(f"  ✓ Deleted agent: {artifact['name']}")

        elif artifact['type'] == 'skill':
            skill_dir = Path(artifact.get('directory', artifact_path.parent)).resolve()
            # Double-check boundary (already validated above, defense in depth)
            if not skill_dir.is_relative_to(skills_dir.resolve()):
                raise ValueError(f"Path traversal detected: {skill_dir}")
            if skill_dir.is_dir():
                shutil.rmtree(skill_dir)
                logger.info(f"  ✓ Deleted skill: {artifact['name']}")

    except Exception as e:
        logger.error(f"  ✗ Failed to delete {artifact['name']}: {e}")
        raise


def remove_from_project_state(project: str, artifact: Dict[str, Any]):
    """
    Remove artifact from project state

    Args:
        project: Project name
        artifact: Artifact metadata
    """
    state = load_project_state(project)

    if artifact['type'] == 'agent':
        agents = state.get('artifacts', {}).get('agents', [])
        if artifact['name'] in agents:
            agents.remove(artifact['name'])
            state.setdefault('artifacts', {})['agents'] = agents

    elif artifact['type'] == 'skill':
        skills = state.get('artifacts', {}).get('skills', [])
        if artifact['name'] in skills:
            skills.remove(artifact['name'])
            state.setdefault('artifacts', {})['skills'] = skills

    # Save updated state
    state_file = STATE_DIR / project / 'agent_generation_state.yaml'
    state_file.parent.mkdir(parents=True, exist_ok=True)

    with open(state_file, 'w') as f:
        yaml.safe_dump(state, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"  Updated project state: removed {artifact['name']}")


def cleanup_project_artifacts(project: str, dry_run: bool = False, force: bool = False):
    """
    Cleanup outdated artifacts for a project

    Args:
        project: Project name
        dry_run: If True, preview without deleting
        force: If True, skip confirmation
    """
    logger.info(f"Cleaning up artifacts for {project}...")

    # Safety check: ensure state exists before cleanup
    state = load_project_state(project)
    if not state or not state.get('artifacts'):
        logger.warning(f"  ⚠ No state file for {project} - skipping cleanup to prevent data loss")
        logger.warning(f"    Run maintain_agent_team.py first to create state")
        return

    # Identify outdated artifacts (validation failures, missing files)
    outdated = identify_outdated_artifacts(project)

    # Also check for orphaned artifacts (not in project state)
    orphaned = identify_orphaned_artifacts(project)

    # Deduplicate by artifact name before extending
    existing_names = {a['name'] for a in outdated}
    unique_orphaned = [a for a in orphaned if a['name'] not in existing_names]
    outdated.extend(unique_orphaned)

    if not outdated:
        logger.info(f"  ✓ No outdated artifacts for {project}")
        return

    logger.info(f"  Found {len(outdated)} outdated artifact(s)")

    # Confirm if not forced
    if not force and not dry_run:
        print("\nArtifacts to delete:")
        for artifact in outdated:
            reason = artifact['reason'].replace('_', ' ').title()
            print(f"  - {artifact['name']} ({reason})")
            if 'errors' in artifact:
                for error in artifact['errors'][:2]:
                    print(f"    Error: {error}")

        response = input("\nProceed with deletion? [y/N]: ").strip().lower()
        if response not in ['y', 'yes']:
            logger.info("  ⊗ Cleanup cancelled by user")
            return

    # Delete each artifact
    deleted_count = 0
    for artifact in outdated:
        try:
            safe_delete_artifact(artifact, project, dry_run)
            if not dry_run:
                remove_from_project_state(project, artifact)
            deleted_count += 1
        except Exception as e:
            logger.error(f"  ✗ Error deleting {artifact['name']}: {e}")

    if dry_run:
        logger.info(f"  [DRY RUN] Would delete {deleted_count} artifact(s)")
    else:
        logger.info(f"  ✓ Deleted {deleted_count} artifact(s)")


def main():
    """CLI entry point"""
    import argparse

    parser = argparse.ArgumentParser(description="Cleanup outdated generated artifacts")
    parser.add_argument('project', help='Project name')
    parser.add_argument('--dry-run', action='store_true', help='Preview without deleting')
    parser.add_argument('--force', action='store_true', help='Skip confirmation')
    args = parser.parse_args()

    # Setup logging
    logging.basicConfig(level=logging.INFO, format='%(message)s')

    try:
        cleanup_project_artifacts(args.project, dry_run=args.dry_run, force=args.force)
    except Exception as e:
        logger.error(f"✗ Cleanup failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

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

# Constants
CLAUDE_DIR = Path(os.environ.get('ORCHESTRATOR_ROOT', '.')) / '.claude'
AGENTS_DIR = CLAUDE_DIR / 'agents'
SKILLS_DIR = CLAUDE_DIR / 'skills'
ARCHIVE_DIR = CLAUDE_DIR / 'archives'
MANIFEST_FILE = CLAUDE_DIR / 'generated' / 'manifest.yaml'


def load_manifest() -> Dict[str, Any]:
    """Load generation manifest"""
    if MANIFEST_FILE.exists():
        try:
            with open(MANIFEST_FILE, 'r') as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            logger.warning(f"Failed to load manifest: {e}")
            return {}

    return {'version': '1.0', 'projects': {}}


def save_manifest(manifest: Dict[str, Any]):
    """Save generation manifest"""
    try:
        MANIFEST_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(MANIFEST_FILE, 'w') as f:
            yaml.safe_dump(manifest, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logger.error(f"Failed to save manifest: {e}")
        raise


def identify_outdated_artifacts(project: str) -> List[Dict[str, Any]]:
    """
    Identify artifacts eligible for cleanup

    Args:
        project: Project name

    Returns:
        List of outdated artifacts with metadata
    """
    # Load manifest
    manifest = load_manifest()
    project_data = manifest.get('projects', {}).get(project, {})

    if not project_data:
        logger.info(f"  No manifest entry for {project}, checking filesystem...")
        # Fall back to filesystem scan
        return identify_orphaned_artifacts(project)

    outdated = []

    # Check each agent in manifest
    for agent in project_data.get('agents', []):
        agent_file = AGENTS_DIR / agent['file'].replace('agents/', '')

        # Criteria 1: File doesn't exist (orphaned entry)
        if not agent_file.exists():
            outdated.append({
                'type': 'agent',
                'name': agent['name'],
                'file': str(agent_file),
                'reason': 'orphaned_entry'
            })
            continue

        # Criteria 2: Validation failures
        validation = validate_artifact(agent_file)
        if not validation['passed']:
            outdated.append({
                'type': 'agent',
                'name': agent['name'],
                'file': str(agent_file),
                'reason': 'validation_failure',
                'errors': validation['errors']
            })

    # Check each skill in manifest
    for skill in project_data.get('skills', []):
        skill_dir = SKILLS_DIR / skill['directory'].replace('skills/', '').rstrip('/')
        skill_file = skill_dir / 'SKILL.md'

        # Criteria 1: File doesn't exist (orphaned entry)
        if not skill_file.exists():
            outdated.append({
                'type': 'skill',
                'name': skill['name'],
                'file': str(skill_file),
                'directory': str(skill_dir),
                'reason': 'orphaned_entry'
            })
            continue

        # Criteria 2: Validation failures
        validation = validate_artifact(skill_file)
        if not validation['passed']:
            outdated.append({
                'type': 'skill',
                'name': skill['name'],
                'file': str(skill_file),
                'directory': str(skill_dir),
                'reason': 'validation_failure',
                'errors': validation['errors']
            })

    return outdated


def identify_orphaned_artifacts(project: str) -> List[Dict[str, Any]]:
    """
    Find artifacts on filesystem with no manifest entry

    Args:
        project: Project name

    Returns:
        List of orphaned artifacts
    """
    orphaned = []

    # Check agents
    if AGENTS_DIR.exists():
        for agent_file in AGENTS_DIR.glob(f'{project}-*.md'):
            # Check if generated
            validation = validate_yaml_frontmatter(agent_file)
            if validation.get('passed') and validation.get('frontmatter', {}).get('generated'):
                orphaned.append({
                    'type': 'agent',
                    'name': agent_file.stem,
                    'file': str(agent_file),
                    'reason': 'no_manifest_entry'
                })

    # Check skills
    if SKILLS_DIR.exists():
        for skill_dir in SKILLS_DIR.glob(f'{project}-*'):
            if skill_dir.is_dir():
                skill_file = skill_dir / 'SKILL.md'
                if skill_file.exists():
                    validation = validate_yaml_frontmatter(skill_file)
                    if validation.get('passed') and validation.get('frontmatter', {}).get('generated'):
                        orphaned.append({
                            'type': 'skill',
                            'name': skill_dir.name,
                            'file': str(skill_file),
                            'directory': str(skill_dir),
                            'reason': 'no_manifest_entry'
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
    # Create archive directory with timestamp
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    archive_dir = ARCHIVE_DIR / project / timestamp
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

    # 0. Path traversal protection - verify path is within expected directory
    if artifact['type'] == 'agent':
        if not artifact_path.is_relative_to(AGENTS_DIR.resolve()):
            raise ValueError(f"Refusing to delete agent path outside agents directory: {artifact_path}")
    elif artifact['type'] == 'skill':
        skill_dir = Path(artifact.get('directory', artifact_path.parent)).resolve()
        if not skill_dir.is_relative_to(SKILLS_DIR.resolve()):
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
            if not skill_dir.is_relative_to(SKILLS_DIR.resolve()):
                raise ValueError(f"Path traversal detected: {skill_dir}")
            if skill_dir.is_dir():
                shutil.rmtree(skill_dir)
                logger.info(f"  ✓ Deleted skill: {artifact['name']}")

    except Exception as e:
        logger.error(f"  ✗ Failed to delete {artifact['name']}: {e}")
        raise


def remove_from_manifest(project: str, artifact: Dict[str, Any]):
    """
    Remove artifact from manifest

    Args:
        project: Project name
        artifact: Artifact metadata
    """
    manifest = load_manifest()

    if project not in manifest.get('projects', {}):
        return

    project_data = manifest['projects'][project]

    if artifact['type'] == 'agent':
        project_data['agents'] = [
            a for a in project_data.get('agents', [])
            if a['name'] != artifact['name']
        ]

    elif artifact['type'] == 'skill':
        project_data['skills'] = [
            s for s in project_data.get('skills', [])
            if s['name'] != artifact['name']
        ]

    save_manifest(manifest)
    logger.debug(f"  Updated manifest: removed {artifact['name']}")


def cleanup_project_artifacts(project: str, dry_run: bool = False, force: bool = False):
    """
    Cleanup outdated artifacts for a project

    Args:
        project: Project name
        dry_run: If True, preview without deleting
        force: If True, skip confirmation
    """
    logger.info(f"Cleaning up artifacts for {project}...")

    # Identify outdated artifacts
    outdated = identify_outdated_artifacts(project)

    # Also check for orphaned artifacts (not in manifest)
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
                remove_from_manifest(project, artifact)
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

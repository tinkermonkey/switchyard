#!/usr/bin/env python3
"""
Cleanup Orphaned Feature Branches

This script cleans up feature branches for closed parent issues.
Should be run periodically (e.g., daily via cron).

Usage:
    python scripts/cleanup_orphaned_branches.py [--project PROJECT_NAME]
"""

import asyncio
import logging
import argparse
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.feature_branch_manager import feature_branch_manager
from services.github_integration import GitHubIntegration
from config.manager import config_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


async def cleanup_project_branches(project_name: str):
    """Cleanup orphaned branches for a specific project"""
    logger.info(f"Cleaning up orphaned branches for project: {project_name}")

    try:
        # Get project config
        project_config = config_manager.get_project_config(project_name)
        if not project_config or not hasattr(project_config, 'repository'):
            logger.error(f"Project {project_name} not found or has no repository configured")
            return

        # Parse repository info
        repo_parts = project_config.repository.split('/')
        if len(repo_parts) != 2:
            logger.error(f"Invalid repository format: {project_config.repository}")
            return

        repo_owner, repo_name = repo_parts

        # Create GitHub integration
        gh_integration = GitHubIntegration(repo_owner=repo_owner, repo_name=repo_name)

        # Run cleanup
        await feature_branch_manager.cleanup_orphaned_branches(
            project=project_name,
            github_integration=gh_integration
        )

        logger.info(f"Cleanup completed for project: {project_name}")

    except Exception as e:
        logger.error(f"Error cleaning up project {project_name}: {e}", exc_info=True)


async def cleanup_all_projects():
    """Cleanup orphaned branches for all configured projects"""
    logger.info("Starting cleanup of orphaned branches for all projects")

    # Get all project configurations
    project_configs = config_manager.get_all_project_configs()

    if not project_configs:
        logger.warning("No projects configured")
        return

    # Cleanup each project
    for project_name in project_configs.keys():
        await cleanup_project_branches(project_name)

    logger.info("Cleanup completed for all projects")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Cleanup orphaned feature branches for closed parent issues"
    )
    parser.add_argument(
        '--project',
        type=str,
        help='Specific project to cleanup (default: all projects)'
    )

    args = parser.parse_args()

    # Run cleanup
    if args.project:
        asyncio.run(cleanup_project_branches(args.project))
    else:
        asyncio.run(cleanup_all_projects())


if __name__ == '__main__':
    main()

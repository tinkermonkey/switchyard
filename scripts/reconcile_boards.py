#!/usr/bin/env python3
"""
Quick script to reconcile GitHub project boards without starting full orchestrator
"""
import os
import sys
import asyncio
import logging

# Add project root to path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)
os.chdir(project_root)

from config.manager import config_manager
from config.state_manager import state_manager as github_state_manager
from services.github_project_manager import GitHubProjectManager

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

async def main():
    """Reconcile all projects with GitHub"""
    logger.info("🔧 Starting GitHub project reconciliation...")

    # Initialize GitHub project manager
    github_pm = GitHubProjectManager(config_manager, github_state_manager)

    # Get all configured projects
    projects = config_manager.list_projects()
    logger.info(f"📋 Found {len(projects)} configured projects")

    success_count = 0
    failure_count = 0

    for project_name in projects:
        if github_state_manager.needs_reconciliation(project_name):
            logger.info(f"🔄 Reconciling project: {project_name}")
            success = await github_pm.reconcile_project(project_name)

            if success:
                logger.info(f"✅ Successfully reconciled {project_name}")
                success_count += 1
            else:
                logger.error(f"❌ Failed to reconcile {project_name}")
                failure_count += 1
        else:
            logger.info(f"✓ Project '{project_name}' is already synchronized")
            success_count += 1

    logger.info(f"\n📊 Reconciliation complete:")
    logger.info(f"   ✅ Successful: {success_count}")
    logger.info(f"   ❌ Failed: {failure_count}")

    if failure_count > 0:
        logger.warning(f"\n⚠️  {failure_count} projects failed reconciliation")
        logger.warning("   Check your GitHub token permissions and network connectivity")
        sys.exit(1)
    else:
        logger.info("\n🎉 All projects successfully reconciled!")

if __name__ == "__main__":
    asyncio.run(main())

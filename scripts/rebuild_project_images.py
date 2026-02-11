#!/usr/bin/env python3
"""
Rebuild Project Docker Images

Rebuilds Docker images for all projects with Dockerfile.agent files.
Optionally triggers dev_environment_setup and dev_environment_verifier agents.

Usage:
    python scripts/rebuild_project_images.py [options]

Options:
    --project PROJECT       Rebuild only the specified project
    --with-agents          Queue dev environment setup/verification agents
    --dry-run              Show what would be done without executing
    --update-state         Update dev container state to VERIFIED after successful builds
    -h, --help             Show this help message

Examples:
    # Rebuild all project images
    python scripts/rebuild_project_images.py

    # Rebuild specific project
    python scripts/rebuild_project_images.py --project context-studio

    # Rebuild with state update
    python scripts/rebuild_project_images.py --update-state

    # Rebuild and trigger dev environment agents
    python scripts/rebuild_project_images.py --with-agents

    # Dry run to see what would happen
    python scripts/rebuild_project_images.py --dry-run
"""

import argparse
import logging
import os
import subprocess
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set ORCHESTRATOR_ROOT for running outside Docker container
# Inside container: /app (set by container environment)
# Outside container: Current directory (clauditoreum/)
if 'ORCHESTRATOR_ROOT' not in os.environ:
    os.environ['ORCHESTRATOR_ROOT'] = str(Path(__file__).parent.parent.resolve())

from config.manager import config_manager
from services.dev_container_state import dev_container_state, DevContainerStatus

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Lazy imports for task queue (only needed for --with-agents)
# This allows the script to run on host for basic rebuilds
Task = None
TaskPriority = None
TaskQueue = None


def _import_task_queue():
    """Lazy import of task queue modules"""
    global Task, TaskPriority, TaskQueue
    if Task is None:
        from task_queue.task_manager import Task as _Task, TaskPriority as _TaskPriority, TaskQueue as _TaskQueue
        Task = _Task
        TaskPriority = _TaskPriority
        TaskQueue = _TaskQueue


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


def discover_projects_with_dockerfiles(project_filter: str = None) -> list:
    """
    Discover projects with Dockerfile.agent files

    Args:
        project_filter: Optional specific project name to filter to

    Returns:
        List of project names with Dockerfile.agent
    """
    workspace_root = get_workspace_root()

    # Get all visible projects
    if project_filter:
        projects = [project_filter]
    else:
        projects = config_manager.list_visible_projects()

    # Filter to projects with Dockerfile.agent
    projects_with_dockerfiles = []
    for project in projects:
        dockerfile = workspace_root / project / 'Dockerfile.agent'
        if dockerfile.exists():
            projects_with_dockerfiles.append(project)
        else:
            logger.debug(f"Skipping {project}: No Dockerfile.agent found")

    return projects_with_dockerfiles


def rebuild_project_image(
    project_name: str,
    dry_run: bool = False,
    update_state: bool = False
) -> bool:
    """
    Rebuild Docker image for a project

    Args:
        project_name: Name of the project
        dry_run: If True, show what would be done without executing
        update_state: If True, update dev container state to VERIFIED on success

    Returns:
        True if rebuild succeeded, False otherwise
    """
    workspace_root = get_workspace_root()
    project_dir = workspace_root / project_name
    dockerfile = project_dir / 'Dockerfile.agent'

    # Verify Dockerfile exists
    if not dockerfile.exists():
        logger.warning(f"Skipping {project_name}: No Dockerfile.agent found at {dockerfile}")
        return False

    # Build image name
    image_name = f"{project_name}-agent:latest"

    # Construct build command
    build_cmd = [
        'docker', 'build',
        '-f', str(dockerfile),
        '-t', image_name,
        str(project_dir)
    ]

    if dry_run:
        logger.info(f"[DRY RUN] Would execute: {' '.join(build_cmd)}")
        return True

    # Execute build
    logger.info(f"Building {image_name}...")
    try:
        result = subprocess.run(
            build_cmd,
            capture_output=True,
            text=True,
            timeout=1800  # 30 minute timeout for large builds
        )

        if result.returncode == 0:
            logger.info(f"✓ Successfully built {image_name}")

            # Optionally update dev container state
            if update_state:
                dev_container_state.set_status(
                    project_name,
                    DevContainerStatus.VERIFIED,
                    image_name=image_name
                )
                logger.info(f"  Updated dev container state to VERIFIED")

            return True
        else:
            logger.error(f"✗ Failed to build {image_name}")
            logger.error(f"  Return code: {result.returncode}")
            if result.stderr:
                # Show last 20 lines of error output
                error_lines = result.stderr.strip().split('\n')
                logger.error(f"  Error output (last 20 lines):")
                for line in error_lines[-20:]:
                    logger.error(f"    {line}")
            return False

    except subprocess.TimeoutExpired:
        logger.error(f"✗ Build timeout for {image_name} (exceeded 30 minutes)")
        return False
    except Exception as e:
        logger.error(f"✗ Failed to build {image_name}: {e}")
        return False


def queue_dev_env_setup(project_name: str, task_queue, dry_run: bool = False) -> bool:
    """
    Queue dev environment setup task for a project

    Args:
        project_name: Name of the project
        task_queue: Task queue instance
        dry_run: If True, show what would be done without executing

    Returns:
        True if task queued successfully, False otherwise
    """
    # Import task queue dependencies
    _import_task_queue()

    task_id = str(uuid.uuid4())

    if dry_run:
        logger.info(f"[DRY RUN] Would queue dev_environment_setup for {project_name} (task_id: {task_id})")
        return True

    # Create task with appropriate context
    # Use workspace_type='discussions' to bypass git branch preparation
    # since this is a manual rebuild without a GitHub issue
    task = Task(
        id=task_id,
        agent="dev_environment_setup",
        project=project_name,
        priority=TaskPriority.HIGH,
        context={
            'issue': {
                'title': f'Development environment setup for {project_name}',
                'body': f'Manual rebuild trigger via rebuild_project_images.py\n\nTimestamp: {datetime.now().isoformat()}',
                'number': 0
            },
            'issue_number': 0,
            'board': 'Environment Support',  # Use environment support board
            'project': project_name,
            'repository': project_name,
            'automated_setup': True,
            'use_docker': False,  # CRITICAL: Must be False for setup agent
            'workspace_type': 'discussions',  # Use discussions to bypass branch prep
            'skip_branch_prep': True  # Additional flag to skip branch operations
        },
        created_at=datetime.now().isoformat()
    )

    try:
        task_queue.enqueue(task)
        logger.info(f"✓ Queued dev_environment_setup for {project_name} (task_id: {task_id})")
        return True
    except Exception as e:
        logger.error(f"✗ Failed to queue task for {project_name}: {e}")
        return False


def rebuild_all_projects(args):
    """
    Main function to rebuild project images

    Args:
        args: Parsed command-line arguments
    """
    # Discover projects
    logger.info("Discovering projects with Dockerfile.agent...")
    projects = discover_projects_with_dockerfiles(args.project)

    if not projects:
        if args.project:
            logger.error(f"Project '{args.project}' not found or has no Dockerfile.agent")
        else:
            logger.warning("No projects with Dockerfile.agent found")
        return

    logger.info(f"Found {len(projects)} project(s) with Dockerfile.agent:")
    for project in projects:
        logger.info(f"  - {project}")

    if args.dry_run:
        logger.info("\n[DRY RUN MODE] No changes will be made\n")

    # Mode 1: Rebuild images
    if not args.with_agents:
        logger.info("\n=== Rebuilding Docker Images ===\n")
        results = {}

        for project in projects:
            results[project] = rebuild_project_image(
                project,
                dry_run=args.dry_run,
                update_state=args.update_state
            )

        # Report summary
        successes = sum(1 for r in results.values() if r)
        failures = len(results) - successes

        logger.info("\n=== Rebuild Summary ===")
        logger.info(f"Total: {len(results)} projects")
        logger.info(f"✓ Succeeded: {successes}")
        if failures > 0:
            logger.info(f"✗ Failed: {failures}")
            failed_projects = [p for p, r in results.items() if not r]
            for project in failed_projects:
                logger.info(f"  - {project}")

    # Mode 2: Queue agent tasks
    else:
        logger.info("\n=== Queueing Dev Environment Setup Agents ===\n")

        if args.dry_run:
            logger.info("[DRY RUN] Would queue dev_environment_setup tasks for:")
            for project in projects:
                logger.info(f"  - {project}")
            logger.info("\nNote: dev_environment_verifier will be auto-triggered after each setup completes")
        else:
            # Import task queue dependencies
            _import_task_queue()

            # Initialize task queue
            task_queue = TaskQueue(use_redis=True)
            results = {}

            for project in projects:
                results[project] = queue_dev_env_setup(project, task_queue, dry_run=False)

            # Report summary
            successes = sum(1 for r in results.values() if r)
            failures = len(results) - successes

            logger.info("\n=== Queue Summary ===")
            logger.info(f"Total: {len(results)} projects")
            logger.info(f"✓ Queued: {successes}")
            if failures > 0:
                logger.info(f"✗ Failed to queue: {failures}")
                failed_projects = [p for p, r in results.items() if not r]
                for project in failed_projects:
                    logger.info(f"  - {project}")

            logger.info("\nNote: dev_environment_verifier will be auto-triggered after each setup completes")
            logger.info("Monitor agent execution: curl http://localhost:5001/agents/active")


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Rebuild project Docker images and optionally trigger dev environment agents",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Rebuild all project images
  python scripts/rebuild_project_images.py

  # Rebuild specific project
  python scripts/rebuild_project_images.py --project context-studio

  # Rebuild and update state
  python scripts/rebuild_project_images.py --update-state

  # Rebuild and trigger dev environment agents
  python scripts/rebuild_project_images.py --with-agents

  # Dry run to preview actions
  python scripts/rebuild_project_images.py --dry-run
        """
    )

    parser.add_argument(
        '--project',
        type=str,
        help='Specific project to rebuild (default: all visible projects)'
    )

    parser.add_argument(
        '--with-agents',
        action='store_true',
        help='Queue dev_environment_setup agents instead of directly rebuilding images'
    )

    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be done without executing'
    )

    parser.add_argument(
        '--update-state',
        action='store_true',
        help='Update dev container state to VERIFIED after successful builds (only with direct rebuilds)'
    )

    args = parser.parse_args()

    # Validate argument combinations
    if args.update_state and args.with_agents:
        logger.warning("--update-state is ignored when using --with-agents (agents manage state automatically)")

    # Run rebuild
    try:
        rebuild_all_projects(args)
    except KeyboardInterrupt:
        logger.info("\nInterrupted by user")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Unexpected error: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()

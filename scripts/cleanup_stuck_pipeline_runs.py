#!/usr/bin/env python3
"""
Cleanup Stuck Pipeline Runs

This script identifies and cleans up pipeline runs that are marked as "active"
but have actually completed (containers have exited). This can happen when
end_pipeline_run() fails due to errors like UnboundLocalError.

Usage:
    python scripts/cleanup_stuck_pipeline_runs.py [--project PROJECT_NAME] [--dry-run]
"""

import asyncio
import logging
import argparse
import sys
import subprocess
from pathlib import Path
from datetime import datetime

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from services.pipeline_run import PipelineRunManager
from config.manager import config_manager

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


def check_container_status(container_name_pattern: str) -> dict:
    """
    Check the status of a Docker container.

    Args:
        container_name_pattern: Pattern to match container name

    Returns:
        dict with 'exists', 'running', 'exit_code' keys
    """
    try:
        # Check if container exists and its status
        result = subprocess.run(
            ['docker', 'ps', '-a', '--filter', f'name={container_name_pattern}',
             '--format', '{{.Names}}\t{{.Status}}\t{{.State}}'],
            capture_output=True,
            text=True,
            check=True
        )

        if not result.stdout.strip():
            return {'exists': False, 'running': False, 'exit_code': None}

        lines = result.stdout.strip().split('\n')
        if not lines:
            return {'exists': False, 'running': False, 'exit_code': None}

        # Parse first matching container
        parts = lines[0].split('\t')
        if len(parts) < 3:
            return {'exists': False, 'running': False, 'exit_code': None}

        name, status, state = parts
        running = state.lower() == 'running'

        # Try to extract exit code from status if exited
        exit_code = None
        if 'Exited' in status:
            try:
                # Status format: "Exited (0) X hours ago"
                exit_code = int(status.split('(')[1].split(')')[0])
            except (IndexError, ValueError):
                pass

        return {
            'exists': True,
            'running': running,
            'exit_code': exit_code,
            'status': status
        }

    except subprocess.CalledProcessError as e:
        logger.warning(f"Error checking container {container_name_pattern}: {e}")
        return {'exists': False, 'running': False, 'exit_code': None}
    except Exception as e:
        logger.error(f"Unexpected error checking container {container_name_pattern}: {e}")
        return {'exists': False, 'running': False, 'exit_code': None}


async def cleanup_stuck_runs(project_name: str = None, dry_run: bool = False):
    """
    Cleanup stuck pipeline runs.

    Args:
        project_name: Optional project name to filter by
        dry_run: If True, only report stuck runs without fixing them
    """
    logger.info(f"Starting cleanup of stuck pipeline runs{f' for project: {project_name}' if project_name else ''}")

    if dry_run:
        logger.info("DRY RUN MODE - No changes will be made")

    try:
        # Initialize pipeline run manager
        pipeline_manager = PipelineRunManager()

        # Get all active pipeline runs from Elasticsearch
        logger.info("Querying for active pipeline runs...")
        query = {
            "query": {
                "bool": {
                    "must": [
                        {"term": {"status": "active"}}
                    ]
                }
            },
            "size": 100,
            "sort": [{"started_at": {"order": "desc"}}]
        }

        if project_name:
            query["query"]["bool"]["must"].append({"term": {"project": project_name}})

        response = pipeline_manager.es.search(
            index="orchestrator-pipeline-run-events-*",
            body=query
        )

        active_runs = [hit['_source'] for hit in response['hits']['hits']]
        logger.info(f"Found {len(active_runs)} active pipeline runs")

        if not active_runs:
            logger.info("No stuck runs to cleanup")
            return

        stuck_runs = []
        truly_active_runs = []

        # Check each active run
        for run in active_runs:
            run_id = run.get('id')
            project = run.get('project')
            issue_number = run.get('issue_number')
            board = run.get('board', 'unknown')
            started_at = run.get('started_at', 'unknown')

            logger.info(f"\nChecking run {run_id} (project: {project}, issue: #{issue_number})")

            # Determine container name pattern based on board
            # Container naming: {stage}-{project}-{issue_number}-{run_id[:8]}
            # Common stages: review-cycle, repair-cycle
            container_patterns = [
                f"review-cycle-{project}-{issue_number}",
                f"repair-cycle-{project}-{issue_number}",
            ]

            container_found = False
            container_running = False
            container_info = None

            for pattern in container_patterns:
                info = check_container_status(pattern)
                if info['exists']:
                    container_found = True
                    container_running = info['running']
                    container_info = info
                    logger.info(f"  Container found: {pattern}")
                    logger.info(f"  Status: {info['status']}")
                    break

            if not container_found:
                logger.info(f"  No container found - likely completed and cleaned up")
                stuck_runs.append({
                    'run': run,
                    'reason': 'container_not_found',
                    'container_info': None
                })
            elif not container_running and container_info['exit_code'] is not None:
                logger.info(f"  Container exited with code {container_info['exit_code']} - run is stuck")
                stuck_runs.append({
                    'run': run,
                    'reason': 'container_exited',
                    'container_info': container_info
                })
            else:
                logger.info(f"  Container is still running - keeping as active")
                truly_active_runs.append(run)

        # Report findings
        logger.info("\n" + "=" * 80)
        logger.info(f"SUMMARY:")
        logger.info(f"  Total active runs: {len(active_runs)}")
        logger.info(f"  Truly active: {len(truly_active_runs)}")
        logger.info(f"  Stuck (need cleanup): {len(stuck_runs)}")
        logger.info("=" * 80)

        if stuck_runs:
            logger.info("\nStuck runs to cleanup:")
            for item in stuck_runs:
                run = item['run']
                reason = item['reason']
                logger.info(f"  - {run['id']} (project: {run['project']}, issue: #{run['issue_number']}) - {reason}")

        if dry_run:
            logger.info("\nDRY RUN MODE - Skipping actual cleanup")
            return

        # Perform cleanup
        if stuck_runs:
            logger.info("\nCleaning up stuck runs...")
            for item in stuck_runs:
                run = item['run']
                project = run['project']
                issue_number = run['issue_number']
                run_id = run['id']

                try:
                    reason = "Cleanup: Container exited but run not marked as completed"
                    if item['reason'] == 'container_not_found':
                        reason = "Cleanup: Container no longer exists, assuming completion"
                    elif item['container_info'] and item['container_info']['exit_code'] is not None:
                        exit_code = item['container_info']['exit_code']
                        if exit_code == 0:
                            reason = f"Cleanup: Container exited successfully (code {exit_code})"
                        else:
                            reason = f"Cleanup: Container exited with error (code {exit_code})"

                    logger.info(f"  Ending run {run_id} for {project} #{issue_number}")
                    pipeline_manager.end_pipeline_run(project, issue_number, reason=reason)
                    logger.info(f"  ✓ Successfully ended run {run_id}")

                except Exception as e:
                    logger.error(f"  ✗ Failed to end run {run_id}: {e}", exc_info=True)

        logger.info("\nCleanup completed!")

    except Exception as e:
        logger.error(f"Error during cleanup: {e}", exc_info=True)
        raise


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description="Cleanup stuck pipeline runs that failed to complete properly"
    )
    parser.add_argument(
        '--project',
        type=str,
        help='Specific project to cleanup (default: all projects)'
    )
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Report stuck runs without actually cleaning them up'
    )

    args = parser.parse_args()

    # Run cleanup
    asyncio.run(cleanup_stuck_runs(
        project_name=args.project,
        dry_run=args.dry_run
    ))


if __name__ == '__main__':
    main()

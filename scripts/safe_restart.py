#!/usr/bin/env python3
"""
Safe Container Restart Script

Safely restarts a Docker container by checking for active repair cycles and investigations.
Usage: python3 safe_restart.py <container_name_or_pattern>
"""

import sys
import os
import logging
import subprocess
import argparse
from pathlib import Path
import redis

# Add /app to python path to import services
sys.path.append('/app')
# Also add current directory's parent to path for local testing
sys.path.append(str(Path(__file__).parent.parent))

try:
    from services.agent_container_recovery import get_agent_container_recovery
    from services.medic.claude_investigation_queue import ClaudeInvestigationQueue
    from services.medic.claude_report_manager import ClaudeReportManager
except ImportError:
    # Allow script to be imported for testing even if services aren't available
    pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('safe_restart')

def check_safety(container_name: str) -> bool:
    """
    Check if it's safe to restart the container.
    Returns True if safe, False otherwise.
    """
    try:
        # Connect to Redis
        redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
        
        # Check active repair cycles
        recovery = get_agent_container_recovery()
        repair_containers = recovery.get_running_repair_cycle_containers()
        
        for rc in repair_containers:
            if container_name in rc['name'] or rc['name'] in container_name:
                logger.warning(f"UNSAFE: Container {container_name} matches active repair cycle {rc['name']}")
                return False
            
            # If restarting a project container, check if repair cycle is using it
            # Repair cycles run in their own containers, but they might depend on project services?
            # Usually repair cycles are self-contained or use the project dev container.
            # If we are restarting the project dev container, we should check if a repair cycle is running for that project.
            
            # Parse repair cycle name to get project
            info = recovery.parse_repair_cycle_container_name(rc['name'])
            if info and info['project'] in container_name:
                 logger.warning(f"UNSAFE: Container {container_name} matches project of active repair cycle {rc['name']}")
                 return False

        # Check active Claude investigations
        claude_queue = ClaudeInvestigationQueue(redis_client)
        active_investigations = claude_queue.get_active()
        
        if active_investigations:
            # We don't easily know which container an investigation is using, 
            # but usually they run on host or ephemeral containers.
            # If we are restarting a project container, we should check if any investigation is for that project.
            report_manager = ClaudeReportManager()
            
            for fingerprint_id in active_investigations:
                summary = report_manager.get_investigation_summary(fingerprint_id)
                project = summary.get('project')
                if project and project in container_name:
                    logger.warning(f"UNSAFE: Container {container_name} matches project of active investigation {fingerprint_id}")
                    return False

        return True

    except Exception as e:
        logger.error(f"Error checking safety: {e}")
        # Fail safe
        return False

def restart_container(container_name: str):
    """Restart the container"""
    logger.info(f"Restarting container: {container_name}")
    try:
        # Find full container name if pattern provided
        if '*' in container_name or '?' in container_name:
             # Use docker ps to find match
             cmd = f"docker ps --format '{{{{.Names}}}}' | grep '{container_name}' | head -n 1"
             result = subprocess.run(cmd, shell=True, capture_output=True, text=True)
             if result.stdout.strip():
                 container_name = result.stdout.strip()
             else:
                 logger.error(f"No container found matching pattern: {container_name}")
                 sys.exit(1)

        subprocess.run(['docker', 'restart', container_name], check=True)
        logger.info(f"Successfully restarted {container_name}")
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to restart container: {e}")
        sys.exit(1)

def main():
    parser = argparse.ArgumentParser(description='Safely restart a Docker container')
    parser.add_argument('container', help='Container name or pattern')
    parser.add_argument('--force', action='store_true', help='Bypass safety checks')
    
    args = parser.parse_args()
    
    if not args.force:
        if not check_safety(args.container):
            logger.error("Safety check failed. Use --force to override.")
            sys.exit(1)
    
    restart_container(args.container)

if __name__ == '__main__':
    main()

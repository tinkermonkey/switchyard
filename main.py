import asyncio
import threading
import time
from pathlib import Path

from flask.cli import F
from config.environment import Environment
from config.manager import config_manager
from config.state_manager import state_manager as github_state_manager
from monitoring.logging import OrchestratorLogger
from monitoring.metrics import MetricsCollector
from state_management.manager import StateManager
from task_queue.task_manager import TaskQueue
from claude.session_manager import ClaudeSessionManager
from monitoring.health_monitor import HealthMonitor
from services.github_project_manager import GitHubProjectManager
from services.project_monitor import ProjectMonitor
from services.project_workspace import workspace_manager
from services.scheduled_tasks import get_scheduled_tasks_service
from services.dev_container_state import dev_container_state
from agents.orchestrator_integration import process_task_integrated

async def main():
    # Load configuration
    env_config = Environment()

    # Initialize components
    logger = OrchestratorLogger("orchestrator")

    # Configure root logger to ensure all logging works
    import logging
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    # Add console handler to root logger if not already present
    if not root_logger.handlers:
        console = logging.StreamHandler()
        console.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        console.setFormatter(formatter)
        root_logger.addHandler(console)

    root_logger.info("=== Orchestrator starting up ===")

    metrics = MetricsCollector()

    # Initialize state management
    state_manager = StateManager(Path("orchestrator_data/state"))

    # Initialize task queue with Redis
    task_queue = TaskQueue(use_redis=True)

    # Initialize health monitor
    health_monitor = HealthMonitor(orchestrator=None)

    # Initialize GitHub project manager with new configuration system
    github_project_manager = GitHubProjectManager(config_manager, github_state_manager)

    # Initialize all project workspaces on startup
    logger.info("Initializing project workspaces")
    projects_needing_setup = workspace_manager.initialize_all_projects()
    logger.info("Project workspaces initialized")

    # Verify Docker images for all projects marked as verified
    # This handles cases where Docker context changed or images were lost
    logger.info("Verifying Docker images for verified projects")
    for project_name in projects_needing_setup.keys():
        image_verified = dev_container_state.verify_and_update_status(project_name)
        if not image_verified:
            # Image was marked verified but doesn't exist - mark for setup
            logger.info(f"Project {project_name} needs dev environment setup (Docker image missing)")
            projects_needing_setup[project_name] = True

    # Queue dev_environment_setup tasks for projects that need it
    from task_queue.task_manager import Task, TaskPriority
    from datetime import datetime

    for project_name, needs_setup in projects_needing_setup.items():
        if needs_setup:
            logger.info(f"Queuing dev_environment_setup task for {project_name}")

            task = Task(
                id=f"dev_env_setup_{project_name}_{int(datetime.now().timestamp())}",
                agent="dev_environment_setup",
                project=project_name,
                priority=TaskPriority.HIGH,  # High priority for initial setup
                context={
                    'issue': {
                        'title': f'Development environment setup for {project_name}',
                        'body': 'Automated setup of development environment, Dockerfile.agent generation, and validation',
                        'number': 0  # No GitHub issue for automated setup
                    },
                    'issue_number': 0,
                    'board': 'system',  # System-initiated task
                    'repository': project_name,
                    'automated_setup': True,  # Flag to indicate this is automated setup
                    'use_docker': False  # Run locally in orchestrator environment to access Docker for building project images
                },
                created_at=datetime.now().isoformat()
            )

            task_queue.enqueue(task)
            logger.info(f"Queued dev_environment_setup task: {task.id}")

    # Reconcile all projects on startup
    projects = config_manager.list_projects()
    for project_name in projects:
        failure_count = 0
        # Always verify boards exist in GitHub, even if config hasn't changed
        # This handles the case where the orchestrator is moved to a new system
        needs_reconcile = github_state_manager.needs_reconciliation(project_name)

        if needs_reconcile:
            logger.info(f"Reconciling project configuration: {project_name} (config changed)")
        else:
            logger.info(f"Verifying project boards exist in GitHub: {project_name}")

        # Always run reconciliation - it will discover existing boards if they exist
        success = await github_project_manager.reconcile_project(project_name)
        if not success:
            logger.log_error(f"Failed to reconcile project '{project_name}' - GitHub project management is not working")
            failure_count += 1

    # If all of the projects failed to reconcile, exit
    if failure_count == len(projects) and failure_count > 0:
        logger.log_error("All projects failed to reconcile - GitHub project management is not working")
        exit(1)

    # Start project monitor in background
    project_monitor = ProjectMonitor(task_queue, config_manager)
    monitor_thread = threading.Thread(
        target=project_monitor.monitor_projects,
        daemon=True
    )
    monitor_thread.start()

    # Start scheduled tasks service for periodic maintenance
    scheduler = get_scheduled_tasks_service()
    scheduler.start()
    logger.info("Scheduled tasks service started")

    # Main orchestration loop
    logger.debug("Entering main orchestration loop")

    # Health check state and retry tracking
    consecutive_health_failures = 0
    max_consecutive_failures = 10  # Exit only after 10 consecutive failures (transient issues should recover)
    health_check_backoff = 10  # Start with 10 second backoff
    max_backoff = 300  # Max 5 minutes between health checks
    # NOTE: health_monitor.py writes to Redis with 10-minute TTL to ensure key doesn't expire
    # even when health checks are running at max_backoff (300s) with execution delays
    last_health_check = 0

    while True:
        try:
            # Periodic health check with exponential backoff on failures
            current_time = time.time()
            if current_time - last_health_check >= health_check_backoff:
                logger.debug("Running health check")
                health = await health_monitor.check_health()
                last_health_check = current_time
                logger.debug(f"Health check complete: healthy={health['healthy']}")

                if not health['healthy']:
                    consecutive_health_failures += 1
                    logger.log_warning(f"System health check failed (attempt {consecutive_health_failures}/{max_consecutive_failures}): {health}")

                    # Check specifically for GitHub project management failures
                    github_check = health['checks'].get('github', {})
                    if not github_check.get('healthy', False):
                        error_msg = github_check.get('error', 'Unknown error')

                        # Check if this is a transient network error
                        is_transient = any(keyword in error_msg.lower() for keyword in [
                            'eof', 'timeout', 'connection', 'network', 'temporary'
                        ])

                        if is_transient:
                            logger.log_warning(f"Transient GitHub connectivity issue detected: {error_msg}")
                            logger.log_warning(f"Will retry with {health_check_backoff}s backoff")
                            # Exponential backoff for transient errors
                            health_check_backoff = min(health_check_backoff * 2, max_backoff)
                        else:
                            logger.log_error(f"GitHub connectivity/permissions failed: {error_msg}")
                            if github_check.get('critical'):
                                logger.log_error(f"Critical issue: {github_check['critical']}")

                            # Only exit if we've failed consistently (not a transient issue)
                            if consecutive_health_failures >= max_consecutive_failures:
                                logger.log_error(f"GitHub access has failed {max_consecutive_failures} consecutive times")
                                logger.log_error("Exiting due to persistent GitHub connectivity failure")
                                exit(1)
                    else:
                        # Non-GitHub health issues can continue with warnings
                        logger.log_warning("Non-critical health check failures detected - continuing with degraded functionality")
                else:
                    # Health check passed - reset counters and backoff
                    if consecutive_health_failures > 0:
                        logger.info(f"Health check recovered after {consecutive_health_failures} failures")
                    consecutive_health_failures = 0
                    health_check_backoff = 10  # Reset to 10 seconds

            # Process next task
            logger.debug("Checking task queue for new tasks")
            task = task_queue.dequeue()
            logger.debug(f"Dequeued task: {task.id if task else 'None'}")
            if task:
                logger.log_agent_start(task.agent, task.id, task.context)
                
                start_time = time.time()
                try:
                    result = await process_task_integrated(task, state_manager, logger)
                    duration = time.time() - start_time
                    
                    logger.log_agent_complete(
                        task.agent, 
                        task.id, 
                        duration, 
                        result
                    )
                    metrics.record_task_complete(
                        task.agent,
                        duration,
                        success=True
                    )

                    # Record pipeline-specific metrics
                    if hasattr(result, 'get') and result.get('quality_metrics'):
                        quality_scores = result['quality_metrics']
                        for metric_name, score in quality_scores.items():
                            # Record quality metrics if the method exists
                            if hasattr(metrics, 'record_quality_metric'):
                                metrics.record_quality_metric(task.agent, metric_name, score)
                    
                except Exception as e:
                    logger.log_error(f"Task failed: {e}")
                    metrics.record_task_complete(
                        task.agent,
                        time.time() - start_time,
                        success=False
                    )
            
            await asyncio.sleep(5)
            
        except KeyboardInterrupt:
            logger.info("Shutting down orchestrator")
            break
        except Exception as e:
            logger.log_error(f"Orchestrator error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
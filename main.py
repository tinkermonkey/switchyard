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
from state_management.git_state import GitStateManager
from task_queue.task_manager import TaskQueue
from claude.session_manager import ClaudeSessionManager
from monitoring.health_monitor import HealthMonitor
from services.github_project_manager import GitHubProjectManager
from services.project_monitor import ProjectMonitor
from services.project_workspace import workspace_manager
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

    metrics = MetricsCollector(port=env_config.metrics_port)

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
        if github_state_manager.needs_reconciliation(project_name):
            logger.info(f"Reconciling project configuration: {project_name}")
            success = await github_project_manager.reconcile_project(project_name)
            if not success:
                logger.log_error(f"Failed to reconcile project '{project_name}' - GitHub project management is not working")
                failure_count += 1
        else:
            logger.info(f"Project '{project_name}' is already synchronized")

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
    
    # Main orchestration loop
    logger.info("Entering main orchestration loop")

    while True:
        try:
            logger.debug("Loop iteration starting - checking health")
            # Check health - GitHub failures are FATAL
            health = await health_monitor.check_health()
            logger.debug(f"Health check complete: healthy={health['healthy']}")
            if not health['healthy']:
                logger.log_error(f"System health check failed: {health}")

                # Check specifically for GitHub project management failures
                github_check = health['checks'].get('github', {})
                if not github_check.get('healthy', False):
                    logger.log_error("GitHub connectivity/permissions failed")
                    if github_check.get('critical'):
                        logger.log_error(f"Fatal issue: {github_check['critical']}")
                    if 'projects_access' in github_check and github_check['projects_access'] == 'failed':
                        logger.log_error("GitHub Projects v2 access is required")
                        exit(1)
                    else:
                        logger.log_error("GitHub access is required for core functionality")
                        exit(1)

                # Non-GitHub health issues can continue with warnings
                logger.log_warning("Non-critical health check failures detected - monitoring for issues")

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
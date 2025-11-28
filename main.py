import asyncio
import threading
import time
import signal
import os
import logging
from pathlib import Path
from elasticsearch import Elasticsearch
from elasticsearch.exceptions import ConnectionError as ESConnectionError

from flask.cli import F
from monitoring.timestamp_utils import utc_now, utc_isoformat
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


def wait_for_elasticsearch(max_retries=30, retry_delay=2):
    """
    Wait for Elasticsearch to be ready before proceeding with startup.
    
    This prevents cleanup operations from failing due to Elasticsearch 
    not being ready yet when the orchestrator starts.
    
    Args:
        max_retries: Maximum number of connection attempts (default: 30)
        retry_delay: Delay in seconds between retries (default: 2)
        
    Returns:
        True if Elasticsearch is ready, False if max retries exceeded
    """
    logger = logging.getLogger("orchestrator")
    es_host = os.environ.get('ELASTICSEARCH_HOST', 'elasticsearch:9200')
    
    logger.info(f"Waiting for Elasticsearch at {es_host} to be ready...")
    
    for attempt in range(1, max_retries + 1):
        try:
            es = Elasticsearch([f"http://{es_host}"])
            
            # Try to ping Elasticsearch
            if es.ping():
                # Also verify we can actually query (ensures indices are ready)
                es.cluster.health(wait_for_status='yellow', timeout='5s')
                logger.info(f"Elasticsearch is ready (attempt {attempt}/{max_retries})")
                return True
            else:
                logger.debug(f"Elasticsearch ping failed (attempt {attempt}/{max_retries})")
                
        except ESConnectionError as e:
            logger.debug(f"Elasticsearch connection refused (attempt {attempt}/{max_retries}): {e}")
        except Exception as e:
            logger.warning(f"Elasticsearch health check failed (attempt {attempt}/{max_retries}): {e}")
        
        if attempt < max_retries:
            time.sleep(retry_delay)
    
    logger.error(f"Elasticsearch did not become ready after {max_retries} attempts")
    return False


def setup_zombie_process_reaper():
    """
    Setup SIGCHLD signal handler to automatically reap zombie child processes.
    
    This is critical for long-running processes that spawn many subprocesses
    (especially from daemon threads), as Python's automatic subprocess cleanup
    doesn't always work correctly when subprocess.run() is called from threads
    with custom asyncio event loops.
    
    Without this, git processes and other subprocess calls accumulate as zombies,
    eventually causing resource exhaustion and mysterious hanging behavior.
    """
    def sigchld_handler(signum, frame):
        """Reap all zombie child processes"""
        # Use WNOHANG to avoid blocking
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
                if pid == 0:
                    # No more zombie processes
                    break
            except ChildProcessError:
                # No child processes
                break
            except Exception:
                # Ignore other errors in signal handler
                break
    
    # Register the signal handler
    signal.signal(signal.SIGCHLD, sigchld_handler)

async def main():
    # Setup zombie process reaper FIRST before any other initialization
    # This prevents accumulation of defunct child processes from subprocess calls
    setup_zombie_process_reaper()
    
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
    root_logger.info("Zombie process reaper enabled (SIGCHLD handler registered)")

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

    # Wait for Elasticsearch to be ready before cleanup operations
    # This prevents cleanup failures when Elasticsearch isn't ready yet
    elasticsearch_ready = wait_for_elasticsearch(max_retries=30, retry_delay=2)
    if not elasticsearch_ready:
        logger.warning(
            "Elasticsearch is not ready - some cleanup operations may be skipped. "
            "Pipeline runs and events will not be cleaned up properly."
        )

    # NEW: Recover or cleanup running agent containers first
    logger.info("Recovering or cleaning up running agent containers")
    from services.agent_container_recovery import get_agent_container_recovery
    container_recovery = get_agent_container_recovery()
    recovered, killed, errors = container_recovery.recover_or_cleanup_containers()
    logger.info(f"Container recovery: {recovered} recovered, {killed} killed, {errors} errors")

    # NEW: Recover or cleanup running repair cycle containers
    logger.info("Recovering or cleaning up running repair cycle containers")
    rc_recovered, rc_killed, rc_errors = container_recovery.recover_or_cleanup_repair_cycle_containers()
    logger.info(f"Repair cycle container recovery: {rc_recovered} recovered, {rc_killed} killed, {rc_errors} errors")

    # Clean up orphaned Redis keys from agent containers that completed after orchestrator restart
    logger.info("Cleaning up orphaned agent container tracking keys")
    from claude.docker_runner import DockerAgentRunner
    DockerAgentRunner.cleanup_orphaned_redis_keys()
    logger.info("Orphaned Redis key cleanup complete")

    # Clean up stuck in_progress execution states from interrupted agent runs
    # This now runs AFTER container recovery, so it won't clean up states for recovered containers
    logger.info("Cleaning up stuck in_progress execution states")
    from services.work_execution_state import work_execution_tracker
    work_execution_tracker.cleanup_stuck_in_progress_states()
    logger.info("Execution state cleanup complete")

    # NEW: Recover pipeline locks on startup
    # Check for stale locks and release them, then process waiting issues
    logger.info("Recovering pipeline locks from orchestrator restart")
    from services.pipeline_lock_manager import get_pipeline_lock_manager
    from services.pipeline_queue_manager import get_pipeline_queue_manager
    from services.project_monitor import ProjectMonitor

    lock_manager = get_pipeline_lock_manager()
    locks_recovered = 0
    locks_released = 0

    # Create a temporary ProjectMonitor instance for checking issue columns
    temp_monitor = ProjectMonitor(task_queue, config_manager)

    for project_name in config_manager.list_visible_projects():
        try:
            project_config = config_manager.get_project_config(project_name)
            for pipeline in project_config.pipelines:
                if not pipeline.active:
                    continue

                lock = lock_manager.get_lock(project_name, pipeline.board_name)

                if lock and lock.lock_status == 'locked':
                    logger.info(
                        f"Found pipeline lock for {project_name}/{pipeline.board_name} "
                        f"held by issue #{lock.locked_by_issue}"
                    )

                    # Check if the lock holder is still in an active column
                    # or has reached an exit column
                    lock_holder_column = temp_monitor.get_issue_column_sync(
                        project_name, pipeline.board_name, lock.locked_by_issue
                    )

                    workflow_template = config_manager.get_workflow_template(pipeline.workflow)
                    exit_columns = getattr(workflow_template, 'pipeline_exit_columns', [])

                    # Check if the lock holder's column has an agent
                    column_has_agent = False
                    if lock_holder_column:
                        for col in workflow_template.columns:
                            if col.name == lock_holder_column:
                                column_has_agent = col.agent and col.agent != 'null'
                                break

                    # Release lock if:
                    # 1. Issue is removed from board (no column)
                    # 2. Issue is in an exit column
                    # 3. Issue is in a column with no agent (like Backlog)
                    should_release = (
                        not lock_holder_column or
                        lock_holder_column in exit_columns or
                        not column_has_agent
                    )

                    if should_release:
                        # Lock holder is no longer active or in exit column - release lock
                        reason = (
                            'removed' if not lock_holder_column
                            else 'no agent' if not column_has_agent
                            else 'exit column'
                        )
                        logger.info(
                            f"Releasing orphaned lock for {project_name}/{pipeline.board_name} "
                            f"(issue #{lock.locked_by_issue} in column '{lock_holder_column or 'removed'}', reason: {reason})"
                        )

                        lock_manager.release_lock(
                            project_name, pipeline.board_name, lock.locked_by_issue
                        )
                        locks_released += 1

                        # Remove issue from queue if present
                        # (it will be re-added if moved back to trigger column)
                        pipeline_queue = get_pipeline_queue_manager(
                            project_name, pipeline.board_name
                        )
                        if pipeline_queue.is_issue_in_queue(lock.locked_by_issue):
                            pipeline_queue.remove_issue_from_queue(lock.locked_by_issue)

                        # NOTE: We don't process the next waiting issue here
                        # The regular monitoring loop will pick it up
                    else:
                        # Lock holder is still active - keep the lock
                        logger.info(
                            f"Keeping lock for {project_name}/{pipeline.board_name} "
                            f"(issue #{lock.locked_by_issue} still in column '{lock_holder_column}')"
                        )
                        locks_recovered += 1

                        # CRITICAL: Check if repair cycle container is already running
                        # Agent container recovery may reconnect to it, so don't re-trigger
                        should_retrigger = True

                        # Check if there's a repair cycle container for this issue
                        redis_key = f"repair_cycle:container:{project_name}:{lock.locked_by_issue}"
                        container_name = None
                        if task_queue.redis_client:
                            container_name = task_queue.redis_client.get(redis_key)

                            if container_name:
                                # Check if container actually exists
                                import subprocess
                                try:
                                    result = subprocess.run(
                                        ['docker', 'inspect', container_name.decode() if isinstance(container_name, bytes) else container_name],
                                        capture_output=True,
                                        timeout=5
                                    )
                                    if result.returncode == 0:
                                        logger.info(
                                            f"Repair cycle container {container_name} already exists for issue #{lock.locked_by_issue} "
                                            f"- skipping re-trigger (agent container recovery will reconnect)"
                                        )
                                        should_retrigger = False
                                except Exception as e:
                                    logger.warning(f"Error checking container {container_name}: {e}")

                        # Re-trigger agent only if no container is running
                        # (May have been interrupted mid-execution during restart)
                        if should_retrigger:
                            try:
                                logger.info(
                                    f"Re-triggering agent for recovered lock holder "
                                    f"issue #{lock.locked_by_issue} in column '{lock_holder_column}'"
                                )
                                temp_monitor.trigger_agent_for_status(
                                    project_name=project_name,
                                    board_name=pipeline.board_name,
                                    issue_number=lock.locked_by_issue,
                                    status=lock_holder_column,
                                    repository=project_config.github['repo']
                                )
                            except Exception as trigger_error:
                                logger.error(
                                    f"Failed to re-trigger agent for issue #{lock.locked_by_issue}: {trigger_error}"
                                )
                                import traceback
                                logger.error(traceback.format_exc())

        except Exception as e:
            logger.error(f"Error recovering locks for project {project_name}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    logger.info(
        f"Pipeline lock recovery complete: {locks_recovered} locks kept, "
        f"{locks_released} locks released"
    )

    # NOTE: Stall detection moved to after startup rescan completes
    # This prevents race condition where review cycles haven't queued tasks yet

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

    for project_name, needs_setup in projects_needing_setup.items():
        if needs_setup:
            logger.info(f"Queuing dev_environment_setup task for {project_name}")

            task = Task(
                id=f"dev_env_setup_{project_name}_{int(utc_now().timestamp())}",
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
                created_at=utc_isoformat()
            )

            task_queue.enqueue(task)
            logger.info(f"Queued dev_environment_setup task: {task.id}")

    # Reconcile all visible (non-hidden) projects on startup
    # Hidden projects (like test-project) are excluded from normal operations
    projects = config_manager.list_visible_projects()
    for project_name in projects:
        failure_count = 0
        # Always verify boards exist in GitHub, even if config hasn't changed
        # This handles the case where the orchestrator is moved to a new system
        needs_reconcile = github_state_manager.needs_reconciliation(project_name)

        if needs_reconcile:
            logger.info(f"Reconciling project configuration: {project_name} (config changed)")
        else:
            logger.info(f"Verifying project boards exist in GitHub: {project_name}")

        # Check GitHub circuit breaker before reconciliation
        from services.github_owner_utils import _github_circuit_breaker
        from services.circuit_breaker import CircuitState

        if _github_circuit_breaker.state == CircuitState.OPEN:
            logger.log_warning(
                f"Skipping reconciliation for {project_name} - GitHub circuit breaker is open "
                f"(will retry when circuit recovers)"
            )
            failure_count += 1
            continue

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

    # CRITICAL: Wait for project monitor to complete startup rescan before starting workers
    # This ensures repair cycles are prioritized and queued tasks don't execute prematurely
    logger.info("Waiting for project monitor to complete startup rescan...")
    rescan_timeout = 300  # 5 minutes should be plenty
    if project_monitor.rescan_complete.wait(timeout=rescan_timeout):
        logger.info("Startup rescan complete, proceeding with worker pool initialization")
    else:
        logger.log_warning(f"Startup rescan did not complete within {rescan_timeout}s - proceeding anyway")

    # Now that startup rescan is complete and review cycles have queued tasks,
    # it's safe to clean up stale pipeline runs and agent events
    if elasticsearch_ready:
        logger.info("Cleaning up stale active pipeline runs")
        from services.pipeline_run import get_pipeline_run_manager
        pipeline_run_manager = get_pipeline_run_manager()
        pipeline_run_manager.cleanup_stale_active_runs_on_startup()
        logger.info("Pipeline run cleanup complete")
    else:
        logger.warning("Skipping pipeline run cleanup - Elasticsearch not available")

    # Clean up stale agent events from Redis stream (requires Elasticsearch)
    if elasticsearch_ready:
        logger.info("Cleaning up stale agent events from Redis stream")
        from monitoring.observability import get_observability_manager
        observability = get_observability_manager()
        observability.cleanup_stale_agent_events_on_startup()
        logger.info("Stale agent event cleanup complete")
    else:
        logger.warning("Skipping agent event cleanup - Elasticsearch not available")

    # Initialize worker pool for parallel task processing
    # orchestrator_workers controls concurrency:
    #   1 = single-threaded (backward compatible, default)
    #   2-8 = multi-threaded (recommended for multi-project deployments)
    num_workers = env_config.orchestrator_workers
    logger.info(f"Configuring task execution with {num_workers} worker(s)")

    worker_pool = None
    if num_workers > 1:
        # Multi-threaded execution via worker pool
        from services.worker_pool import WorkerPoolManager
        worker_pool = WorkerPoolManager(num_workers, task_queue, metrics, logger)
        worker_pool.start()
        logger.info(f"Worker pool started with {num_workers} workers (multi-threaded mode)")
    else:
        logger.info("Single-threaded mode enabled (orchestrator_workers=1)")

    # Main orchestration loop
    logger.debug("Entering main orchestration loop")

    # Health check state and retry tracking
    consecutive_health_failures = 0
    max_consecutive_failures = 15  # Exit only after 15 consecutive failures (increased for resilience)
    health_check_backoff = 10  # Start with 10 second backoff
    max_backoff = 300  # Max 5 minutes between health checks
    # NOTE: health_monitor.py writes to Redis with 10-minute TTL to ensure key doesn't expire
    # even when health checks are running at max_backoff (300s) with execution delays
    last_health_check = 0

    try:
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

                # Worker pool mode vs single-threaded mode
                if worker_pool:
                    # Multi-threaded: Workers handle tasks, main loop just monitors
                    active_tasks = worker_pool.get_active_tasks()
                    if active_tasks:
                        logger.debug(f"Active tasks: {len(active_tasks)} across {num_workers} workers")

                    # Sleep longer since workers are doing the work
                    await asyncio.sleep(10)
                else:
                    # Single-threaded: Main loop processes tasks (original behavior)
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

    finally:
        # Cleanup: Stop worker pool if running
        if worker_pool:
            logger.info("Stopping worker pool...")
            worker_pool.stop()

if __name__ == "__main__":
    asyncio.run(main())
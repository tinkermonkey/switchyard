"""
Worker Pool Manager for Multi-Threaded Task Processing

Manages a pool of worker threads that process tasks from the Redis queue in parallel.
Each worker can execute tasks for any project, enabling concurrent execution across
different projects while maintaining per-project pipeline locks.
"""

import asyncio
import threading
import logging
import time
from typing import Optional, List, Dict, Any
from pathlib import Path

from task_queue.task_manager import TaskQueue, Task
from agents.orchestrator_integration import process_task_integrated
from state_management.manager import StateManager
from monitoring.metrics import MetricsCollector

logger = logging.getLogger(__name__)


class TaskWorker:
    """
    Worker thread that processes tasks from the shared Redis queue.

    Each worker runs independently and dequeues tasks in priority order.
    Workers are thread-safe and can run concurrently without conflicts.
    """

    def __init__(
        self,
        worker_id: int,
        task_queue: TaskQueue,
        metrics: MetricsCollector,
        orchestrator_logger
    ):
        self.worker_id = worker_id
        self.task_queue = task_queue
        self.metrics = metrics
        self.logger = orchestrator_logger
        self.running = True
        self.current_task: Optional[Task] = None
        self.tasks_processed = 0
        self.tasks_failed = 0

        # Each worker gets its own StateManager instance
        # (though state files use file locks for safety)
        self.state_manager = StateManager(
            Path(f"orchestrator_data/state")
        )

    async def run(self):
        """Main worker loop - continuously processes tasks from queue"""
        logger.info(f"Worker {self.worker_id} started")

        while self.running:
            try:
                # Atomic dequeue from Redis (thread-safe)
                task = self.task_queue.dequeue()

                if task:
                    self.current_task = task
                    logger.info(
                        f"[Worker {self.worker_id}] Processing task {task.id} "
                        f"(agent: {task.agent}, project: {task.project})"
                    )

                    # Log agent start
                    self.logger.log_agent_start(task.agent, task.id, task.context)

                    # Execute task
                    start_time = time.time()
                    max_retries = 3
                    attempt = 0
                    
                    try:
                        while True:
                            attempt += 1
                            try:
                                result = await process_task_integrated(
                                    task, self.state_manager, self.logger
                                )
                                duration = time.time() - start_time

                                # Log completion
                                self.logger.log_agent_complete(
                                    task.agent,
                                    task.id,
                                    duration,
                                    result
                                )

                                # Record metrics
                                self.metrics.record_task_complete(
                                    task.agent,
                                    duration,
                                    success=True
                                )

                                # Record quality metrics if present
                                if hasattr(result, 'get') and result.get('quality_metrics'):
                                    quality_scores = result['quality_metrics']
                                    for metric_name, score in quality_scores.items():
                                        if hasattr(self.metrics, 'record_quality_metric'):
                                            self.metrics.record_quality_metric(
                                                task.agent, metric_name, score
                                            )

                                self.tasks_processed += 1
                                logger.info(
                                    f"[Worker {self.worker_id}] Completed task {task.id} "
                                    f"in {duration:.1f}s"
                                )
                                break # Success, exit retry loop

                            except Exception as e:
                                # Check if we should retry
                                if attempt <= max_retries:
                                    logger.warning(
                                        f"[Worker {self.worker_id}] Task {task.id} failed (attempt {attempt}/{max_retries}): {e}. "
                                        f"Retrying in 5 seconds..."
                                    )
                                    await asyncio.sleep(5)
                                    continue
                                
                                # Final failure handling
                                duration = time.time() - start_time
                                self.logger.log_error(f"[Worker {self.worker_id}] Task {task.id} failed: {e}")

                                # Record failure metrics
                                self.metrics.record_task_complete(
                                    task.agent,
                                    duration,
                                    success=False
                                )

                                self.tasks_failed += 1
                                logger.error(
                                    f"[Worker {self.worker_id}] Task {task.id} failed after {duration:.1f}s: {e}"
                                )
                                break # Failure, exit retry loop

                    finally:
                        self.current_task = None
                else:
                    # No task available, sleep briefly
                    await asyncio.sleep(2)

            except Exception as e:
                logger.error(f"[Worker {self.worker_id}] Unexpected error: {e}")
                import traceback
                logger.error(traceback.format_exc())

                # Clear current task on error
                self.current_task = None

                # Backoff on error
                await asyncio.sleep(5)

        logger.info(
            f"Worker {self.worker_id} stopped "
            f"(processed: {self.tasks_processed}, failed: {self.tasks_failed})"
        )

    def stop(self):
        """Signal worker to stop"""
        self.running = False

    def get_status(self) -> Dict[str, Any]:
        """Get worker status for monitoring"""
        return {
            'worker_id': self.worker_id,
            'running': self.running,
            'current_task': {
                'id': self.current_task.id,
                'agent': self.current_task.agent,
                'project': self.current_task.project
            } if self.current_task else None,
            'tasks_processed': self.tasks_processed,
            'tasks_failed': self.tasks_failed
        }


class WorkerPoolManager:
    """
    Manages a pool of worker threads for concurrent task processing.

    The worker pool enables parallel execution of tasks across different projects
    while maintaining thread safety through Redis atomic operations and file locks.
    """

    def __init__(
        self,
        num_workers: int,
        task_queue: TaskQueue,
        metrics: MetricsCollector,
        orchestrator_logger
    ):
        self.num_workers = num_workers
        self.task_queue = task_queue
        self.metrics = metrics
        self.logger = orchestrator_logger
        self.workers: List[TaskWorker] = []
        self.worker_threads: List[threading.Thread] = []
        self.running = False

    def start(self):
        """Start all worker threads"""
        if self.running:
            logger.warning("Worker pool already running")
            return

        self.running = True
        logger.info(f"Starting worker pool with {self.num_workers} workers")

        for i in range(self.num_workers):
            worker = TaskWorker(
                worker_id=i,
                task_queue=self.task_queue,
                metrics=self.metrics,
                orchestrator_logger=self.logger
            )
            self.workers.append(worker)

            # Create thread for worker
            thread = threading.Thread(
                target=self._run_worker_loop,
                args=(worker,),
                daemon=True,
                name=f"Worker-{i}"
            )
            thread.start()
            self.worker_threads.append(thread)

        logger.info(f"Worker pool started: {self.num_workers} workers active")

    def _run_worker_loop(self, worker: TaskWorker):
        """Run worker in its own async event loop"""
        # Each worker thread gets its own event loop
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)

        try:
            loop.run_until_complete(worker.run())
        except Exception as e:
            logger.error(f"Worker {worker.worker_id} crashed: {e}")
            import traceback
            logger.error(traceback.format_exc())
        finally:
            loop.close()

    def stop(self):
        """Stop all worker threads gracefully"""
        if not self.running:
            return

        logger.info("Stopping worker pool...")
        self.running = False

        # Signal all workers to stop
        for worker in self.workers:
            worker.stop()

        # Wait for threads to finish (with timeout)
        for i, thread in enumerate(self.worker_threads):
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning(f"Worker {i} did not stop cleanly")

        logger.info("Worker pool stopped")

    def get_active_tasks(self) -> List[Dict[str, Any]]:
        """Get list of currently executing tasks across all workers"""
        active = []
        for worker in self.workers:
            if worker.current_task:
                active.append({
                    'worker_id': worker.worker_id,
                    'task_id': worker.current_task.id,
                    'agent': worker.current_task.agent,
                    'project': worker.current_task.project
                })
        return active

    def get_worker_stats(self) -> Dict[str, Any]:
        """Get statistics about worker pool"""
        return {
            'num_workers': self.num_workers,
            'running': self.running,
            'workers': [w.get_status() for w in self.workers],
            'total_processed': sum(w.tasks_processed for w in self.workers),
            'total_failed': sum(w.tasks_failed for w in self.workers)
        }

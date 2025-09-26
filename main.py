import asyncio
import threading
import time
from pathlib import Path
from config.environment import Environment
from monitoring.logging import OrchestratorLogger
from monitoring.metrics import MetricsCollector
from state_management.manager import StateManager
from state_management.git_state import GitStateManager
from state_management.project_state import ProjectStateManager
from task_queue.task_manager import TaskQueue
from claude.session_manager import ClaudeSessionManager
from monitoring.health_monitor import HealthMonitor
from pipeline.orchestrator import SequentialPipeline
from pipeline.factory import create_default_pipeline
from services.simple_webhook_server import start_webhook_server
from services.project_manager import ProjectManager
from agents.agent_stages import process_task_integrated

async def main():
    # Load configuration
    config = Environment()
    
    # Initialize components
    logger = OrchestratorLogger("orchestrator")
    metrics = MetricsCollector(port=config.metrics_port)
    
    # Initialize state management
    state_manager = StateManager(Path("orchestrator_data/state"))
    git_state = GitStateManager(Path("orchestrator_data/state"))
    
    # Initialize task queue
    task_queue = TaskQueue()
    
    # Initialize session manager
    session_manager = ClaudeSessionManager()
    
    # Initialize health monitor
    health_monitor = HealthMonitor(orchestrator=None)
    
    # Initialize project manager
    project_manager = ProjectManager()

    # Initialize pipeline with MCP configuration
    pipeline = create_default_pipeline()
    
    # Start webhook server in background
    webhook_thread = threading.Thread(
        target=start_webhook_server,
        args=(config.webhook_port,)
    )
    webhook_thread.start()
    
    # Main orchestration loop
    logger.log_info("Orchestrator started")
    
    while True:
        try:
            # Check health
            health = await health_monitor.check_health()
            if not health['healthy']:
                logger.log_warning(f"Health check failed: {health}")
            
            # Process next task
            task = task_queue.dequeue()
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
            
            await asyncio.sleep(1)
            
        except KeyboardInterrupt:
            logger.log_info("Shutting down orchestrator")
            break
        except Exception as e:
            logger.log_error(f"Orchestrator error: {e}")
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
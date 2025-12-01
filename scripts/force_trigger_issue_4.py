import sys
import os
import logging

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Add current directory to path
sys.path.append(os.getcwd())

from config.manager import ConfigManager
from services.project_monitor import ProjectMonitor
from task_queue.task_manager import TaskQueue
from services.pipeline_run import PipelineRunManager

def main():
    try:
        logger.info("Initializing services...")
        config_manager = ConfigManager()
        task_queue = TaskQueue()
        pipeline_run_manager = PipelineRunManager(task_queue)
        
        monitor = ProjectMonitor(task_queue, config_manager)
        
        project_name = "documentation_robotics_viewer"
        board_name = "Planning & Design"
        issue_number = 4
        status = "Work Breakdown"
        repository = "documentation_robotics_viewer" 
        
        logger.info(f"Triggering agent for {project_name} issue #{issue_number}...")
        
        # We need to make sure the project config is loaded so it knows the org
        project_config = config_manager.get_project_config(project_name)
        if not project_config:
            logger.error(f"Project config not found for {project_name}")
            return

        logger.info(f"Project Org: {project_config.github['org']}")
        
        monitor.trigger_agent_for_status(
            project_name=project_name,
            board_name=board_name,
            issue_number=issue_number,
            status=status,
            repository=repository
        )
        
        logger.info("Trigger completed. Waiting for execution...")
        import time
        time.sleep(600)
        
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)

if __name__ == "__main__":
    main()

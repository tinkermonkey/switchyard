import sys
import os
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

sys.path.append(os.getcwd())

from services.pipeline_lock_manager import PipelineLockManager
from task_queue.task_manager import TaskQueue

def main():
    try:
        task_queue = TaskQueue(use_redis=True)
        lock_manager = PipelineLockManager(redis_client=task_queue.redis_client)
        
        project_name = "documentation_robotics_viewer"
        board_name = "Planning & Design"
        issue_number = 7
        
        logger.info(f"Releasing lock for {project_name}/{board_name} held by issue #{issue_number}...")
        
        lock_manager.release_lock(project_name, board_name, issue_number)
        
        logger.info("Lock released.")
    except Exception as e:
        logger.error(f"Error: {e}", exc_info=True)

if __name__ == "__main__":
    main()

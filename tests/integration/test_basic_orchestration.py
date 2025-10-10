import asyncio
import pytest
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from datetime import datetime

import logging

logger = logging.getLogger(__name__)
@pytest.mark.asyncio
async def test_basic_orchestration():
    """Test basic orchestration with Business Analyst"""

    task_queue = TaskQueue()

    # Create test task
    test_task = Task(
        id="test_ba_001",
        agent="business_analyst",
        project="test_project",
        priority=TaskPriority.HIGH,
        context={
            "issue": {
                "title": "User Login Feature",
                "body": "As a user, I need to be able to log into the system",
                "labels": ["feature", "authentication"]
            }
        },
        created_at=datetime.now().isoformat()
    )

    # Enqueue task
    task_queue.enqueue(test_task)
    logger.info(" Task queued successfully")

    # Test dequeue
    retrieved_task = task_queue.dequeue()
    assert retrieved_task.id == test_task.id
    logger.info(" Task dequeue working")

    logger.info("Integration test passed!")

if __name__ == "__main__":
    asyncio.run(test_basic_orchestration())
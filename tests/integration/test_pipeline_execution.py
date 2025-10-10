import asyncio
import sys
import pytest
from datetime import datetime
from task_queue.task_manager import Task, TaskPriority
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger
from agents.orchestrator_integration import process_task_integrated

import logging

logger = logging.getLogger(__name__)

@pytest.mark.asyncio
async def test_pipeline_execution():
    """Test complete pipeline execution with Business Analyst"""

    # Setup
    state_manager = StateManager()
    logger = OrchestratorLogger("test")

    logger.info(" Testing Pipeline Execution...")

    # Create test task
    test_task = Task(
        id="integration_test_001",
        agent="business_analyst",
        project="test_project",
        priority=TaskPriority.HIGH,
        context={
            "issue": {
                "title": "User Authentication System",
                "body": "Need secure login/logout functionality with password reset",
                "labels": ["security", "authentication", "feature"]
            }
        },
        created_at=datetime.now().isoformat()
    )

    # Execute
    try:
        result = await process_task_integrated(test_task, state_manager, logger)
        logger.info(" Pipeline integration test passed")
        logger.info(f"📄 Result keys: {list(result.keys())}")

        # Validate result structure
        assert 'pipeline_id' in result
        assert 'task_id' in result
        assert result['task_id'] == test_task.id

        logger.info(" Result structure validated")
        return True
    except Exception as e:
        logger.info(f" Integration test failed: {e}")
        return False

if __name__ == "__main__":
    success = asyncio.run(test_pipeline_execution())
    sys.exit(0 if success else 1)
import asyncio
import sys
from datetime import datetime
from task_queue.task_manager import Task, TaskPriority
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger

async def test_pipeline_integration():
    """Test pipeline execution with real Business Analyst"""

    # Setup
    state_manager = StateManager()
    logger = OrchestratorLogger("test")

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

    # Import the process_task function
    from agents.orchestrator_integration import process_task_integrated

    # Execute
    try:
        result = await process_task_integrated(test_task, state_manager, logger)
        print("✅ Pipeline integration test passed")
        print(f"📄 Result keys: {list(result.keys())}")
        return True
    except Exception as e:
        print(f"❌ Integration test failed: {e}")
        import traceback
        traceback.print_exc()
        return False

if __name__ == "__main__":
    asyncio.run(test_pipeline_integration())
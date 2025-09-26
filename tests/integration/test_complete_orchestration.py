import asyncio
import time
from datetime import datetime
from task_queue.task_manager import TaskQueue, Task, TaskPriority

async def test_complete_orchestration():
    """Test complete flow: enqueue -> pipeline -> state -> handoff -> logging"""

    print("📋 Starting complete orchestration test...")

    # 1. Setup task queue
    task_queue = TaskQueue()

    # 2. Create test task
    test_task = Task(
        id="complete_test_001",
        agent="business_analyst",
        project="end_to_end_test",
        priority=TaskPriority.HIGH,
        context={
            "issue": {
                "title": "E-commerce Checkout Flow",
                "body": "Users need ability to add items to cart, review order, and complete purchase with payment processing",
                "labels": ["feature", "e-commerce", "payment", "high-priority"]
            }
        },
        created_at=datetime.now().isoformat()
    )

    # 3. Enqueue task
    task_queue.enqueue(test_task)
    print("✅ Task enqueued")

    # 4. Verify task can be dequeued
    dequeued_task = task_queue.dequeue()
    assert dequeued_task.id == test_task.id
    print("✅ Task dequeued successfully")

    # 5. Process through pipeline (simulating main.py behavior)
    from agents.agent_stages import process_task_integrated
    from state_management.manager import StateManager
    from monitoring.logging import OrchestratorLogger

    state_manager = StateManager()
    logger = OrchestratorLogger("complete_test")

    start_time = time.time()
    result = await process_task_integrated(dequeued_task, state_manager, logger)
    duration = time.time() - start_time

    print(f"✅ Pipeline execution completed in {duration:.2f}s")

    # 6. Verify all expected artifacts
    from pathlib import Path

    # Check state files
    checkpoints = list(Path("orchestrator_data/state/checkpoints").glob("*.json"))
    assert len(checkpoints) > 0, "No checkpoint files created"
    print(f"✅ Found {len(checkpoints)} checkpoint files")

    # Check handoff files
    handoffs = list(Path("orchestrator_data/handoffs").glob("*.json"))
    assert len(handoffs) > 0, "No handoff packages created"
    print(f"✅ Found {len(handoffs)} handoff packages")

    # Check log files
    logs = list(Path("orchestrator_data/logs").glob("*.log"))
    assert len(logs) > 0, "No log files created"
    print(f"✅ Found {len(logs)} log files")

    # 7. Validate result structure
    assert 'pipeline_id' in result
    assert 'requirements_analysis' in result
    assert 'quality_metrics' in result
    print("✅ Result structure validated")

    print("🎯 Complete orchestration test PASSED!")
    return True

if __name__ == "__main__":
    asyncio.run(test_complete_orchestration())
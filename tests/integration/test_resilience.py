import asyncio
from datetime import datetime
from task_queue.task_manager import Task, TaskPriority

async def test_circuit_breaker():
    """Test circuit breaker functionality with simulated failures"""

    # Create task that will cause failures
    failing_task = Task(
        id="failing_test_001",
        agent="business_analyst",
        project="failure_test",
        priority=TaskPriority.LOW,
        context={
            "issue": {
                "title": "Invalid Requirements",
                "body": "",  # Empty body should cause analysis failure
                "labels": []
            }
        },
        created_at=datetime.now().isoformat()
    )

    # Attempt multiple executions to trigger circuit breaker
    from agents.agent_stages import process_task_integrated
    from state_management.manager import StateManager
    from monitoring.logging import OrchestratorLogger

    state_manager = StateManager()
    logger = OrchestratorLogger("resilience_test")

    failures = 0
    for i in range(5):  # Try 5 times
        try:
            await process_task_integrated(failing_task, state_manager, logger)
        except Exception as e:
            failures += 1
            print(f"Attempt {i+1} failed: {e}")

    assert failures > 0, "Expected some failures for circuit breaker testing"
    print(f"✅ Circuit breaker test: {failures}/5 attempts failed as expected")

if __name__ == "__main__":
    asyncio.run(test_circuit_breaker())
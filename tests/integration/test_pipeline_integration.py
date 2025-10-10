import asyncio
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import patch, Mock
from task_queue.task_manager import Task, TaskPriority
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger

import logging

logger = logging.getLogger(__name__)
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

    # Create a temporary test project directory
    with tempfile.TemporaryDirectory() as tmpdir:
        test_project_dir = Path(tmpdir) / "test_project"
        test_project_dir.mkdir()
        
        # Create some basic project files
        (test_project_dir / "README.md").write_text("# Test Project")
        (test_project_dir / "main.py").write_text("print('Hello, world!')")

        # Mock the workspace manager to return our test directory
        with patch('services.project_workspace.workspace_manager') as mock_wm:
            mock_wm.get_project_dir.return_value = test_project_dir

            # Execute
            try:
                result = await process_task_integrated(test_task, state_manager, logger)
                logger.info(" Pipeline integration test passed")
                logger.info(f"📄 Result keys: {list(result.keys())}")
                return True
            except Exception as e:
                logger.info(f" Integration test failed: {e}")
                import traceback
                traceback.print_exc()
                return False

if __name__ == "__main__":
    asyncio.run(test_pipeline_integration())
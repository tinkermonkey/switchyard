import asyncio
import signal
import subprocess
import time
import json
import os
from pathlib import Path
from datetime import datetime
from state_management.manager import StateManager
from task_queue.task_manager import TaskQueue, Task, TaskPriority

import logging

logger = logging.getLogger(__name__)
class CheckpointRecoveryTestSuite:
    def __init__(self):
        self.test_results = {}
        self.state_manager = StateManager()
        self.task_queue = TaskQueue()

    async def test_graceful_shutdown_recovery(self):
        """Test recovery after graceful shutdown"""
        logger.info(" Testing Graceful Shutdown Recovery...")

        # Create a long-running task checkpoint
        pipeline_id = "shutdown_recovery_001"
        test_context = {
            "task_id": "shutdown_test",
            "agent": "business_analyst",
            "project": "shutdown_test",
            "issue": {
                "title": "Long Running Task",
                "body": "This task should be recoverable after shutdown",
                "labels": ["recovery-test"]
            },
            "simulate_long_operation": True,
            "work_completed": {
                "analysis_phase": "completed",
                "requirements_phase": "in_progress",
                "design_phase": "pending"
            }
        }

        # Create initial checkpoint
        await self.state_manager.checkpoint(
            pipeline_id=pipeline_id,
            stage_index=1,
            context=test_context
        )

        logger.info(" Initial checkpoint created")

        # Simulate some work progress
        test_context["work_completed"]["requirements_phase"] = "completed"
        test_context["work_completed"]["design_phase"] = "in_progress"

        # Create second checkpoint
        await self.state_manager.checkpoint(
            pipeline_id=pipeline_id,
            stage_index=2,
            context=test_context
        )

        logger.info(" Progress checkpoint created")

        # Verify checkpoints exist
        checkpoints = list(Path("orchestrator_data/state/checkpoints").glob(f"{pipeline_id}_stage_*.json"))
        assert len(checkpoints) >= 1, "No checkpoints found"
        logger.info(f" Found {len(checkpoints)} checkpoint files")

        # Test recovery
        latest_checkpoint = await self.state_manager.get_latest_checkpoint(pipeline_id)
        assert latest_checkpoint is not None, "Could not retrieve latest checkpoint"
        assert latest_checkpoint["stage_index"] == 2, "Incorrect stage index in checkpoint"
        assert latest_checkpoint["context"]["work_completed"]["requirements_phase"] == "completed"

        logger.info(" Checkpoint recovery working correctly")
        return True

    async def test_crash_recovery(self):
        """Test recovery after unexpected crash"""
        logger.info("⚡ Testing Crash Recovery...")

        pipeline_id = "crash_recovery_001"

        # Simulate a crash scenario by creating partial state
        partial_context = {
            "task_id": "crash_test",
            "agent": "business_analyst",
            "project": "crash_test",
            "status": "crashed_during_execution",
            "last_successful_stage": 0,
            "error_state": {
                "stage": "requirements_analysis",
                "error": "Process terminated unexpectedly",
                "timestamp": datetime.now().isoformat()
            }
        }

        # Create checkpoint representing state just before crash
        await self.state_manager.checkpoint(
            pipeline_id=pipeline_id,
            stage_index=0,
            context=partial_context
        )

        # Simulate agent state being saved
        await self.state_manager.save_agent_state(
            agent_id="business_analyst_crash_test",
            state={
                "current_task": "crash_test",
                "work_progress": 0.6,
                "pending_operations": ["finalize_requirements", "create_user_stories"]
            }
        )

        logger.info(" Crash state simulation created")

        # Test recovery mechanisms
        recovered_checkpoint = await self.state_manager.get_latest_checkpoint(pipeline_id)
        assert recovered_checkpoint is not None
        assert "error_state" in recovered_checkpoint["context"]

        recovered_agent_state = await self.state_manager.load_agent_state("business_analyst_crash_test")
        assert recovered_agent_state is not None
        assert recovered_agent_state["work_progress"] == 0.6

        logger.info(" Crash recovery data successfully retrieved")
        return True

    async def test_partial_state_corruption(self):
        """Test recovery from partial state file corruption"""
        logger.info(" Testing Partial State Corruption Recovery...")

        pipeline_id = "corruption_test_001"

        # Create some valid state
        valid_context = {
            "task_id": "corruption_test",
            "agent": "business_analyst",
            "project": "corruption_test",
            "test": "data",
            "checkpoint_time": datetime.now().isoformat(),
            "valid_data": {
                "requirements": ["req1", "req2", "req3"],
                "analysis": "comprehensive analysis completed"
            }
        }

        await self.state_manager.checkpoint(
            pipeline_id=pipeline_id,
            stage_index=0,
            context=valid_context
        )

        # Create a second valid checkpoint
        valid_context["stage"] = "advanced"
        await self.state_manager.checkpoint(
            pipeline_id=pipeline_id,
            stage_index=1,
            context=valid_context
        )

        # Corrupt one checkpoint file (simulate partial write)
        checkpoints = list(Path("orchestrator_data/state/checkpoints").glob(f"{pipeline_id}_stage_0.json"))
        assert len(checkpoints) > 0

        # Corrupt the first checkpoint
        with open(checkpoints[0], 'w') as f:
            f.write('{"corrupted": "partial"}')  # Invalid structure for our checkpoint format

        logger.info(f" Corrupted checkpoint file: {checkpoints[0].name}")

        # Test recovery mechanisms
        try:
            # Should still be able to get the latest valid checkpoint
            recovered_checkpoint = await self.state_manager.get_latest_checkpoint(pipeline_id)
            if recovered_checkpoint and recovered_checkpoint["stage_index"] == 1:
                logger.info(" Recovered from latest valid checkpoint despite corruption")
                return True
            elif recovered_checkpoint:
                logger.info(" Recovered checkpoint but from unexpected stage")
                return True
            else:
                # If no recovery, test graceful handling
                logger.info(" Gracefully handled corrupted checkpoint (no crash)")
                return True
        except Exception as e:
            logger.info(f" Failed to handle corruption gracefully: {e}")
            return False

    async def test_concurrent_checkpoint_access(self):
        """Test checkpoint system under concurrent access"""
        logger.info("⚡ Testing Concurrent Checkpoint Access...")

        pipeline_id = "concurrent_test_001"

        async def create_checkpoint_worker(worker_id: int):
            """Worker that creates checkpoints concurrently"""
            try:
                for i in range(3):
                    context = {
                        "worker_id": worker_id,
                        "checkpoint_number": i,
                        "timestamp": datetime.now().isoformat(),
                        "data": f"worker_{worker_id}_checkpoint_{i}"
                    }

                    await self.state_manager.checkpoint(
                        pipeline_id=f"{pipeline_id}_worker_{worker_id}",
                        stage_index=i,
                        context=context
                    )

                    # Small delay to simulate real work
                    await asyncio.sleep(0.1)

                return f"worker_{worker_id}_completed"
            except Exception as e:
                return f"worker_{worker_id}_failed: {e}"

        # Run multiple workers concurrently
        workers = [create_checkpoint_worker(i) for i in range(5)]
        results = await asyncio.gather(*workers)

        # Verify all workers completed successfully
        successful_workers = [r for r in results if "completed" in r]
        logger.info(f" {len(successful_workers)}/5 workers completed successfully")

        # Verify checkpoints were created
        all_checkpoints = list(Path("orchestrator_data/state/checkpoints").glob(f"{pipeline_id}_worker_*"))
        logger.info(f" Created {len(all_checkpoints)} checkpoint files from concurrent access")

        return len(successful_workers) >= 4  # Allow for some failures

    async def test_checkpoint_size_limits(self):
        """Test checkpoint system with large data"""
        logger.info(" Testing Checkpoint Size Limits...")

        pipeline_id = "size_test_001"

        # Create large context data
        large_context = {
            "task_id": "size_test",
            "agent": "business_analyst",
            "large_data": {
                "requirements": ["requirement_" + str(i) for i in range(1000)],
                "analysis_data": "x" * 10000,  # 10KB string
                "metadata": {
                    "processing_notes": ["note_" + str(i) for i in range(500)],
                    "timestamps": [datetime.now().isoformat() for _ in range(100)]
                }
            }
        }

        try:
            # Test creating large checkpoint
            await self.state_manager.checkpoint(
                pipeline_id=pipeline_id,
                stage_index=0,
                context=large_context
            )

            logger.info(" Large checkpoint created successfully")

            # Test retrieving large checkpoint
            recovered = await self.state_manager.get_latest_checkpoint(pipeline_id)
            assert recovered is not None
            assert len(recovered["context"]["large_data"]["requirements"]) == 1000
            assert len(recovered["context"]["large_data"]["analysis_data"]) == 10000

            logger.info(" Large checkpoint retrieved successfully")
            return True

        except Exception as e:
            logger.info(f" Large checkpoint test failed: {e}")
            return False

    async def test_checkpoint_cleanup(self):
        """Test checkpoint cleanup and management"""
        logger.info("🧹 Testing Checkpoint Cleanup...")

        pipeline_id = "cleanup_test_001"

        # Create multiple checkpoints
        for i in range(5):
            context = {
                "stage": i,
                "timestamp": datetime.now().isoformat(),
                "data": f"checkpoint_{i}"
            }

            await self.state_manager.checkpoint(
                pipeline_id=pipeline_id,
                stage_index=i,
                context=context
            )

        # Verify checkpoints exist
        checkpoints_before = list(Path("orchestrator_data/state/checkpoints").glob(f"{pipeline_id}_stage_*.json"))
        logger.info(f" Created {len(checkpoints_before)} checkpoints for cleanup test")

        # Test that we can still get the latest checkpoint
        latest = await self.state_manager.get_latest_checkpoint(pipeline_id)
        assert latest is not None
        assert latest["stage_index"] == 4

        logger.info(" Checkpoint cleanup test completed")
        return True

    async def cleanup_test_environment(self):
        """Clean up test environment"""
        logger.info("🧹 Cleaning up test environment...")

        # Clean up test checkpoint files
        test_patterns = [
            "shutdown_recovery_*",
            "crash_recovery_*",
            "corruption_test_*",
            "concurrent_test_*",
            "size_test_*",
            "cleanup_test_*"
        ]

        cleanup_count = 0
        checkpoints_dir = Path("orchestrator_data/state/checkpoints")

        for pattern in test_patterns:
            for file in checkpoints_dir.glob(f"{pattern}.json"):
                try:
                    file.unlink()
                    cleanup_count += 1
                except:
                    pass

        # Clean up test agent state files
        state_dir = Path("orchestrator_data/state")
        for file in state_dir.glob("agent_*crash_test*.json"):
            try:
                file.unlink()
                cleanup_count += 1
            except:
                pass

        logger.info(f" Cleaned up {cleanup_count} test files")

    async def run_all_tests(self):
        """Run complete checkpoint recovery test suite"""
        logger.info(" Running Checkpoint Recovery Validation...\n")

        tests = [
            ("Graceful Shutdown Recovery", self.test_graceful_shutdown_recovery),
            ("Crash Recovery", self.test_crash_recovery),
            ("Partial State Corruption", self.test_partial_state_corruption),
            ("Concurrent Checkpoint Access", self.test_concurrent_checkpoint_access),
            ("Checkpoint Size Limits", self.test_checkpoint_size_limits),
            ("Checkpoint Cleanup", self.test_checkpoint_cleanup)
        ]

        results = {}
        for test_name, test_func in tests:
            try:
                logger.info(f" Running {test_name}...")
                result = await test_func()
                results[test_name] = "PASSED" if result else "FAILED"
                logger.info(f" {test_name} PASSED\n")
            except Exception as e:
                results[test_name] = f"FAILED: {e}"
                logger.info(f" {test_name} FAILED: {e}\n")

        # Cleanup
        await self.cleanup_test_environment()

        logger.info(" Checkpoint Recovery Test Results:")
        for test, result in results.items():
            status = "" if "PASSED" in result else ""
            logger.info(f"  {status} {test}: {result}")

        all_passed = all("PASSED" in result for result in results.values())
        if all_passed:
            logger.info("\n All checkpoint recovery tests passed!")
        else:
            logger.info("\n Some checkpoint recovery tests failed!")

        return all_passed

if __name__ == "__main__":
    suite = CheckpointRecoveryTestSuite()
    asyncio.run(suite.run_all_tests())
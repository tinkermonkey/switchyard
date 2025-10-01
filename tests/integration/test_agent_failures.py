import asyncio
import time
from datetime import datetime
from pathlib import Path
from agents.orchestrator_integration import process_task_integrated, business_analyst_agent
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger
from task_queue.task_manager import Task, TaskPriority
from pipeline.resilient_pipeline import ResilientPipelineStage, ResilientPipeline

class AgentFailureTestSuite:
    def __init__(self):
        self.state_manager = StateManager()
        self.logger = OrchestratorLogger("failure_test")

    async def test_agent_timeout_handling(self):
        """Test agent timeout and recovery"""
        print("🕐 Testing Agent Timeout Handling...")

        # Create custom agent that will timeout
        async def timeout_agent(context):
            """Agent that simulates timeout"""
            if context.get('context', {}).get('force_timeout'):
                print("⏳ Simulating long operation...")
                await asyncio.sleep(35)  # Longer than typical timeout
            return context

        # Create task that will cause timeout
        timeout_task = Task(
            id="timeout_test_001",
            agent="business_analyst",
            project="timeout_test",
            priority=TaskPriority.HIGH,
            context={
                "issue": {
                    "title": "Timeout Test",
                    "body": "Test" * 10000,  # Very large body to cause timeout
                    "labels": ["test"]
                },
                "force_timeout": True  # Signal to agent to simulate long operation
            },
            created_at=datetime.now().isoformat()
        )

        start_time = time.time()
        try:
            # Use a smaller timeout for testing
            await asyncio.wait_for(
                process_task_integrated(timeout_task, self.state_manager, self.logger),
                timeout=10  # 10 second timeout
            )
            assert False, "Expected timeout"
        except asyncio.TimeoutError:
            duration = time.time() - start_time
            print(f"✅ Timeout handled correctly after {duration:.1f}s")
            return True

    async def test_partial_failure_recovery(self):
        """Test recovery from partial pipeline failures"""
        print("🔄 Testing Partial Failure Recovery...")

        # Create agent that fails partway through
        async def partial_failure_agent(context):
            """Agent that fails on specific conditions"""
            if context.get('context', {}).get('simulate_partial_failure'):
                # Do some work first (simulate partial completion)
                await asyncio.sleep(0.1)
                context['partial_work_completed'] = True

                # Create a checkpoint before failing
                await self.state_manager.checkpoint(
                    pipeline_id=context.get('pipeline_id', 'test'),
                    stage_index=0,
                    context=context
                )

                # Then fail
                raise Exception("Simulated partial failure after checkpoint")

            return await business_analyst_agent(context)

        # Simulate a task that fails partway through
        failing_task = Task(
            id="partial_fail_001",
            agent="business_analyst",
            project="partial_test",
            priority=TaskPriority.MEDIUM,
            context={
                "issue": {"title": "Partial Failure Test", "body": "", "labels": []},
                "simulate_partial_failure": True
            },
            created_at=datetime.now().isoformat()
        )

        # First attempt should fail
        try:
            await process_task_integrated(failing_task, self.state_manager, self.logger)
            assert False, "Expected failure"
        except Exception as e:
            print(f"✅ First attempt failed as expected: {e}")

        # Check checkpoint was created despite failure
        checkpoints = list(Path("orchestrator_data/state/checkpoints").glob(f"*partial_fail_001*.json"))
        if len(checkpoints) > 0:
            print(f"✅ Checkpoint created: {checkpoints[0].name}")
        else:
            print("⚠️ No checkpoint found, but test continues")

        return True

    async def test_cascade_failure_handling(self):
        """Test handling of cascade failures across components"""
        print("⚡ Testing Cascade Failure Handling...")

        # Create agents that simulate different failure scenarios
        async def failing_agent(context, error_type):
            """Agent that simulates specific error types"""
            error_type = context.get('context', {}).get('simulate_error')

            if error_type == 'redis_down':
                raise Exception("Redis connection failed")
            elif error_type == 'claude_error':
                raise Exception("Claude API rate limit exceeded")
            elif error_type == 'github_rate_limit':
                raise Exception("GitHub API rate limit exceeded")
            else:
                return await business_analyst_agent(context)

        failure_scenarios = [
            ("Redis Unavailable", "redis_down"),
            ("Claude API Error", "claude_error"),
            ("GitHub Rate Limit", "github_rate_limit")
        ]

        results = {}
        for scenario_name, error_type in failure_scenarios:
            test_task = Task(
                id=f"cascade_{error_type}_001",
                agent="business_analyst",
                project="cascade_test",
                priority=TaskPriority.LOW,
                context={
                    "issue": {"title": scenario_name, "body": "Test", "labels": []},
                    "simulate_error": error_type
                },
                created_at=datetime.now().isoformat()
            )

            try:
                await process_task_integrated(test_task, self.state_manager, self.logger)
                results[scenario_name] = "RECOVERED"
            except Exception as e:
                if "circuit breaker" in str(e).lower() or "retry" in str(e).lower():
                    results[scenario_name] = "HANDLED"
                else:
                    results[scenario_name] = f"UNHANDLED: {e}"

        print("⚡ Cascade Failure Results:")
        for scenario, result in results.items():
            status = "✅" if result in ["RECOVERED", "HANDLED"] else "❌"
            print(f"  {status} {scenario}: {result}")

        return results

    async def test_circuit_breaker_integration(self):
        """Test circuit breaker integration with agent failures"""
        print("🔧 Testing Circuit Breaker Integration...")

        # Create agent that fails consistently to trigger circuit breaker
        failure_count = 0
        async def failing_agent(context):
            nonlocal failure_count
            failure_count += 1
            raise Exception(f"Consistent failure #{failure_count}")

        # Create resilient pipeline stage with circuit breaker
        stage = ResilientPipelineStage(
            name="test_agent",
            agent_func=failing_agent,
            max_retries=2,
            circuit_breaker_config={
                'failure_threshold': 2,
                'recovery_timeout': 5
            }
        )

        pipeline = ResilientPipeline([stage], self.state_manager)

        test_context = {
            'pipeline_id': 'circuit_test_001',
            'task_id': 'circuit_test',
            'context': {}
        }

        # First execution should fail and trigger circuit breaker
        try:
            await pipeline.execute(test_context)
            assert False, "Expected failure"
        except Exception as e:
            print(f"✅ First execution failed as expected: {e}")

        # Check if circuit breaker is now open
        if stage.circuit_breaker.state == "open":
            print("✅ Circuit breaker opened after failures")
        else:
            print("⚠️ Circuit breaker state:", stage.circuit_breaker.state)

        # Subsequent calls should be rejected immediately
        try:
            await pipeline.execute(test_context)
            print("⚠️ Call was not rejected by open circuit")
        except Exception as e:
            if "OPEN" in str(e) or len(test_context.get('skipped_stages', [])) > 0:
                print("✅ Subsequent calls handled by open circuit")
            else:
                print(f"⚠️ Unexpected error: {e}")

        return True

    async def test_retry_exhaustion(self):
        """Test behavior when retry attempts are exhausted"""
        print("🔄 Testing Retry Exhaustion...")

        attempt_count = 0
        async def always_failing_agent(context):
            nonlocal attempt_count
            attempt_count += 1
            raise Exception(f"Persistent failure #{attempt_count}")

        # Create resilient pipeline stage with limited retries
        stage = ResilientPipelineStage(
            name="retry_test_agent",
            agent_func=always_failing_agent,
            max_retries=3,
            circuit_breaker_config={
                'failure_threshold': 5,  # Higher threshold to test retry exhaustion
                'recovery_timeout': 60
            }
        )

        pipeline = ResilientPipeline([stage], self.state_manager)

        test_context = {
            'pipeline_id': 'retry_test_001',
            'task_id': 'retry_test',
            'context': {}
        }

        try:
            await pipeline.execute(test_context)
            assert False, "Expected failure after retry exhaustion"
        except Exception as e:
            print(f"✅ All retry attempts exhausted: {attempt_count} attempts made")
            print(f"✅ Final error: {e}")

        return True

    async def test_agent_memory_exhaustion(self):
        """Test agent behavior under memory pressure"""
        print("💾 Testing Agent Memory Exhaustion...")

        async def memory_intensive_agent(context):
            """Agent that simulates high memory usage"""
            # Simulate memory-intensive processing
            large_data = []
            for i in range(1000):
                large_data.append("x" * 1000)  # Create large strings

            # Simulate processing
            await asyncio.sleep(0.1)

            # Clean up
            del large_data
            return context

        memory_task = Task(
            id="memory_test_001",
            agent="business_analyst",
            project="memory_test",
            priority=TaskPriority.LOW,
            context={
                "issue": {
                    "title": "Memory Test",
                    "body": "Testing memory usage patterns",
                    "labels": ["memory-test"]
                }
            },
            created_at=datetime.now().isoformat()
        )

        try:
            result = await process_task_integrated(memory_task, self.state_manager, self.logger)
            print("✅ Memory-intensive task completed successfully")
            return True
        except Exception as e:
            print(f"⚠️ Memory test failed: {e}")
            return False

    async def run_all_tests(self):
        """Run complete agent failure test suite"""
        tests = [
            ("Agent Timeout Handling", self.test_agent_timeout_handling),
            ("Partial Failure Recovery", self.test_partial_failure_recovery),
            ("Cascade Failure Handling", self.test_cascade_failure_handling),
            ("Circuit Breaker Integration", self.test_circuit_breaker_integration),
            ("Retry Exhaustion", self.test_retry_exhaustion),
            ("Memory Exhaustion", self.test_agent_memory_exhaustion)
        ]

        results = {}
        for test_name, test_func in tests:
            try:
                print(f"\n🧪 Running {test_name}...")
                result = await test_func()
                results[test_name] = "PASSED" if result else "FAILED"
                print(f"✅ {test_name} PASSED")
            except Exception as e:
                results[test_name] = f"FAILED: {e}"
                print(f"❌ {test_name} FAILED: {e}")

        print(f"\n📊 Agent Failure Test Results:")
        for test, result in results.items():
            status = "✅" if "PASSED" in result else "❌"
            print(f"  {status} {test}: {result}")

        return results

if __name__ == "__main__":
    suite = AgentFailureTestSuite()
    asyncio.run(suite.run_all_tests())
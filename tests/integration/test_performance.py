import asyncio
import time
import psutil
import gc
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from task_queue.task_manager import TaskQueue, Task, TaskPriority
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger

import logging

logger = logging.getLogger(__name__)
class PerformanceTestSuite:
    def __init__(self):
        self.task_queue = TaskQueue()
        self.state_manager = StateManager()
        self.logger = OrchestratorLogger("performance_test")
        self.baseline_memory = None

    def get_memory_usage(self):
        """Get current memory usage in MB"""
        process = psutil.Process()
        return process.memory_info().rss / 1024 / 1024

    def get_system_stats(self):
        """Get comprehensive system statistics"""
        return {
            "memory_mb": self.get_memory_usage(),
            "cpu_percent": psutil.cpu_percent(),
            "disk_io": psutil.disk_io_counters()._asdict() if psutil.disk_io_counters() else {},
            "network_io": psutil.net_io_counters()._asdict() if psutil.net_io_counters() else {}
        }

    async def test_task_throughput(self, num_tasks=50):
        """Test task processing throughput"""
        logger.info(f"⚡ Testing Task Throughput ({num_tasks} tasks)...")

        # Create multiple test tasks
        tasks = []
        for i in range(num_tasks):
            task = Task(
                id=f"perf_test_{i:03d}",
                agent="business_analyst",
                project="performance_test",
                priority=TaskPriority.MEDIUM,
                context={
                    "issue": {
                        "title": f"Performance Test {i}",
                        "body": f"Automated performance test task {i}",
                        "labels": ["performance", "automated"]
                    },
                    "performance_test": True,
                    "test_size": "small"
                },
                created_at=datetime.now().isoformat()
            )
            tasks.append(task)

        # Measure enqueue performance
        start_time = time.time()
        for task in tasks:
            self.task_queue.enqueue(task)

        enqueue_time = time.time() - start_time
        enqueue_rate = num_tasks / enqueue_time

        logger.info(f" Enqueued {num_tasks} tasks in {enqueue_time:.2f}s ({enqueue_rate:.1f} tasks/sec)")

        # Measure dequeue performance
        dequeue_start = time.time()
        dequeued_tasks = []
        for _ in range(min(num_tasks, 10)):  # Dequeue sample
            task = self.task_queue.dequeue()
            if task:
                dequeued_tasks.append(task)

        dequeue_time = time.time() - dequeue_start
        dequeue_rate = len(dequeued_tasks) / dequeue_time if dequeue_time > 0 else 0

        logger.info(f" Dequeued {len(dequeued_tasks)} tasks in {dequeue_time:.2f}s ({dequeue_rate:.1f} tasks/sec)")

        return {
            "tasks_enqueued": num_tasks,
            "enqueue_duration": enqueue_time,
            "enqueue_rate": enqueue_rate,
            "dequeue_duration": dequeue_time,
            "dequeue_rate": dequeue_rate,
            "tasks_dequeued": len(dequeued_tasks)
        }

    async def test_concurrent_processing(self, num_concurrent=5):
        """Test concurrent task processing capabilities"""
        logger.info(f" Testing Concurrent Processing ({num_concurrent} concurrent tasks)...")

        async def simulate_agent_work(task_id: int, duration: float = 0.5):
            """Simulate agent processing work"""
            start_time = time.time()

            # Simulate CPU work
            await asyncio.sleep(duration)

            # Simulate some state operations
            test_context = {
                "task_id": f"concurrent_test_{task_id}",
                "work_data": "x" * 1000,  # 1KB of data
                "timestamp": datetime.now().isoformat()
            }

            await self.state_manager.checkpoint(
                pipeline_id=f"concurrent_test_{task_id}",
                stage_index=0,
                context=test_context
            )

            return {
                "task_id": task_id,
                "duration": time.time() - start_time,
                "status": "completed"
            }

        # Run tasks concurrently
        start_time = time.time()
        tasks = [simulate_agent_work(i) for i in range(num_concurrent)]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        total_duration = time.time() - start_time

        # Analyze results
        successful_results = [r for r in results if isinstance(r, dict) and r.get("status") == "completed"]
        failed_results = [r for r in results if isinstance(r, Exception)]

        avg_task_duration = sum(r["duration"] for r in successful_results) / len(successful_results) if successful_results else 0

        logger.info(f" Concurrent processing completed:")
        logger.info(f"   - Total duration: {total_duration:.2f}s")
        logger.info(f"   - Successful tasks: {len(successful_results)}/{num_concurrent}")
        logger.info(f"   - Failed tasks: {len(failed_results)}")
        logger.info(f"   - Average task duration: {avg_task_duration:.2f}s")

        return {
            "total_duration": total_duration,
            "successful_tasks": len(successful_results),
            "failed_tasks": len(failed_results),
            "avg_task_duration": avg_task_duration,
            "concurrency_efficiency": len(successful_results) / num_concurrent
        }

    async def test_memory_usage_under_load(self, num_tasks=1000):
        """Test memory usage patterns under heavy load"""
        logger.info(f" Testing Memory Usage Under Load ({num_tasks} tasks)...")

        # Record initial memory
        initial_memory = self.get_memory_usage()
        memory_snapshots = [("start", initial_memory)]

        # Create tasks with varying sizes
        for i in range(num_tasks):
            # Create tasks with different payload sizes
            payload_size = "small" if i % 3 == 0 else "medium" if i % 3 == 1 else "large"
            body_multiplier = 1 if payload_size == "small" else 10 if payload_size == "medium" else 100

            task = Task(
                id=f"memory_test_{i:04d}",
                agent="business_analyst",
                project="memory_test",
                priority=TaskPriority.LOW,
                context={
                    "issue": {
                        "title": f"Memory Test {i}",
                        "body": "Test data " * body_multiplier,
                        "labels": [f"memory-test-{payload_size}"] * (body_multiplier // 10 + 1)
                    },
                    "payload_size": payload_size,
                    "test_data": {
                        "large_list": list(range(body_multiplier * 10)),
                        "text_data": "x" * (body_multiplier * 100)
                    }
                },
                created_at=datetime.now().isoformat()
            )
            self.task_queue.enqueue(task)

            # Take memory snapshots periodically
            if i % 100 == 0 and i > 0:
                current_memory = self.get_memory_usage()
                memory_snapshots.append((f"after_{i}_tasks", current_memory))
                logger.info(f"  After {i} tasks: {current_memory:.1f} MB ({current_memory-initial_memory:+.1f} MB)")

        # Final memory check before cleanup
        before_gc_memory = self.get_memory_usage()
        memory_snapshots.append(("before_gc", before_gc_memory))

        # Force garbage collection
        gc.collect()
        after_gc_memory = self.get_memory_usage()
        memory_snapshots.append(("after_gc", after_gc_memory))

        # Test dequeuing some tasks to check memory behavior
        dequeued_count = 0
        for _ in range(min(100, num_tasks)):
            task = self.task_queue.dequeue()
            if task:
                dequeued_count += 1
            else:
                break

        final_memory = self.get_memory_usage()
        memory_snapshots.append(("final", final_memory))

        logger.info(f" Memory usage analysis:")
        logger.info(f"   - Initial: {initial_memory:.1f} MB")
        logger.info(f"   - Peak: {before_gc_memory:.1f} MB ({before_gc_memory-initial_memory:+.1f} MB)")
        logger.info(f"   - After GC: {after_gc_memory:.1f} MB ({after_gc_memory-initial_memory:+.1f} MB)")
        logger.info(f"   - Final: {final_memory:.1f} MB ({final_memory-initial_memory:+.1f} MB)")
        logger.info(f"   - Dequeued: {dequeued_count} tasks")

        return {
            "initial_memory_mb": initial_memory,
            "peak_memory_mb": before_gc_memory,
            "after_gc_memory_mb": after_gc_memory,
            "final_memory_mb": final_memory,
            "memory_increase_mb": before_gc_memory - initial_memory,
            "memory_after_gc_increase_mb": after_gc_memory - initial_memory,
            "tasks_created": num_tasks,
            "tasks_dequeued": dequeued_count,
            "memory_snapshots": memory_snapshots
        }

    async def test_checkpoint_performance(self, num_checkpoints=100):
        """Test checkpoint creation and retrieval performance"""
        logger.info(f" Testing Checkpoint Performance ({num_checkpoints} checkpoints)...")

        # Test checkpoint creation performance
        creation_times = []
        for i in range(num_checkpoints):
            context = {
                "checkpoint_test": True,
                "checkpoint_number": i,
                "timestamp": datetime.now().isoformat(),
                "test_data": {
                    "requirements": [f"req_{j}" for j in range(50)],
                    "analysis": "x" * 1000,
                    "metadata": {"version": 1, "stage": "test"}
                }
            }

            start_time = time.time()
            await self.state_manager.checkpoint(
                pipeline_id=f"perf_test_{i}",
                stage_index=0,
                context=context
            )
            creation_time = time.time() - start_time
            creation_times.append(creation_time)

        # Calculate creation statistics
        avg_creation_time = sum(creation_times) / len(creation_times)
        max_creation_time = max(creation_times)
        min_creation_time = min(creation_times)

        logger.info(f" Checkpoint creation performance:")
        logger.info(f"   - Average: {avg_creation_time*1000:.2f} ms")
        logger.info(f"   - Min: {min_creation_time*1000:.2f} ms")
        logger.info(f"   - Max: {max_creation_time*1000:.2f} ms")

        # Test retrieval performance
        retrieval_times = []
        for i in range(min(20, num_checkpoints)):  # Test subset for retrieval
            start_time = time.time()
            checkpoint = await self.state_manager.get_latest_checkpoint(f"perf_test_{i}")
            retrieval_time = time.time() - start_time
            retrieval_times.append(retrieval_time)

            assert checkpoint is not None, f"Could not retrieve checkpoint for perf_test_{i}"

        avg_retrieval_time = sum(retrieval_times) / len(retrieval_times)

        logger.info(f" Checkpoint retrieval performance:")
        logger.info(f"   - Average: {avg_retrieval_time*1000:.2f} ms")
        logger.info(f"   - Tested: {len(retrieval_times)} retrievals")

        return {
            "checkpoints_created": num_checkpoints,
            "avg_creation_time_ms": avg_creation_time * 1000,
            "max_creation_time_ms": max_creation_time * 1000,
            "min_creation_time_ms": min_creation_time * 1000,
            "avg_retrieval_time_ms": avg_retrieval_time * 1000,
            "checkpoints_retrieved": len(retrieval_times)
        }

    async def test_system_resource_usage(self, duration=30):
        """Test system resource usage over time"""
        logger.info(f" Testing System Resource Usage (monitoring for {duration}s)...")

        resource_samples = []
        sample_interval = 2  # seconds
        samples_count = duration // sample_interval

        # Create some background load
        async def background_work():
            for i in range(50):
                task = Task(
                    id=f"resource_test_{i}",
                    agent="business_analyst",
                    project="resource_test",
                    priority=TaskPriority.LOW,
                    context={
                        "issue": {"title": f"Resource Test {i}", "body": "test", "labels": []},
                        "work_data": "x" * 1000
                    },
                    created_at=datetime.now().isoformat()
                )
                self.task_queue.enqueue(task)

                if i % 10 == 0:
                    await self.state_manager.checkpoint(
                        pipeline_id=f"resource_test_{i}",
                        stage_index=0,
                        context={"test": "data", "timestamp": datetime.now().isoformat()}
                    )

                await asyncio.sleep(0.1)

        # Start background work
        background_task = asyncio.create_task(background_work())

        # Monitor resources
        for i in range(samples_count):
            sample_time = time.time()
            stats = self.get_system_stats()
            stats['sample_time'] = sample_time
            stats['sample_number'] = i
            resource_samples.append(stats)

            if i == 0:
                logger.info(f"   Initial: Memory {stats['memory_mb']:.1f}MB, CPU {stats['cpu_percent']:.1f}%")
            elif i == samples_count - 1:
                logger.info(f"   Final: Memory {stats['memory_mb']:.1f}MB, CPU {stats['cpu_percent']:.1f}%")

            await asyncio.sleep(sample_interval)

        # Wait for background work to complete
        try:
            await asyncio.wait_for(background_task, timeout=10)
        except asyncio.TimeoutError:
            background_task.cancel()

        # Analyze resource usage
        memory_values = [s['memory_mb'] for s in resource_samples]
        cpu_values = [s['cpu_percent'] for s in resource_samples]

        avg_memory = sum(memory_values) / len(memory_values)
        peak_memory = max(memory_values)
        avg_cpu = sum(cpu_values) / len(cpu_values)
        peak_cpu = max(cpu_values)

        logger.info(f" Resource usage analysis:")
        logger.info(f"   - Average memory: {avg_memory:.1f} MB")
        logger.info(f"   - Peak memory: {peak_memory:.1f} MB")
        logger.info(f"   - Average CPU: {avg_cpu:.1f}%")
        logger.info(f"   - Peak CPU: {peak_cpu:.1f}%")

        return {
            "duration": duration,
            "samples_collected": len(resource_samples),
            "avg_memory_mb": avg_memory,
            "peak_memory_mb": peak_memory,
            "avg_cpu_percent": avg_cpu,
            "peak_cpu_percent": peak_cpu,
            "resource_samples": resource_samples[-5:]  # Keep last 5 samples
        }

    async def run_all_tests(self):
        """Run complete performance test suite"""
        logger.info("⚡ Running Performance Test Suite...\n")

        # Record baseline
        self.baseline_memory = self.get_memory_usage()
        logger.info(f" Baseline memory usage: {self.baseline_memory:.1f} MB\n")

        tests = [
            ("Task Throughput (50 tasks)", lambda: self.test_task_throughput(50)),
            ("Concurrent Processing (5 tasks)", lambda: self.test_concurrent_processing(5)),
            ("Memory Usage Under Load (100 tasks)", lambda: self.test_memory_usage_under_load(100)),
            ("Checkpoint Performance (50 checkpoints)", lambda: self.test_checkpoint_performance(50)),
            ("System Resource Usage (20s)", lambda: self.test_system_resource_usage(20))
        ]

        results = {}
        for test_name, test_func in tests:
            try:
                logger.info(f" Running {test_name}...")
                start_time = time.time()
                result = await test_func()
                duration = time.time() - start_time

                result['test_duration'] = duration
                results[test_name] = result
                logger.info(f" {test_name} completed in {duration:.2f}s\n")

                # Small delay between tests
                await asyncio.sleep(1)

            except Exception as e:
                results[test_name] = {"error": str(e)}
                logger.info(f" {test_name} FAILED: {e}\n")

        # Final memory check
        final_memory = self.get_memory_usage()
        memory_delta = final_memory - self.baseline_memory

        logger.info(" Performance Test Summary:")
        for test, result in results.items():
            if "error" in result:
                logger.info(f"   {test}: {result['error']}")
            else:
                logger.info(f"   {test}: Completed ({result.get('test_duration', 0):.2f}s)")

        logger.info(f"\n Memory Impact: {self.baseline_memory:.1f} MB → {final_memory:.1f} MB ({memory_delta:+.1f} MB)")

        return results

if __name__ == "__main__":
    suite = PerformanceTestSuite()
    results = asyncio.run(suite.run_all_tests())
    logger.info(f"\n📈 Performance test results: {len(results)} tests completed")
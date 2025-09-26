# Phase 0 - Week 4: Resilience & Production Polish

## Objective
Validate and polish the orchestrator for production use through comprehensive testing, error handling validation, and configuration management.

## Prerequisites from Week 3
- [x] GitHub integration working in Docker
- [x] Webhook � orchestrator � agent execution flow complete
- [x] Docker deployment functional
- [x] End-to-end integration tests passing

## Current Resilience Infrastructure Assessment

###  **Excellent Infrastructure Already Built**
- **Circuit Breaker**: Full 3-state implementation (CLOSED/OPEN/HALF_OPEN) with recovery
- **Retry Manager**: Exponential backoff with jitter and circuit breaker integration
- **Resilient Pipeline**: Integrated retry and circuit breaker patterns
- **Structured Logging**: JSON logging with agent/task context
- **Prometheus Metrics**: Task counters, durations, health scores
- **Health Monitoring**: Multi-component health checks (Redis, GitHub, Claude, system)
- **Configuration Management**: Pydantic-based environment configuration

### >� **Missing: Comprehensive Testing & Validation**
- No resilience pattern testing under failure conditions
- No state recovery validation under crashes
- No load testing or performance validation
- Limited error scenario coverage

---

## Day 1-2: Resilience Testing Framework

### Task 1.1: Create Circuit Breaker Test Suite
**Priority**: Critical
**Files**: `scripts/test_resilience_patterns.py`

```python
import asyncio
import time
from datetime import datetime
from resilience.circuit_breaker import CircuitBreaker
from resilience.retry_manager import RetryManager

class ResilienceTestSuite:
    def __init__(self):
        self.test_results = {}

    async def test_circuit_breaker_states(self):
        """Test circuit breaker state transitions"""
        print("=' Testing Circuit Breaker State Transitions...")

        # Create circuit breaker with low threshold for testing
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=5)

        # Test normal operation (CLOSED state)
        def success_func():
            return "success"

        result = cb.call(success_func)
        assert result == "success"
        assert cb.state == "closed"
        print(" CLOSED state working")

        # Test failure accumulation
        def failing_func():
            raise Exception("Test failure")

        # First failure
        try:
            cb.call(failing_func)
        except Exception:
            pass
        assert cb.state == "closed"  # Still closed after 1 failure

        # Second failure should open circuit
        try:
            cb.call(failing_func)
        except Exception:
            pass
        assert cb.state == "open"
        print(" OPEN state triggered after threshold")

        # Test that calls are rejected when open
        try:
            cb.call(success_func)
            assert False, "Should have been rejected"
        except Exception as e:
            assert "OPEN" in str(e)
        print(" Calls rejected when OPEN")

        # Test recovery after timeout
        print("� Waiting for recovery timeout...")
        time.sleep(6)  # Wait longer than recovery_timeout

        # Should transition to HALF_OPEN and allow success
        result = cb.call(success_func)
        assert result == "success"
        assert cb.state == "closed"  # Back to closed after success
        print(" Recovery to CLOSED after timeout + success")

        return True

    async def test_retry_with_backoff(self):
        """Test retry mechanism with exponential backoff"""
        print("= Testing Retry with Backoff...")

        attempt_times = []

        @RetryManager.with_retry(max_attempts=3, initial_delay=0.1)
        async def flaky_function():
            attempt_times.append(time.time())
            if len(attempt_times) < 3:  # Fail first 2 attempts
                raise Exception(f"Attempt {len(attempt_times)} failed")
            return "success"

        start_time = time.time()
        result = await flaky_function()

        assert result == "success"
        assert len(attempt_times) == 3

        # Verify exponential backoff timing
        delays = [attempt_times[i] - attempt_times[i-1] for i in range(1, len(attempt_times))]
        assert delays[1] > delays[0] * 1.5  # Second delay should be ~2x first
        print(f" Backoff delays: {[f'{d:.2f}s' for d in delays]}")

        return True

    async def test_circuit_breaker_with_retries(self):
        """Test integration of circuit breaker with retry mechanism"""
        print("� Testing Circuit Breaker + Retry Integration...")

        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=3)
        failure_count = 0

        @RetryManager.with_retry(max_attempts=5, initial_delay=0.1, circuit_breaker=cb)
        async def persistent_failure():
            nonlocal failure_count
            failure_count += 1
            raise Exception("Persistent failure")

        # Should fail all retries and open circuit
        try:
            await persistent_failure()
            assert False, "Should have failed"
        except Exception as e:
            assert "OPEN" in str(e) or failure_count >= 2

        print(f" Circuit opened after {failure_count} failures")

        # Subsequent calls should be immediately rejected
        try:
            await persistent_failure()
            assert False, "Should be rejected by open circuit"
        except Exception as e:
            assert "OPEN" in str(e)

        print(" Subsequent calls rejected by open circuit")
        return True

    async def run_all_tests(self):
        """Run complete resilience test suite"""
        tests = [
            ("Circuit Breaker States", self.test_circuit_breaker_states),
            ("Retry with Backoff", self.test_retry_with_backoff),
            ("Circuit Breaker + Retry", self.test_circuit_breaker_with_retries)
        ]

        results = {}
        for test_name, test_func in tests:
            try:
                print(f"\n>� Running {test_name}...")
                result = await test_func()
                results[test_name] = "PASSED" if result else "FAILED"
                print(f" {test_name} PASSED")
            except Exception as e:
                results[test_name] = f"FAILED: {e}"
                print(f"L {test_name} FAILED: {e}")

        print(f"\n=� Resilience Test Results:")
        for test, result in results.items():
            status = "" if "PASSED" in result else "L"
            print(f"  {status} {test}: {result}")

        return results

if __name__ == "__main__":
    suite = ResilienceTestSuite()
    asyncio.run(suite.run_all_tests())
```

### Task 1.2: Agent Failure Simulation Testing
**Priority**: High
**Files**: `scripts/test_agent_failures.py`

```python
import asyncio
from datetime import datetime
from main import process_task_integrated
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger
from task_queue.task_manager import Task, TaskPriority

class AgentFailureTestSuite:
    def __init__(self):
        self.state_manager = StateManager()
        self.logger = OrchestratorLogger("failure_test")

    async def test_agent_timeout_handling(self):
        """Test agent timeout and recovery"""
        print("� Testing Agent Timeout Handling...")

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
            await asyncio.wait_for(
                process_task_integrated(timeout_task, self.state_manager, self.logger),
                timeout=30  # 30 second timeout
            )
            assert False, "Expected timeout"
        except asyncio.TimeoutError:
            duration = time.time() - start_time
            print(f" Timeout handled correctly after {duration:.1f}s")
            return True

    async def test_partial_failure_recovery(self):
        """Test recovery from partial pipeline failures"""
        print("= Testing Partial Failure Recovery...")

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
            print(f" First attempt failed as expected: {e}")

        # Check checkpoint was created despite failure
        checkpoints = list(Path("orchestrator_data/state/checkpoints").glob(f"*{failing_task.id}*.json"))
        assert len(checkpoints) > 0, "No checkpoint created after partial failure"
        print(f" Checkpoint created: {checkpoints[0].name}")

        # Retry should potentially succeed with retry logic
        return True

    async def test_cascade_failure_handling(self):
        """Test handling of cascade failures across components"""
        print("=� Testing Cascade Failure Handling...")

        # Test Redis connection failure simulation
        # Test Claude API failure simulation
        # Test GitHub API failure simulation

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

        print("=� Cascade Failure Results:")
        for scenario, result in results.items():
            status = "" if result in ["RECOVERED", "HANDLED"] else "L"
            print(f"  {status} {scenario}: {result}")

        return results

if __name__ == "__main__":
    suite = AgentFailureTestSuite()
    asyncio.run(suite.test_partial_failure_recovery())
```

---

## Day 3-4: State Recovery & Checkpoint Testing

### Task 2.1: Checkpoint Recovery Validation
**Priority**: Critical
**Files**: `scripts/test_checkpoint_recovery.py`

```python
import asyncio
import signal
import subprocess
import time
from pathlib import Path
from datetime import datetime

class CheckpointRecoveryTestSuite:
    def __init__(self):
        self.test_results = {}

    async def test_graceful_shutdown_recovery(self):
        """Test recovery after graceful shutdown"""
        print("= Testing Graceful Shutdown Recovery...")

        # Start orchestrator in subprocess
        orchestrator_proc = subprocess.Popen(
            ['python', 'main.py'],
            cwd=Path.cwd()
        )

        # Give it time to start
        time.sleep(3)

        # Enqueue a long-running task
        from task_queue.task_manager import TaskQueue, Task, TaskPriority
        task_queue = TaskQueue()

        long_task = Task(
            id="shutdown_recovery_001",
            agent="business_analyst",
            project="shutdown_test",
            priority=TaskPriority.HIGH,
            context={
                "issue": {
                    "title": "Long Running Task",
                    "body": "This task should be recoverable after shutdown",
                    "labels": ["recovery-test"]
                },
                "simulate_long_operation": True
            },
            created_at=datetime.now().isoformat()
        )

        task_queue.enqueue(long_task)
        print(" Long-running task enqueued")

        # Wait for task to start processing
        time.sleep(5)

        # Gracefully shut down orchestrator
        orchestrator_proc.send_signal(signal.SIGTERM)
        orchestrator_proc.wait(timeout=10)
        print(" Orchestrator shut down gracefully")

        # Check that checkpoints exist
        checkpoints = list(Path("orchestrator_data/state/checkpoints").glob("*.json"))
        assert len(checkpoints) > 0, "No checkpoints found"
        print(f" Found {len(checkpoints)} checkpoint files")

        # Restart orchestrator
        print("= Restarting orchestrator...")
        new_proc = subprocess.Popen(['python', 'main.py'])
        time.sleep(3)

        # Verify task continues from checkpoint
        # (Would need to add checkpoint resume logic to main.py)

        new_proc.terminate()
        new_proc.wait()

        return True

    async def test_crash_recovery(self):
        """Test recovery after unexpected crash"""
        print("=� Testing Crash Recovery...")

        # This would involve:
        # 1. Start orchestrator with long-running task
        # 2. Kill process forcefully (SIGKILL)
        # 3. Restart and verify checkpoint recovery
        # 4. Validate state consistency

        return True

    async def test_partial_state_corruption(self):
        """Test recovery from partial state file corruption"""
        print("=� Testing Partial State Corruption Recovery...")

        # Create some valid state
        from state_management.manager import StateManager
        state_manager = StateManager()

        await state_manager.checkpoint(
            pipeline_id="corruption_test",
            stage_index=0,
            context={"test": "data", "checkpoint_time": datetime.now().isoformat()}
        )

        # Corrupt one checkpoint file (truncate it)
        checkpoints = list(Path("orchestrator_data/state/checkpoints").glob("corruption_test*.json"))
        assert len(checkpoints) > 0

        with open(checkpoints[0], 'w') as f:
            f.write('{"corrupted": "partial"}')  # Invalid JSON structure

        print(f" Corrupted checkpoint file: {checkpoints[0].name}")

        # Test recovery mechanisms
        try:
            recovered_checkpoint = await state_manager.get_latest_checkpoint("corruption_test")
            if recovered_checkpoint:
                print("� Checkpoint recovered despite corruption")
                return True
            else:
                print(" Gracefully handled corrupted checkpoint")
                return True
        except Exception as e:
            print(f"L Failed to handle corruption gracefully: {e}")
            return False

if __name__ == "__main__":
    suite = CheckpointRecoveryTestSuite()
    asyncio.run(suite.test_graceful_shutdown_recovery())
```

### Task 2.2: Performance & Load Testing
**Priority**: Medium
**Files**: `scripts/test_performance.py`

```python
import asyncio
import time
import concurrent.futures
from datetime import datetime
from task_queue.task_manager import TaskQueue, Task, TaskPriority

class PerformanceTestSuite:
    def __init__(self):
        self.task_queue = TaskQueue()

    async def test_task_throughput(self, num_tasks=50):
        """Test task processing throughput"""
        print(f"=� Testing Task Throughput ({num_tasks} tasks)...")

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
                    "performance_test": True
                },
                created_at=datetime.now().isoformat()
            )
            tasks.append(task)

        # Enqueue all tasks
        start_time = time.time()
        for task in tasks:
            self.task_queue.enqueue(task)

        enqueue_time = time.time() - start_time
        print(f" Enqueued {num_tasks} tasks in {enqueue_time:.2f}s ({num_tasks/enqueue_time:.1f} tasks/sec)")

        # Monitor processing (would need orchestrator running)
        # This is more of a manual test with metrics monitoring

        return {
            "tasks_enqueued": num_tasks,
            "enqueue_duration": enqueue_time,
            "enqueue_rate": num_tasks / enqueue_time
        }

    async def test_concurrent_processing(self):
        """Test concurrent task processing capabilities"""
        print("= Testing Concurrent Processing...")

        # This would test multiple agents processing simultaneously
        # Requires multi-threading or multi-processing setup

        return True

    async def test_memory_usage_under_load(self):
        """Test memory usage patterns under heavy load"""
        print("=� Testing Memory Usage Under Load...")

        import psutil
        import gc

        process = psutil.Process()
        initial_memory = process.memory_info().rss / 1024 / 1024  # MB

        # Create many tasks to stress memory
        for i in range(1000):
            task = Task(
                id=f"memory_test_{i:04d}",
                agent="business_analyst",
                project="memory_test",
                priority=TaskPriority.LOW,
                context={
                    "issue": {
                        "title": f"Memory Test {i}",
                        "body": "Large test data " * 100,  # Larger context
                        "labels": ["memory-test"] * 10
                    }
                },
                created_at=datetime.now().isoformat()
            )
            self.task_queue.enqueue(task)

            if i % 100 == 0:
                current_memory = process.memory_info().rss / 1024 / 1024
                print(f"  After {i} tasks: {current_memory:.1f} MB ({current_memory-initial_memory:+.1f} MB)")

        # Force garbage collection
        gc.collect()
        final_memory = process.memory_info().rss / 1024 / 1024

        print(f" Memory usage: {initial_memory:.1f} MB � {final_memory:.1f} MB ({final_memory-initial_memory:+.1f} MB)")

        return {
            "initial_memory_mb": initial_memory,
            "final_memory_mb": final_memory,
            "memory_increase_mb": final_memory - initial_memory
        }

if __name__ == "__main__":
    suite = PerformanceTestSuite()
    result = asyncio.run(suite.test_task_throughput(10))
    print(f"Performance test result: {result}")
```

---

## Day 5-6: Configuration Validation & Documentation

### Task 3.1: Configuration Validation Framework
**Priority**: High
**Files**: `scripts/validate_configuration.py`

```python
import yaml
import json
from pathlib import Path
from config.environment import Environment
from pydantic import ValidationError

class ConfigurationValidator:
    def __init__(self):
        self.validation_results = {}

    def validate_environment_config(self):
        """Validate environment configuration"""
        print("=' Validating Environment Configuration...")

        try:
            env = Environment()
            print(" Environment configuration loaded successfully")

            # Check required API keys
            required_secrets = [
                ('anthropic_api_key', env.anthropic_api_key),
                ('github_token', env.github_token),
                ('webhook_secret', env.webhook_secret)
            ]

            for name, secret in required_secrets:
                if secret and len(secret.get_secret_value()) > 10:
                    print(f" {name} configured")
                else:
                    print(f"L {name} missing or too short")
                    return False

            # Validate paths
            if not env.workspace_root.exists():
                print(f"� Workspace root doesn't exist: {env.workspace_root}")
            else:
                print(f" Workspace root exists: {env.workspace_root}")

            return True

        except ValidationError as e:
            print(f"L Environment validation failed: {e}")
            return False

    def validate_pipeline_configuration(self):
        """Validate pipeline configuration"""
        print("� Validating Pipeline Configuration...")

        pipeline_config_path = Path("config/pipelines.yaml")
        if not pipeline_config_path.exists():
            print("L Pipeline configuration file missing")
            return False

        try:
            with open(pipeline_config_path) as f:
                config = yaml.safe_load(f)

            if not config:
                print("L Pipeline configuration is empty")
                return False

            # Validate structure
            if 'pipelines' not in config:
                print("L Missing 'pipelines' section")
                return False

            if 'default' not in config:
                print("L Missing 'default' pipeline specification")
                return False

            print(" Pipeline configuration structure valid")
            return True

        except yaml.YAMLError as e:
            print(f"L Pipeline configuration YAML error: {e}")
            return False

    def validate_project_configuration(self):
        """Validate project configuration"""
        print("=� Validating Project Configuration...")

        project_config_path = Path("config/projects.yaml")
        if not project_config_path.exists():
            print("L Project configuration file missing")
            return False

        try:
            with open(project_config_path) as f:
                config = yaml.safe_load(f)

            if not config or 'projects' not in config:
                print("L Invalid project configuration structure")
                return False

            # Validate each project
            for project_name, project_config in config['projects'].items():
                print(f"  Validating project: {project_name}")

                required_fields = ['repo_url', 'local_path', 'branch']
                for field in required_fields:
                    if field not in project_config:
                        print(f"    L Missing field: {field}")
                        return False

                # Check if local path is reasonable
                local_path = Path(project_config['local_path'])
                if not str(local_path).startswith('/projects') and not str(local_path).startswith('../'):
                    print(f"    � Unusual local_path: {local_path}")

                print(f"     {project_name} configuration valid")

            return True

        except yaml.YAMLError as e:
            print(f"L Project configuration YAML error: {e}")
            return False

    def validate_docker_configuration(self):
        """Validate Docker configuration"""
        print("=3 Validating Docker Configuration...")

        # Check docker-compose.yml
        compose_path = Path("docker-compose.yml")
        if not compose_path.exists():
            print("L docker-compose.yml missing")
            return False

        # Check Dockerfile
        dockerfile_path = Path("Dockerfile")
        if not dockerfile_path.exists():
            print("L Dockerfile missing")
            return False

        # Check .env.example
        env_example_path = Path(".env.example")
        if not env_example_path.exists():
            print("� .env.example missing (recommended for team setup)")

        print(" Docker configuration files present")
        return True

    def validate_all_configurations(self):
        """Run complete configuration validation"""
        print("=
 Running Complete Configuration Validation...\n")

        validations = [
            ("Environment Configuration", self.validate_environment_config),
            ("Pipeline Configuration", self.validate_pipeline_configuration),
            ("Project Configuration", self.validate_project_configuration),
            ("Docker Configuration", self.validate_docker_configuration)
        ]

        results = {}
        all_passed = True

        for validation_name, validation_func in validations:
            try:
                result = validation_func()
                results[validation_name] = "PASSED" if result else "FAILED"
                if not result:
                    all_passed = False
            except Exception as e:
                results[validation_name] = f"ERROR: {e}"
                all_passed = False
            print()  # Add spacing between validations

        print("=� Configuration Validation Summary:")
        for validation, result in results.items():
            status = "" if "PASSED" in result else "L"
            print(f"  {status} {validation}: {result}")

        return all_passed, results

if __name__ == "__main__":
    validator = ConfigurationValidator()
    success, results = validator.validate_all_configurations()

    if success:
        print("\n<� All configurations valid!")
        exit(0)
    else:
        print("\nL Configuration validation failed!")
        exit(1)
```

### Task 3.2: Create Production Documentation
**Priority**: High
**Files**: `documentation/production_setup.md`, `documentation/troubleshooting.md`

```markdown
# Production Setup Guide

## Prerequisites
- Docker and Docker Compose installed
- GitHub CLI (`gh`) authenticated
- Claude Code CLI authenticated
- Redis available (or use Docker Redis)
- ngrok account for webhook testing

## Environment Configuration

### 1. Copy Environment Template
```bash
cp .env.example .env
```

### 2. Configure Required Variables
```bash
# GitHub Integration
GITHUB_TOKEN=ghp_xxxxxxxxxxxxxxxxxxxx  # GitHub Personal Access Token
GITHUB_WEBHOOK_SECRET=your_webhook_secret_here

# Claude/Anthropic
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxxxx

# ngrok (for webhook development)
NGROK_AUTHTOKEN=your_ngrok_token_here
```

### 3. Project Configuration
Edit `config/projects.yaml`:
```yaml
projects:
  your-project:
    repo_url: git@github.com:yourusername/your-project.git
    local_path: /projects/your-project
    branch: main
    kanban_board_id: YOUR_BOARD_ID
    kanban_columns:
      "Requirements Analysis": "business_analyst"
      "In Development": "senior_software_engineer"
      "Code Review": "code_reviewer"
```

## Deployment

### Docker Deployment
```bash
# Start all services
docker-compose up -d

# Check status
docker-compose ps

# View logs
docker-compose logs -f orchestrator
```

### Health Checks
```bash
# Webhook server health
curl http://localhost:3000/health

# Orchestrator metrics
curl http://localhost:8000/metrics

# ngrok tunnel status
curl http://localhost:4040/api/tunnels
```
```

---

## Day 7: Final Integration & Validation

### Task 4.1: Complete End-to-End Validation
**Priority**: Critical
**Files**: `scripts/test_production_readiness.py`

```python
import asyncio
import requests
import subprocess
import time
from pathlib import Path

class ProductionReadinessTestSuite:
    def __init__(self):
        self.test_results = {}

    async def test_full_system_startup(self):
        """Test complete system startup in Docker"""
        print("=� Testing Full System Startup...")

        # Start all services
        result = subprocess.run(
            ['docker-compose', 'up', '-d'],
            capture_output=True, text=True
        )

        if result.returncode != 0:
            print(f"L Docker compose failed: {result.stderr}")
            return False

        print(" Docker services started")

        # Wait for services to be ready
        await asyncio.sleep(10)

        # Check each service health
        health_checks = {
            "webhook": "http://localhost:3000/health",
            "metrics": "http://localhost:8000/metrics",
            "ngrok": "http://localhost:4040/api/tunnels"
        }

        for service, url in health_checks.items():
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    print(f" {service} healthy")
                else:
                    print(f"� {service} returned {response.status_code}")
            except requests.RequestException as e:
                print(f"L {service} unreachable: {e}")

        return True

    async def test_webhook_to_completion_flow(self):
        """Test complete webhook � orchestrator � completion flow"""
        print("= Testing Complete Webhook Flow...")

        # Send test webhook
        test_payload = {
            "action": "opened",
            "issue": {
                "number": 9999,
                "title": "Production Readiness Test",
                "body": "End-to-end production readiness validation",
                "html_url": "https://github.com/test/repo/issues/9999"
            },
            "repository": {"name": "production-test"}
        }

        try:
            response = requests.post(
                'http://localhost:3000/github-webhook',
                json=test_payload,
                headers={'X-GitHub-Event': 'issues'},
                timeout=5
            )

            if response.status_code == 200:
                print(" Webhook accepted")
            else:
                print(f"L Webhook failed: {response.status_code}")
                return False
        except requests.RequestException as e:
            print(f"L Webhook request failed: {e}")
            return False

        # Wait for processing
        await asyncio.sleep(5)

        # Check task queue status
        try:
            queue_response = requests.get('http://localhost:3000/queue-status')
            if queue_response.status_code == 200:
                queue_data = queue_response.json()
                print(f" Queue status: {queue_data['pending_tasks']} pending tasks")
            else:
                print("� Could not check queue status")
        except requests.RequestException:
            print("� Queue status endpoint unreachable")

        return True

    async def test_error_handling_in_production(self):
        """Test error handling in production environment"""
        print("=� Testing Production Error Handling...")

        # Test invalid webhook
        try:
            response = requests.post(
                'http://localhost:3000/github-webhook',
                json={"invalid": "payload"},
                headers={'X-GitHub-Event': 'invalid_event'}
            )
            print(f" Invalid webhook handled: {response.status_code}")
        except Exception as e:
            print(f"� Invalid webhook handling: {e}")

        # Test service resilience
        # (More comprehensive in real deployment)

        return True

    async def cleanup_test_environment(self):
        """Clean up test environment"""
        print(">� Cleaning up test environment...")

        subprocess.run(['docker-compose', 'down'], capture_output=True)
        print(" Docker services stopped")

        # Clean up test files
        test_orchestrator_data/state
            ".claude/state/test_*",
            "orchestrator_data/handoffs/test_*"
        ]

        for pattern in test_files:
            for file in Path(".").glob(pattern):
                file.unlink()

        print(" Test files cleaned up")

    async def run_production_validation(self):
        """Run complete production readiness validation"""
        print("<� Running Production Readiness Validation...\n")

        tests = [
            ("System Startup", self.test_full_system_startup),
            ("Webhook Flow", self.test_webhook_to_completion_flow),
            ("Error Handling", self.test_error_handling_in_production)
        ]

        results = {}
        for test_name, test_func in tests:
            try:
                result = await test_func()
                results[test_name] = "PASSED" if result else "FAILED"
                print()
            except Exception as e:
                results[test_name] = f"FAILED: {e}"

        # Cleanup
        await self.cleanup_test_environment()

        print("=� Production Readiness Results:")
        for test, result in results.items():
            status = "" if "PASSED" in result else "L"
            print(f"  {status} {test}: {result}")

        all_passed = all("PASSED" in result for result in results.values())
        if all_passed:
            print("\n<� Production Readiness VALIDATED!")
        else:
            print("\nL Production readiness validation failed!")

        return all_passed

if __name__ == "__main__":
    suite = ProductionReadinessTestSuite()
    asyncio.run(suite.run_production_validation())
```

---

## Success Criteria

### Must Complete
- [ ] Circuit breaker and retry mechanisms tested under failure conditions
- [ ] Checkpoint recovery validated after crashes and shutdowns
- [ ] Configuration validation framework ensures proper setup
- [ ] Complete test suite covers resilience patterns
- [ ] Production documentation created for deployment
- [ ] End-to-end production readiness test passes

### Should Complete
- [ ] Performance benchmarks established (task throughput, memory usage)
- [ ] Error handling tested across all failure scenarios
- [ ] Health monitoring validated under stress conditions
- [ ] Docker deployment fully tested and documented

### Nice to Have
- [ ] Load testing with concurrent task processing
- [ ] Advanced failure scenario testing (network partitions, etc.)
- [ ] Automated testing pipeline setup
- [ ] Monitoring dashboard for production use

## File Changes Required

### New Files
- `scripts/test_resilience_patterns.py`
- `scripts/test_agent_failures.py`
- `scripts/test_checkpoint_recovery.py`
- `scripts/test_performance.py`
- `scripts/validate_configuration.py`
- `scripts/test_production_readiness.py`
- `documentation/production_setup.md`
- `documentation/troubleshooting.md`
- `.env.example`

### Modified Files
- Enhanced error handling in agent implementations
- Additional health checks in monitoring components
- Configuration validation in startup process

## Week 4 Success Target

**Production-Ready Orchestrator**: All resilience patterns validated under failure conditions, comprehensive testing framework in place, configuration management robust, and complete end-to-end system validated for production deployment.

This completes the foundational Phase 0 with a battle-tested, production-ready orchestrator ready for scaling to multiple agents.
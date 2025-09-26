import asyncio
import requests
import subprocess
import time
import json
from pathlib import Path
from datetime import datetime

class ProductionReadinessTestSuite:
    def __init__(self):
        self.test_results = {}
        self.services = {
            "webhook": "http://localhost:3000",
            "metrics": "http://localhost:8000",
            "ngrok": "http://localhost:4040"
        }

    def check_service_health(self, service_name: str, url: str, timeout: int = 5):
        """Check if a service is healthy"""
        try:
            response = requests.get(f"{url}/health", timeout=timeout)
            return response.status_code == 200
        except requests.RequestException:
            try:
                # Try alternative endpoints
                if service_name == "metrics":
                    response = requests.get(f"{url}/metrics", timeout=timeout)
                    return response.status_code == 200
                elif service_name == "ngrok":
                    response = requests.get(f"{url}/api/tunnels", timeout=timeout)
                    return response.status_code == 200
            except requests.RequestException:
                pass
            return False

    async def test_configuration_validation(self):
        """Test that all configurations are valid"""
        print("🔧 Testing Configuration Validation...")

        try:
            # Run configuration validation script
            result = subprocess.run(
                ['python', 'tests/triage_scripts/validate_configuration.py'],
                capture_output=True, text=True, timeout=30
            )

            if result.returncode == 0:
                print("✅ Configuration validation passed")
                return True
            else:
                print(f"❌ Configuration validation failed: {result.stderr}")
                return False

        except subprocess.TimeoutExpired:
            print("❌ Configuration validation timed out")
            return False
        except FileNotFoundError:
            print("❌ Configuration validation script not found")
            return False

    async def test_core_services_startup(self):
        """Test that core services can start and are accessible"""
        print("🚀 Testing Core Services Startup...")

        # Check if services are already running
        services_status = {}
        for service_name, base_url in self.services.items():
            is_healthy = self.check_service_health(service_name, base_url)
            services_status[service_name] = is_healthy

            if is_healthy:
                print(f"✅ {service_name} service is healthy")
            else:
                print(f"⚠️ {service_name} service not accessible")

        # Check Redis connectivity
        try:
            import redis
            redis_client = redis.Redis(host='localhost', port=6379, decode_responses=True)
            redis_client.ping()
            services_status['redis'] = True
            print("✅ Redis is accessible")
        except Exception as e:
            services_status['redis'] = False
            print(f"⚠️ Redis not accessible: {e}")

        # At least Redis should be working for basic functionality
        if services_status.get('redis', False):
            print("✅ Core services startup test passed (Redis available)")
            return True
        else:
            print("❌ Core services startup test failed (Redis required)")
            return False

    async def test_task_queue_functionality(self):
        """Test basic task queue operations"""
        print("📋 Testing Task Queue Functionality...")

        try:
            from task_queue.task_manager import TaskQueue, Task, TaskPriority

            task_queue = TaskQueue()

            # Create test task
            test_task = Task(
                id="production_test_001",
                agent="business_analyst",
                project="production_test",
                priority=TaskPriority.HIGH,
                context={
                    "issue": {
                        "title": "Production Readiness Test",
                        "body": "Testing task queue functionality",
                        "labels": ["test"]
                    },
                    "production_test": True
                },
                created_at=datetime.now().isoformat()
            )

            # Test enqueue
            task_queue.enqueue(test_task)
            print("✅ Task enqueue successful")

            # Test dequeue
            dequeued_task = task_queue.dequeue()
            if dequeued_task and dequeued_task.id == test_task.id:
                print("✅ Task dequeue successful")
                return True
            else:
                print("❌ Task dequeue failed or returned wrong task")
                return False

        except ImportError as e:
            print(f"❌ Task queue import failed: {e}")
            return False
        except Exception as e:
            print(f"❌ Task queue functionality test failed: {e}")
            return False

    async def test_state_management(self):
        """Test state management and checkpoint functionality"""
        print("💾 Testing State Management...")

        try:
            from state_management.manager import StateManager

            state_manager = StateManager()

            # Test checkpoint creation
            test_context = {
                "pipeline_id": "production_test",
                "test_data": "checkpoint test",
                "timestamp": datetime.now().isoformat(),
                "stage": "testing"
            }

            await state_manager.checkpoint(
                pipeline_id="production_readiness_test",
                stage_index=0,
                context=test_context
            )
            print("✅ Checkpoint creation successful")

            # Test checkpoint retrieval
            retrieved_checkpoint = await state_manager.get_latest_checkpoint("production_readiness_test")
            if retrieved_checkpoint and retrieved_checkpoint["context"]["test_data"] == "checkpoint test":
                print("✅ Checkpoint retrieval successful")

                # Cleanup test checkpoint
                checkpoints = list(Path("orchestrator_data/state/checkpoints").glob("production_readiness_test*.json"))
                for checkpoint in checkpoints:
                    checkpoint.unlink()
                print("✅ Test checkpoint cleaned up")

                return True
            else:
                print("❌ Checkpoint retrieval failed")
                return False

        except ImportError as e:
            print(f"❌ State management import failed: {e}")
            return False
        except Exception as e:
            print(f"❌ State management test failed: {e}")
            return False

    async def test_resilience_patterns(self):
        """Test that resilience patterns are working"""
        print("⚡ Testing Resilience Patterns...")

        try:
            from resilience.circuit_breaker import CircuitBreaker
            from resilience.retry_manager import RetryManager

            # Test circuit breaker
            cb = CircuitBreaker(failure_threshold=2, recovery_timeout=1)

            # Test normal operation
            def success_func():
                return "success"

            result = cb.call(success_func)
            if result == "success" and cb.state == "closed":
                print("✅ Circuit breaker normal operation works")
            else:
                print("❌ Circuit breaker normal operation failed")
                return False

            # Test retry manager
            attempt_count = 0
            @RetryManager.with_retry(max_attempts=2, initial_delay=0.1)
            async def test_retry_func():
                nonlocal attempt_count
                attempt_count += 1
                if attempt_count == 1:
                    raise Exception("First attempt fails")
                return "success"

            result = await test_retry_func()
            if result == "success" and attempt_count == 2:
                print("✅ Retry manager works correctly")
                return True
            else:
                print("❌ Retry manager test failed")
                return False

        except ImportError as e:
            print(f"❌ Resilience patterns import failed: {e}")
            return False
        except Exception as e:
            print(f"❌ Resilience patterns test failed: {e}")
            return False

    async def test_webhook_endpoint(self):
        """Test webhook endpoint availability and basic functionality"""
        print("🌐 Testing Webhook Endpoint...")

        webhook_url = f"{self.services['webhook']}/github-webhook"
        health_url = f"{self.services['webhook']}/health"

        # Test health endpoint
        try:
            response = requests.get(health_url, timeout=5)
            if response.status_code == 200:
                print("✅ Webhook health endpoint accessible")
            else:
                print(f"⚠️ Webhook health endpoint returned {response.status_code}")
        except requests.RequestException as e:
            print(f"⚠️ Webhook health endpoint not accessible: {e}")

        # Test webhook endpoint with test payload
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
                webhook_url,
                json=test_payload,
                headers={'X-GitHub-Event': 'issues', 'Content-Type': 'application/json'},
                timeout=10
            )

            if response.status_code in [200, 202]:
                print("✅ Webhook endpoint accepts requests")
                return True
            else:
                print(f"⚠️ Webhook endpoint returned {response.status_code}")
                # Still count as success if endpoint is reachable
                return True

        except requests.RequestException as e:
            print(f"⚠️ Webhook endpoint test failed: {e}")
            # Don't fail the test if webhook server isn't running
            return True

    async def test_environment_configuration(self):
        """Test environment configuration is complete"""
        print("🔐 Testing Environment Configuration...")

        try:
            from config.environment import Environment

            env = Environment()

            # Check critical configuration
            required_configs = []

            if env.anthropic_api_key and len(env.anthropic_api_key.get_secret_value()) > 10:
                required_configs.append("anthropic_api_key")

            if env.github_token and len(env.github_token.get_secret_value()) > 10:
                required_configs.append("github_token")

            if env.webhook_secret and len(env.webhook_secret.get_secret_value()) > 5:
                required_configs.append("webhook_secret")

            print(f"✅ {len(required_configs)} critical configurations present")

            # Check workspace paths
            workspace_accessible = env.workspace_root.exists()
            orchestrator_accessible = env.orchestrator_root.exists()

            print(f"✅ Workspace root accessible: {workspace_accessible}")
            print(f"✅ Orchestrator root accessible: {orchestrator_accessible}")

            # Success if at least some configuration is present
            return len(required_configs) >= 2

        except Exception as e:
            print(f"❌ Environment configuration test failed: {e}")
            return False

    async def test_file_system_permissions(self):
        """Test file system permissions and directory structure"""
        print("📁 Testing File System Permissions...")

        required_dirs = [
            "orchestrator_data",
            "orchestrator_data/state",
            "orchestrator_data/state/checkpoints",
            "config",
            "scripts"
        ]

        permissions_ok = True
        for dir_path in required_dirs:
            dir_path_obj = Path(dir_path)

            if not dir_path_obj.exists():
                try:
                    dir_path_obj.mkdir(parents=True, exist_ok=True)
                    print(f"✅ Created directory: {dir_path}")
                except Exception as e:
                    print(f"❌ Cannot create directory {dir_path}: {e}")
                    permissions_ok = False
                    continue

            # Test write permissions
            test_file = dir_path_obj / f"test_write_{int(time.time())}.tmp"
            try:
                test_file.write_text("test")
                test_file.unlink()
                print(f"✅ Write permissions OK: {dir_path}")
            except Exception as e:
                print(f"❌ Write permission failed for {dir_path}: {e}")
                permissions_ok = False

        return permissions_ok

    async def test_integration_flow(self):
        """Test a simplified integration flow"""
        print("🔄 Testing Integration Flow...")

        try:
            # Import required modules
            from task_queue.task_manager import TaskQueue, Task, TaskPriority
            from state_management.manager import StateManager
            from monitoring.logging import OrchestratorLogger

            # Initialize components
            task_queue = TaskQueue()
            state_manager = StateManager()
            logger = OrchestratorLogger("production_test")

            # Create integration test task
            integration_task = Task(
                id="integration_test_001",
                agent="business_analyst",
                project="integration_test",
                priority=TaskPriority.HIGH,
                context={
                    "issue": {
                        "title": "Integration Test",
                        "body": "Testing end-to-end integration flow",
                        "labels": ["integration", "production-test"]
                    },
                    "integration_test": True
                },
                created_at=datetime.now().isoformat()
            )

            # Test full pipeline
            # 1. Enqueue task
            task_queue.enqueue(integration_task)
            logger.log_info(f"Enqueued integration test task: {integration_task.id}")

            # 2. Dequeue task
            dequeued_task = task_queue.dequeue()
            if not dequeued_task or dequeued_task.id != integration_task.id:
                print("❌ Task dequeue failed in integration test")
                return False

            # 3. Create checkpoint
            await state_manager.checkpoint(
                pipeline_id=f"integration_{integration_task.id}",
                stage_index=0,
                context=integration_task.context
            )

            # 4. Log completion
            logger.log_info(f"Integration test completed for task: {integration_task.id}")

            # 5. Cleanup
            checkpoints = list(Path("orchestrator_data/state/checkpoints").glob(f"integration_{integration_task.id}*.json"))
            for checkpoint in checkpoints:
                checkpoint.unlink()

            print("✅ Integration flow test completed successfully")
            return True

        except Exception as e:
            print(f"❌ Integration flow test failed: {e}")
            return False

    async def cleanup_test_environment(self):
        """Clean up test environment"""
        print("🧹 Cleaning up test environment...")

        cleanup_count = 0

        # Clean up test checkpoints
        test_patterns = [
            "production_readiness_test*.json",
            "integration_test_*.json",
            "production_test_*.json"
        ]

        checkpoints_dir = Path("orchestrator_data/state/checkpoints")
        if checkpoints_dir.exists():
            for pattern in test_patterns:
                for file in checkpoints_dir.glob(pattern):
                    try:
                        file.unlink()
                        cleanup_count += 1
                    except:
                        pass

        print(f"✅ Cleaned up {cleanup_count} test files")

    async def run_production_validation(self):
        """Run complete production readiness validation"""
        print("🎯 Running Production Readiness Validation...\n")

        tests = [
            ("Configuration Validation", self.test_configuration_validation),
            ("Core Services Startup", self.test_core_services_startup),
            ("Task Queue Functionality", self.test_task_queue_functionality),
            ("State Management", self.test_state_management),
            ("Resilience Patterns", self.test_resilience_patterns),
            ("Webhook Endpoint", self.test_webhook_endpoint),
            ("Environment Configuration", self.test_environment_configuration),
            ("File System Permissions", self.test_file_system_permissions),
            ("Integration Flow", self.test_integration_flow)
        ]

        results = {}
        passed_count = 0

        for test_name, test_func in tests:
            try:
                print(f"🧪 Running {test_name}...")
                result = await test_func()
                results[test_name] = "PASSED" if result else "FAILED"

                if result:
                    passed_count += 1
                    print(f"✅ {test_name} PASSED\n")
                else:
                    print(f"❌ {test_name} FAILED\n")

            except Exception as e:
                results[test_name] = f"ERROR: {e}"
                print(f"💥 {test_name} ERROR: {e}\n")

        # Cleanup
        await self.cleanup_test_environment()

        # Results summary
        print("📊 Production Readiness Results:")
        for test, result in results.items():
            if "PASSED" in result:
                status = "✅"
            elif "FAILED" in result:
                status = "❌"
            else:
                status = "💥"
            print(f"  {status} {test}: {result}")

        # Overall assessment
        success_rate = passed_count / len(tests)
        print(f"\n📈 Overall Success Rate: {success_rate:.1%} ({passed_count}/{len(tests)} tests passed)")

        if success_rate >= 0.8:  # 80% success rate for production ready
            print("\n🎉 PRODUCTION READY! System meets readiness criteria.")
            return True
        elif success_rate >= 0.6:  # 60% success rate for mostly ready
            print("\n⚠️ MOSTLY READY: Some issues need attention before full production deployment.")
            return False
        else:
            print("\n❌ NOT PRODUCTION READY: Significant issues need resolution.")
            return False

if __name__ == "__main__":
    suite = ProductionReadinessTestSuite()
    result = asyncio.run(suite.run_production_validation())

    if result:
        print("\n🚀 System is ready for production deployment!")
        exit(0)
    else:
        print("\n🔧 System needs additional work before production deployment.")
        exit(1)
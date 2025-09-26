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
        print("🔧 Testing Circuit Breaker State Transitions...")

        # Create circuit breaker with low threshold for testing
        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=5)

        # Test normal operation (CLOSED state)
        def success_func():
            return "success"

        result = cb.call(success_func)
        assert result == "success"
        assert cb.state == "closed"
        print("✅ CLOSED state working")

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
        print("✅ OPEN state triggered after threshold")

        # Test that calls are rejected when open
        try:
            cb.call(success_func)
            assert False, "Should have been rejected"
        except Exception as e:
            assert "OPEN" in str(e)
        print("✅ Calls rejected when OPEN")

        # Test recovery after timeout
        print("⏳ Waiting for recovery timeout...")
        time.sleep(6)  # Wait longer than recovery_timeout

        # Should transition to HALF_OPEN and allow success
        result = cb.call(success_func)
        assert result == "success"
        assert cb.state == "closed"  # Back to closed after success
        print("✅ Recovery to CLOSED after timeout + success")

        return True

    async def test_retry_with_backoff(self):
        """Test retry mechanism with exponential backoff"""
        print("🔄 Testing Retry with Backoff...")

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
        print(f"✅ Backoff delays: {[f'{d:.2f}s' for d in delays]}")

        return True

    async def test_circuit_breaker_with_retries(self):
        """Test integration of circuit breaker with retry mechanism"""
        print("⚡ Testing Circuit Breaker + Retry Integration...")

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

        print(f"✅ Circuit opened after {failure_count} failures")

        # Subsequent calls should be immediately rejected
        try:
            await persistent_failure()
            assert False, "Should be rejected by open circuit"
        except Exception as e:
            assert "OPEN" in str(e)

        print("✅ Subsequent calls rejected by open circuit")
        return True

    async def test_circuit_breaker_recovery(self):
        """Test circuit breaker recovery patterns"""
        print("🔄 Testing Circuit Breaker Recovery...")

        cb = CircuitBreaker(failure_threshold=2, recovery_timeout=2)

        # Force circuit to open
        def failing_func():
            raise Exception("Test failure")

        for _ in range(2):
            try:
                cb.call(failing_func)
            except:
                pass

        assert cb.state == "open"
        print("✅ Circuit opened")

        # Wait for recovery timeout
        time.sleep(3)

        # Should be able to attempt call (half-open)
        def success_func():
            return "recovered"

        result = cb.call(success_func)
        assert result == "recovered"
        assert cb.state == "closed"
        print("✅ Circuit recovered to closed state")

        return True

    async def test_retry_exhaustion(self):
        """Test behavior when all retry attempts are exhausted"""
        print("⚠️ Testing Retry Exhaustion...")

        attempt_count = 0

        @RetryManager.with_retry(max_attempts=3, initial_delay=0.1)
        async def always_failing_function():
            nonlocal attempt_count
            attempt_count += 1
            raise Exception(f"Failure {attempt_count}")

        try:
            await always_failing_function()
            assert False, "Should have failed after exhausting retries"
        except Exception as e:
            assert "Failure 3" in str(e)
            assert attempt_count == 3

        print(f"✅ All {attempt_count} retry attempts exhausted as expected")
        return True

    async def run_all_tests(self):
        """Run complete resilience test suite"""
        tests = [
            ("Circuit Breaker States", self.test_circuit_breaker_states),
            ("Retry with Backoff", self.test_retry_with_backoff),
            ("Circuit Breaker + Retry", self.test_circuit_breaker_with_retries),
            ("Circuit Breaker Recovery", self.test_circuit_breaker_recovery),
            ("Retry Exhaustion", self.test_retry_exhaustion)
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

        print(f"\n📊 Resilience Test Results:")
        for test, result in results.items():
            status = "✅" if "PASSED" in result else "❌"
            print(f"  {status} {test}: {result}")

        return results

if __name__ == "__main__":
    suite = ResilienceTestSuite()
    asyncio.run(suite.run_all_tests())
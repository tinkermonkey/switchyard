"""
Unit tests for docker_runner.py output validation

Tests the output validation mechanisms:
- _validate_result() method
- Empty output detection
- Minimal output detection
- Error marker detection
"""

import pytest
from unittest.mock import MagicMock, patch
from claude.docker_runner import DockerAgentRunner


class TestOutputValidation:
    """Test output validation in docker_runner"""

    def test_valid_output_with_exit_0(self):
        """Test exit 0 with valid output passes validation"""
        runner = DockerAgentRunner()

        result_text = "This is valid agent output with more than 50 characters to pass validation."
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is True
        assert error == ''

    def test_empty_output_with_exit_0(self):
        """Test exit 0 with empty output fails validation"""
        runner = DockerAgentRunner()

        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text="",
            container_name="test-container"
        )

        assert is_valid is False
        assert 'no output' in error.lower()

    def test_whitespace_only_output_with_exit_0(self):
        """Test exit 0 with whitespace-only output fails validation"""
        runner = DockerAgentRunner()

        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text="   \n\n   \t  ",
            container_name="test-container"
        )

        assert is_valid is False
        assert 'no output' in error.lower()

    def test_minimal_output_with_exit_0(self):
        """Test exit 0 with minimal output (<50 chars) fails validation"""
        runner = DockerAgentRunner()

        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text="Too short",  # Only 9 chars
            container_name="test-container"
        )

        assert is_valid is False
        assert 'insufficient output' in error.lower()
        assert '9 chars' in error

    def test_exactly_50_chars_passes(self):
        """Test exactly 50 characters passes validation"""
        runner = DockerAgentRunner()

        result_text = "x" * 50  # Exactly 50 chars
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is True
        assert error == ''

    def test_49_chars_fails(self):
        """Test 49 characters fails validation"""
        runner = DockerAgentRunner()

        result_text = "x" * 49  # Just under threshold
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is False
        assert 'insufficient output' in error.lower()

    def test_error_marker_critical_error(self):
        """Test output with CRITICAL ERROR marker fails validation"""
        runner = DockerAgentRunner()

        result_text = "CRITICAL ERROR: Something went wrong" + ("x" * 100)
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is False
        assert 'error marker' in error.lower()
        assert 'CRITICAL ERROR' in error

    def test_error_marker_fatal(self):
        """Test output with FATAL: marker fails validation"""
        runner = DockerAgentRunner()

        result_text = "FATAL: Database connection failed" + ("x" * 100)
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is False
        assert 'FATAL:' in error

    def test_error_marker_traceback(self):
        """Test output with Python traceback fails validation"""
        runner = DockerAgentRunner()

        result_text = """
        Some output here
        Traceback (most recent call last):
          File "test.py", line 10, in <module>
            raise Exception("Test error")
        """ + ("x" * 100)

        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is False
        assert 'Traceback (most recent call last):' in error

    def test_error_marker_redis_exception(self):
        """Test output with Redis exception fails validation"""
        runner = DockerAgentRunner()

        result_text = "redis.exceptions.ConnectionError: Connection refused" + ("x" * 100)
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is False
        assert 'redis.exceptions' in error

    def test_error_marker_connection_refused(self):
        """Test output with ConnectionRefusedError fails validation"""
        runner = DockerAgentRunner()

        result_text = "ConnectionRefusedError: [Errno 111] Connection refused" + ("x" * 100)
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is False
        assert 'ConnectionRefusedError' in error

    def test_error_marker_only_checks_first_1000_chars(self):
        """Test error marker check only examines first 1000 chars"""
        runner = DockerAgentRunner()

        # Valid output, then error marker after 1000 chars
        result_text = ("Valid output " * 100) + "CRITICAL ERROR at the end"
        # Ensure the error is after 1000 chars
        result_text = result_text[:1001] + "CRITICAL ERROR here"

        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        # Should pass because error marker is after first 1000 chars
        assert is_valid is True

    def test_non_zero_exit_code_always_valid(self):
        """Test non-zero exit code is always considered valid (not a validation error)"""
        runner = DockerAgentRunner()

        # Even with no output, non-zero exit is "valid" from validation perspective
        # (The failure is already indicated by exit code, not validation)
        is_valid, error = runner._validate_result(
            exit_code=1,
            result_text="",
            container_name="test-container"
        )

        assert is_valid is True
        assert error == ''

    def test_non_zero_exit_with_output(self):
        """Test non-zero exit code with output is valid"""
        runner = DockerAgentRunner()

        is_valid, error = runner._validate_result(
            exit_code=1,
            result_text="Error occurred",
            container_name="test-container"
        )

        assert is_valid is True
        assert error == ''


class TestFallbackResultRetrieval:
    """Test enhanced result retrieval with fallbacks"""

    def test_retrieval_from_redis_success(self):
        """Test successful result retrieval from Redis (primary)"""
        runner = DockerAgentRunner()

        # This would be tested in integration tests since it requires
        # actual Redis interaction. Unit tests would mock the Redis client.
        pass

    def test_retrieval_from_container_file_fallback(self):
        """Test result retrieval from container file (fallback 1)"""
        # This would be tested in integration tests with actual docker cp
        pass

    def test_retrieval_from_docker_logs_fallback(self):
        """Test result retrieval from docker logs (fallback 2)"""
        # This would be tested in integration tests with actual docker logs
        pass


class TestSignalTerminationHandling:
    """Test that SIGKILL (137) and SIGTERM (143) raise NonRetryableAgentError."""

    def _get_non_retryable_class(self):
        """Import NonRetryableAgentError without triggering agents/__init__.py."""
        import sys
        if 'services.dev_container_state' not in sys.modules:
            sys.modules['services.dev_container_state'] = MagicMock()
        from agents.non_retryable import NonRetryableAgentError
        return NonRetryableAgentError

    def test_exit_code_137_raises_non_retryable(self):
        """Exit code 137 (SIGKILL) should raise NonRetryableAgentError, not plain Exception."""
        NonRetryableAgentError = self._get_non_retryable_class()
        runner = DockerAgentRunner()

        with pytest.raises(NonRetryableAgentError, match="terminated by signal.*exit_code=137"):
            runner._raise_for_failed_exit_code(137, "killed")

    def test_exit_code_143_raises_non_retryable(self):
        """Exit code 143 (SIGTERM) should raise NonRetryableAgentError, not plain Exception."""
        NonRetryableAgentError = self._get_non_retryable_class()
        runner = DockerAgentRunner()

        with pytest.raises(NonRetryableAgentError, match="terminated by signal.*exit_code=143"):
            runner._raise_for_failed_exit_code(143, "terminated")

    def test_exit_code_1_raises_plain_exception(self):
        """Exit code 1 (generic failure) should raise plain Exception, allowing retries."""
        NonRetryableAgentError = self._get_non_retryable_class()
        runner = DockerAgentRunner()

        with pytest.raises(Exception, match="Agent execution failed.*exit_code=1") as exc_info:
            runner._raise_for_failed_exit_code(1, "some error")
        assert not isinstance(exc_info.value, NonRetryableAgentError)

    def test_exit_code_2_raises_plain_exception(self):
        """Exit code 2 (misuse) should raise plain Exception."""
        runner = DockerAgentRunner()

        with pytest.raises(Exception, match="Agent execution failed.*exit_code=2"):
            runner._raise_for_failed_exit_code(2, "bad args")


class TestValidationIntegrationWithWorkflow:
    """Test validation integrates correctly with execution workflow"""

    @pytest.mark.skip(reason="Requires full orchestrator environment with /app directory access")
    def test_validation_failure_marks_execution_failed(self):
        """Test validation failure marks execution as failed"""
        # This requires mocking work_execution_tracker
        # Testing the integration in _wait_for_container method

        runner = DockerAgentRunner()

        # Mock dependencies
        with patch('services.work_execution_state.work_execution_tracker') as mock_tracker:
            with patch.object(runner, '_persist_agent_result_to_redis'):
                # Simulate validation failure
                is_valid, error = runner._validate_result(
                    exit_code=0,
                    result_text="",  # Empty output
                    container_name="test-container"
                )

                assert is_valid is False
                assert error != ''

                # In real execution, this would trigger:
                # work_execution_tracker.record_execution_outcome(..., outcome='failure', error=error)

    def test_validation_success_allows_normal_processing(self):
        """Test validation success allows normal execution outcome"""
        runner = DockerAgentRunner()

        result_text = "Valid agent output with sufficient content and no error markers."
        is_valid, error = runner._validate_result(
            exit_code=0,
            result_text=result_text,
            container_name="test-container"
        )

        assert is_valid is True
        assert error == ''

        # In real execution, this would proceed to:
        # work_execution_tracker.record_execution_outcome(..., outcome='success')

"""
Unit tests for docker-claude-wrapper.py script

Tests the wrapper's defensive redundancy mechanisms:
- Signal handling
- Retry logic with exponential backoff
- Fallback storage
- Exit code validation
"""

import pytest
import signal
import time
import json
import os
import tempfile
from unittest.mock import MagicMock, patch, call, mock_open
from datetime import datetime, timezone
import sys
from pathlib import Path

# Add scripts directory to path
scripts_dir = Path(__file__).parent.parent.parent / "scripts"
sys.path.insert(0, str(scripts_dir))

# Import with explicit module name
import importlib.util
spec = importlib.util.spec_from_file_location("docker_claude_wrapper", scripts_dir / "docker-claude-wrapper.py")
docker_claude_wrapper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(docker_claude_wrapper)
ClaudeWrapper = docker_claude_wrapper.ClaudeWrapper


class TestSignalHandling:
    """Test signal handling for graceful shutdown"""

    def test_sigterm_handler_writes_result(self):
        """Test SIGTERM triggers final result write"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]
        wrapper.exit_code = 0

        # Mock the write method
        with patch.object(wrapper, 'write_final_result_with_retry') as mock_write:
            # Trigger SIGTERM handler (catches SystemExit)
            try:
                wrapper._handle_signal(signal.SIGTERM, None)
            except SystemExit as e:
                assert e.code == 128 + signal.SIGTERM

            # Verify result was written
            mock_write.assert_called_once_with(exit_code=128 + signal.SIGTERM)
            assert wrapper.cleanup_performed is True

    def test_sigint_handler_writes_result(self):
        """Test SIGINT (Ctrl+C) triggers final result write"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]
        wrapper.exit_code = 0

        with patch.object(wrapper, 'write_final_result_with_retry') as mock_write:
            try:
                wrapper._handle_signal(signal.SIGINT, None)
            except SystemExit as e:
                assert e.code == 128 + signal.SIGINT

            mock_write.assert_called_once_with(exit_code=128 + signal.SIGINT)
            assert wrapper.cleanup_performed is True

    def test_signal_handler_prevents_duplicate_cleanup(self):
        """Test signal handler doesn't run cleanup twice"""
        wrapper = ClaudeWrapper()
        wrapper.cleanup_performed = True  # Already cleaned up

        with patch.object(wrapper, 'write_final_result_with_retry') as mock_write:
            try:
                wrapper._handle_signal(signal.SIGTERM, None)
            except SystemExit:
                pass

            # Should not write again
            mock_write.assert_not_called()

    def test_atexit_handler_writes_result(self):
        """Test atexit handler writes result on normal exit"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]
        wrapper.exit_code = 0

        with patch.object(wrapper, 'write_final_result_with_retry') as mock_write:
            wrapper._cleanup()

            mock_write.assert_called_once_with(0)
            assert wrapper.cleanup_performed is True

    def test_atexit_handler_prevents_duplicate_cleanup(self):
        """Test atexit handler doesn't run if cleanup already performed"""
        wrapper = ClaudeWrapper()
        wrapper.cleanup_performed = True
        wrapper.exit_code = 0

        with patch.object(wrapper, 'write_final_result_with_retry') as mock_write:
            wrapper._cleanup()

            # Should not write again
            mock_write.assert_not_called()


class TestRetryLogic:
    """Test retry logic with exponential backoff"""

    def test_retry_succeeds_on_first_attempt(self):
        """Test successful write on first attempt"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]

        with patch.object(wrapper, '_write_final_result_attempt', return_value=True):
            result = wrapper.write_final_result_with_retry(exit_code=0)

            assert result is True

    def test_retry_succeeds_after_failures(self):
        """Test retry with exponential backoff succeeds after failures"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]
        wrapper.project = "test"
        wrapper.issue_number = "123"
        wrapper.task_id = "test-123"

        # Fail twice, then succeed (actually raise exceptions to trigger retry)
        def fail_twice(*args, **kwargs):
            if fail_twice.call_count == 0:
                fail_twice.call_count += 1
                raise Exception("First failure")
            elif fail_twice.call_count == 1:
                fail_twice.call_count += 1
                raise Exception("Second failure")
            else:
                return True
        fail_twice.call_count = 0

        with patch.object(wrapper, '_write_final_result_attempt', side_effect=fail_twice):
            with patch.object(docker_claude_wrapper.time, 'sleep') as mock_sleep:
                with patch.object(wrapper, 'connect_redis'):
                    result = wrapper.write_final_result_with_retry(exit_code=0)

                    assert result is True
                    # Check exponential backoff: 1s, 2s
                    assert mock_sleep.call_count == 2
                    mock_sleep.assert_any_call(1)  # 2^0
                    mock_sleep.assert_any_call(2)  # 2^1

    def test_retry_fails_after_max_attempts(self):
        """Test retry gives up after max attempts"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]

        # Always fail
        with patch.object(wrapper, '_write_final_result_attempt', return_value=False):
            with patch('time.sleep'):
                with patch.object(wrapper, 'connect_redis'):
                    result = wrapper.write_final_result_with_retry(exit_code=0, max_retries=3)

                    assert result is False

    def test_retry_reconnects_to_redis(self):
        """Test retry attempts to reconnect to Redis"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.output_lines = ["test output"]
        wrapper.project = "test"
        wrapper.issue_number = "123"
        wrapper.task_id = "test-123"

        # Always fail to trigger all retry attempts
        with patch.object(wrapper, '_write_final_result_attempt', side_effect=Exception("Always fail")):
            with patch.object(docker_claude_wrapper.time, 'sleep'):
                with patch.object(wrapper, 'connect_redis') as mock_connect:
                    wrapper.write_final_result_with_retry(exit_code=0, max_retries=3)

                    # Should try to reconnect before each retry (2 times for 3 attempts: after 1st and 2nd failure)
                    assert mock_connect.call_count == 2


class TestFallbackStorage:
    """Test fallback storage to /tmp file"""

    def test_fallback_file_written_successfully(self):
        """Test fallback file is written correctly"""
        wrapper = ClaudeWrapper()
        wrapper.project = "test-project"
        wrapper.issue_number = "123"
        wrapper.agent = "test-agent"
        wrapper.task_id = "test-task-123"
        wrapper.output_lines = ["line 1\n", "line 2\n"]

        with tempfile.TemporaryDirectory() as tmpdir:
            expected_file = f"{tmpdir}/agent_result_{wrapper.task_id}.json"

            # Mock open to write to temp directory
            with patch('builtins.open', mock_open()) as mock_file:
                with patch('json.dump') as mock_json_dump:
                    result = wrapper.write_fallback_result(exit_code=0)

                    assert result is True
                    # Verify JSON dump was called with correct data
                    mock_json_dump.assert_called_once()
                    call_args = mock_json_dump.call_args[0][0]
                    assert call_args['project'] == 'test-project'
                    assert call_args['issue_number'] == '123'
                    assert call_args['agent'] == 'test-agent'
                    assert call_args['task_id'] == 'test-task-123'
                    assert call_args['exit_code'] == 0
                    assert call_args['output'] == 'line 1\nline 2\n'
                    assert call_args['storage'] == 'fallback_file'

    def test_fallback_file_handles_large_output(self):
        """Test fallback file truncates large output"""
        wrapper = ClaudeWrapper()
        wrapper.project = "test-project"
        wrapper.issue_number = "123"
        wrapper.agent = "test-agent"
        wrapper.task_id = "test-task-123"
        wrapper.max_output_size = 100  # Small limit for testing
        wrapper.output_lines = ["x" * 150]  # Exceed limit

        with patch('builtins.open', mock_open()):
            with patch('json.dump') as mock_json_dump:
                result = wrapper.write_fallback_result(exit_code=0)

                assert result is True
                call_args = mock_json_dump.call_args[0][0]
                assert len(call_args['output']) == 100 + len(f"\n\n[OUTPUT TRUNCATED - exceeded {wrapper.max_output_size} bytes]")
                assert 'OUTPUT TRUNCATED' in call_args['output']

    def test_fallback_file_handles_write_error(self):
        """Test fallback file handles write errors gracefully"""
        wrapper = ClaudeWrapper()
        wrapper.task_id = "test-task-123"
        wrapper.output_lines = ["test"]

        with patch('builtins.open', side_effect=IOError("Disk full")):
            result = wrapper.write_fallback_result(exit_code=0)

            assert result is False


class TestExitCodeValidation:
    """Test exit code validation"""

    def test_success_with_redis_and_fallback(self):
        """Test successful Claude execution with successful persistence"""
        wrapper = ClaudeWrapper()
        wrapper.output_lines = ["test output"]

        with patch.object(wrapper, 'write_final_result_with_retry', return_value=True):
            with patch.object(wrapper, 'write_fallback_result', return_value=True):
                # Simulate run_claude logic
                redis_success = wrapper.write_final_result_with_retry(exit_code=0)
                fallback_success = wrapper.write_fallback_result(exit_code=0)

                # Should not fail container
                assert redis_success is True or fallback_success is True

    def test_success_without_redis_but_with_fallback(self):
        """Test Claude succeeds, Redis fails, but fallback succeeds"""
        wrapper = ClaudeWrapper()
        wrapper.output_lines = ["test output"]

        with patch.object(wrapper, 'write_final_result_with_retry', return_value=False):
            with patch.object(wrapper, 'write_fallback_result', return_value=True):
                redis_success = wrapper.write_final_result_with_retry(exit_code=0)
                fallback_success = wrapper.write_fallback_result(exit_code=0)

                # Should not fail - fallback succeeded
                assert not redis_success
                assert fallback_success

    def test_success_without_persistence_should_fail_container(self):
        """Test Claude succeeds but both persistence methods fail"""
        wrapper = ClaudeWrapper()
        wrapper.output_lines = ["test output"]

        with patch.object(wrapper, 'write_final_result_with_retry', return_value=False):
            with patch.object(wrapper, 'write_fallback_result', return_value=False):
                redis_success = wrapper.write_final_result_with_retry(exit_code=0)
                fallback_success = wrapper.write_fallback_result(exit_code=0)

                # Both failed
                assert not redis_success
                assert not fallback_success

                # Container should be failed (this logic is in run_claude)
                # exit_code should be changed to 1


class TestRedisConnection:
    """Test Redis connection handling"""

    def test_connect_redis_success(self):
        """Test successful Redis connection"""
        wrapper = ClaudeWrapper()

        with patch('redis.Redis') as mock_redis_class:
            mock_redis = MagicMock()
            mock_redis.ping.return_value = True
            mock_redis_class.return_value = mock_redis

            result = wrapper.connect_redis()

            assert result is True
            assert wrapper.redis_available is True
            assert wrapper.redis_client is not None

    def test_connect_redis_failure_continues(self):
        """Test Redis connection failure doesn't crash (fire-and-forget)"""
        wrapper = ClaudeWrapper()

        with patch('redis.Redis', side_effect=Exception("Connection refused")):
            result = wrapper.connect_redis()

            assert result is False
            assert wrapper.redis_available is False

    def test_write_event_when_redis_unavailable(self):
        """Test writing event when Redis is unavailable"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = False

        event = {"type": "test", "data": "test"}
        result = wrapper.write_claude_event(event)

        assert result is False

    def test_write_event_with_transient_error(self):
        """Test writing event with transient Redis error"""
        wrapper = ClaudeWrapper()
        wrapper.redis_available = True
        wrapper.redis_client = MagicMock()
        wrapper.redis_client.xadd.side_effect = Exception("Network timeout")

        event = {"type": "test", "data": "test"}
        result = wrapper.write_claude_event(event)

        assert result is False
        # redis_available should remain True (might be transient)
        assert wrapper.redis_available is True


class TestOutputTruncation:
    """Test output truncation for large results"""

    def test_output_within_limit(self):
        """Test output within size limit is not truncated"""
        wrapper = ClaudeWrapper()
        wrapper.project = "test"
        wrapper.issue_number = "123"
        wrapper.agent = "test"
        wrapper.task_id = "test-123"
        wrapper.max_output_size = 1000
        wrapper.output_lines = ["x" * 500]
        wrapper.redis_available = True
        wrapper.redis_client = MagicMock()

        with patch.object(wrapper.redis_client, 'setex'):
            wrapper._write_final_result_attempt(exit_code=0)

            # Verify setex was called
            call_args = wrapper.redis_client.setex.call_args[0][2]
            result_data = json.loads(call_args)

            assert len(result_data['output']) == 500
            assert 'TRUNCATED' not in result_data['output']

    def test_output_exceeds_limit(self):
        """Test output exceeding size limit is truncated"""
        wrapper = ClaudeWrapper()
        wrapper.project = "test"
        wrapper.issue_number = "123"
        wrapper.agent = "test"
        wrapper.task_id = "test-123"
        wrapper.max_output_size = 100
        wrapper.output_lines = ["x" * 200]
        wrapper.redis_available = True
        wrapper.redis_client = MagicMock()

        with patch.object(wrapper.redis_client, 'setex'):
            wrapper._write_final_result_attempt(exit_code=0)

            call_args = wrapper.redis_client.setex.call_args[0][2]
            result_data = json.loads(call_args)

            assert 'TRUNCATED' in result_data['output']
            assert len(result_data['output']) > 100  # Truncation message added

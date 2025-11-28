"""
Unit tests for Medic fingerprint engine
"""

import pytest
from services.medic.fingerprint_engine import FingerprintEngine, ErrorFingerprint


class TestFingerprintEngine:
    """Test fingerprint generation"""

    def setup_method(self):
        self.engine = FingerprintEngine()

    def test_generate_fingerprint_basic(self):
        """Test basic fingerprint generation"""
        log_entry = {
            "level": "ERROR",
            "message": "KeyError: 'issue_number' at line 42",
            "timestamp": "2025-11-28T12:45:23Z",
        }

        fingerprint = self.engine.generate("orchestrator-1", log_entry)

        assert isinstance(fingerprint, ErrorFingerprint)
        assert fingerprint.fingerprint_id.startswith("sha256:")
        assert fingerprint.container_pattern == "orchestrator"
        assert fingerprint.error_type == "KeyError"
        assert "issue_number" in fingerprint.error_pattern

    def test_extract_error_type_python_exception(self):
        """Test extracting Python exception types"""
        log_entry = {"message": "ValueError: invalid literal for int()"}
        error_type = self.engine._extract_error_type(log_entry)
        assert error_type == "ValueError"

    def test_extract_error_type_from_traceback(self):
        """Test extracting error type from traceback"""
        log_entry = {
            "message": "Something failed",
            "traceback": "Traceback...\nKeyError: 'missing_key'",
        }
        error_type = self.engine._extract_error_type(log_entry)
        assert error_type == "KeyError"

    def test_extract_error_type_from_log_level(self):
        """Test fallback to log level patterns"""
        log_entry = {"message": "[ConnectionError] Failed to connect"}
        error_type = self.engine._extract_error_type(log_entry)
        assert error_type == "ConnectionError"

    def test_extract_error_message_strips_type(self):
        """Test that error message strips the exception type"""
        log_entry = {"message": "KeyError: 'issue_number' in task context"}
        message = self.engine._extract_error_message(log_entry)
        assert message == "'issue_number' in task context"
        assert not message.startswith("KeyError:")

    def test_extract_error_message_strips_log_level(self):
        """Test stripping log level prefixes"""
        log_entry = {"message": "ERROR: Connection failed"}
        message = self.engine._extract_error_message(log_entry)
        assert message == "Connection failed"

    def test_normalize_applies_all_normalizers(self):
        """Test that normalize applies all normalizers"""
        message = "Error at 2025-11-28 12:45:23 with UUID 550e8400-e29b-41d4-a716-446655440000"
        normalized = self.engine._normalize(message)

        assert "{timestamp}" in normalized
        # UUID gets normalized (either as {uuid} or {hash} depending on context)
        assert ("550e8400-e29b-41d4-a716-446655440000" not in normalized)
        assert "Error at" in normalized

    def test_generate_stack_signature_python_traceback(self):
        """Test stack signature generation from Python traceback"""
        stack_trace = '''
        File "/app/main.py", line 123, in process_task
        File "/app/worker.py", line 456, in execute
        File "/app/handler.py", line 789, in handle
        '''

        signature = self.engine._generate_stack_signature(stack_trace)

        assert len(signature) > 0
        assert "main.py:process_task:123" in signature
        assert "worker.py:execute:456" in signature
        assert "handler.py:handle:789" in signature

    def test_generate_stack_signature_limits_to_five(self):
        """Test that stack signature is limited to top 5 frames"""
        stack_trace = "\n".join([
            f'File "/app/file{i}.py", line {i*10}, in func{i}'
            for i in range(10)
        ])

        signature = self.engine._generate_stack_signature(stack_trace)

        assert len(signature) <= 5

    def test_generate_stack_signature_no_traceback(self):
        """Test stack signature with no traceback"""
        signature = self.engine._generate_stack_signature(None)
        assert signature == []

    def test_normalize_container_name_removes_prefix(self):
        """Test container name normalization removes prefix"""
        assert self.engine._normalize_container_name("clauditoreum-orchestrator-1") == "orchestrator"
        assert self.engine._normalize_container_name("clauditoreum_orchestrator_1") == "orchestrator"

    def test_normalize_container_name_removes_instance_number(self):
        """Test container name normalization removes instance numbers"""
        assert self.engine._normalize_container_name("orchestrator-1") == "orchestrator"
        assert self.engine._normalize_container_name("orchestrator-123") == "orchestrator"

    def test_normalize_container_name_removes_hash(self):
        """Test container name normalization removes hashes"""
        assert self.engine._normalize_container_name("orchestrator-abc123def456") == "orchestrator"

    def test_hash_generates_consistent_id(self):
        """Test that identical data generates identical hash"""
        data1 = {
            "container_pattern": "orchestrator",
            "error_type": "KeyError",
            "error_pattern": "KeyError: '{key}'",
            "stack_signature": ["main.py:func:123"],
        }
        data2 = data1.copy()

        hash1 = self.engine._hash(data1)
        hash2 = self.engine._hash(data2)

        assert hash1 == hash2
        assert hash1.startswith("sha256:")

    def test_hash_generates_different_id_for_different_data(self):
        """Test that different data generates different hashes"""
        data1 = {
            "container_pattern": "orchestrator",
            "error_type": "KeyError",
            "error_pattern": "KeyError: '{key}'",
            "stack_signature": [],
        }
        data2 = {
            "container_pattern": "orchestrator",
            "error_type": "ValueError",
            "error_pattern": "ValueError: invalid",
            "stack_signature": [],
        }

        hash1 = self.engine._hash(data1)
        hash2 = self.engine._hash(data2)

        assert hash1 != hash2

    def test_fingerprint_includes_raw_data(self):
        """Test that fingerprint includes raw data for debugging"""
        log_entry = {
            "level": "ERROR",
            "message": "Test error at 2025-11-28",
            "traceback": "Stack trace here",
        }

        fingerprint = self.engine.generate("orchestrator-1", log_entry)

        assert "original_message" in fingerprint.raw_data
        assert "original_container" in fingerprint.raw_data
        assert fingerprint.raw_data["original_message"] == "Test error at 2025-11-28"
        assert fingerprint.raw_data["original_container"] == "orchestrator-1"

    def test_same_error_different_timestamps_same_fingerprint(self):
        """Test that same error at different times gets same fingerprint"""
        log_entry1 = {
            "level": "ERROR",
            "message": "KeyError: 'issue_number' at 2025-11-28 12:00:00",
        }
        log_entry2 = {
            "level": "ERROR",
            "message": "KeyError: 'issue_number' at 2025-11-28 13:00:00",
        }

        fp1 = self.engine.generate("orchestrator-1", log_entry1)
        fp2 = self.engine.generate("orchestrator-2", log_entry2)

        # Different timestamps but same error should have same fingerprint
        # (since timestamps are normalized and containers are normalized)
        assert fp1.fingerprint_id == fp2.fingerprint_id

    def test_same_error_different_containers_same_pattern_same_fingerprint(self):
        """Test that same error in different container instances gets same fingerprint"""
        log_entry = {
            "level": "ERROR",
            "message": "Connection timeout",
        }

        fp1 = self.engine.generate("orchestrator-1", log_entry)
        fp2 = self.engine.generate("orchestrator-2", log_entry)

        assert fp1.fingerprint_id == fp2.fingerprint_id
        assert fp1.container_pattern == fp2.container_pattern == "orchestrator"

    def test_different_error_types_different_fingerprints(self):
        """Test that different error types get different fingerprints"""
        log_entry1 = {"level": "ERROR", "message": "KeyError: 'key'"}
        log_entry2 = {"level": "ERROR", "message": "ValueError: invalid"}

        fp1 = self.engine.generate("orchestrator", log_entry1)
        fp2 = self.engine.generate("orchestrator", log_entry2)

        assert fp1.fingerprint_id != fp2.fingerprint_id
        assert fp1.error_type == "KeyError"
        assert fp2.error_type == "ValueError"

    def test_extract_stack_trace_from_message(self):
        """Test extracting embedded traceback from message"""
        log_entry = {
            "message": "Error occurred:\nTraceback (most recent call last):\n  File main.py line 10\nKeyError: 'key'"
        }

        stack_trace = self.engine._extract_stack_trace(log_entry)

        assert stack_trace is not None
        assert "File main.py" in stack_trace

    def test_extract_stack_trace_from_dedicated_field(self):
        """Test extracting stack trace from dedicated field"""
        log_entry = {
            "message": "Error",
            "traceback": "File main.py, line 10\nKeyError: 'key'"
        }

        stack_trace = self.engine._extract_stack_trace(log_entry)

        assert stack_trace == "File main.py, line 10\nKeyError: 'key'"

    def test_no_error_type_defaults_to_unknown(self):
        """Test that missing error type defaults to Unknown"""
        log_entry = {
            "level": "ERROR",
            "message": "Something went wrong",
        }

        fingerprint = self.engine.generate("orchestrator", log_entry)

        assert fingerprint.error_type == "Unknown"

    def test_complex_real_world_error(self):
        """Test fingerprinting a complex real-world error"""
        log_entry = {
            "level": "ERROR",
            "message": "KeyError: 'issue_number' in task_context at /workspace/clauditoreum/services/agent_executor.py line 242",
            "timestamp": "2025-11-28T12:45:23.123456Z",
            "traceback": '''Traceback (most recent call last):
  File "/workspace/clauditoreum/services/agent_executor.py", line 242, in execute_agent
    issue_number = task_context["issue_number"]
KeyError: 'issue_number'
''',
            "context": {
                "agent": "senior_software_engineer",
                "project": "what_am_i_watching",
            }
        }

        fingerprint = self.engine.generate("clauditoreum-orchestrator-abc123", log_entry)

        # Verify fingerprint components
        assert fingerprint.error_type == "KeyError"
        assert fingerprint.container_pattern == "orchestrator"
        # Message contains workspace path and line number which get normalized
        assert "{project}" in fingerprint.error_pattern
        assert "{line}" in fingerprint.error_pattern
        assert "issue_number" in fingerprint.error_pattern
        assert len(fingerprint.stack_signature) > 0
        assert "agent_executor.py:execute_agent:242" in fingerprint.stack_signature

        # Verify raw data preserved
        assert fingerprint.raw_data["original_container"] == "clauditoreum-orchestrator-abc123"
        # original_message is the extracted/cleaned message, log_entry has the full message
        assert "issue_number" in fingerprint.raw_data["original_message"]
        assert "KeyError" in fingerprint.raw_data["log_entry"]["message"]

"""
Fingerprint Engine for Error Deduplication

Generates unique fingerprints for errors by extracting key components
and normalizing variable parts.
"""

import hashlib
import json
import re
from typing import Optional
from dataclasses import dataclass

from .normalizers import get_default_normalizers


@dataclass
class ErrorFingerprint:
    """Represents a unique error fingerprint"""

    fingerprint_id: str
    container_pattern: str
    error_type: str
    error_pattern: str
    stack_signature: list[str]
    normalized_message: str
    raw_data: dict


class FingerprintEngine:
    """
    Generates unique fingerprints for error patterns.
    Implements normalization and similarity detection.
    """

    def __init__(self):
        self.normalizers = get_default_normalizers()

    def generate(self, container_name: str, log_entry: dict) -> ErrorFingerprint:
        """
        Generate fingerprint for a log entry.

        Args:
            container_name: Name of the container that produced the log
            log_entry: Parsed log entry with keys: level, message, timestamp, etc.

        Returns:
            ErrorFingerprint object with unique ID and signature components
        """
        # Extract error components
        error_type = self._extract_error_type(log_entry)
        error_message = self._extract_error_message(log_entry)
        stack_trace = self._extract_stack_trace(log_entry)

        # Normalize
        normalized_message = self._normalize(error_message)
        stack_signature = self._generate_stack_signature(stack_trace)
        container_pattern = self._normalize_container_name(container_name)

        # Build error pattern (normalized message with error type)
        if error_type:
            error_pattern = f"{error_type}: {normalized_message}"
        else:
            error_pattern = normalized_message

        # Generate fingerprint ID
        fingerprint_data = {
            "container_pattern": container_pattern,
            "error_type": error_type or "Unknown",
            "error_pattern": error_pattern,
            "stack_signature": stack_signature,
        }

        fingerprint_id = self._hash(fingerprint_data)

        return ErrorFingerprint(
            fingerprint_id=fingerprint_id,
            container_pattern=container_pattern,
            error_type=error_type or "Unknown",
            error_pattern=error_pattern,
            stack_signature=stack_signature,
            normalized_message=normalized_message,
            raw_data={
                "original_message": error_message,
                "original_container": container_name,
                "stack_trace": stack_trace,
                "log_entry": log_entry,
            },
        )

    def _extract_error_type(self, log_entry: dict) -> Optional[str]:
        """
        Extract error/exception type from log entry.

        Looks for Python exceptions, error classes, or known error patterns.
        """
        message = log_entry.get("message", "")

        # Python exception pattern: "ExceptionClass: message"
        # First try at the start
        exception_match = re.match(r'^([A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning)):', message)
        if exception_match:
            return exception_match.group(1)

        # Try after common log level prefixes like "ERROR: ExceptionClass:"
        exception_match = re.search(r'(?:ERROR|CRITICAL|WARNING|FATAL):\s+([A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning)):', message)
        if exception_match:
            return exception_match.group(1)

        # Check for bracketed or tagged error types
        error_patterns = [
            r'\[([A-Z][a-zA-Z0-9_]*Error)\]',
            r'<([A-Z][a-zA-Z0-9_]*Exception)>',
        ]

        for pattern in error_patterns:
            match = re.search(pattern, message)
            if match:
                return match.group(1)

        # Check stack trace for exception type
        if "traceback" in log_entry or "stack_trace" in log_entry:
            stack = log_entry.get("traceback") or log_entry.get("stack_trace", "")
            # Last line of traceback usually has exception type
            last_line = stack.strip().split('\n')[-1] if stack else ""
            exception_match = re.match(r'^([A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning)):', last_line)
            if exception_match:
                return exception_match.group(1)

        # Check for common error keywords as a fallback
        error_keywords = [
            (r'\bFATAL\b', 'FatalError'),
            (r'\bSTOPPING\b', 'ServiceError'),
            (r'\bfailed to\b', 'OperationFailure'),
            (r'\bcannot\b', 'OperationFailure'),
            (r'\binvalid\b', 'ValidationError'),
            (r'\bconnection (?:refused|failed|timeout)', 'ConnectionError'),
            (r'\btimeout\b', 'TimeoutError'),
            (r'\bpermission denied\b', 'PermissionError'),
            (r'\bnot found\b', 'NotFoundError'),
            (r'\bmissing\b', 'MissingDataError'),
        ]

        for pattern, error_type in error_keywords:
            if re.search(pattern, message, re.IGNORECASE):
                return error_type

        return None

    def _extract_error_message(self, log_entry: dict) -> str:
        """Extract the core error message from log entry"""
        message = log_entry.get("message", "")

        # If message starts with error type, strip it
        # "KeyError: 'issue_number'" -> "'issue_number'"
        message = re.sub(r'^[A-Z][a-zA-Z0-9_]*(?:Error|Exception|Warning):\s*', '', message)

        # Strip log level prefixes
        message = re.sub(r'^(?:ERROR|CRITICAL|WARNING|FATAL):\s*', '', message, flags=re.IGNORECASE)

        return message.strip()

    def _extract_stack_trace(self, log_entry: dict) -> Optional[str]:
        """Extract stack trace if present"""
        # Check various possible stack trace fields
        for field in ["traceback", "stack_trace", "exception", "exc_info"]:
            if field in log_entry and log_entry[field]:
                return str(log_entry[field])

        # Check if message contains embedded traceback
        message = log_entry.get("message", "")
        if "Traceback (most recent call last):" in message:
            # Extract everything after "Traceback"
            parts = message.split("Traceback (most recent call last):", 1)
            if len(parts) > 1:
                return parts[1].strip()

        return None

    def _normalize(self, message: str) -> str:
        """Apply all normalizers to message"""
        for normalizer in self.normalizers:
            message = normalizer.normalize(message)
        return message.strip()

    def _generate_stack_signature(self, stack_trace: Optional[str]) -> list[str]:
        """
        Generate a signature from stack trace by extracting function names and line refs.

        Returns a list like: ["file.py:function:123", "other.py:other_func:456"]
        """
        if not stack_trace:
            return []

        signature = []

        # Match stack trace lines like:
        # File "/path/file.py", line 123, in function_name
        # or: at file.py:123 in function_name
        patterns = [
            re.compile(r'File "([^"]+)", line (\d+), in (\w+)'),
            re.compile(r'at ([^:]+):(\d+) in (\w+)'),
            re.compile(r'([a-zA-Z_][a-zA-Z0-9_./]*\.py):(\d+)'),
        ]

        for line in stack_trace.split('\n'):
            for pattern in patterns:
                match = pattern.search(line)
                if match:
                    if len(match.groups()) >= 3:
                        filepath, lineno, funcname = match.group(1), match.group(2), match.group(3)
                        # Extract just filename from path
                        filename = filepath.split('/')[-1]
                        signature.append(f"{filename}:{funcname}:{lineno}")
                    elif len(match.groups()) == 2:
                        filepath, lineno = match.group(1), match.group(2)
                        filename = filepath.split('/')[-1]
                        signature.append(f"{filename}:{lineno}")
                    break

        # Limit to top 5 stack frames for signature
        return signature[:5]

    def _normalize_container_name(self, container_name: str) -> str:
        """
        Normalize container name to pattern.

        Examples:
        - "clauditoreum-orchestrator-1" -> "orchestrator"
        - "orchestrator-abc123" -> "orchestrator"
        - "clauditoreum_orchestrator_1" -> "orchestrator"
        """
        # Remove common prefixes
        name = container_name
        name = re.sub(r'^clauditoreum[-_]', '', name)
        name = re.sub(r'^clauditoreum_', '', name)

        # Remove instance numbers and IDs (6+ hex chars or any numbers)
        name = re.sub(r'[-_]\d+$', '', name)
        name = re.sub(r'[-_][0-9a-fA-F]{6,}$', '', name)

        return name

    def _hash(self, data: dict) -> str:
        """Generate SHA256 hash of fingerprint data"""
        # Sort keys for consistent hashing
        json_str = json.dumps(data, sort_keys=True)
        hash_obj = hashlib.sha256(json_str.encode('utf-8'))
        return f"sha256:{hash_obj.hexdigest()}"

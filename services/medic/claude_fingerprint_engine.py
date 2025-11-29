"""
Claude Failure Fingerprint Engine

Generates PROJECT-SCOPED fingerprints for Claude Code tool execution failures.
Unlike Docker log fingerprints, these are tied to specific projects since
each codebase has unique solutions.
"""

import hashlib
import json
import re
import logging
from typing import Optional, Dict
from dataclasses import dataclass

from .normalizers import get_default_normalizers
from .claude_clustering_engine import FailureCluster

logger = logging.getLogger(__name__)


@dataclass
class ClaudeFailureFingerprint:
    """Represents a unique Claude Code failure fingerprint"""

    fingerprint_id: str
    project: str  # NOT normalized - key discriminator
    tool_name: str
    error_type: str
    error_pattern: str
    context_signature: str
    cluster_metadata: dict
    raw_data: dict


class ClaudeFingerprintEngine:
    """
    Generates fingerprints for Claude Code tool execution failures.

    Fingerprints are PROJECT-SCOPED and include cluster metadata.
    """

    def __init__(self):
        self.normalizers = get_default_normalizers()
        self.logger = logger

    def generate_from_cluster(self, cluster: FailureCluster) -> ClaudeFailureFingerprint:
        """
        Generate failure signature from cluster.

        Fingerprint Components:
        1. Project name (NOT normalized - signatures are project-specific)
        2. Tool name (normalized - e.g., "Read", "Bash", "Edit")
        3. Error pattern (normalized message)
        4. Error type (extracted from error message)
        5. Context signature (file paths, commands, etc.)

        Args:
            cluster: FailureCluster object

        Returns:
            ClaudeFailureFingerprint object
        """
        primary = cluster.get_primary_failure()
        context = cluster.get_fingerprint_context()

        # Extract error details
        error_message = self._extract_error_message(primary)
        normalized_message = self._normalize_error_message(error_message)
        error_type = self._extract_error_type(error_message)
        tool_name = primary.get('tool_name', 'unknown')

        # Extract context signature (commands, file paths, etc.)
        context_sig = self._extract_context_signature(primary, cluster)

        # Generate fingerprint hash
        fingerprint_string = (
            f"{cluster.project}||"  # Project is KEY part of fingerprint
            f"{tool_name}||"
            f"{error_type}||"
            f"{normalized_message}||"
            f"{context_sig}"
        )

        fingerprint_id = f"sha256:{hashlib.sha256(fingerprint_string.encode()).hexdigest()}"

        return ClaudeFailureFingerprint(
            fingerprint_id=fingerprint_id,
            project=cluster.project,  # NOT normalized
            tool_name=tool_name,
            error_type=error_type,
            error_pattern=normalized_message,
            context_signature=context_sig,
            cluster_metadata=context["cluster_metadata"],
            raw_data={
                "cluster_id": cluster.cluster_id,
                "first_failure": cluster.first_failure,
                "last_failure": cluster.last_failure,
                "all_failures": cluster.failures,
            }
        )

    def _extract_error_message(self, failure: Dict) -> str:
        """Extract error message from failure event"""
        # Try result_event first
        if 'result_event' in failure:
            try:
                content = failure['result_event']['raw_event']['event']['message']['content']
                if isinstance(content, list) and len(content) > 0:
                    error_content = content[0].get('content', '')
                    return str(error_content)
            except (KeyError, IndexError, TypeError):
                pass

        # Fallback
        return failure.get('error_message', 'Unknown error')

    def _normalize_error_message(self, message: str) -> str:
        """
        Normalize error message using existing normalizers.

        IMPORTANT: Project paths are partially normalized:
        - /workspace/{project}/src/file.js remains intact
        - Project name is PRESERVED for fingerprinting
        - Only file paths within project are normalized
        """
        normalized = message
        for normalizer in self.normalizers:
            normalized = normalizer.normalize(normalized)

        return normalized[:500]  # First 500 chars

    def _extract_error_type(self, message: str) -> str:
        """
        Extract error type from message.

        Examples:
        - "Exit code 1" -> "exit_code_error"
        - "ENOENT: no such file" -> "file_not_found"
        - "npm error" -> "npm_error"
        - "Error: Could not resolve" -> "resolution_error"
        """
        message_lower = message.lower()

        # Exit codes
        if "exit code" in message_lower:
            return "exit_code_error"

        # File system errors
        if "enoent" in message_lower or "no such file" in message_lower:
            return "file_not_found"
        if "eacces" in message_lower or "permission denied" in message_lower:
            return "permission_denied"

        # Package manager errors
        if "npm error" in message_lower:
            return "npm_error"
        if "yarn error" in message_lower:
            return "yarn_error"
        if "pnpm error" in message_lower:
            return "pnpm_error"

        # Build errors
        if "could not resolve" in message_lower:
            return "resolution_error"
        if "syntax error" in message_lower:
            return "syntax_error"
        if "parse error" in message_lower:
            return "parse_error"

        # Runtime errors
        if "timeout" in message_lower:
            return "timeout_error"
        if "connection refused" in message_lower or "econnrefused" in message_lower:
            return "connection_error"

        # Generic
        if "error:" in message_lower:
            return "generic_error"

        return "unknown_error"

    def _extract_context_signature(self, failure: dict, cluster: FailureCluster) -> str:
        """
        Extract context signature from tool parameters and errors.

        For Bash: Normalize command patterns
        For Read/Edit/Write: Normalize file paths
        For other tools: Extract key parameters
        """
        tool_name = failure.get('tool_name', '')

        # Try to get tool params from call_event
        tool_params = {}
        if 'call_event' in failure:
            tool_params = failure['call_event'].get('tool_params', {})

        if tool_name == 'Bash':
            # Extract command pattern
            command = tool_params.get('command', '')
            return self._normalize_bash_command(command)

        elif tool_name in ['Read', 'Edit', 'Write']:
            # Extract file path pattern
            file_path = tool_params.get('file_path', '')
            return self._normalize_file_path(file_path, cluster.project)

        elif tool_name == 'Grep':
            # Pattern + path
            pattern = tool_params.get('pattern', '')
            return f"grep:{pattern[:50]}"  # First 50 chars of pattern

        elif tool_name == 'Glob':
            # Glob pattern
            pattern = tool_params.get('pattern', '')
            return f"glob:{pattern[:50]}"

        else:
            # Generic: first 100 chars of params JSON
            try:
                return json.dumps(tool_params)[:100]
            except:
                return "unknown_context"

    def _normalize_bash_command(self, command: str) -> str:
        """
        Normalize bash commands for pattern matching.

        Examples:
        - "npm install" -> "npm:install"
        - "npm run build" -> "npm:run:build"
        - "docker build -f Dockerfile.agent" -> "docker:build"
        - "pytest tests/test_foo.py" -> "pytest:tests"
        """
        if not command:
            return "bash:empty"

        command_lower = command.lower().strip()

        # Extract command name and primary action
        parts = command_lower.split()
        if not parts:
            return "bash:empty"

        cmd = parts[0]

        # NPM/Yarn/PNPM
        if cmd in ['npm', 'yarn', 'pnpm']:
            action = parts[1] if len(parts) > 1 else ''
            subaction = parts[2] if len(parts) > 2 else ''
            return f"{cmd}:{action}:{subaction}".rstrip(':')

        # Docker
        if cmd == 'docker':
            action = parts[1] if len(parts) > 1 else ''
            return f"docker:{action}"

        # Python/Pytest
        if cmd in ['python', 'python3', 'pytest', 'py.test']:
            return f"{cmd}:script"

        # Git
        if cmd == 'git':
            action = parts[1] if len(parts) > 1 else ''
            return f"git:{action}"

        # Make
        if cmd == 'make':
            target = parts[1] if len(parts) > 1 else ''
            return f"make:{target}"

        # Generic
        return f"bash:{cmd}"

    def _normalize_file_path(self, path: str, project: str) -> str:
        """
        Normalize file paths within project.

        Examples:
        - "/workspace/my_project/src/foo.ts" -> "src/foo.ts"
        - "/workspace/my_project/package.json" -> "package.json"

        IMPORTANT: Keep project name in signature context, but normalize
        paths relative to project root.
        """
        if not path:
            return "no_path"

        # Remove project-specific prefix
        project_prefix = f"/workspace/{project}/"
        if path.startswith(project_prefix):
            return path[len(project_prefix):]

        # Try without /workspace prefix (may already be relative)
        workspace_prefix = "/workspace/"
        if path.startswith(workspace_prefix):
            # Has workspace but different project - keep as is
            return path

        # Already relative or absolute elsewhere
        return path


# Export
__all__ = ['ClaudeFingerprintEngine', 'ClaudeFailureFingerprint']

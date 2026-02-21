#!/usr/bin/env python3
"""
Wrapper around Claude Code that streams events directly to Redis.

This eliminates orchestrator monitoring thread dependency by having the
container write events directly to Redis. Provides resilience during
orchestrator restarts.

Usage:
    docker-claude-wrapper.py [claude args] < prompt.txt

Environment Variables:
    REDIS_HOST: Redis hostname (default: redis)
    REDIS_PORT: Redis port (default: 6379)
    AGENT: Agent name (required)
    TASK_ID: Task ID (required)
    PROJECT: Project name (required)
    ISSUE_NUMBER: Issue number (required)
"""

import sys
import os
import subprocess
import json
import time
import signal
import atexit
from typing import Dict, Optional, List
from datetime import datetime, timezone


class ClaudeWrapper:
    def __init__(self):
        # Environment configuration
        self.redis_host = os.environ.get('REDIS_HOST', 'redis')
        self.redis_port = int(os.environ.get('REDIS_PORT', '6379'))
        self.agent = os.environ.get('AGENT', 'unknown')
        self.task_id = os.environ.get('TASK_ID', 'unknown')
        self.project = os.environ.get('PROJECT', 'unknown')
        self.issue_number = os.environ.get('ISSUE_NUMBER', 'unknown')

        # State
        self.redis_client: Optional[any] = None
        self.redis_available = False
        self.output_lines: List[str] = []
        self.max_output_size = 5 * 1024 * 1024  # 5MB limit
        self.cleanup_performed = False  # Prevent duplicate cleanup
        self.exit_code: Optional[int] = None  # Track exit code for cleanup

        # Register signal handlers and atexit
        signal.signal(signal.SIGTERM, self._handle_signal)
        signal.signal(signal.SIGINT, self._handle_signal)
        atexit.register(self._cleanup)

    def connect_redis(self) -> bool:
        """
        Connect to Redis with timeout. Returns False on failure (doesn't raise).

        Fire-and-forget pattern: If Redis is down, continue without it.
        """
        try:
            import redis
            self.redis_client = redis.Redis(
                host=self.redis_host,
                port=self.redis_port,
                socket_timeout=1.0,  # 1-second write timeout
                socket_connect_timeout=2.0,  # 2-second connect timeout
                health_check_interval=30,  # Check connection health every 30s
                decode_responses=False  # Binary mode for JSON
            )

            # Test connection
            self.redis_client.ping()
            self.redis_available = True
            self._log("✓ Connected to Redis")
            return True

        except Exception as e:
            self._log(f"⚠ Redis unavailable: {e}", level='WARNING')
            self._log("⚠ Continuing without Redis - events will be logged to stderr", level='WARNING')
            self.redis_available = False
            return False

    def _handle_signal(self, signum, frame):
        """
        Signal handler for graceful shutdown.

        Ensures final result is written before termination (SIGTERM, SIGINT).
        This handles orchestrator restarts that send SIGTERM to containers.
        """
        if not self.cleanup_performed:
            self._log(f"Received signal {signum}, writing final result before exit")
            # Use special exit code to indicate signal termination
            self.write_final_result_with_retry(exit_code=128 + signum)
            self.cleanup_performed = True
        sys.exit(128 + signum)

    def _cleanup(self):
        """
        atexit handler for graceful shutdown.

        Called automatically on normal exit to ensure final result is written.
        """
        if not self.cleanup_performed and self.exit_code is not None:
            self._log("atexit cleanup: writing final result")
            self.write_final_result_with_retry(self.exit_code)
            self.cleanup_performed = True

    def write_claude_event(self, event: Dict) -> bool:
        """
        Write Claude event to Redis Stream.

        Returns True on success, False on failure. Non-blocking - continues
        execution even if Redis write fails.

        Writes to:
        - Redis Stream: orchestrator:claude_logs_stream (for log collector)
        - Redis Pub/Sub: orchestrator:claude_stream (for real-time websocket)
        """
        if not self.redis_available:
            return False

        try:
            # Prepare event data
            event_data = {
                'agent': self.agent,
                'task_id': self.task_id,
                'project': self.project,
                'issue_number': self.issue_number,
                'timestamp': event.get('timestamp', time.time()),
                'event': event
            }

            # Write to Redis Stream (for log collector)
            self.redis_client.xadd(
                'orchestrator:claude_logs_stream',
                {'log': json.dumps(event_data)},
                maxlen=500,  # Keep last 500 events (prevents unbounded growth)
                approximate=True  # Allow approximate trimming (more efficient)
            )

            # Update TTL (2 hours)
            self.redis_client.expire('orchestrator:claude_logs_stream', 7200)

            # Publish to pub/sub (for real-time websocket updates)
            self.redis_client.publish(
                'orchestrator:claude_stream',
                json.dumps(event_data)
            )

            return True

        except Exception as e:
            self._log(f"⚠ Failed to write event to Redis: {e}", level='WARNING')
            # Don't set redis_available = False - might be transient error
            return False

    def _write_final_result_attempt(self, exit_code: int) -> bool:
        """
        Single attempt to write final result to Redis.

        Returns True on success, False on failure.
        """
        if not self.redis_available:
            return False

        try:
            # Truncate output if too large
            output = ''.join(self.output_lines)
            if len(output) > self.max_output_size:
                output = (
                    output[:self.max_output_size] +
                    f"\n\n[OUTPUT TRUNCATED - exceeded {self.max_output_size} bytes]"
                )

            result = {
                'container_name': os.environ.get('HOSTNAME', 'unknown'),
                'project': self.project,
                'issue_number': self.issue_number,
                'agent': self.agent,
                'task_id': self.task_id,
                'exit_code': exit_code,
                'output': output,
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'recovered': False
            }

            # Write with 2-hour TTL
            redis_key = f"agent_result:{self.project}:{self.issue_number}:{self.task_id}"
            self.redis_client.setex(
                redis_key,
                7200,
                json.dumps(result)
            )

            return True

        except Exception as e:
            raise Exception(f"Redis write failed: {e}")

    def write_final_result_with_retry(self, exit_code: int, max_retries: int = 3) -> bool:
        """
        Write final result with exponential backoff retry.

        Tries multiple times with exponential backoff: 1s, 2s, 4s.
        On success, logs to stderr. On failure after all retries, returns False.

        Returns: True if any attempt succeeded, False if all failed
        """
        for attempt in range(max_retries):
            try:
                if self._write_final_result_attempt(exit_code):
                    redis_key = f"agent_result:{self.project}:{self.issue_number}:{self.task_id}"
                    if attempt == 0:
                        self._log(f"✓ Wrote final result to Redis: {redis_key}")
                    else:
                        self._log(f"✓ Wrote final result to Redis on attempt {attempt + 1}: {redis_key}")
                    return True

            except Exception as e:
                if attempt < max_retries - 1:
                    delay = 2 ** attempt  # 1s, 2s, 4s
                    self._log(
                        f"⚠ Final result write failed (attempt {attempt + 1}), retrying in {delay}s: {e}",
                        level='WARNING'
                    )
                    time.sleep(delay)
                    # Try to reconnect to Redis
                    self.connect_redis()
                else:
                    self._log(
                        f"❌ Final result write failed after {max_retries} attempts: {e}",
                        level='ERROR'
                    )

        return False

    def write_fallback_result(self, exit_code: int) -> bool:
        """
        Write result to fallback storage (/tmp file).

        Orchestrator can retrieve this via 'docker cp' if Redis is unavailable.
        File is automatically cleaned up when container exits (--rm flag).

        Returns: True on success, False on failure
        """
        try:
            # Prepare result data
            output = ''.join(self.output_lines)
            if len(output) > self.max_output_size:
                output = (
                    output[:self.max_output_size] +
                    f"\n\n[OUTPUT TRUNCATED - exceeded {self.max_output_size} bytes]"
                )

            result = {
                'container_name': os.environ.get('HOSTNAME', 'unknown'),
                'project': self.project,
                'issue_number': self.issue_number,
                'agent': self.agent,
                'task_id': self.task_id,
                'exit_code': exit_code,
                'output': output,
                'completed_at': datetime.now(timezone.utc).isoformat(),
                'recovered': False,
                'storage': 'fallback_file'  # Mark as fallback storage
            }

            # Write to /tmp (container filesystem)
            result_file = f"/tmp/agent_result_{self.task_id}.json"
            with open(result_file, 'w') as f:
                json.dump(result, f, indent=2)

            self._log(f"✓ Wrote fallback result to {result_file}")
            return True

        except Exception as e:
            self._log(f"❌ Failed to write fallback result file: {e}", level='ERROR')
            return False

    def run_claude(self, claude_args: List[str]) -> int:
        """
        Run Claude Code with streaming output capture.

        Reads from stdin, streams to Claude Code, captures output and writes
        events to Redis in real-time.

        Returns Claude Code exit code.
        """
        # Start Claude Code process
        process = subprocess.Popen(
            ['claude'] + claude_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1  # Line buffered
        )

        # Read stdin and pass to Claude
        stdin_data = sys.stdin.read()

        # Start subprocess with stdin
        process.stdin.write(stdin_data)
        process.stdin.close()

        # Stream stdout and parse events
        for line in process.stdout:
            # Capture output
            self.output_lines.append(line)

            # Write to stdout (for orchestrator monitoring if available)
            print(line, end='', flush=True)

            # Try to parse as JSON event
            try:
                event = json.loads(line.strip())

                # Write to Redis
                self.write_claude_event(event)

            except json.JSONDecodeError:
                # Not JSON - plain text output
                pass

        # Capture stderr
        stderr = process.stderr.read()
        if stderr:
            self.output_lines.append(stderr)
            print(stderr, file=sys.stderr, end='', flush=True)

        # Wait for completion
        exit_code = process.wait()

        # Store exit code for atexit handler
        self.exit_code = exit_code

        # Write final result with defensive redundancy
        redis_success = False
        fallback_success = False

        # Try 1: Write to Redis with retry
        if self.write_final_result_with_retry(exit_code):
            redis_success = True

        # Try 2: Write to fallback file
        if self.write_fallback_result(exit_code):
            fallback_success = True

        # Fallback 3: Output is already in stdout/stderr (docker logs)
        # This happens automatically via print() calls during streaming

        # Validate: If Claude succeeded but we couldn't persist result anywhere, fail the container
        if exit_code == 0 and not redis_success and not fallback_success:
            self._log(
                "❌ CRITICAL: Claude succeeded but result persistence failed to both Redis and file - "
                "failing container to trigger retry",
                level='ERROR'
            )
            # Mark cleanup as performed to prevent atexit from trying again
            self.cleanup_performed = True
            return 1  # Force failure exit code

        # Mark cleanup as performed (result successfully written)
        self.cleanup_performed = True

        return exit_code

    def _log(self, message: str, level: str = 'INFO'):
        """Log to stderr (visible in docker logs)"""
        timestamp = datetime.now(timezone.utc).isoformat()
        print(f"[{timestamp}] [{level}] docker-claude-wrapper: {message}", file=sys.stderr, flush=True)


def main():
    wrapper = ClaudeWrapper()

    # Connect to Redis (fire-and-forget)
    wrapper.connect_redis()

    # Run Claude Code with provided args
    claude_args = sys.argv[1:]
    exit_code = wrapper.run_claude(claude_args)

    sys.exit(exit_code)


if __name__ == '__main__':
    main()

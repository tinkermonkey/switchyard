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
    PIPELINE_RUN_ID: Pipeline run ID (optional, included in stream events for ES queries)
"""

import sys
import os
import subprocess
import json
import time
import signal
import atexit
import threading
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
        self.pipeline_run_id = os.environ.get('PIPELINE_RUN_ID', '')

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

    def _kill_descendant_processes(self, pid: int) -> None:
        """Kill all descendant processes of pid after Claude exits.

        Prevents container stall from background bash tasks that outlive the
        Claude session — e.g. monitoring loops whose pgrep pattern self-matches
        the loop's own command string and therefore never terminate.
        """
        try:
            children = []
            for entry in os.listdir('/proc'):
                if not entry.isdigit():
                    continue
                try:
                    with open(f'/proc/{entry}/status') as f:
                        for line in f:
                            if line.startswith('PPid:'):
                                if int(line.split()[1]) == pid:
                                    children.append(int(entry))
                                break
                except OSError:
                    continue
            for child_pid in children:
                self._kill_descendant_processes(child_pid)
                try:
                    os.kill(child_pid, signal.SIGTERM)
                except OSError:
                    pass
        except Exception:
            pass

    def write_claude_event(self, event: Dict) -> bool:
        """
        Write Claude event to Redis.

        Returns True on success, False on failure. Non-blocking - continues
        execution even if Redis write fails.

        Writes to:
        - Redis Stream: orchestrator:claude_logs_stream (for log_collector → ES persistence)
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
                'pipeline_run_id': self.pipeline_run_id,
                'timestamp': event.get('timestamp', time.time()),
                'event': event
            }

            serialized = json.dumps(event_data)

            # Write to Redis Stream (persistent, consumed by log_collector → ES)
            self.redis_client.xadd(
                'orchestrator:claude_logs_stream',
                {'log': serialized},
                maxlen=50000,
                approximate=True
            )

            # Publish to pub/sub (real-time websocket updates only, no persistence)
            self.redis_client.publish(
                'orchestrator:claude_stream',
                serialized
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

    def _terminate_process_group(self, process) -> None:
        """Reap Claude and ALL its descendants via its session process group.

        Claude is started with start_new_session=True, so it leads its own
        session. Background tasks it spawned remain in that session even after
        being reparented to PID 1 when Claude exits, so a single killpg clears
        orphans that would otherwise hold the container's stdout open and stall
        it. No-ops if the group is already gone.
        """
        # start_new_session=True makes Claude its own session/group leader, so its
        # PGID equals its PID. Use that directly: os.getpgid() would fail once the
        # leader is reaped (which is exactly when we call this), but the group lives
        # on while orphaned members remain, so killpg still reaches them.
        pgid = process.pid
        try:
            os.killpg(pgid, signal.SIGTERM)
        except OSError:
            return  # group already gone
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass
        # Escalate: SIGKILL the whole group to clear stragglers that ignored
        # SIGTERM or were reparented to PID 1 but remain in the session.
        try:
            os.killpg(pgid, signal.SIGKILL)
        except OSError:
            pass
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            pass

    def run_claude(self, claude_args: List[str]) -> int:
        """
        Run Claude Code with streaming output capture.

        Reads from stdin, streams to Claude Code, captures output and writes
        events to Redis in real-time.

        Returns Claude Code exit code.
        """
        # Start Claude Code in its OWN session/process group (start_new_session=True)
        # so we can reap the entire group — including background tasks Claude leaves
        # running that get reparented to PID 1 but stay in this session — with a
        # single killpg, instead of relying on stdout reaching EOF.
        process = subprocess.Popen(
            ['claude'] + claude_args,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,  # Line buffered
            start_new_session=True,
        )

        result_seen = threading.Event()
        stderr_chunks: List[str] = []

        def _read_stdout():
            # Reads until stdout EOF. Teardown is NOT gated on this thread finishing:
            # an orphaned background task can hold stdout open long after the turn ends.
            try:
                for line in process.stdout:
                    self.output_lines.append(line)
                    print(line, end='', flush=True)
                    stripped = line.strip()
                    if not stripped:
                        continue
                    try:
                        event = json.loads(stripped)
                    except json.JSONDecodeError:
                        continue
                    self.write_claude_event(event)
                    # ANY result event marks the end of Claude's turn (session end),
                    # whether it ended cleanly or stuck/errored. Teardown keys off this
                    # so a non-clean end no longer leaves the container hanging.
                    if isinstance(event, dict) and event.get('type') == 'result':
                        result_seen.set()
            except Exception as e:
                self._log(f"stdout reader error: {e}", level='WARNING')

        def _read_stderr():
            try:
                data = process.stderr.read()
                if data:
                    stderr_chunks.append(data)
                    print(data, file=sys.stderr, end='', flush=True)
            except Exception:
                pass

        # Read stdin and pass to Claude
        stdin_data = sys.stdin.read()
        try:
            process.stdin.write(stdin_data)
            process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

        stdout_thread = threading.Thread(target=_read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        # Wait for Claude's turn to end (result event) OR the claude process to exit.
        # Deliberately NOT waiting for stdout EOF — that is exactly what an orphaned
        # background task holds open, causing the container to hang after work is done.
        _CLEANUP_GRACE_SECONDS = 8
        while not result_seen.is_set() and process.poll() is None:
            time.sleep(0.2)

        if result_seen.is_set():
            # Turn finished. Give Claude a brief moment to exit on its own, then reap
            # the whole session group (Claude + any orphaned background tasks).
            killed_by_us = False
            try:
                process.wait(timeout=_CLEANUP_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                self._log(
                    "Claude turn ended but process still alive after grace period; "
                    "terminating session group"
                )
                killed_by_us = True
            self._terminate_process_group(process)
            exit_code = process.returncode
            # The turn produced a result, so it succeeded. Normalize any signal-derived
            # exit code to 0: negative codes (process killed by an uncaught signal) AND
            # positive 128+N codes. The `claude` CLI is a shell-wrapped node launcher
            # that returns 143 (128+SIGTERM) / 137 (128+SIGKILL) when WE reap it after a
            # clean turn; without this, our own teardown would be misreported as an agent
            # failure (exit_code=143) and trigger spurious retries.
            if killed_by_us or exit_code is None or exit_code < 0 or exit_code >= 128:
                exit_code = 0
        else:
            # Claude exited without a result event. Reap any stragglers and surface
            # its real exit code (downstream validation handles empty output).
            exit_code = process.poll()
            self._terminate_process_group(process)
            if exit_code is None:
                exit_code = process.returncode
            if exit_code is None:
                exit_code = 1

        # Drain remaining buffered output. Once the session group is dead the pipes
        # close, so these joins return promptly.
        stdout_thread.join(timeout=10)
        stderr_thread.join(timeout=5)
        if stderr_chunks:
            self.output_lines.extend(stderr_chunks)

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

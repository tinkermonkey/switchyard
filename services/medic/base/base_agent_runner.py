"""
Base Investigation Agent Runner

Abstract base class for investigation agent runners.
Provides common Claude Code integration with customizable investigation prompts.
"""

import logging
import asyncio
import subprocess
from abc import ABC, abstractmethod
from typing import Optional, Dict
from pathlib import Path

logger = logging.getLogger(__name__)


class BaseInvestigationAgentRunner(ABC):
    """
    Abstract base class for investigation agent runners.

    Provides common Claude Code integration and process management.
    Subclasses customize investigation prompts and context preparation.
    """

    def __init__(self, workspace_root: str = "/workspace/clauditoreum"):
        """
        Initialize agent runner.

        Args:
            workspace_root: Path to Clauditoreum codebase
        """
        self.workspace_root = Path(workspace_root)
        logger.info(f"{self.__class__.__name__} initialized with workspace: {self.workspace_root}")

    @abstractmethod
    def _build_investigation_prompt(
        self, fingerprint_id: str, context_file: str, **kwargs
    ) -> str:
        """
        Build the investigation prompt for Claude Code.

        Args:
            fingerprint_id: Failure signature ID
            context_file: Path to context file
            **kwargs: Additional context (e.g., project for Claude failures)

        Returns:
            Prompt string to send to Claude Code
        """
        pass

    @abstractmethod
    def _get_claude_model(self) -> str:
        """
        Get the Claude model to use for investigations.

        Returns:
            Model name (e.g., "claude-sonnet-4-5-20250929")
        """
        pass

    @abstractmethod
    def _get_investigation_agent_name(self) -> str:
        """
        Get the agent name for observability.

        Returns:
            Agent name (e.g., "medic-investigator" or "claude-medic-investigator")
        """
        pass

    def get_claude_version(self) -> Optional[str]:
        """
        Check Claude Code CLI availability and return version.

        Returns:
            Version string or None if not available
        """
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0:
                version = result.stdout.strip()
                logger.info(f"Claude Code CLI version: {version}")
                return version
            return None
        except Exception as e:
            logger.error(f"Claude Code CLI not available: {e}")
            return None

    async def launch_investigation(
        self,
        fingerprint_id: str,
        context_file: str,
        output_log: str,
        observability_manager=None,
        agent_execution_id: Optional[str] = None,
        **kwargs
    ) -> Optional[Dict]:
        """
        Launch Claude Code investigation in isolated Docker container.

        Args:
            fingerprint_id: Failure signature ID
            context_file: Path to context.json
            output_log: Path to investigation_log.txt
            observability_manager: Optional observability manager for events
            agent_execution_id: Optional execution ID for tracking in UI
            **kwargs: Additional context (e.g., project for Claude failures)

        Returns:
            Dict with 'task' (asyncio task), 'container_name', 'agent_execution_id', or None if launch failed
        """
        try:
            # Build investigation prompt (subclass-specific)
            prompt = self._build_investigation_prompt(
                fingerprint_id, context_file, **kwargs
            )

            # Import docker_runner
            from claude.docker_runner import docker_runner

            # Build context for docker_runner
            context = {
                'agent': self._get_investigation_agent_name(),
                'task_id': fingerprint_id[:16],  # Short ID for container name
                'project': 'clauditoreum',  # Investigating orchestrator logs
                'observability': observability_manager,
                'claude_model': self._get_claude_model(),
                'agent_execution_id': agent_execution_id,  # For UI tracking
            }

            # Container name for tracking
            container_name = f"claude-agent-clauditoreum-{fingerprint_id[:16]}"

            logger.info(f"Launching investigation for {fingerprint_id} in container {container_name}")

            # Create output file writer for streaming to log file
            logger.info(f"Opening log file: {output_log}")
            try:
                log_file_handle = open(output_log, "w")
                logger.info(f"Log file opened successfully")
            except Exception as e:
                logger.error(f"Failed to open log file {output_log}: {e}", exc_info=True)
                return None

            def stream_to_file_and_redis(event):
                """Stream Claude Code events to log file AND Redis (same as pipeline agents)"""
                import json
                import time
                try:
                    # Write to local log file
                    log_file_handle.write(json.dumps(event) + "\n")
                    log_file_handle.flush()

                    # Publish to Redis using EXACT same format as pipeline agents
                    if observability_manager and observability_manager.enabled:
                        event_data = {
                            'agent': context['agent'],
                            'task_id': context['task_id'],
                            'project': context['project'],
                            'pipeline_run_id': None,  # Investigations don't have pipeline_run_id
                            'timestamp': event.get('timestamp') or time.time(),
                            'event': event
                        }
                        event_json = json.dumps(event_data)

                        # Publish to pub/sub for real-time delivery
                        observability_manager.redis.publish('orchestrator:claude_stream', event_json)

                        # Also add to Redis Stream for history (with automatic trimming)
                        claude_stream_key = "orchestrator:claude_logs_stream"
                        observability_manager.redis.xadd(
                            claude_stream_key,
                            {'log': event_json},
                            maxlen=500,
                            approximate=True
                        )

                        # Set 2-hour TTL on the stream
                        observability_manager.redis.expire(claude_stream_key, 7200)
                except Exception as e:
                    logger.error(f"Error in stream callback: {e}")

            # Launch investigation as async task
            async def run_investigation():
                import sys
                print(f"[ASYNC TASK STARTED - STDOUT] {fingerprint_id[:16]}", file=sys.stdout, flush=True)
                logger.info(f"[ASYNC TASK START] run_investigation() called for {fingerprint_id}")
                print(f"[ASYNC TASK LOGGED] {fingerprint_id[:16]}", file=sys.stdout, flush=True)
                try:
                    logger.info(f"Starting run_investigation task for {fingerprint_id}")
                    # Run investigation in isolated container with full authentication
                    logger.info(f"Calling docker_runner.run_agent_in_container for {fingerprint_id}")
                    result = await docker_runner.run_agent_in_container(
                        prompt=prompt,
                        context=context,
                        project_dir=Path('/workspace/clauditoreum'),
                        mcp_servers=[],  # No MCP needed for investigations
                        stream_callback=stream_to_file_and_redis
                    )
                    logger.info(f"Investigation {fingerprint_id} completed in container, result length: {len(result)}")
                    return result
                except Exception as e:
                    logger.error(f"Investigation {fingerprint_id} failed: {e}", exc_info=True)
                    raise
                finally:
                    log_file_handle.close()

            # Start the task but don't await it (caller will monitor it)
            logger.info(f"Creating async task for {fingerprint_id}")
            task = asyncio.create_task(run_investigation(), name=f"investigation-{fingerprint_id[:16]}")
            logger.info(f"Task created successfully: {task}")
            logger.info(f"Task state immediately: done={task.done()}, cancelled={task.cancelled()}")

            result = {
                'task': task,
                'container_name': container_name,  # Track container for monitoring
                'log_file': log_file_handle,
                'agent_execution_id': agent_execution_id,  # For UI tracking
            }

            logger.info(f"Returning investigation_info for {fingerprint_id} with container {container_name}, execution_id={agent_execution_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to launch investigation for {fingerprint_id}: {e}", exc_info=True)
            return None

    async def kill_investigation(self, investigation_info: Dict) -> bool:
        """
        Cancel a running investigation task and kill its container.

        Args:
            investigation_info: Dict with 'task' and 'container_name' keys

        Returns:
            True if killed successfully
        """
        try:
            killed = False

            # Kill the Docker container first
            container_name = investigation_info.get('container_name')
            if container_name:
                try:
                    result = subprocess.run(
                        ['docker', 'kill', container_name],
                        capture_output=True,
                        text=True,
                        timeout=10
                    )
                    if result.returncode == 0:
                        logger.info(f"Killed container {container_name}")
                        killed = True
                except Exception as e:
                    logger.warning(f"Failed to kill container {container_name}: {e}")

            # Cancel the async task
            task = investigation_info.get('task')
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info("Cancelled investigation task")
                killed = True

            return killed
        except Exception as e:
            logger.error(f"Failed to kill investigation: {e}")
            return False

    def is_investigation_running(self, investigation_info: Dict) -> bool:
        """
        Check if investigation is still running.

        Args:
            investigation_info: Dict with 'task' key

        Returns:
            True if running
        """
        task = investigation_info.get('task')
        if task:
            return not task.done()
        return False

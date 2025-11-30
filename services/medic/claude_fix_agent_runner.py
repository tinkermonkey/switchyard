"""
Claude Fix Agent Runner

Launches Claude Code as a host process (outside Docker) to execute fixes for Claude tool failures.
"""

import logging
import asyncio
import os
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ClaudeFixAgentRunner:
    """
    Launches and manages Claude Code fix execution processes.

    Runs Claude Code on the HOST (not in Docker) so it can:
    - Access project codebases
    - Run docker commands (via safe_restart.py)
    - Read/write fix plans
    """

    def __init__(self, workspace_root: str = "/workspace/clauditoreum"):
        """
        Initialize Claude fix agent runner.

        Args:
            workspace_root: Path to Clauditoreum codebase (orchestrator)
        """
        self.workspace_root = Path(workspace_root)
        logger.info(f"ClaudeFixAgentRunner initialized with workspace: {self.workspace_root}")

    def get_claude_version(self) -> Optional[str]:
        """
        Check Claude Code CLI availability and return version.

        Returns:
            Version string or None if not available
        """
        import subprocess
        try:
            result = subprocess.run(
                ["claude", "--version"],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return None
        except Exception as e:
            logger.warning(f"Claude Code CLI not available: {e}")
            return None

    async def launch_fix(
        self,
        fingerprint_id: str,
        fix_plan_file: str,
        output_log: str,
        project: str,
        observability_manager=None
    ) -> Optional[dict]:
        """
        Launch Claude Code fix execution process.

        Args:
            fingerprint_id: Failure signature ID
            fix_plan_file: Path to fix_plan.md
            output_log: Path to fix_log.txt
            project: Project name
            observability_manager: Optional observability manager for events

        Returns:
            Dict with 'task' (asyncio task), or None if launch failed
        """
        try:
            # Build fix prompt
            prompt = self._build_fix_prompt(fingerprint_id, fix_plan_file, project)

            # Build context for run_claude_code
            try:
                from claude.claude_integration import run_claude_code
                logger.info(f"Successfully imported run_claude_code for {fingerprint_id}")
            except Exception as e:
                logger.error(f"Failed to import run_claude_code: {e}", exc_info=True)
                return None

            context = {
                'agent': 'claude-fix-executor',
                'task_id': f"fix-{fingerprint_id}",
                'project': 'clauditoreum',  # Fixer runs from orchestrator context
                'work_dir': str(self.workspace_root),
                'use_docker': False,  # Run on host to access Docker CLI and projects
                'observability': observability_manager,
                'claude_model': 'claude-sonnet-4-5-20250929',
            }

            logger.info(f"Launching Claude fix for {fingerprint_id} (project: {project})")

            # Create output file writer for streaming to log file
            log_file_handle = open(output_log, "w")

            def stream_to_file(event):
                """Stream Claude Code events to log file"""
                import json
                try:
                    log_file_handle.write(json.dumps(event) + "\n")
                    log_file_handle.flush()
                except Exception as e:
                    logger.error(f"Error writing to log file: {e}")

            context['stream_callback'] = stream_to_file

            # Launch fix as async task
            async def run_fix():
                logger.info(f"run_fix() started for {fingerprint_id}")
                try:
                    logger.info(f"Calling run_claude_code for {fingerprint_id}")
                    result = await run_claude_code(prompt, context)
                    logger.info(f"Claude fix {fingerprint_id} completed, result length: {len(result)}")
                    return result
                except Exception as e:
                    logger.error(f"Claude fix {fingerprint_id} failed: {e}", exc_info=True)
                    raise
                finally:
                    log_file_handle.close()

            # Start the task but don't await it (caller will monitor it)
            task = asyncio.create_task(run_fix())

            # Yield control to event loop to allow task to start
            await asyncio.sleep(0)

            result = {
                'task': task,
                'pid': None,
                'log_file': log_file_handle,
            }

            logger.info(f"Claude fix task created for {fingerprint_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to launch Claude fix for {fingerprint_id}: {e}", exc_info=True)
            return None

    def _build_fix_prompt(self, fingerprint_id: str, fix_plan_file: str, project: str) -> str:
        """
        Build the fix execution prompt for Claude Code.

        Returns:
            Prompt string to send to Claude Code
        """
        prompt = f"""# Claude Medic Fix Execution

You are the Fix Execution Agent. Your task is to apply the fix plan for a failure signature.

**Failure Signature ID**: {fingerprint_id}
**Project**: {project}
**Fix Plan File**: {fix_plan_file}

## Instructions

1. **Read the Fix Plan**:
   ```bash
   cat {fix_plan_file}
   ```

2. **Apply the Fix**:
   - Follow the steps in the fix plan carefully.
   - Edit files as necessary.
   - Run tests if specified in the plan to verify the fix.

3. **Handle Container Restarts Safely**:
   - If the fix plan requires restarting a Docker container (e.g., to apply configuration changes), **DO NOT** use `docker restart` directly.
   - Instead, use the safe restart script:
     ```bash
     python3 /app/scripts/safe_restart.py <container_name_or_pattern>
     ```
   - This script ensures that no active repair cycles or investigations are interrupted.
   - If the script fails (e.g., due to safety checks), report this in your final summary.

4. **Verify**:
   - Verify that the changes have been applied correctly.

5. **Report**:
   - Provide a summary of the actions taken.
   - State clearly if the fix was successful or if there were issues.

Begin execution now!
"""
        return prompt

    async def kill_fix(self, fix_info: dict) -> bool:
        """
        Cancel a running fix task.

        Args:
            fix_info: Dict with 'task' key

        Returns:
            True if killed successfully
        """
        try:
            task = fix_info.get('task')
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info("Cancelled Claude fix task")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill Claude fix: {e}")
            return False

    def is_fix_running(self, fix_info: dict) -> bool:
        """
        Check if fix is still running.

        Args:
            fix_info: Dict with 'task' key

        Returns:
            True if running
        """
        task = fix_info.get('task')
        if task:
            return not task.done()
        return False


# Export
__all__ = ['ClaudeFixAgentRunner']

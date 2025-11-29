"""
Claude Investigation Agent Runner

Launches Claude Code as a host process (outside Docker) to investigate Claude tool execution failures.
"""

import logging
import asyncio
import os
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class ClaudeInvestigationAgentRunner:
    """
    Launches and manages Claude Code investigation processes for Claude tool failures.

    Runs Claude Code on the HOST (not in Docker) so it can:
    - Access project codebases
    - Query Elasticsearch for claude-streams-* data
    - Read/write investigation reports to /medic/claude/
    """

    def __init__(self, workspace_root: str = "/workspace/clauditoreum"):
        """
        Initialize Claude agent runner.

        Args:
            workspace_root: Path to Clauditoreum codebase (orchestrator)
        """
        self.workspace_root = Path(workspace_root)
        self.instructions_file = self.workspace_root / "services/medic/claude_investigator_instructions.md"
        logger.info(f"ClaudeInvestigationAgentRunner initialized with workspace: {self.workspace_root}")

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

    async def launch_investigation(
        self,
        fingerprint_id: str,
        context_file: str,
        output_log: str,
        project: str,
        observability_manager=None
    ) -> Optional[dict]:
        """
        Launch Claude Code investigation process for Claude tool failure.

        Args:
            fingerprint_id: Failure signature ID
            context_file: Path to context.json
            output_log: Path to investigation_log.txt
            project: Project name (for context)
            observability_manager: Optional observability manager for events

        Returns:
            Dict with 'task' (asyncio task), or None if launch failed
        """
        try:
            # Build investigation prompt
            prompt = self._build_investigation_prompt(fingerprint_id, context_file, project)

            # Build context for run_claude_code
            from claude.claude_integration import run_claude_code

            context = {
                'agent': 'claude-medic-investigator',
                'task_id': fingerprint_id,
                'project': 'clauditoreum',  # Investigator runs from orchestrator context
                'work_dir': str(self.workspace_root),
                'use_docker': False,  # CRITICAL: Run on host to access Elasticsearch and projects
                'observability': observability_manager,
                'claude_model': 'claude-sonnet-4-5-20250929',
            }

            logger.info(f"Launching Claude investigation for {fingerprint_id} (project: {project})")

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

            # Launch investigation as async task
            async def run_investigation():
                try:
                    result = await run_claude_code(prompt, context)
                    logger.info(f"Claude investigation {fingerprint_id} completed, result length: {len(result)}")
                    return result
                except Exception as e:
                    logger.error(f"Claude investigation {fingerprint_id} failed: {e}", exc_info=True)
                    raise
                finally:
                    log_file_handle.close()

            # Start the task but don't await it (caller will monitor it)
            task = asyncio.create_task(run_investigation())

            result = {
                'task': task,
                'pid': None,  # No direct PID with run_claude_code
                'log_file': log_file_handle,
            }

            logger.info(f"Claude investigation task created for {fingerprint_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to launch Claude investigation for {fingerprint_id}: {e}", exc_info=True)
            return None

    def _build_investigation_prompt(self, fingerprint_id: str, context_file: str, project: str) -> str:
        """
        Build the investigation prompt for Claude Code.

        Returns:
            Prompt string to send to Claude Code
        """
        # Load instructions from markdown file
        instructions = ""
        if self.instructions_file.exists():
            with open(self.instructions_file, "r") as f:
                instructions = f.read()
        else:
            logger.warning(f"Instructions file not found: {self.instructions_file}")

        prompt = f"""# Claude Medic Investigation

You are investigating a Claude Code tool execution failure signature.

**Failure Signature ID**: {fingerprint_id}
**Project**: {project}
**Context File**: {context_file}

## Investigation Instructions

{instructions}

## Environment Setup

The following environment variables are set for your investigation:

- `MEDIC_FINGERPRINT_ID`: {fingerprint_id}
- `MEDIC_CONTEXT_FILE`: {context_file}
- `MEDIC_PROJECT`: {project}

## Quick Start

1. **Read the context file** to understand the failure pattern:
   ```bash
   cat {context_file}
   ```

2. **Navigate to the project** to examine its structure:
   ```bash
   cd /workspace/{project}/
   ls -la
   ```

3. **Check Claude Code configuration**:
   ```bash
   cat /workspace/{project}/.claude/CLAUDE.md
   ls /workspace/{project}/.claude/agents/
   ls /workspace/{project}/.claude/skills/
   ```

4. **Query Elasticsearch** for failure details:
   ```bash
   curl -s "http://elasticsearch:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{{
     "query": {{
       "bool": {{
         "must": [
           {{"term": {{"project": "{project}"}}}},
           {{"term": {{"event_category": "tool_result"}}}},
           {{"term": {{"success": false}}}}
         ]
       }}
     }},
     "size": 20,
     "sort": [{{"timestamp": "desc"}}]
   }}' | python3 -m json.tool
   ```

5. **Create your reports** in `/medic/claude/{fingerprint_id}/`:
   - **diagnosis.md** + **fix_plan.md** (for actionable issues)
   - OR **ignored.md** (for non-actionable issues)

## Your Goal

Create specific, actionable recommendations to help the Claude Code agent succeed in this project. Focus on:

1. **CLAUDE.md improvements**: Add guidance, best practices, project structure
2. **Sub-agent creation**: Specialized agents for complex workflows
3. **Skill development**: Reusable patterns for common tasks
4. **Environment fixes**: Dockerfile.agent dependencies

Remember: You're helping the AI agent work better, not just fixing the immediate error.

Begin your investigation now!
"""
        return prompt

    async def kill_investigation(self, investigation_info: dict) -> bool:
        """
        Cancel a running investigation task.

        Args:
            investigation_info: Dict with 'task' key (from launch_investigation)

        Returns:
            True if killed successfully
        """
        try:
            task = investigation_info.get('task')
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                logger.info("Cancelled Claude investigation task")
                return True
            return False
        except Exception as e:
            logger.error(f"Failed to kill Claude investigation: {e}")
            return False

    def is_investigation_running(self, investigation_info: dict) -> bool:
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


# Export
__all__ = ['ClaudeInvestigationAgentRunner']

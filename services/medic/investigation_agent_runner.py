"""
Investigation Agent Runner

Launches Claude Code as a host process (outside Docker) to investigate failures.
"""

import logging
import asyncio
import subprocess
import os
import signal
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)


class InvestigationAgentRunner:
    """
    Launches and manages Claude Code investigation processes.

    Runs Claude Code on the HOST (not in Docker) so it can:
    - Access Docker logs via `docker logs` command
    - Read Clauditoreum codebase
    - Access Elasticsearch for historical data
    """

    def __init__(self, workspace_root: str = "/workspace/clauditoreum"):
        """
        Initialize agent runner.

        Args:
            workspace_root: Path to Clauditoreum codebase
        """
        self.workspace_root = Path(workspace_root)
        logger.info(f"InvestigationAgentRunner initialized with workspace: {self.workspace_root}")

    async def launch_investigation(
        self, fingerprint_id: str, context_file: str, output_log: str, observability_manager=None
    ) -> Optional[dict]:
        """
        Launch Claude Code investigation process using run_claude_code().

        Args:
            fingerprint_id: Failure signature ID
            context_file: Path to context.json
            output_log: Path to investigation_log.txt
            observability_manager: Optional observability manager for events

        Returns:
            Dict with 'pid' (for compatibility) and 'task' (asyncio task), or None if launch failed
        """
        try:
            # Build investigation prompt
            prompt = self._build_investigation_prompt(fingerprint_id, context_file)

            # Build context for run_claude_code
            # This will run LOCALLY on the host (not in Docker) so it can access docker logs
            from claude.claude_integration import run_claude_code

            context = {
                'agent': 'medic-investigator',
                'task_id': fingerprint_id,
                'project': 'clauditoreum',  # Medic monitors the orchestrator itself
                'work_dir': str(self.workspace_root),
                'use_docker': False,  # CRITICAL: Run on host to access docker logs
                'observability': observability_manager,
                'claude_model': 'claude-sonnet-4-5-20250929',
            }

            logger.info(f"Launching investigation for {fingerprint_id} using run_claude_code()")

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
                    logger.info(f"Investigation {fingerprint_id} completed, result length: {len(result)}")
                    return result
                except Exception as e:
                    logger.error(f"Investigation {fingerprint_id} failed: {e}", exc_info=True)
                    raise
                finally:
                    log_file_handle.close()

            # Start the task but don't await it (caller will monitor it)
            task = asyncio.create_task(run_investigation())

            # Get the task's underlying process info (for compatibility with monitoring)
            # Note: Since we're using run_claude_code, we don't have direct PID access
            # We'll use the task object for monitoring instead
            result = {
                'task': task,
                'pid': None,  # No direct PID with run_claude_code
                'log_file': log_file_handle,
            }

            logger.info(f"Investigation task created for {fingerprint_id}")
            return result

        except Exception as e:
            logger.error(f"Failed to launch investigation for {fingerprint_id}: {e}", exc_info=True)
            return None

    def _build_investigation_prompt(self, fingerprint_id: str, context_file: str) -> str:
        """
        Build the investigation prompt for Claude Code.

        Returns:
            Prompt string to send to Claude Code
        """
        prompt = f"""# Medic Investigation

You are investigating failure signature: {fingerprint_id}

## Your Task

1. Read the context file: {context_file}
2. Analyze the error pattern and sample log entries
3. Access Docker container logs for additional context using:
   ```bash
   docker logs clauditoreum-orchestrator-1 --since 24h --tail 1000
   docker logs clauditoreum-observability-server-1 --since 24h --tail 1000
   ```
4. Examine the Clauditoreum codebase to identify root cause
5. Create investigation reports in /medic/{fingerprint_id}/

## Required Outputs

You MUST create ONE of the following outcomes:

### Option A: Actionable Issue (create both files)
- **diagnosis.md**: Root cause analysis with evidence
- **fix_plan.md**: Proposed solution with implementation steps

### Option B: Non-Actionable Issue (create single file)
- **ignored.md**: Explanation of why this is not actionable

## Available Tools

- Read files from the Clauditoreum codebase at /workspace/clauditoreum
- Execute bash commands:
  - `docker logs <container>` to access container logs
  - `grep`, `find`, `cat` for log analysis
- Access to the filesystem to save reports

## Report Templates

### diagnosis.md
```markdown
# Root Cause Diagnosis

**Failure Signature:** `{fingerprint_id}`
**Investigation Date:** [today's date]

## Error Summary
[Brief 1-2 sentence summary]

## Root Cause Analysis
[Detailed explanation of what's causing the error]

## Evidence
### Log Analysis
[Relevant log excerpts]

### Code Analysis
[Code sections that are problematic]

### System State
[Any relevant system state information]

## Impact Assessment
- Severity: High/Medium/Low
- Frequency: [N per day/hour]
- Affected Components: [list]
```

### fix_plan.md
```markdown
# Fix Plan

**Failure Signature:** `{fingerprint_id}`

## Proposed Solution
[High-level description of the fix]

## Implementation Steps
1. [Step 1]
2. [Step 2]
3. [etc.]

## Code Changes Required
### File: [path/to/file.py]
```python
# Before
[current code]

# After
[proposed code]
```

## Testing Strategy
[How to verify the fix works]

## Risks and Considerations
[Any risks or side effects]

## Deployment Plan
[How to safely deploy this fix]
```

### ignored.md
```markdown
# Investigation Outcome: Ignored

**Failure Signature:** `{fingerprint_id}`

## Reason for Ignoring
[Explanation - e.g., external service issue, expected behavior, etc.]

## Recommendation
[Any recommendations even if not fixing - e.g., add monitoring, update docs]
```

## Important Guidelines

- Be thorough but concise
- Focus on ROOT CAUSE, not just symptoms
- Provide EVIDENCE for your conclusions
- Make fix plans ACTIONABLE with specific steps
- Use REAL log data and code from the codebase
- If you determine the issue is not actionable (e.g., external service, expected behavior), create ignored.md instead

Begin your investigation now.
"""
        return prompt

    def check_process(self, pid: int) -> bool:
        """
        Check if process is still running.

        Args:
            pid: Process ID

        Returns:
            True if running, False otherwise
        """
        try:
            # Send signal 0 (doesn't actually signal, just checks if process exists)
            os.kill(pid, 0)
            return True
        except OSError:
            return False

    def terminate_process(self, pid: int, timeout: int = 30) -> bool:
        """
        Terminate investigation process gracefully, then forcefully if needed.

        Args:
            pid: Process ID
            timeout: Seconds to wait before SIGKILL

        Returns:
            True if terminated successfully
        """
        if not self.check_process(pid):
            logger.info(f"Process {pid} already terminated")
            return True

        try:
            # Try graceful termination first (SIGTERM to process group)
            logger.info(f"Sending SIGTERM to process group {pid}")
            os.killpg(pid, signal.SIGTERM)

            # Wait for process to exit
            import time
            for _ in range(timeout):
                if not self.check_process(pid):
                    logger.info(f"Process {pid} terminated gracefully")
                    return True
                time.sleep(1)

            # Force kill if still running
            logger.warning(f"Process {pid} did not terminate, sending SIGKILL")
            os.killpg(pid, signal.SIGKILL)
            time.sleep(2)

            if not self.check_process(pid):
                logger.info(f"Process {pid} killed forcefully")
                return True

            logger.error(f"Failed to kill process {pid}")
            return False

        except Exception as e:
            logger.error(f"Error terminating process {pid}: {e}", exc_info=True)
            return False

    def get_claude_version(self) -> Optional[str]:
        """
        Check if Claude Code CLI is available and get version.

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

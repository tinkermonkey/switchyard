"""
Docker Investigation Agent Runner

Launches Claude Code to investigate Docker container log failures.
"""

import logging
from typing import Optional

from services.medic.base import BaseInvestigationAgentRunner

logger = logging.getLogger(__name__)


class DockerInvestigationAgentRunner(BaseInvestigationAgentRunner):
    """
    Docker-specific investigation agent runner.

    Launches Claude Code to investigate failure signatures from Docker logs.
    Runs on the host to access docker logs command.
    """

    def _build_investigation_prompt(
        self, fingerprint_id: str, context_file: str, **kwargs
    ) -> str:
        """
        Build the investigation prompt for Docker failures.

        Args:
            fingerprint_id: Failure signature ID
            context_file: Path to context file
            **kwargs: Additional context (unused for Docker)

        Returns:
            Prompt string to send to Claude Code
        """
        prompt = f"""# Medic Investigation

You are investigating failure signature: {fingerprint_id}

You are running in an isolated Docker container with access to:
- **Orchestrator codebase**: /workspace/clauditoreum/ (full read access)
- **Docker network**: Can access elasticsearch:9200, redis:6379
- **Docker CLI**: Can query container logs via `docker logs`
- **Report output**: Write to /medic/{fingerprint_id}/

## Your Task

1. Read the context file: {context_file}
2. Analyze the error pattern and sample log entries
3. Access Docker container logs for additional context using:
   ```bash
   docker logs clauditoreum-orchestrator-1 --since 24h --tail 1000
   docker logs clauditoreum-observability-server-1 --since 24h --tail 1000
   ```
4. Search and examine the Clauditoreum codebase at /workspace/clauditoreum/ to identify root cause
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

    def _get_claude_model(self) -> str:
        """Get the Claude model to use for Docker investigations."""
        return "claude-sonnet-4-5-20250929"

    def _get_investigation_agent_name(self) -> str:
        """Get the agent name for observability."""
        return "medic-investigator"

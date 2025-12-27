"""
Claude Investigation Agent Runner

Launches Claude Code to investigate Claude Code tool execution failures.
"""

import logging
from pathlib import Path
from typing import Optional

from services.medic.base import BaseInvestigationAgentRunner

logger = logging.getLogger(__name__)


class ClaudeInvestigationAgentRunner(BaseInvestigationAgentRunner):
    """
    Claude-specific investigation agent runner.

    Launches Claude Code to investigate failure signatures from Claude tool execution failures.
    Runs on the host to access Elasticsearch and project workspaces.
    """

    def __init__(self, workspace_root: str = "/workspace/clauditoreum"):
        """
        Initialize Claude agent runner.

        Args:
            workspace_root: Path to Clauditoreum codebase
        """
        super().__init__(workspace_root)
        self.instructions_file = Path(workspace_root) / "services/medic/claude_investigator_instructions.md"
        logger.info("ClaudeInvestigationAgentRunner initialized")

    def _build_investigation_prompt(
        self, fingerprint_id: str, context_file: str, **kwargs
    ) -> str:
        """
        Build the investigation prompt for Claude failures.

        Args:
            fingerprint_id: Failure signature ID
            context_file: Path to context file
            **kwargs: Additional context (project, etc.)

        Returns:
            Prompt string to send to Claude Code
        """
        project = kwargs.get("project", "unknown")

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

You are running in an isolated Docker container with access to:
- **Orchestrator codebase**: /workspace/clauditoreum/ (full read access)
- **Project workspace**: /workspace/{project}/ (if cloned)
- **Elasticsearch**: http://elasticsearch:9200 (query claude-streams-* indices)
- **Redis**: redis:6379
- **Report output**: Write to /medic/claude/{fingerprint_id}/

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

    def _get_claude_model(self) -> str:
        """Get the Claude model to use for Claude investigations."""
        return "claude-sonnet-4-5-20250929"

    def _get_investigation_agent_name(self) -> str:
        """Get the agent name for observability."""
        return "medic-investigator"

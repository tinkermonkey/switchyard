"""
Claude Advisor Agent Runner

Runs the Claude Code Advisor agent to analyze project failures and recommend improvements.
"""

import logging
import os
import json
import asyncio
from typing import Dict, List, Optional
from pathlib import Path
from datetime import datetime

from claude.claude_integration import run_claude_code
from monitoring.observability import ObservabilityManager

logger = logging.getLogger(__name__)


class ClaudeAdvisorAgentRunner:
    """
    Runs the Claude Advisor agent.
    """

    def __init__(self, workspace_root: str = "/workspace/clauditoreum"):
        self.workspace_root = workspace_root
        self.instructions_path = Path(__file__).parent / "claude_advisor_instructions.md"

    async def run_advisor(
        self,
        project: str,
        failures: List[Dict],
        output_path: str,
        observability_manager: Optional[ObservabilityManager] = None
    ) -> Optional[Dict]:
        """
        Run the advisor agent for a project.

        Args:
            project: Project name
            failures: List of failure signature documents
            output_path: Path to save the report
            observability_manager: Optional observability manager

        Returns:
            Dict with execution info or None if failed
        """
        try:
            # Read instructions
            if not self.instructions_path.exists():
                logger.error(f"Instructions file not found: {self.instructions_path}")
                return None
                
            with open(self.instructions_path, "r") as f:
                instructions = f.read()

            # Prepare context data
            context_data = {
                "project": project,
                "failures": failures,
                "analysis_date": datetime.utcnow().isoformat()
            }
            
            # Format failures for the prompt
            failures_text = self._format_failures(failures)
            
            # Build prompt
            prompt = f"""
{instructions}

## Project Context
Project: {project}
Date: {datetime.utcnow().strftime('%Y-%m-%d')}

## Identified Failure Signatures
Here are the failure signatures identified for this project:

{failures_text}

Please analyze these failures and generate your recommendations report.
"""

            logger.info(f"Launching Claude Advisor for project {project} with {len(failures)} failures")

            # Run Claude
            # We use a specialized task ID for tracking
            task_id = f"advisor-{project}-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}"
            
            result = await run_claude_code(
                prompt=prompt,
                context={
                    "project": project,
                    "task_id": task_id,
                    "agent": "claude_advisor",
                    "workspace_root": self.workspace_root
                }
            )
            
            # Process result
            report_content = ""
            if isinstance(result, dict):
                report_content = result.get('result', '')
            else:
                report_content = str(result)
                
            # Save report
            os.makedirs(os.path.dirname(output_path), exist_ok=True)
            with open(output_path, "w") as f:
                f.write(report_content)
                
            logger.info(f"Advisor report saved to {output_path}")
            
            return {
                "task_id": task_id,
                "project": project,
                "report_path": output_path,
                "timestamp": datetime.utcnow().isoformat()
            }

        except Exception as e:
            logger.error(f"Failed to run advisor for {project}: {e}", exc_info=True)
            return None

    def _format_failures(self, failures: List[Dict]) -> str:
        """Format failure signatures for the prompt"""
        if not failures:
            return "No failures found."
            
        lines = []
        for f in failures:
            sig = f.get('signature', {})
            lines.append(f"### Signature ID: {f.get('fingerprint_id')}")
            lines.append(f"- **Tool**: {sig.get('tool_name')}")
            lines.append(f"- **Error**: {sig.get('error_type')}")
            lines.append(f"- **Pattern**: {sig.get('error_pattern')}")
            lines.append(f"- **Context**: {sig.get('context_signature')}")
            lines.append(f"- **Total Failures**: {f.get('total_failures')}")
            lines.append(f"- **Impact Score**: {f.get('impact_score')}")
            lines.append(f"- **Status**: {f.get('status')}")
            lines.append("")
            
        return "\n".join(lines)

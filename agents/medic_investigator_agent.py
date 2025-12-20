"""
Medic Investigator Agent

Investigates recurring failure patterns and diagnoses root causes.
"""

from typing import List, Dict, Any, Optional
from agents.base_maker_agent import MakerAgent


class MedicInvestigatorAgent(MakerAgent):
    """
    Agent that investigates recurring failure patterns.

    Takes a failure signature with error patterns and sample logs,
    investigates the root cause by analyzing the codebase and logs,
    and produces a diagnostic report with recommendations.
    """

    @property
    def agent_display_name(self) -> str:
        return "Medic Investigator"

    @property
    def agent_role_description(self) -> str:
        return "Diagnoses root causes of recurring failures"

    @property
    def output_sections(self) -> List[str]:
        return [
            "root_cause_analysis",
            "affected_components",
            "recommended_fix",
            "prevention_strategy"
        ]

    def build_prompt(
        self,
        task_context: Dict[str, Any],
        previous_output: Optional[Dict[str, str]] = None,
        review_feedback: Optional[str] = None,
    ) -> str:
        """
        Build investigation prompt from failure signature.

        Args:
            task_context: Contains 'failure_signature' with error patterns and sample logs
            previous_output: Previous investigation output (for revisions)
            review_feedback: Feedback from reviewer (for revisions)

        Returns:
            Formatted investigation prompt
        """
        failure_signature = task_context.get("failure_signature", {})
        fingerprint_id = failure_signature.get("fingerprint_id", "unknown")
        error_pattern = failure_signature.get("error_pattern", "")
        sample_logs = failure_signature.get("sample_logs", [])
        occurrence_count = failure_signature.get("occurrence_count", 0)
        first_seen = failure_signature.get("first_seen", "")
        last_seen = failure_signature.get("last_seen", "")

        prompt = f"""# Failure Investigation

You are investigating a recurring failure pattern in the Claude Code orchestrator.

## Failure Signature

**Fingerprint ID**: `{fingerprint_id}`
**Error Pattern**: {error_pattern}
**Occurrences**: {occurrence_count}
**First Seen**: {first_seen}
**Last Seen**: {last_seen}

## Sample Log Entries

"""
        for i, log_entry in enumerate(sample_logs[:5], 1):
            prompt += f"### Sample {i}\n```\n{log_entry}\n```\n\n"

        prompt += """
## Your Task

Investigate this failure pattern and provide a comprehensive diagnosis:

1. **Root Cause Analysis**:
   - What is the underlying cause of this failure?
   - Is it a code bug, configuration issue, race condition, resource exhaustion, or external dependency failure?

2. **Affected Components**:
   - Which files, classes, and functions are involved?
   - Provide specific file paths and line numbers where relevant

3. **Recommended Fix**:
   - What code changes are needed to fix this issue?
   - Include specific implementation suggestions

4. **Prevention Strategy**:
   - How can we prevent this class of failure in the future?
   - What tests, monitoring, or safeguards should be added?

## Available Tools

You have access to:
- The orchestrator codebase at `/workspace/clauditoreum/`
- Elasticsearch at `http://elasticsearch:9200` (query for related failures)
- File search, grep, and read tools
- Docker logs access for recent executions

## Output Format

Provide your investigation in the following sections:

### Root Cause Analysis
[Detailed analysis of what's causing the failure]

### Affected Components
- `/path/to/file.py:123` - Description of involvement
- ...

### Recommended Fix
```python
# Specific code changes needed
```

### Prevention Strategy
- [Preventive measures]
- [Tests to add]
- [Monitoring improvements]
"""

        if review_feedback:
            prompt += f"""
## Revision Request

The previous investigation has been reviewed with the following feedback:

{review_feedback}

Please revise your investigation to address this feedback.
"""

        return prompt

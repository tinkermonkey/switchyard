from typing import Dict, Any
from pipeline.base import PipelineStage
from claude.claude_integration import run_claude_code
from services.dev_container_state import dev_container_state, DevContainerStatus
from prompts import PromptBuilder, PromptContext, IssueContext, ReviewCycleContext
import logging
import json
import re

logger = logging.getLogger(__name__)


class DevEnvironmentVerifierAgent(PipelineStage):
    """
    Dev Environment Verifier that validates dev environment setup.

    Prompt content lives in:
      prompts/content/agents/dev_environment_verifier/review_task.md
      prompts/content/review_cycle/verifier_initial.md
      prompts/content/review_cycle/verifier_rereviewing.md
    """

    def __init__(self, agent_config: Dict[str, Any] = None):
        super().__init__("dev_environment_verifier", agent_config=agent_config)
        self._prompt_builder = PromptBuilder()

    async def execute(self, context: Dict[str, Any]) -> Dict[str, Any]:
        task_context = context.get("context", {})
        issue_raw = task_context.get("issue", {})
        project_name = task_context.get("project") or context.get("project", "unknown")
        previous_stage = task_context.get("previous_stage_output", "")

        if not previous_stage:
            logger.error("No previous_stage_output found. Task context: %s", json.dumps(task_context, indent=2)[:500])
            raise Exception("Dev Environment Verifier needs previous stage output from dev_environment_setup agent")

        review_cycle_raw = task_context.get("review_cycle", {})
        review_cycle = None
        if review_cycle_raw:
            review_cycle = ReviewCycleContext(
                iteration=review_cycle_raw.get("iteration", 0),
                max_iterations=review_cycle_raw.get("max_iterations", 3),
                is_rereviewing=review_cycle_raw.get("is_rereviewing", False),
                previous_review_feedback=review_cycle_raw.get("previous_review_feedback") or "",
            )

        ctx = PromptContext(
            mode="initial",
            agent_name="dev_environment_verifier",
            agent_display_name="Dev Environment Verifier",
            agent_role_description="",
            output_sections=[],
            project=project_name,
            project_name=project_name,
            issue=IssueContext(
                title=issue_raw.get("title", "No title"),
                body=issue_raw.get("body", "No description"),
            ),
            previous_stage=previous_stage,
            review_cycle=review_cycle,
        )

        # Expand {project_name} placeholders in the verification task content
        # by temporarily patching the loader result via build_verifier_prompt
        prompt = self._prompt_builder.build_verifier_prompt(ctx)

        result = await run_claude_code(prompt, context)

        if isinstance(result, dict):
            review_text = result.get("result", "")
            if result.get("output_posted"):
                context["output_posted"] = True
        else:
            review_text = result if isinstance(result, str) else str(result)

        context["agent_output"] = review_text

        # Parse status and update dev container state
        status_match = re.search(r"### Status\s*\*\*(\w+)\*\*", review_text, re.IGNORECASE)
        if status_match:
            status = status_match.group(1).upper()
            if status == "APPROVED":
                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.VERIFIED,
                    image_name=f"{project_name}-agent:latest",
                )
                logger.info("Marked %s dev container as VERIFIED", project_name)
            elif status == "BLOCKED":
                error_match = re.search(
                    r"#### Issues Found\s*(.+?)(?=###|\Z)", review_text, re.DOTALL | re.IGNORECASE
                )
                error_message = error_match.group(1).strip() if error_match else "Verification failed"
                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.BLOCKED,
                    error_message=error_message[:200],
                )
                logger.info("Marked %s dev container as BLOCKED: %s", project_name, error_message[:100])
        else:
            logger.warning("Could not parse verification status for %s", project_name)

        return {"status": "success", "agent_output": review_text}

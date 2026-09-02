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

        direct_prompt = task_context.get("direct_prompt")

        if direct_prompt:
            # Frozen-session resume (see agent_executor.py's
            # _apply_frozen_session_resume): skip rebuilding the full verifier
            # prompt from scratch and use the short continuation prompt
            # instead — Claude's --resume'd session already has the original
            # verification task in its history.
            prompt = direct_prompt
        else:
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

        # Snapshot the status dev_environment_setup left in place (normally
        # IN_PROGRESS) before this agent's session runs. Compared against the
        # post-session status below to detect whether the agent's own tool
        # calls already resolved dev_container_state during the session, even
        # if the final response text we parse doesn't carry the expected
        # marker (see the "could not parse" branch).
        status_before_session = dev_container_state.get_status(project_name)

        result = await run_claude_code(prompt, context)

        if isinstance(result, dict):
            review_text = result.get("result", "")
            if result.get("output_posted"):
                context["output_posted"] = True
        else:
            review_text = result if isinstance(result, str) else str(result)

        context["agent_output"] = review_text

        # Parse status and update dev container state.
        #
        # CRITICAL: every branch below MUST resolve dev_container_state to a status
        # (VERIFIED, BLOCKED, or CHANGES_NEEDED). Leaving it untouched on a parse
        # failure means it stays at whatever dev_environment_setup left it
        # (IN_PROGRESS) forever -- nothing
        # else ever re-checks it, so every task requiring this project's dev container
        # silently defers itself every 30s, indefinitely, with no error ever surfaced
        # (see incident: codetoreum and phone-home both stuck IN_PROGRESS for hours/a day
        # because this exact parse fell through to a silent no-op that still returned
        # "success").
        # [^*\n]+ (not \w+) because "CHANGES NEEDED" is two words separated by a
        # space, which \w doesn't match -- \w+ would fail to match the marker at
        # all for that status and fall through to the "could not parse" branch below.
        status_match = re.search(r"### Status\s*\*\*([^*\n]+)\*\*", review_text, re.IGNORECASE)
        if status_match:
            status = status_match.group(1).strip().upper()
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
            elif status == "CHANGES NEEDED":
                # Distinct from BLOCKED: used when the verifier could not independently
                # confirm a REQUIRED FIX (rather than confirming it's still broken).
                # repair_cycle.py's env-rebuild sub-cycle treats this as retryable.
                error_match = re.search(
                    r"#### Issues Found\s*(.+?)(?=###|\Z)", review_text, re.DOTALL | re.IGNORECASE
                )
                error_message = error_match.group(1).strip() if error_match else "Could not confirm required fix"
                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.CHANGES_NEEDED,
                    error_message=error_message[:200],
                )
                logger.info("Marked %s dev container as CHANGES_NEEDED: %s", project_name, error_message[:100])
            else:
                # Found the "### Status **WORD**" marker but WORD wasn't one we handle.
                error_message = f"Verifier returned unrecognized status '{status}' (expected APPROVED, BLOCKED, or CHANGES NEEDED)"
                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.BLOCKED,
                    error_message=error_message[:200],
                )
                logger.error(
                    "%s for %s -- marking dev container BLOCKED instead of leaving it stuck",
                    error_message, project_name
                )
        else:
            # No "### Status **X**" marker in the final response text. Before
            # forcing BLOCKED, check whether the agent's own tool calls
            # already resolved dev_container_state during the session (per
            # the Step 5 instructions in the prompt) -- if so, the final
            # response is just a closing summary that happened to drop the
            # marker, not a genuinely unresolved verification. Trust the
            # self-reported resolution instead of clobbering it.
            #
            # See incident: phone-home #341 -- the verifier confirmed the fix,
            # called dev_container_state.set_status(VERIFIED) itself mid-session,
            # then this fallback overwrote that back to BLOCKED because the
            # closing "### Summary" text it wrote afterward omitted the marker.
            status_after_session = dev_container_state.get_status(project_name)
            resolved_statuses = (
                DevContainerStatus.VERIFIED,
                DevContainerStatus.BLOCKED,
                DevContainerStatus.CHANGES_NEEDED,
            )
            if (
                status_after_session != status_before_session
                and status_after_session in resolved_statuses
            ):
                logger.warning(
                    "Could not find a '### Status' marker in %s's final verifier response, "
                    "but dev_container_state was already updated to %s during the session "
                    "(from %s) -- honoring that instead of forcing BLOCKED.",
                    project_name, status_after_session.value, status_before_session.value
                )
            else:
                snippet = review_text.strip()[:300]
                error_message = f"Could not parse a status marker from verifier output. Output began: {snippet}"
                dev_container_state.set_status(
                    project_name=project_name,
                    status=DevContainerStatus.BLOCKED,
                    error_message=error_message[:200],
                )
                logger.error(
                    "Could not parse verification status for %s -- marking dev container BLOCKED "
                    "instead of leaving it stuck (see state/dev_containers/%s.yaml for the raw "
                    "output excerpt)",
                    project_name, project_name
                )

        return {"status": "success", "agent_output": review_text}

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

    @staticmethod
    def _record_status(project_name, status, success_log=None, **kwargs) -> bool:
        """Write `status`, and announce it only if the write actually landed.

        Two rules, both learned the hard way, in one place instead of six:

        1. The return value is checked. set_status() is a compare-and-set that
           can be refused, and it can fail outright; an unchecked write leaves
           the environment at IN_PROGRESS, which every member of a shared
           environment then waits on -- the dangling state this module's
           CRITICAL comment exists to prevent.
        2. `success_log` fires only on success. Announcing regardless produced
           adjacent lines reading "Failed to record VERIFIED for features" and
           "Marked ... as VERIFIED"; whichever an operator read second is the
           one they believed. services/dev_container_state.py makes the same
           point about its own reset announcement (#171 review).

        Returns whether the write landed.
        """
        wrote = bool(dev_container_state.set_status(
            project_name=project_name, status=status, **kwargs
        ))
        if not wrote:
            logger.error(
                "Failed to record %s for %s; the environment may remain "
                "IN_PROGRESS and stall every member of it",
                status.value, project_name,
            )
        elif success_log:
            logger.info(*success_log)
        return wrote

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

        # Snapshot dev_container_state's last-write timestamp before this
        # agent's session runs. Compared against the post-session timestamp
        # below to detect whether the agent's own tool calls wrote to
        # dev_container_state *during this session*, even if the final
        # response text we parse doesn't carry the expected marker (see the
        # "could not parse" branch). A timestamp comparison -- rather than
        # comparing the status values themselves -- is deliberate: a session
        # that re-affirms the same status it started with (e.g. re-writes
        # VERIFIED) still counts as this session having resolved it, while an
        # unrelated write from another process landing in this same window
        # would not (its timestamp falls outside what this session produced).
        status_updated_before_session = dev_container_state.get_status_updated_at(project_name)

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
                # (#198) The tag belongs to the dev-container ENVIRONMENT, which
                # several projects may share -- never compose it from the project
                # name here.
                from services.dev_container_environment import (
                    environment_for,
                    image_tag_for,
                )

                expected_tag = image_tag_for(project_name)
                environment = environment_for(project_name)

                # ASSERT, don't trust (#198). The setup agent issues the
                # `docker build` itself from inside its own Claude Code
                # session, so the tag is produced by a model following a
                # prompt. A build tagged from the project name instead of the
                # environment would be a perfectly good image under a name
                # nothing ever reads -- surfacing forever after as "not built"
                # with no indication why. Checking here converts that into one
                # legible failure naming both tags.
                #
                # BLOCKED, deliberately, and NOT retried here. Two earlier
                # attempts to soften this were both worse:
                #
                #   * raising a DockerUnavailableError from verify_image_exists
                #     to separate "missing" from "Docker did not answer" -- the
                #     raise sat inside a try whose own `except Exception`
                #     caught it, so it never fired, while a sibling raise from
                #     an except handler DID escape and broke three unrelated
                #     callers that degrade gracefully on False.
                #   * recording CHANGES_NEEDED instead, so a transient Docker
                #     failure would age out -- but the fault this check exists
                #     for (a model tagging the image after the project) is
                #     DETERMINISTIC, and CHANGES_NEEDED's 30-minute staleness
                #     escape has no attempt counter outside repair_cycle. That
                #     turned one terminal failure into an unbounded loop of
                #     hour-scale rebuilds, per member, with the stage still
                #     reporting success so nothing ever counted it.
                #
                # BLOCKED is terminal, bounded, respected by every member of
                # the environment, and clearable by an operator
                # (scripts/set_dev_container_verified.py). The transient case
                # is made diagnosable instead of special-cased: verify_image_
                # exists now logs Docker's own stderr, so "Cannot connect to
                # the Docker daemon" is distinguishable from "No such image"
                # in the log line right above this verdict.
                if not dev_container_state.verify_image_exists(
                    project_name, image_name=expected_tag
                ):
                    error_message = (
                        f"Verifier approved the environment but the expected image "
                        f"tag {expected_tag!r} does not exist (or is not a genuine "
                        f"agent environment). Most likely the build tagged the image "
                        f"after the project name instead of the dev-container "
                        f"environment {environment!r}; if the log line above reports "
                        f"a Docker error instead, the image could not be checked at "
                        f"all. Re-run dev_environment_setup; it must use the image "
                        f"tag supplied in its prompt verbatim."
                    )
                    # Not truncated -- the remedy is in the second half, and
                    # this is the operator's only persistent record.
                    if self._record_status(
                        project_name,
                        DevContainerStatus.BLOCKED,
                        error_message=error_message,
                    ):
                        logger.error(
                            "Refusing to mark %s VERIFIED: expected image %s is "
                            "missing or unverifiable", project_name, expected_tag,
                        )
                else:
                    self._record_status(
                        project_name,
                        DevContainerStatus.VERIFIED,
                        success_log=(
                            "Marked dev container environment %s as VERIFIED (image %s, "
                            "requested by project %s)",
                            environment, expected_tag, project_name,
                        ),
                        image_name=expected_tag,
                    )
            elif status == "BLOCKED":
                error_match = re.search(
                    r"#### Issues Found\s*(.+?)(?=###|\Z)", review_text, re.DOTALL | re.IGNORECASE
                )
                error_message = error_match.group(1).strip() if error_match else "Verification failed"
                self._record_status(
                    project_name,
                    DevContainerStatus.BLOCKED,
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
                self._record_status(
                    project_name,
                    DevContainerStatus.CHANGES_NEEDED,
                    error_message=error_message[:200],
                )
                logger.info("Marked %s dev container as CHANGES_NEEDED: %s", project_name, error_message[:100])
            else:
                # Found the "### Status **WORD**" marker but WORD wasn't one we handle.
                error_message = f"Verifier returned unrecognized status '{status}' (expected APPROVED, BLOCKED, or CHANGES NEEDED)"
                self._record_status(
                    project_name,
                    DevContainerStatus.BLOCKED,
                    success_log=(
                        "%s for %s -- marking dev container BLOCKED instead of leaving it stuck",
                        error_message, project_name
                    ),
                    error_message=error_message[:200],
                )
        else:
            # No "### Status **X**" marker in the final response text. Before
            # forcing BLOCKED, check whether the agent's own tool calls wrote
            # to dev_container_state *during this session* (per the Step 5
            # instructions in the prompt) -- if so, the final response is just
            # a closing summary that happened to drop the marker, not a
            # genuinely unresolved verification. Trust that self-reported
            # resolution instead of clobbering it, gated on:
            #   1. A write actually happened in this session's window (by
            #      updated_at, not by comparing status values -- a session
            #      that re-affirms the same status it started with must still
            #      count as resolved; comparing raw values would miss that).
            #      This isn't airtight against a write from an unrelated
            #      process landing in the exact same window, but it's a real
            #      improvement over no time-bounding at all.
            #   2. The resulting status is VERIFIED or BLOCKED specifically --
            #      NOT CHANGES_NEEDED. Unlike those two, CHANGES_NEEDED has no
            #      staleness escape of its own (see validate_task_can_run in
            #      agents/orchestrator_integration.py, which deliberately never
            #      re-triggers setup for it) and is owned solely by
            #      repair_cycle's env-rebuild sub-cycle retrying it. Honoring
            #      a self-set CHANGES_NEEDED here, outside that sub-cycle's
            #      supervision, risks recreating the exact dangling-forever
            #      failure mode fixed in 5a8af03/65e5bd7 on this same branch.
            #
            # See incident: phone-home -- the verifier confirmed the fix,
            # called dev_container_state.set_status(VERIFIED) itself mid-session,
            # then this fallback overwrote that back to BLOCKED because the
            # closing "### Summary" text it wrote afterward omitted the marker.
            #
            # Both values come from ONE snapshot of the state file (#171).
            # get_status_updated_at() and get_status() each take the file lock
            # separately, so reading them in sequence could pair a timestamp
            # from before an interleaving write with the status from after it --
            # and then honor a status this session did not produce on the
            # strength of a timestamp change it did. This lock is already
            # released by the time these lines run (see
            # services/dev_container_build_lock.py's "deliberately accepted
            # gaps"), so a single locked read is what makes the pair consistent.
            status_after_session, status_updated_after_session = (
                dev_container_state.get_status_and_updated_at(project_name)
            )
            session_wrote_state = (
                status_updated_before_session != status_updated_after_session
                and status_updated_after_session is not None
            )
            honorable_statuses = (DevContainerStatus.VERIFIED, DevContainerStatus.BLOCKED)
            if session_wrote_state and status_after_session in honorable_statuses:
                logger.warning(
                    "Could not find a '### Status' marker in %s's final verifier response, "
                    "but dev_container_state was written to %s during this session -- "
                    "honoring that instead of forcing BLOCKED.",
                    project_name, status_after_session.value
                )
            else:
                snippet = review_text.strip()[:300]
                error_message = f"Could not parse a status marker from verifier output. Output began: {snippet}"
                self._record_status(
                    project_name,
                    DevContainerStatus.BLOCKED,
                    success_log=(
                        "Could not parse verification status for %s -- marking dev container BLOCKED "
                        "instead of leaving it stuck (see state/dev_containers/%s.yaml for the raw "
                        "output excerpt)",
                        project_name, project_name
                    ),
                    error_message=error_message[:200],
                )

        return {"status": "success", "agent_output": review_text}

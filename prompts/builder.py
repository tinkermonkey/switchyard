"""
PromptBuilder — assembles agent prompts from PromptContext + content files.

All structural scaffolding (section headers, conditional logic, template
strings) lives here.  All prose content (guidelines, quality standards,
review criteria, output instructions) is loaded from prompts/content/ files
via ContentLoader.

Public API:
    builder = PromptBuilder()          # or PromptBuilder(loader=custom_loader)
    prompt  = builder.build(ctx)       # ctx is a PromptContext

The builder also exposes individual section builders for agents that need to
compose only part of a prompt (e.g., standalone review-cycle agents).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from prompts.loader import ContentLoader, default_loader

if TYPE_CHECKING:
    from prompts.context import PromptContext, ReviewCycleContext

logger = logging.getLogger(__name__)

# PR review agents use workflow templates rather than per-agent main_prompt files.
_PR_REVIEW_TEMPLATES = {
    "pr_code_reviewer": "pr_review/code_review",
    "requirements_verifier": "pr_review/requirements",
}

# ---------------------------------------------------------------------------
# PromptBuilder
# ---------------------------------------------------------------------------

class PromptBuilder:
    """
    Assembles a complete agent prompt from a PromptContext.

    Instances can be shared safely — no mutable state after construction.
    """

    def __init__(self, loader: ContentLoader = default_loader):
        self._loader = loader

    # ── Public entry point ─────────────────────────────────────────────────

    def build(self, ctx: "PromptContext") -> str:
        """Return the complete prompt string for this context."""
        if ctx.direct_prompt:
            return ctx.direct_prompt

        if ctx.mode == "question":
            return self._build_question(ctx)
        if ctx.mode == "revision":
            return self._build_revision(ctx)
        return self._build_initial(ctx)

    # ── Specialised builders used by standalone agents ─────────────────────

    def build_reviewer_prompt(
        self,
        ctx: "PromptContext",
        *,
        reviewer_title: str,
        review_domain: str,
    ) -> str:
        """Assemble a prompt for reviewer agents (CodeReviewer, DocumentationEditor)."""
        agent = ctx.agent_name
        rc = ctx.review_cycle
        is_rereviewing = rc.is_rereviewing if rc else False

        iteration_context = self._reviewer_iteration_context(ctx, review_domain=review_domain)
        review_task = self._loader.agent_review_task(agent)
        format_instructions = self._reviewer_format_instructions(ctx, is_rereviewing)
        requirements_section = self._reviewer_requirements_section(ctx)
        context_section = self._reviewer_context_section(ctx)

        return self._loader.workflow_template("review/prompt").format(
            reviewer_title=reviewer_title,
            review_domain=review_domain,
            iteration_context=iteration_context,
            requirements_section=requirements_section,
            context_section=context_section,
            review_task=review_task,
            format_instructions=format_instructions,
        )

    def build_verifier_prompt(self, ctx: "PromptContext") -> str:
        """Assemble the DevEnvironmentVerifier prompt."""
        project_name = ctx.project_name or ctx.project
        iteration_context = self._verifier_iteration_context(ctx)
        verification_task_raw = self._loader.agent_review_task("dev_environment_verifier")
        # Expand {project_name} placeholders inside the content file so shell
        # commands and Python snippets reference the correct project.
        verification_task = verification_task_raw.replace("{project_name}", project_name)

        return self._loader.workflow_template("verification/prompt").format(
            project_name=project_name,
            iteration_context=iteration_context,
            issue_title=ctx.issue.title,
            issue_body=ctx.issue.body,
            previous_stage=ctx.previous_stage,
            verification_task=verification_task,
        )

    def build_from_template(self, ctx: "PromptContext") -> str:
        """
        Build from a workflow or agent-specific template file.

        PRCodeReviewerAgent and RequirementsVerifierAgent use workflow templates
        from workflows/pr_review/.  The template may contain {pr_url},
        {check_name}, {check_content} placeholders.
        """
        workflow_path = _PR_REVIEW_TEMPLATES.get(ctx.agent_name)
        if not workflow_path:
            logger.warning("build_from_template called for unknown agent %s", ctx.agent_name)
            return ""
        template = self._loader.workflow_template(workflow_path)
        if not template:
            return ""
        check_content = ctx.check_content
        if len(check_content) > 15000:
            check_content = check_content[:15000] + "\n\n[... truncated ...]"
        return template.format(
            pr_url=ctx.pr_url,
            check_name=ctx.check_name,
            check_content=check_content,
        )

    # ── Mode-specific builders ─────────────────────────────────────────────

    def _build_initial(self, ctx: "PromptContext") -> str:
        loader = self._loader
        guidelines = loader.agent_guidelines(ctx.agent_name)
        quality_standards = loader.agent_quality_standards(ctx.agent_name)

        previous_stage_section = self._previous_stage_section(ctx)
        quality_section = f"\n## Quality Standards\n{quality_standards}\n" if quality_standards else ""
        guidelines_section = f"\n{guidelines}" if guidelines else ""
        output_instructions = self._output_instructions(ctx, mode="initial")
        sections_list = "\n".join(f"- {s}" for s in ctx.output_sections)

        if ctx.prompt_variant == "implementation":
            prompt = loader.workflow_template("initial/implementation").format(
                agent_display_name=ctx.agent_display_name,
                agent_role_description=ctx.agent_role_description,
                issue_title=ctx.issue.title,
                issue_body=ctx.issue.body,
                previous_work_section=self._previous_work_section(ctx),
                guidelines_section=guidelines_section,
                output_instructions=output_instructions,
            )
        else:
            prompt = loader.workflow_template("initial/standard").format(
                agent_display_name=ctx.agent_display_name,
                agent_role_description=ctx.agent_role_description,
                project=ctx.project,
                issue_title=ctx.issue.title,
                issue_body=ctx.issue.body,
                issue_labels=ctx.issue.labels,
                previous_stage_section=previous_stage_section,
                quality_section=quality_section,
                sections_list=sections_list,
                guidelines_section=guidelines_section,
                output_instructions=output_instructions,
            )

        # Append sub-issue format block if requested (WorkBreakdownAgent)
        if ctx.include_sub_issue_format:
            sub_issue_block = loader.agent_sub_issue_format(ctx.agent_name)
            if sub_issue_block:
                sub_issue_block = sub_issue_block.format(
                    parent_issue_number=ctx.sub_issue_parent_issue_number,
                    discussion_reference_json=ctx.sub_issue_discussion_reference_json,
                )
                prompt = prompt + "\n" + sub_issue_block

        return prompt

    def _build_question(self, ctx: "PromptContext") -> str:
        loader = self._loader
        guidelines = loader.agent_guidelines(ctx.agent_name)
        guidelines_section = f"\n{guidelines}" if guidelines else ""
        output_instructions = self._output_instructions(ctx, mode="question")

        # Prefer file-based context to avoid embedding large history in prompt
        if ctx.pipeline_context_dir:
            try:
                from services.pipeline_context_writer import PipelineContextWriter
                writer = PipelineContextWriter.from_existing(ctx.pipeline_context_dir)
                if writer.exists():
                    return loader.workflow_template("question/file_context").format(
                        agent_display_name=ctx.agent_display_name,
                        agent_role_description=ctx.agent_role_description,
                        issue_title=ctx.issue.title,
                        guidelines_section=guidelines_section,
                        pipeline_context_section=writer.question_prompt_section(),
                        current_question=ctx.current_question,
                        output_instructions=output_instructions,
                    )
            except Exception:
                pass

        # Fallback: embed history directly
        return loader.workflow_template("question/embedded").format(
            agent_display_name=ctx.agent_display_name,
            agent_role_description=ctx.agent_role_description,
            issue_title=ctx.issue.title,
            issue_body=ctx.issue.body,
            guidelines_section=guidelines_section,
            formatted_history=self._format_thread_history(ctx.thread_history),
            current_question=ctx.current_question,
            output_instructions=output_instructions,
        )

    def _build_revision(self, ctx: "PromptContext") -> str:
        rc = ctx.review_cycle
        is_review_cycle = (
            ctx.mode == "revision"
            and rc is not None
        )
        # Detect trigger from the presence of a review_cycle with a context_dir
        # (trigger == 'review_cycle_revision' is captured as review_cycle existing)
        use_file_context = (
            is_review_cycle
            and ctx.pipeline_context_dir
        )

        cycle_context = self._maker_cycle_context(ctx)
        sections_joined = ", ".join(ctx.output_sections) if ctx.output_sections else "all sections"

        if use_file_context and rc:
            feedback_file = f"review_feedback_{rc.iteration}.md"
            maker_file = f"maker_output_{rc.iteration}.md"
            return self._loader.workflow_template("revision/file_based").format(
                agent_display_name=ctx.agent_display_name,
                agent_role_description=ctx.agent_role_description,
                cycle_context=cycle_context,
                issue_title=ctx.issue.title,
                feedback_file=feedback_file,
                maker_file=maker_file,
                sections_joined=sections_joined,
            )

        return self._loader.workflow_template("revision/embedded").format(
            agent_display_name=ctx.agent_display_name,
            agent_role_description=ctx.agent_role_description,
            cycle_context=cycle_context,
            issue_title=ctx.issue.title,
            issue_body=ctx.issue.body,
            previous_output=ctx.previous_output,
            feedback=ctx.feedback,
            sections_joined=sections_joined,
        )

    # ── Section builders ───────────────────────────────────────────────────

    def _output_instructions(self, ctx: "PromptContext", mode: str) -> str:
        loader = self._loader
        is_file_writer = ctx.makes_code_changes or ctx.filesystem_write_allowed
        if mode == "question":
            return (
                loader.workflow_template("question/output_code")
                if is_file_writer
                else loader.workflow_template("question/output_analysis")
            )
        return (
            loader.workflow_template("output/code_writing")
            if is_file_writer
            else loader.workflow_template("output/analysis")
        )

    def _previous_stage_section(self, ctx: "PromptContext") -> str:
        """Return the previous-stage prompt section, preferring file-based context."""
        if ctx.pipeline_context_dir:
            try:
                from services.pipeline_context_writer import PipelineContextWriter
                writer = PipelineContextWriter.from_existing(ctx.pipeline_context_dir)
                if writer.exists():
                    section = writer.stage_prompt_section(ctx.inputs_from)
                    if section:
                        return section
            except Exception:
                pass

        if ctx.previous_stage:
            return f"\n## Previous Stage Output\n{ctx.previous_stage}\n\nBuild upon this previous analysis in your work.\n"

        return ""

    def _previous_work_section(self, ctx: "PromptContext") -> str:
        """
        Return the previous-work section for implementation-variant prompts
        (SeniorSoftwareEngineerAgent).

        Uses stronger language than the standard previous-stage section:
        the history may include QA/testing feedback and the engineer must
        address every identified issue, not just 'build upon' prior analysis.
        """
        if ctx.pipeline_context_dir:
            try:
                from services.pipeline_context_writer import PipelineContextWriter
                writer = PipelineContextWriter.from_existing(ctx.pipeline_context_dir)
                if writer.exists():
                    section = writer.stage_prompt_section(ctx.inputs_from)
                    if section:
                        return section
            except Exception:
                pass

        if ctx.previous_stage:
            return (
                f"\n## Previous Work and Feedback\n\n"
                f"The following is the complete history of agent outputs and feedback for this issue.\n"
                f"This includes outputs from ALL previous stages (design, testing, QA, etc.) and any\n"
                f"user feedback. If this issue was returned from testing or QA, pay special attention\n"
                f"to their feedback and address all issues they identified.\n\n"
                f"{ctx.previous_stage}\n\n"
                f"IMPORTANT: Review all feedback carefully and address every issue that is not already addressed.\n"
            )

        return ""

    def _maker_cycle_context(self, ctx: "PromptContext") -> str:
        """Revision context block describing whether this is a review cycle or plain feedback."""
        rc = ctx.review_cycle
        loader = self._loader
        if rc:
            template = loader.workflow_template("revision/cycle_context")
            return template.format(
                iteration=rc.iteration,
                max_iterations=rc.max_iterations,
                reviewer=rc.reviewer_agent.replace("_", " ").title(),
            ) if template else ""

        return loader.workflow_template("revision/feedback_context")

    # ── Reviewer-specific section builders ────────────────────────────────

    def _reviewer_iteration_context(self, ctx: "PromptContext", review_domain: str = "code") -> str:
        rc = ctx.review_cycle
        if not rc:
            return ""
        loader = self._loader
        maker_title = rc.maker_agent.replace("_", " ").title()

        if rc.post_human_feedback:
            template = loader.workflow_template("review/iteration_post_human")
            return template.format(iteration=rc.iteration, max_iterations=rc.max_iterations) if template else ""

        if rc.is_rereviewing:
            # Prefer agent-specific re-review content (e.g. documentation_editor has different common issues)
            agent_template = loader.agent_rereviewing_context(ctx.agent_name)
            if not agent_template:
                agent_template = loader.workflow_template("review/iteration_rereviewing")

            # Determine how to surface previous feedback
            if rc.context_dir and rc.iteration and rc.iteration > 1:
                prev_feedback_file = f"review_feedback_{rc.iteration - 1}.md"
                prior_feedback_section = f"**Your Previous Review Feedback**: read `/pipeline_context/{prev_feedback_file}`\n"
            elif rc.previous_review_feedback:
                prior_feedback_section = (
                    f"**Your Previous Review Feedback**:\n"
                    f"<previous_feedback>\n{rc.previous_review_feedback}\n</previous_feedback>\n"
                )
            else:
                prior_feedback_section = ""

            return agent_template.format(
                iteration=rc.iteration,
                max_iterations=rc.max_iterations,
                maker_agent_title=maker_title,
                prior_feedback_section=prior_feedback_section,
            ) if agent_template else ""

        template = loader.workflow_template("review/iteration_initial")
        return template.format(
            iteration=rc.iteration,
            max_iterations=rc.max_iterations,
            maker_agent_title=maker_title,
            review_domain=review_domain,
        ) if template else ""

    def _reviewer_requirements_section(self, ctx: "PromptContext") -> str:
        rc = ctx.review_cycle
        if rc and ctx.pipeline_context_dir:
            return (
                f"## Original Requirements\n\n"
                f"**Title**: {ctx.issue.title}\n"
                f"(Full requirements in `/pipeline_context/initial_request.md`)"
            )
        return (
            f"## Original Requirements\n\n"
            f"**Title**: {ctx.issue.title}\n"
            f"**Description**: {ctx.issue.body}"
        )

    def _reviewer_context_section(self, ctx: "PromptContext") -> str:
        rc = ctx.review_cycle
        if rc and ctx.pipeline_context_dir:
            iteration = rc.iteration
            maker_file = f"maker_output_{iteration}.md" if iteration else "maker_output_1.md"
            prev_feedback_note = ""
            if rc.is_rereviewing and iteration and iteration > 1:
                prev_feedback_file = f"review_feedback_{iteration - 1}.md"
                prev_feedback_note = (
                    f"- **`{prev_feedback_file}`** — your previous feedback; "
                    f"verify those issues are now resolved\n"
                )
            return (
                f"\n## Review Cycle Context Files\n\n"
                f"All context files are at `/pipeline_context/`:\n"
                f"- **`current_diff.md`** — git changes to review (stat + commits) ← run `git diff` from those commits\n"
                f"- **`{maker_file}`** — current implementation to review\n"
                f"- `initial_request.md` — original requirements to verify against\n"
                f"{prev_feedback_note}"
                f"- Earlier numbered files show the full iteration history\n\n"
                f"**Review focus**: Read `current_diff.md` for the list of changed files, then use\n"
                f"`git diff <base_commit> HEAD -- <file>` to examine the actual changes. Review ONLY\n"
                f"additions (`+`) and deletions (`-`). Do not review unchanged code.\n"
            )

        # Embedded documentation (DocumentationEditorAgent passes docs via previous_stage)
        if ctx.previous_stage:
            return f"\n## Documentation to Review\n\n{ctx.previous_stage}\n"

        # Fallback: embedded change manifest (code reviewer without a context dir)
        change_manifest = ctx.change_manifest
        if change_manifest:
            return (
                f"\n## Code Changes\n\n{change_manifest}\n\n"
                f"**Review focus**: Use the `git diff` commands listed above to fetch and examine the actual\n"
                f"changes before reviewing. Review ONLY additions (`+`) and deletions (`-`) in the diff output.\n"
                f"Do not review unchanged code.\n"
            )
        return ""

    def _reviewer_format_instructions(self, ctx: "PromptContext", is_rereviewing: bool) -> str:
        loader = self._loader
        if is_rereviewing:
            return loader.agent_format_rereviewing(ctx.agent_name)
        return loader.agent_format_initial(ctx.agent_name)

    # ── Verifier-specific ──────────────────────────────────────────────────

    def _verifier_iteration_context(self, ctx: "PromptContext") -> str:
        rc = ctx.review_cycle
        if not rc:
            return ""
        loader = self._loader
        if rc.is_rereviewing:
            prior = ""
            if rc.previous_review_feedback:
                prior = (
                    f"**Your Previous Review Feedback**:\n"
                    f"<previous_feedback>\n{rc.previous_review_feedback}\n</previous_feedback>\n\n"
                )
            template = loader.workflow_template("verification/iteration_rereviewing")
            return template.format(
                iteration=rc.iteration,
                max_iterations=rc.max_iterations,
                prior_feedback_section=prior,
            ) if template else ""

        template = loader.workflow_template("verification/iteration_initial")
        return template.format(
            iteration=rc.iteration,
            max_iterations=rc.max_iterations,
        ) if template else ""

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _format_thread_history(history: list) -> str:
        if not history:
            return ""
        parts = []
        for msg in history:
            role = msg.get("role", "user")
            author = msg.get("author", "unknown")
            body_raw = msg.get("body", "")
            if isinstance(body_raw, dict):
                body = body_raw.get("formatted_text", "") or body_raw.get("text", "") or str(body_raw)
            else:
                body = str(body_raw)
            body = body.strip()
            if role == "agent":
                parts.append(f"**You** ({author}):\n{body}\n")
            else:
                parts.append(f"**@{author}**:\n{body}\n")
        return "\n".join(parts)

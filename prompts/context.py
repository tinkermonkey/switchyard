"""
Prompt data model.

All information needed to build any agent prompt is captured in PromptContext.
PromptBuilder consumes a PromptContext and produces a prompt string.

Usage:
    ctx = PromptContext.from_task_context(
        task_context=task_context,
        agent_name="business_analyst",
        agent_display_name="Business Analyst",
        agent_role_description="...",
        output_sections=["Executive Summary", "Functional Requirements"],
        makes_code_changes=False,
        filesystem_write_allowed=False,
    )
    prompt = PromptBuilder().build(ctx)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class IssueContext:
    title: str = ""
    body: str = ""
    labels: list = field(default_factory=list)


@dataclass
class ReviewCycleContext:
    """State for a single step in a maker-checker review cycle."""
    iteration: int = 0
    max_iterations: int = 3
    maker_agent: str = ""
    reviewer_agent: str = ""
    is_rereviewing: bool = False
    post_human_feedback: bool = False
    previous_review_feedback: str = ""
    # Mounted directory path (inside container); None means embed context inline
    context_dir: Optional[str] = None


@dataclass
class PromptContext:
    """
    Complete input data model for prompt assembly.

    Fields are intentionally flat so PromptBuilder can inspect them without
    reaching back into raw task_context dicts.
    """

    # ── Identity ──────────────────────────────────────────────────────────────
    agent_name: str                  # registry key, e.g. "business_analyst"
    agent_display_name: str          # human label, e.g. "Business Analyst"
    agent_role_description: str      # one-sentence role
    output_sections: list            # ordered section names for initial/revision prompts

    # ── Execution mode ────────────────────────────────────────────────────────
    # 'initial' | 'question' | 'revision'
    mode: str = "initial"

    # ── Task inputs ───────────────────────────────────────────────────────────
    project: str = ""
    issue: IssueContext = field(default_factory=IssueContext)

    # ── Previous stage context ────────────────────────────────────────────────
    previous_stage: str = ""
    pipeline_context_dir: Optional[str] = None   # file-based context dir
    inputs_from: list = field(default_factory=list)  # upstream agent names

    # ── Review cycle (maker revision) ─────────────────────────────────────────
    review_cycle: Optional[ReviewCycleContext] = None

    # ── Question mode ─────────────────────────────────────────────────────────
    thread_history: list = field(default_factory=list)
    current_question: str = ""

    # ── Revision mode (non-review-cycle) ──────────────────────────────────────
    feedback: str = ""
    previous_output: str = ""

    # ── Capability flags (drive output instruction variant) ───────────────────
    makes_code_changes: bool = False
    filesystem_write_allowed: bool = False

    # ── Passthrough ───────────────────────────────────────────────────────────
    # When set, PromptBuilder.build() returns this directly, skipping all logic.
    direct_prompt: str = ""

    # ── Template variant ──────────────────────────────────────────────────────
    # 'standard'        — analysis framing (default)
    # 'implementation'  — code implementation framing (SeniorSoftwareEngineer)
    prompt_variant: str = "standard"

    # ── Sub-issue mode (WorkBreakdownAgent) ───────────────────────────────────
    include_sub_issue_format: bool = False
    sub_issue_parent_issue_number: str = "unknown"
    sub_issue_discussion_reference_json: str = ""

    # ── PR review agents ──────────────────────────────────────────────────────
    pr_url: str = ""
    check_name: str = ""
    check_content: str = ""

    # ── Code reviewer (standalone, non-file-based) ───────────────────────────
    change_manifest: str = ""  # embedded change summary when no pipeline_context_dir

    # ── Dev environment verifier ──────────────────────────────────────────────
    project_name: str = ""   # explicit project name (verifier needs it expanded)

    # ── Reference repositories ────────────────────────────────────────────────
    # Pre-rendered section injected into every prompt when the project has reference_repos configured.
    # Empty string when no reference repos are configured (section is omitted).
    reference_repos_section: str = ""

    # ─────────────────────────────────────────────────────────────────────────
    # Factory
    # ─────────────────────────────────────────────────────────────────────────

    @classmethod
    def from_task_context(
        cls,
        task_context: dict,
        *,
        agent_name: str,
        agent_display_name: str,
        agent_role_description: str,
        output_sections: list,
        makes_code_changes: bool = False,
        filesystem_write_allowed: bool = False,
        prompt_variant: str = "standard",
        include_sub_issue_format: bool = False,
    ) -> "PromptContext":
        """
        Construct a PromptContext from the raw task_context dict that the
        orchestrator passes into agent.execute().

        Centralises all task_context key lookups in one place.
        """
        issue_raw = task_context.get("issue", {})
        review_cycle_raw = task_context.get("review_cycle", {})

        # ── Review cycle ──────────────────────────────────────────────────
        review_cycle = None
        if review_cycle_raw:
            review_cycle = ReviewCycleContext(
                iteration=review_cycle_raw.get("iteration", 0),
                max_iterations=review_cycle_raw.get("max_iterations", 3),
                maker_agent=review_cycle_raw.get("maker_agent", ""),
                reviewer_agent=review_cycle_raw.get("reviewer_agent", ""),
                is_rereviewing=review_cycle_raw.get("is_rereviewing", False),
                post_human_feedback=review_cycle_raw.get("post_human_feedback", False),
                previous_review_feedback=review_cycle_raw.get("previous_review_feedback") or "",
                context_dir=task_context.get("pipeline_context_dir"),
            )

        # ── Mode detection ────────────────────────────────────────────────
        is_conversational = (
            task_context.get("trigger") == "feedback_loop"
            and task_context.get("conversation_mode") == "threaded"
            and len(task_context.get("thread_history", [])) > 0
        )
        is_revision = (
            task_context.get("trigger") in ["review_cycle_revision", "feedback_loop"]
            or "revision" in task_context
            or "feedback" in task_context
        )
        if is_conversational:
            mode = "question"
        elif is_revision:
            mode = "revision"
        else:
            mode = "initial"

        # ── Feedback / revision fields ────────────────────────────────────
        revision_data = task_context.get("revision", {})
        feedback_raw = task_context.get("feedback", {})
        feedback_text = (
            revision_data.get("feedback")
            or (feedback_raw.get("formatted_text") if isinstance(feedback_raw, dict) else str(feedback_raw))
            or ""
        )
        previous_output = revision_data.get("previous_output") or task_context.get("previous_output", "")

        # ── Sub-issue extras (WorkBreakdownAgent) ─────────────────────────
        sub_issue_parent = "unknown"
        sub_issue_disc_ref = ""
        if include_sub_issue_format:
            sub_issue_parent = str(task_context.get("issue_number", "unknown"))
            # caller is expected to populate these after construction if needed
            # (WorkBreakdownAgent overrides _build_prompt_context to set them)

        return cls(
            agent_name=agent_name,
            agent_display_name=agent_display_name,
            agent_role_description=agent_role_description,
            output_sections=output_sections,
            mode=mode,
            project=task_context.get("project", "unknown"),
            issue=IssueContext(
                title=issue_raw.get("title", "No title"),
                body=issue_raw.get("body", "No description"),
                labels=issue_raw.get("labels", []),
            ),
            previous_stage=task_context.get("previous_stage_output", ""),
            pipeline_context_dir=task_context.get("pipeline_context_dir"),
            inputs_from=task_context.get("inputs_from", []),
            review_cycle=review_cycle,
            thread_history=task_context.get("thread_history", []),
            current_question=feedback_text if mode == "question" else "",
            feedback=feedback_text,
            previous_output=previous_output,
            makes_code_changes=makes_code_changes,
            filesystem_write_allowed=filesystem_write_allowed,
            direct_prompt=task_context.get("direct_prompt", ""),
            prompt_variant=prompt_variant,
            include_sub_issue_format=include_sub_issue_format,
            sub_issue_parent_issue_number=sub_issue_parent,
            project_name=task_context.get("project") or "",
            pr_url=task_context.get("pr_url", ""),
            check_name=task_context.get("check_name", ""),
            check_content=task_context.get("check_content", ""),
            reference_repos_section=cls._build_reference_repos_section(
                task_context.get("project", "")
            ),
        )

    @staticmethod
    def _build_reference_repos_section(project: str) -> str:
        """Return a rendered reference repos section for the project, or empty string."""
        if not project:
            return ""
        try:
            from config.manager import config_manager
            project_config = config_manager.get_project_config(project)
            repos = project_config.reference_repos or []
            if not repos:
                return ""
            entries = "\n".join(
                f"- **{r.mount_path or f'/reference/{r.name}'}** — {r.description.strip()}"
                for r in repos
            )
            from prompts.loader import default_loader
            template = default_loader.workflow_template("context/reference_repos")
            return template.format(entries=entries) if template else ""
        except Exception:
            logger.debug("Could not build reference_repos_section for project %r", project, exc_info=True)
            return ""

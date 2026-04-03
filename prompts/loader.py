"""
Content file loader for prompt assembly.

All prose content (guidelines, quality standards, review criteria, output
instructions, and structural prompt templates) lives in prompts/content/ as
markdown files.  Files may contain {variable} placeholders that are filled in
by PromptBuilder after loading.

Files may optionally include YAML frontmatter (--- ... ---) which documents
the invocation path and available variables.  Frontmatter is stripped
automatically before content is returned — it is for human reference only.

Directory layout:
    prompts/content/
        workflows/
            initial/
                standard.md              structural template — analysis agents, initial mode
                implementation.md        structural template — code agents, initial mode
                previous_work_fallback.md previous work section fallback ({previous_stage})
            question/
                file_context.md    structural template — question mode, file-based context
                embedded.md        structural template — question mode, embedded history
                output_code.md     output instructions — question mode, file-writing agents
                output_analysis.md output instructions — question mode, analysis agents
            revision/
                file_based.md      structural template — revision mode, file-based context
                embedded.md        structural template — revision mode, embedded feedback
                cycle_context.md   iteration context block — maker during review cycle
                feedback_context.md iteration context block — maker during feedback loop
            review/
                prompt.md          structural template — reviewer agents
                iteration_initial.md       iteration context — initial review pass
                iteration_rereviewing.md   iteration context — re-review pass
                iteration_post_human.md    iteration context — post-human-escalation pass
                requirements_file_based.md requirements section — file-based context ({issue_title})
                requirements_embedded.md   requirements section — embedded ({issue_title}, {issue_body})
                context_file_based.md      context section — file-based ({maker_file}, {prev_feedback_note})
                context_embedded_docs.md   context section — embedded docs ({previous_stage})
                context_embedded_changes.md context section — embedded changes ({change_manifest})
                prior_feedback_section.md  prior feedback block ({previous_review_feedback})
            verification/
                prompt.md          structural template — verifier agents
                iteration_initial.md       iteration context — initial verification
                iteration_rereviewing.md   iteration context — re-verification
            pr_review/
                code_review.md     fully custom template — PRCodeReviewerAgent
                requirements.md    fully custom template — RequirementsVerifierAgent
                main_review.md     PR review prompt body (pr_url, prior_cycle_section, checkout_instruction)
                prior_cycles.md    prior-cycle context block (prior_cycle_context)
                authority_*.md     static authority-framing blocks for RequirementsVerifierAgent
                verification_main.md  verification prompt (pr_url, authority_framing, context_name, context_content)
                consolidation.md   consolidation prompt (phase_blocks); uses {{ }} for JSON schema
            output/
                code_writing.md    output instructions — initial/revision, file-writing agents
                analysis.md        output instructions — initial/revision, analysis agents
            repair/
                test_output_format.md   static JSON response schema; concatenated, not formatted
                runner_compilation.md   runner prompt — compilation failures; concatenated
                runner_pre_commit.md    runner prompt — pre-commit hook failures; concatenated
                runner_ci.md            runner prompt — CI pipeline failures; concatenated
                runner_storybook.md     runner prompt — Storybook build failures; concatenated
                runner_unit.md          runner prompt — unit test failures; concatenated
                runner_integration.md   runner prompt — integration test failures; concatenated
                runner_generic.md       runner prompt — unknown test type ({test_type}); formatted then concatenated
                warning_review.md       inline warning review prompt ({source_file}, {warning_text})
                systemic_analysis.md    systemic failure analysis prompt (5 variables); uses {{ }} for JSON schema
                systemic_fix.md         systemic fix prompt ({test_type}, {known_pattern}, {failure_digest}, {attempt_note})
            analysis/
                pipeline_run.md             pipeline run analysis prompt (5 variables incl. sentinel strings); uses {{ }} for JSON
                architecture_discovery.md   codebase architecture analysis prompt ({project})
                techstack_discovery.md      tech stack discovery prompt ({project})
                conventions_discovery.md    coding conventions discovery prompt ({project})
            artifacts/
                generate_agent.md    agent definition generation prompt (13 variables); uses {{ }} for AI-facing placeholders
                generate_skill.md    skill definition generation prompt (10 variables); uses {{ }} for AI-facing placeholders
                review_artifacts.md  artifact quality review prompt (5 variables)
                generate_strategy.md agent team strategy generation prompt (4 variables); uses {{ }} for JSON schema
        agents/
            {agent_name}/
                role_description.md        (one-sentence role injected at prompt top)
                guidelines.md              (optional)
                quality_standards.md       (optional)
                review_task.md             (standalone reviewer agents)
                format_initial.md          (reviewer format block, initial pass)
                format_rereviewing.md      (reviewer format block, re-review)
                rereviewing_context.md     (agent-specific re-review context override, optional)
                sub_issue_format.md        (JSON sub-issue output format, WorkBreakdownAgent)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_CONTENT_ROOT = Path(__file__).parent / "content"


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter delimited by opening and closing --- lines.

    Frontmatter is for human reference only (documents invocation path and
    available variables).  It is stripped here so callers receive clean
    template content without risk of {variable} references in YAML
    descriptions interfering with str.format() calls.
    """
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content  # no closing ---, return as-is
    return content[end + 4:].lstrip("\n")


@lru_cache(maxsize=256)
def _load_file(path: Path) -> str:
    """Load a content file, returning empty string if not found.

    Frontmatter (--- ... ---) is stripped before returning.

    Results are cached permanently for the process lifetime — intentional for
    production use where content files change only via deployments (which restart
    the container).  In development, restart the process to pick up file edits.
    """
    try:
        return _strip_frontmatter(path.read_text(encoding="utf-8")).strip()
    except FileNotFoundError:
        return ""
    except Exception as exc:
        logger.warning("Failed to load prompt content file %s: %s", path, exc)
        return ""


class ContentLoader:
    """
    Thin wrapper around the content file tree.

    All methods return empty string when the requested file does not exist,
    so callers never need to guard against missing optional content.
    """

    def __init__(self, content_root: Path = _CONTENT_ROOT):
        self._root = content_root

    # ── Workflow templates ─────────────────────────────────────────────────

    def workflow_template(self, path: str) -> str:
        """Load any content file from prompts/content/workflows/.

        path is relative to the workflows/ directory, without the .md extension.

        Examples:
            "initial/standard"        — standard initial mode template
            "question/file_context"   — question mode with file-based context
            "revision/cycle_context"  — maker revision cycle context block
            "review/prompt"           — reviewer agent structural template
            "pr_review/code_review"   — PR code reviewer custom template
            "output/code_writing"     — output instructions for file-writing agents
        """
        return _load_file(self._root / "workflows" / f"{path}.md")

    # ── Agent content ──────────────────────────────────────────────────────

    def agent_guidelines(self, agent_name: str) -> str:
        return _load_file(self._root / "agents" / agent_name / "guidelines.md")

    def agent_quality_standards(self, agent_name: str) -> str:
        return _load_file(self._root / "agents" / agent_name / "quality_standards.md")

    def agent_review_task(self, agent_name: str) -> str:
        """Main review task section for standalone reviewer agents."""
        return _load_file(self._root / "agents" / agent_name / "review_task.md")

    def agent_format_initial(self, agent_name: str) -> str:
        """Output format block for the first pass of a review."""
        return _load_file(self._root / "agents" / agent_name / "format_initial.md")

    def agent_format_rereviewing(self, agent_name: str) -> str:
        """Output format block for a re-review pass."""
        return _load_file(self._root / "agents" / agent_name / "format_rereviewing.md")

    def agent_sub_issue_format(self, agent_name: str) -> str:
        """JSON sub-issue output format block (WorkBreakdownAgent)."""
        return _load_file(self._root / "agents" / agent_name / "sub_issue_format.md")

    def agent_role_description(self, agent_name: str) -> str:
        """One-sentence role description injected at the top of every prompt."""
        return _load_file(self._root / "agents" / agent_name / "role_description.md")

    def agent_rereviewing_context(self, agent_name: str) -> str:
        """Agent-specific re-review iteration context block (overrides the shared template).

        Optional — returns empty string if the agent has no agent-specific override,
        in which case the caller should fall back to workflow_template("review/iteration_rereviewing").
        """
        return _load_file(self._root / "agents" / agent_name / "rereviewing_context.md")


# Module-level singleton — agents share one loader instance.
default_loader = ContentLoader()

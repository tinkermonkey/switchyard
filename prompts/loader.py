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
                standard.md        structural template — analysis agents, initial mode
                implementation.md  structural template — code agents, initial mode
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
            verification/
                prompt.md          structural template — verifier agents
                iteration_initial.md       iteration context — initial verification
                iteration_rereviewing.md   iteration context — re-verification
            pr_review/
                code_review.md     fully custom template — PRCodeReviewerAgent
                requirements.md    fully custom template — RequirementsVerifierAgent
            output/
                code_writing.md    output instructions — initial/revision, file-writing agents
                analysis.md        output instructions — initial/revision, analysis agents
        agents/
            {agent_name}/
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

    def agent_rereviewing_context(self, agent_name: str) -> str:
        """Agent-specific re-review iteration context block (overrides the shared template).

        Optional — returns empty string if the agent has no agent-specific override,
        in which case the caller should fall back to workflow_template("review/iteration_rereviewing").
        """
        return _load_file(self._root / "agents" / agent_name / "rereviewing_context.md")


# Module-level singleton — agents share one loader instance.
default_loader = ContentLoader()

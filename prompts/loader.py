"""
Content file loader for prompt assembly.

All prose content (guidelines, quality standards, review criteria, output
instructions, review cycle blocks) lives in prompts/content/ as markdown
files.  Files may contain {variable} placeholders that are filled in by
PromptBuilder after loading.

Directory layout:
    prompts/content/
        output_instructions/
            code_writing.md
            analysis.md
            code_writing_question.md
            analysis_question.md
        review_cycle/
            maker_revision_cycle.md
            maker_feedback.md
            reviewer_initial.md
            reviewer_rereviewing.md
            reviewer_post_human.md
            verifier_initial.md
            verifier_rereviewing.md
        agents/
            {agent_name}/
                guidelines.md          (optional)
                quality_standards.md   (optional)
                review_task.md         (standalone reviewer agents)
                format_initial.md      (reviewer format block, initial pass)
                format_rereviewing.md  (reviewer format block, re-review)
                main_prompt.md         (fully custom prompt template)
"""

from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

_CONTENT_ROOT = Path(__file__).parent / "content"


@lru_cache(maxsize=256)
def _load_file(path: Path) -> str:
    """Load a content file, returning empty string if not found.

    Results are cached permanently for the process lifetime — intentional for
    production use where content files change only via deployments (which restart
    the container).  In development, restart the process to pick up file edits.
    """
    try:
        return path.read_text(encoding="utf-8").strip()
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

    def agent_main_prompt(self, agent_name: str) -> str:
        """Fully custom prompt template (for PR reviewer / requirements verifier)."""
        return _load_file(self._root / "agents" / agent_name / "main_prompt.md")

    def agent_sub_issue_format(self, agent_name: str) -> str:
        """JSON sub-issue output format block (WorkBreakdownAgent)."""
        return _load_file(self._root / "agents" / agent_name / "sub_issue_format.md")

    def agent_rereviewing_context(self, agent_name: str) -> str:
        """Agent-specific re-review iteration context block (overrides the shared template).

        Optional — returns empty string if the agent has no agent-specific override,
        in which case the caller should fall back to review_cycle_reviewer_rereviewing().
        """
        return _load_file(self._root / "agents" / agent_name / "rereviewing_context.md")

    # ── Output instructions ────────────────────────────────────────────────

    def output_instructions_code_writing(self) -> str:
        return _load_file(self._root / "output_instructions" / "code_writing.md")

    def output_instructions_analysis(self) -> str:
        return _load_file(self._root / "output_instructions" / "analysis.md")

    def output_instructions_code_writing_question(self) -> str:
        return _load_file(self._root / "output_instructions" / "code_writing_question.md")

    def output_instructions_analysis_question(self) -> str:
        return _load_file(self._root / "output_instructions" / "analysis_question.md")

    # ── Review cycle blocks ────────────────────────────────────────────────

    def review_cycle_maker_revision_cycle(self) -> str:
        """Revision context block when trigger == 'review_cycle_revision'."""
        return _load_file(self._root / "review_cycle" / "maker_revision_cycle.md")

    def review_cycle_maker_feedback(self) -> str:
        """Revision context block for generic feedback loop."""
        return _load_file(self._root / "review_cycle" / "maker_feedback.md")

    def review_cycle_reviewer_initial(self) -> str:
        return _load_file(self._root / "review_cycle" / "reviewer_initial.md")

    def review_cycle_reviewer_rereviewing(self) -> str:
        return _load_file(self._root / "review_cycle" / "reviewer_rereviewing.md")

    def review_cycle_reviewer_post_human(self) -> str:
        return _load_file(self._root / "review_cycle" / "reviewer_post_human.md")

    def review_cycle_verifier_initial(self) -> str:
        return _load_file(self._root / "review_cycle" / "verifier_initial.md")

    def review_cycle_verifier_rereviewing(self) -> str:
        return _load_file(self._root / "review_cycle" / "verifier_rereviewing.md")


# Module-level singleton — agents share one loader instance.
default_loader = ContentLoader()

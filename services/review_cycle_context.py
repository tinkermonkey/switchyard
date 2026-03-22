"""
Review Cycle Context Writer

Writes review cycle context pieces as numbered files into a temp directory
that gets mounted into agent containers at /review_cycle_context/.

This replaces embedding large text blobs in agent prompts with file references,
reducing maker agent context overload and giving the reviewer access to the
full chronological history.
"""

import logging
import os
import shutil

logger = logging.getLogger(__name__)

# Base directory for context files, within the workspace volume so it
# survives orchestrator container restarts (host-mounted volume).
_CONTEXT_BASE = '/workspace/.orchestrator/tmp/review_cycle_context'


class ReviewCycleContextWriter:
    """
    Manages a per-cycle directory of context files for maker and reviewer agents.

    Files are named with a numeric prefix so they sort chronologically:
        initial_request.md
        maker_output_1.md
        review_feedback_1.md
        maker_output_2.md
        review_feedback_2.md
        ...
        current_diff.md   (overwritten before each reviewer run)

    The directory path is stored in ReviewCycleState so it can be re-attached
    after an orchestrator restart (the files persist on the host volume).
    """

    def __init__(self, context_dir: str):
        self._context_dir = context_dir

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def setup(cls, issue_number: int, pipeline_run_id: str) -> 'ReviewCycleContextWriter':
        """Create a new context directory for a review cycle."""
        run_prefix = (pipeline_run_id or '')[:8]
        dir_name = f'{issue_number}_{run_prefix}' if run_prefix else str(issue_number)
        context_dir = os.path.join(_CONTEXT_BASE, dir_name)
        try:
            os.makedirs(context_dir, exist_ok=True)
            logger.info(f"Created review cycle context dir: {context_dir}")
        except Exception as e:
            logger.error(f"Failed to create review cycle context dir {context_dir}: {e}")
            raise
        return cls(context_dir)

    @classmethod
    def from_existing(cls, context_dir: str) -> 'ReviewCycleContextWriter':
        """Re-attach to an existing context directory after orchestrator restart."""
        return cls(context_dir)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def context_dir(self) -> str:
        """Container-side path to the context directory."""
        return self._context_dir

    def exists(self) -> bool:
        """Return True if the context directory exists on disk."""
        return os.path.isdir(self._context_dir)

    # ------------------------------------------------------------------
    # File writes
    # ------------------------------------------------------------------

    def write_initial_request(self, issue_title: str, issue_body: str):
        """Write the original issue/task as initial_request.md."""
        content = f"# {issue_title}\n\n{issue_body or ''}"
        self._write_file('initial_request.md', content)

    def write_maker_output(self, output: str, iteration_number: int):
        """Write a maker agent output as maker_output_{N}.md."""
        self._write_file(f'maker_output_{iteration_number}.md', output or '')

    def write_review_feedback(self, feedback: str, iteration_number: int):
        """Write a reviewer output as review_feedback_{N}.md."""
        self._write_file(f'review_feedback_{iteration_number}.md', feedback or '')

    def write_current_diff(self, change_manifest: str):
        """Write (or overwrite) current_diff.md before each reviewer run."""
        self._write_file('current_diff.md', change_manifest or '')

    def _write_file(self, filename: str, content: str):
        path = os.path.join(self._context_dir, filename)
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.debug(f"Wrote context file: {path} ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Failed to write context file {path}: {e}")
            raise

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def repopulate_from_state(
        self,
        issue: dict,
        maker_outputs: list,
        review_outputs: list,
    ):
        """
        Re-create any missing context files from in-memory state.

        Called when the context directory was lost (e.g. volume remount) but
        the state YAML still has the outputs. Writes are idempotent — existing
        files are skipped.
        """
        if not self.exists():
            os.makedirs(self._context_dir, exist_ok=True)

        initial_path = os.path.join(self._context_dir, 'initial_request.md')
        if not os.path.exists(initial_path):
            self.write_initial_request(
                issue.get('title', ''),
                issue.get('body', ''),
            )

        # maker_outputs use iteration N, but files are named maker_output_{N+1}.md
        # (iteration 0 → maker_output_1.md, iteration 1 → maker_output_2.md, etc.)
        for entry in maker_outputs:
            n = entry.get('iteration', 0)
            filename = f'maker_output_{n + 1}.md'
            path = os.path.join(self._context_dir, filename)
            if not os.path.exists(path):
                self._write_file(filename, entry.get('output', ''))

        for entry in review_outputs:
            n = entry.get('iteration', 1)
            filename = f'review_feedback_{n}.md'
            path = os.path.join(self._context_dir, filename)
            if not os.path.exists(path):
                self._write_file(filename, entry.get('output', ''))

        logger.info(
            f"Repopulated context dir from state: {self._context_dir} "
            f"({len(maker_outputs)} maker, {len(review_outputs)} review outputs)"
        )

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------

    def list_files(self) -> list[str]:
        """Return sorted list of filenames present in the context directory."""
        if not self.exists():
            return []
        return sorted(os.listdir(self._context_dir))

    def maker_prompt_section(self, review_iteration: int) -> str:
        """
        Return a prompt section telling the maker agent where to find its context.
        review_iteration is the iteration number of the feedback being addressed.
        """
        files = self.list_files()
        if not files:
            return ''

        file_lines = '\n'.join(f'- `{f}`' for f in files)
        feedback_file = f'review_feedback_{review_iteration}.md'
        maker_file = f'maker_output_{review_iteration}.md'

        # Fall back gracefully if expected file names don't exist yet
        feedback_ref = (
            f'`{feedback_file}`' if feedback_file in files
            else 'the most recent `review_feedback_*.md`'
        )
        maker_ref = (
            f'`{maker_file}`' if maker_file in files
            else 'the most recent `maker_output_*.md`'
        )

        return f"""
## Review Cycle Context Files

All context for this review cycle is available at `/review_cycle_context/`:

{file_lines}

- **Read {feedback_ref} first** — this is the feedback you must address
- `{maker_file if maker_file in files else 'maker_output_*.md'}` contains the implementation that was reviewed
- `initial_request.md` has the original requirements
- Earlier numbered files show the full iteration history if needed
"""

    def reviewer_prompt_section(self, review_iteration: int) -> str:
        """
        Return a prompt section telling the reviewer agent where to find its context.
        review_iteration is the current review iteration number.
        """
        files = self.list_files()
        if not files:
            return ''

        file_lines = '\n'.join(f'- `{f}`' for f in files)
        prev_feedback_file = f'review_feedback_{review_iteration - 1}.md'
        maker_file = f'maker_output_{review_iteration}.md'

        prev_feedback_note = ''
        if review_iteration > 1 and prev_feedback_file in files:
            prev_feedback_note = (
                f'- **`{prev_feedback_file}`** — your previous feedback; '
                f'verify those issues are now resolved\n'
            )

        return f"""
## Review Cycle Context Files

All context for this review cycle is available at `/review_cycle_context/`:

{file_lines}

- **`current_diff.md`** — the git changes to review ← primary focus
- `{'`' + maker_file + '`' if maker_file in files else 'the most recent `maker_output_*.md`'}` — current implementation
- `initial_request.md` — original requirements to verify against
{prev_feedback_note}- Earlier numbered files show the full iteration history
"""

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Remove the context directory after the review cycle completes."""
        if os.path.isdir(self._context_dir):
            try:
                shutil.rmtree(self._context_dir)
                logger.info(f"Cleaned up review cycle context dir: {self._context_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up context dir {self._context_dir}: {e}")

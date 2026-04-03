"""
Pipeline Context Writer

Writes pipeline stage outputs and conversation history as files into a temp
directory that gets mounted into agent containers at /pipeline_context/.

This generalizes the review cycle's file-based context pattern to ALL pipeline
stages, so agents can read prior outputs from disk instead of receiving large
embedded text blocks in their prompts.
"""

import logging
import os

from services.agent_context_writer import AgentContextWriter

logger = logging.getLogger(__name__)

# Base directory for pipeline context files, within the workspace volume so it
# survives orchestrator container restarts (host-mounted volume).
_PIPELINE_CONTEXT_BASE = '/workspace/.orchestrator/tmp/pipeline_context'


class PipelineContextWriter(AgentContextWriter):
    """
    Manages a per-pipeline-run directory of context files for planning agents.

    Files written:
        initial_request.md              — original issue title + body
        {agent_name}_output.md          — each stage's output (overwritten on re-run)
        conversation_turn_{N}.md        — numbered user questions in order
        latest_question.md              — always the most recent question (stable path)

    review-cycle files (when used by a maker-checker review cycle):
        current_diff.md                 — (overwritten) git diff before each reviewer run
        maker_output_{N}.md             — maker agent output for iteration N
        review_feedback_{N}.md          — reviewer feedback for iteration N

    The directory path is stored in PipelineRun.context_dir so it can be
    re-attached after an orchestrator restart (the files persist on the host volume).
    """

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def setup(cls, issue_number: int, pipeline_run_id: str) -> 'PipelineContextWriter':
        """Create a new context directory for a pipeline run."""
        run_prefix = (pipeline_run_id or '')[:8]
        dir_name = f'{issue_number}_{run_prefix}' if run_prefix else str(issue_number)
        context_dir = os.path.join(_PIPELINE_CONTEXT_BASE, dir_name)
        try:
            os.makedirs(context_dir, exist_ok=True)
            logger.info(f"Created pipeline context dir: {context_dir}")
        except Exception as e:
            logger.error(f"Failed to create pipeline context dir {context_dir}: {e}")
            raise
        return cls(context_dir)

    @classmethod
    def from_existing(cls, context_dir: str) -> 'PipelineContextWriter':
        """Re-attach to an existing context directory after orchestrator restart."""
        return cls(context_dir)

    # ------------------------------------------------------------------
    # File writes
    # ------------------------------------------------------------------

    def write_stage_output(self, agent_name: str, output: str):
        """Write (or overwrite) a stage agent's output as {agent_name}_output.md."""
        self._write_file(f'{agent_name}_output.md', output or '')

    def write_conversation_turn(self, turn_number: int, author: str, question: str):
        """Write a user question as conversation_turn_{N}.md."""
        content = f"**@{author}**:\n\n{question or ''}"
        self._write_file(f'conversation_turn_{turn_number}.md', content)

    def write_latest_question(self, author: str, question: str):
        """Write (or overwrite) latest_question.md with the current user question."""
        content = f"**@{author}**:\n\n{question or ''}"
        self._write_file('latest_question.md', content)

    def write_maker_output(self, output: str, iteration_number: int):
        """Write a maker agent output as maker_output_{N}.md."""
        self._write_file(f'maker_output_{iteration_number}.md', output or '')

    def write_review_feedback(self, feedback: str, iteration_number: int):
        """Write a reviewer output as review_feedback_{N}.md."""
        self._write_file(f'review_feedback_{iteration_number}.md', feedback or '')

    def write_current_diff(self, change_manifest: str):
        """Write (or overwrite) current_diff.md before each reviewer run."""
        self._write_file('current_diff.md', change_manifest or '')

    def repopulate_review_cycle_from_state(
        self,
        issue: dict,
        maker_outputs: list,
        review_outputs: list,
    ):
        """
        Re-create any missing review cycle context files from in-memory state.

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
            f"Repopulated pipeline context dir from review cycle state: {self._context_dir} "
            f"({len(maker_outputs)} maker, {len(review_outputs)} review outputs)"
        )

    # ------------------------------------------------------------------
    # Turn counting
    # ------------------------------------------------------------------

    def _next_turn_number(self) -> int:
        """Return the next conversation turn number (1-indexed)."""
        if not self.exists():
            return 1
        existing = [
            f for f in os.listdir(self._context_dir)
            if f.startswith('conversation_turn_') and f.endswith('.md')
        ]
        return len(existing) + 1

    # ------------------------------------------------------------------
    # Prompt helpers
    # ------------------------------------------------------------------

    def stage_prompt_section(self, inputs_from: list) -> str:
        """
        Return a prompt section telling the agent where to find its context files.

        inputs_from is the list of upstream agent names from the pipeline stage
        config (e.g. ['idea_researcher', 'business_analyst']).
        """
        files = self.list_files()
        if not files:
            return ''

        file_lines = []
        for agent_name in (inputs_from or []):
            filename = f'{agent_name}_output.md'
            if filename in files:
                display = agent_name.replace('_', ' ').title()
                file_lines.append(f'- `{filename}` — {display} output')

        if 'initial_request.md' in files:
            file_lines.append('- `initial_request.md` — original issue')

        if not file_lines:
            return ''

        file_list = '\n'.join(file_lines)
        return f"""
## Pipeline Context Files

Prior stage outputs are available at `/pipeline_context/`:

{file_list}

Read the relevant files before starting your analysis.
"""

    def question_prompt_section(self) -> str:
        """
        Return a prompt section for conversational mode pointing to context files.
        """
        files = self.list_files()
        if not files:
            return ''

        turn_files = sorted(f for f in files if f.startswith('conversation_turn_'))
        stage_files = sorted(f for f in files if f.endswith('_output.md'))

        turn_list = '\n'.join(f'- `{f}`' for f in turn_files) or '(none yet)'
        stage_list = '\n'.join(f'- `{f}`' for f in stage_files) or '(none yet)'

        return f"""
## Pipeline Context Files

All context for this conversation is at `/pipeline_context/`:

**Current question** (read this first):
- `latest_question.md`

**Conversation history**:
{turn_list}

**Stage outputs** (background context):
{stage_list}
- `initial_request.md` — original issue

"""

    # ------------------------------------------------------------------
    # Recovery
    # ------------------------------------------------------------------

    def repopulate_from_state(
        self,
        issue: dict,
        stage_outputs: dict,
        conversation_turns: list,
    ):
        """
        Re-create any missing context files from in-memory state.

        Called when the context directory was lost (e.g. volume remount) but
        state data is still available. Writes are idempotent — existing files
        are skipped.
        """
        if not self.exists():
            os.makedirs(self._context_dir, exist_ok=True)

        initial_path = os.path.join(self._context_dir, 'initial_request.md')
        if not os.path.exists(initial_path):
            self.write_initial_request(
                issue.get('title', ''),
                issue.get('body', ''),
            )

        for agent_name, output in (stage_outputs or {}).items():
            filename = f'{agent_name}_output.md'
            path = os.path.join(self._context_dir, filename)
            if not os.path.exists(path):
                self._write_file(filename, output)

        for i, turn in enumerate(conversation_turns or [], start=1):
            filename = f'conversation_turn_{i}.md'
            path = os.path.join(self._context_dir, filename)
            if not os.path.exists(path):
                self.write_conversation_turn(i, turn.get('author', 'user'), turn.get('body', ''))

        if conversation_turns:
            last_turn = conversation_turns[-1]
            self.write_latest_question(
                last_turn.get('author', 'user'),
                last_turn.get('body', ''),
            )

        logger.info(
            f"Repopulated pipeline context dir from state: {self._context_dir} "
            f"({len(stage_outputs or {})} stages, {len(conversation_turns or [])} turns)"
        )

"""
AgentContextWriter - Base class for context directory writers.

Provides shared filesystem primitives used by ReviewCycleContextWriter
and PipelineContextWriter. Both subclasses write context files into a
temp directory that gets mounted read-only into agent containers.
"""

import logging
import os
import shutil

logger = logging.getLogger(__name__)


class AgentContextWriter:
    """
    Base class for writing agent context files to a temp directory.

    Subclasses add domain-specific write methods on top of these shared
    filesystem primitives.
    """

    def __init__(self, context_dir: str):
        self._context_dir = context_dir

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def context_dir(self) -> str:
        """Path to the context directory."""
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
    # Queries
    # ------------------------------------------------------------------

    def list_files(self) -> list:
        """Return sorted list of filenames present in the context directory."""
        if not self.exists():
            return []
        return sorted(os.listdir(self._context_dir))

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup(self):
        """Remove the context directory."""
        if os.path.isdir(self._context_dir):
            try:
                shutil.rmtree(self._context_dir)
                logger.info(f"Cleaned up context dir: {self._context_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up context dir {self._context_dir}: {e}")

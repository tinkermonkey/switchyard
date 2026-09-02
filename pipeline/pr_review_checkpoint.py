"""
PR Review Stage Checkpoint System

PRReviewStage.execute() orchestrates multiple sequential Docker container
launches (Phase 1 code review, Phase 2 context verifications, Phase 4
consolidation) from a single in-process coroutine. If the orchestrator
restarts mid-cycle, that coroutine — and every phase output it has
accumulated so far, held only in local variables — is gone. The Docker
containers themselves are unaffected (they're sibling processes under the
host's Docker daemon, not children of the orchestrator process), but nothing
is left to consume their output and continue the sequence.

This module persists each phase's completed output to disk as it finishes,
keyed by (project, issue_number), so a fresh PRReviewStage.execute() started
after a restart can skip re-running any phase already checkpointed for the
current review cycle and reuse its real output instead of discarding it.

Mirrors pipeline/repair_cycle_checkpoint.py's atomic-write/backup pattern.

Checkpoint Structure:
    {
        "version": "1.0",
        "checkpoint_time": "2026-09-01T19:00:00.000000Z",
        "cycle": 1,
        "phases": {
            "code_review": "...phase 1 output...",
            "parent_issue": "...phase 2 output...",
            "consolidation": "...phase 4 output..."
        }
    }
"""

import json
import logging
import shutil
from typing import Any, Dict, Optional
from pathlib import Path

from monitoring.timestamp_utils import utc_isoformat


logger = logging.getLogger(__name__)


class PRReviewCheckpoint:
    """Manages checkpoint save/load for PR review stage phase outputs."""

    CHECKPOINT_VERSION = "1.0"

    def __init__(self, project_name: str, issue_number: int, base_dir: Optional[Path] = None):
        """
        Args:
            project_name: Project name.
            issue_number: GitHub issue number.
            base_dir: Override for the state root (default: the orchestrator's real
                state directory). Tests pass a tmp_path here instead of touching
                /workspace/switchyard.
        """
        self.project_name = project_name
        self.issue_number = issue_number

        state_dir = base_dir if base_dir is not None else Path("/workspace/switchyard/state/projects")
        pr_review_dir = state_dir / project_name / "pr_review_checkpoints"
        pr_review_dir.mkdir(parents=True, exist_ok=True)

        self.checkpoint_file = pr_review_dir / f"{issue_number}.json"
        self.backup_file = pr_review_dir / f"{issue_number}.backup.json"

    def save_phase_output(self, cycle: int, phase_key: str, output: str) -> bool:
        """
        Persist one phase's completed output for the given review cycle.

        A checkpoint found on disk for a different cycle than `cycle` is
        stale (left over from an earlier review pass) and is discarded
        rather than merged into — a phase output from review cycle 1 must
        never be reused while processing cycle 2.

        Args:
            cycle: The review cycle this phase output belongs to (1-based).
            phase_key: Stable identifier for the phase (e.g. "code_review",
                an authority_key like "parent_issue", or "consolidation").
            output: The phase's raw text output.

        Returns:
            True if saved successfully, False otherwise.
        """
        try:
            checkpoint = self._load_from_file(self.checkpoint_file)
            if not checkpoint or checkpoint.get('cycle') != cycle:
                checkpoint = {'cycle': cycle, 'phases': {}}

            checkpoint.setdefault('phases', {})[phase_key] = output
            checkpoint['version'] = self.CHECKPOINT_VERSION
            checkpoint['checkpoint_time'] = utc_isoformat()

            temp_file = self.checkpoint_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(checkpoint, f, indent=2, default=str)

            if self.checkpoint_file.exists():
                shutil.copy2(self.checkpoint_file, self.backup_file)

            temp_file.replace(self.checkpoint_file)

            logger.info(
                f"PR review checkpoint saved: {self.project_name}/#{self.issue_number} "
                f"cycle={cycle} phase={phase_key}"
            )
            return True

        except Exception as e:
            logger.error(
                f"Failed to save PR review checkpoint for {self.project_name}/"
                f"#{self.issue_number} phase={phase_key}: {e}", exc_info=True
            )
            return False

    def get_phase_output(self, cycle: int, phase_key: str) -> Optional[str]:
        """
        Return a previously-checkpointed phase's output, or None if no
        checkpoint exists, it belongs to a different cycle, or this phase
        hasn't been checkpointed yet.
        """
        checkpoint = self.load_checkpoint()
        if not checkpoint or checkpoint.get('cycle') != cycle:
            return None
        return checkpoint.get('phases', {}).get(phase_key)

    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """Load checkpoint state, falling back to backup if the primary is corrupted."""
        checkpoint = self._load_from_file(self.checkpoint_file)
        if checkpoint:
            return checkpoint

        checkpoint = self._load_from_file(self.backup_file)
        if checkpoint:
            logger.info(
                f"Recovered PR review checkpoint from backup for "
                f"{self.project_name}/#{self.issue_number}"
            )
            return checkpoint

        return None

    def _load_from_file(self, filepath: Path) -> Optional[Dict[str, Any]]:
        try:
            if not filepath.exists():
                return None

            with open(filepath, 'r') as f:
                checkpoint = json.load(f)

            version = checkpoint.get('version')
            if version != self.CHECKPOINT_VERSION:
                logger.warning(
                    f"PR review checkpoint version mismatch for "
                    f"{self.project_name}/#{self.issue_number}: "
                    f"expected {self.CHECKPOINT_VERSION}, got {version}"
                )
                return None

            return checkpoint

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse PR review checkpoint {filepath}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to load PR review checkpoint {filepath}: {e}", exc_info=True)
            return None

    def clear_checkpoint(self) -> bool:
        """Clear checkpoint files. Call whenever a review cycle genuinely concludes
        (clean pass, issues found and returned to dev, or all phases failed) so a
        later review cycle never reuses this cycle's phase output."""
        try:
            if self.checkpoint_file.exists():
                self.checkpoint_file.unlink()
            if self.backup_file.exists():
                self.backup_file.unlink()
            logger.info(
                f"Cleared PR review checkpoint for {self.project_name}/#{self.issue_number}"
            )
            return True
        except Exception as e:
            logger.error(
                f"Failed to clear PR review checkpoint for "
                f"{self.project_name}/#{self.issue_number}: {e}", exc_info=True
            )
            return False

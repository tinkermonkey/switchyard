"""
Repair Cycle Checkpoint System

Handles atomic saving and loading of repair cycle state to enable
restart resilience when running in Docker containers.

Checkpoint Structure:
    {
        "version": "1.0",
        "checkpoint_time": "2025-10-17T12:34:56.789012Z",
        "project": "project-name",
        "issue_number": 123,
        "pipeline_run_id": "abc123",
        "stage_name": "Testing",
        "test_type": "unit",
        "test_type_index": 0,
        "iteration": 3,
        "agent_call_count": 15,
        "files_fixed": ["test_user.py", "test_auth.py"],
        "test_results": {...},
        "cycle_results": [...]
    }
"""

import json
import logging
import os
import shutil
from typing import Dict, Any, Optional
from pathlib import Path
from datetime import datetime

from monitoring.timestamp_utils import utc_now, utc_isoformat


logger = logging.getLogger(__name__)


class RepairCycleCheckpoint:
    """Manages checkpoint save/load for repair cycle state"""

    CHECKPOINT_VERSION = "1.0"

    def __init__(self, project_dir: str, project_name: str = None, issue_number: int = None):
        """
        Initialize checkpoint manager.

        Args:
            project_dir: Absolute path to project workspace (for reference)
            project_name: Project name (extracted from project_dir if not provided)
            issue_number: Issue number (required if provided separately)
        """
        self.project_dir = Path(project_dir)
        
        # Extract project and issue from project_dir path if not provided
        if project_name is None:
            # project_dir is /workspace/{project}, extract project name
            project_name = self.project_dir.name
        
        self.project_name = project_name
        self.issue_number = issue_number
        
        # Store checkpoint in state directory (keeps project workspace clean)
        if issue_number is not None:
            state_dir = Path("/workspace/clauditoreum/state/projects")
            repair_cycle_dir = state_dir / project_name / "repair_cycles" / str(issue_number)
            repair_cycle_dir.mkdir(parents=True, exist_ok=True)

            self.checkpoint_file = repair_cycle_dir / "checkpoint.json"
            self.backup_file = repair_cycle_dir / "checkpoint.backup.json"
        else:
            # Fallback to old location if issue_number not provided (for backward compatibility)
            logger.warning("RepairCycleCheckpoint initialized without issue_number, using project directory")
            self.checkpoint_file = self.project_dir / ".repair_cycle_checkpoint.json"
            self.backup_file = self.project_dir / ".repair_cycle_checkpoint.backup.json"

    def save_checkpoint(self, state: Dict[str, Any]) -> bool:
        """
        Atomically save checkpoint state.

        Writes to temp file first, then moves to actual checkpoint file.
        Keeps backup of previous checkpoint.

        Args:
            state: Current repair cycle state

        Returns:
            True if save successful, False otherwise
        """
        try:
            # Add metadata
            checkpoint = {
                "version": self.CHECKPOINT_VERSION,
                "checkpoint_time": utc_isoformat(),
                **state
            }

            # Write to temp file first
            temp_file = self.checkpoint_file.with_suffix('.tmp')
            with open(temp_file, 'w') as f:
                json.dump(checkpoint, f, indent=2, default=str)

            # Backup existing checkpoint if it exists
            if self.checkpoint_file.exists():
                shutil.copy2(self.checkpoint_file, self.backup_file)
                logger.debug(f"Backed up checkpoint to {self.backup_file}")

            # Atomic move (rename) temp file to checkpoint file
            temp_file.replace(self.checkpoint_file)

            logger.info(
                f"Checkpoint saved: iteration={state.get('iteration')}, "
                f"test_type={state.get('test_type')}, "
                f"agent_calls={state.get('agent_call_count')}"
            )
            return True

        except Exception as e:
            logger.error(f"Failed to save checkpoint: {e}", exc_info=True)
            return False

    def load_checkpoint(self) -> Optional[Dict[str, Any]]:
        """
        Load checkpoint state.

        Tries primary checkpoint first, falls back to backup if corrupted.

        Returns:
            Checkpoint state dict or None if no valid checkpoint
        """
        # Try primary checkpoint
        checkpoint = self._load_from_file(self.checkpoint_file)
        if checkpoint:
            return checkpoint

        # Try backup
        logger.warning("Primary checkpoint invalid, trying backup...")
        checkpoint = self._load_from_file(self.backup_file)
        if checkpoint:
            logger.info("Recovered checkpoint from backup")
            return checkpoint

        logger.info("No valid checkpoint found")
        return None

    def _load_from_file(self, filepath: Path) -> Optional[Dict[str, Any]]:
        """Load and validate checkpoint from file"""
        try:
            if not filepath.exists():
                return None

            with open(filepath, 'r') as f:
                checkpoint = json.load(f)

            # Validate version
            version = checkpoint.get('version')
            if version != self.CHECKPOINT_VERSION:
                logger.warning(
                    f"Checkpoint version mismatch: expected {self.CHECKPOINT_VERSION}, "
                    f"got {version}"
                )
                return None

            logger.info(
                f"Loaded checkpoint from {filepath.name}: "
                f"iteration={checkpoint.get('iteration')}, "
                f"test_type={checkpoint.get('test_type')}"
            )
            return checkpoint

        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse checkpoint {filepath}: {e}")
            return None
        except Exception as e:
            logger.error(f"Failed to load checkpoint {filepath}: {e}", exc_info=True)
            return None

    def clear_checkpoint(self) -> bool:
        """
        Clear checkpoint files.

        Call when repair cycle completes successfully.

        Returns:
            True if cleared successfully
        """
        try:
            if self.checkpoint_file.exists():
                self.checkpoint_file.unlink()
                logger.info("Cleared checkpoint file")

            if self.backup_file.exists():
                self.backup_file.unlink()
                logger.debug("Cleared backup checkpoint file")

            return True

        except Exception as e:
            logger.error(f"Failed to clear checkpoint: {e}", exc_info=True)
            return False

    def checkpoint_exists(self) -> bool:
        """Check if a checkpoint exists"""
        return self.checkpoint_file.exists() or self.backup_file.exists()

    def get_checkpoint_age_seconds(self) -> Optional[float]:
        """
        Get age of checkpoint in seconds.

        Returns:
            Age in seconds or None if no checkpoint
        """
        try:
            if not self.checkpoint_file.exists():
                return None

            mtime = self.checkpoint_file.stat().st_mtime
            return datetime.now().timestamp() - mtime

        except Exception as e:
            logger.error(f"Failed to get checkpoint age: {e}")
            return None


def create_checkpoint_state(
    project: str,
    issue_number: int,
    pipeline_run_id: str,
    stage_name: str,
    test_type: str,
    test_type_index: int,
    iteration: int,
    agent_call_count: int,
    files_fixed: list,
    test_results: Optional[Dict[str, Any]] = None,
    cycle_results: Optional[list] = None
) -> Dict[str, Any]:
    """
    Create checkpoint state dictionary.

    Helper function to construct properly formatted checkpoint state.

    Args:
        project: Project name
        issue_number: GitHub issue number
        pipeline_run_id: Current pipeline run ID
        stage_name: Stage name (e.g., "Testing")
        test_type: Current test type (unit/integration/e2e)
        test_type_index: Index in test_configs list
        iteration: Current iteration number
        agent_call_count: Total agent calls so far
        files_fixed: List of files fixed so far
        test_results: Latest test results (optional)
        cycle_results: Completed cycle results (optional)

    Returns:
        Checkpoint state dict
    """
    return {
        "project": project,
        "issue_number": issue_number,
        "pipeline_run_id": pipeline_run_id,
        "stage_name": stage_name,
        "test_type": test_type,
        "test_type_index": test_type_index,
        "iteration": iteration,
        "agent_call_count": agent_call_count,
        "files_fixed": files_fixed,
        "test_results": test_results,
        "cycle_results": cycle_results or []
    }

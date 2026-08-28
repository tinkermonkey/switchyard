"""
PR Review State Manager

Tracks review cycle counts and history for the PR review agent.
Prevents infinite review loops by enforcing a maximum cycle count.
"""

import yaml
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional
from datetime import datetime, timezone

logger = logging.getLogger(__name__)


class PRReviewStateManager:
    """
    Manages PR review cycle state for parent issues.

    Persists to state/projects/{project}/pr_review_state.yaml
    """

    def __init__(self, state_root: Optional[str] = None):
        if state_root is None:
            state_root = Path(__file__).parent.parent / "state" / "projects"
        self.state_root = Path(state_root)

    def _get_state_file(self, project_name: str) -> Path:
        project_dir = self.state_root / project_name
        project_dir.mkdir(parents=True, exist_ok=True)
        return project_dir / "pr_review_state.yaml"

    def _get_lock_file(self, project_name: str) -> Path:
        state_file = self._get_state_file(project_name)
        return state_file.with_suffix(state_file.suffix + '.lock')

    def _load_state_unlocked(self, state_file: Path) -> Dict[str, Any]:
        """Read state from disk. Caller must already hold the file lock."""
        if not state_file.exists():
            return {"pr_reviews": {}}
        try:
            with open(state_file, 'r') as f:
                data = yaml.safe_load(f)
            return data or {"pr_reviews": {}}
        except yaml.YAMLError as e:
            logger.error(f"Corrupted PR review state for {state_file}: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Failed to load PR review state for {state_file}: {e}", exc_info=True)
            raise

    def _save_state_unlocked(self, state_file: Path, data: Dict[str, Any]):
        """Write state to disk. Caller must already hold the file lock."""
        try:
            with open(state_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)
        except Exception as e:
            logger.error(f"Failed to save PR review state for {state_file}: {e}")
            raise

    def _load_state(self, project_name: str) -> Dict[str, Any]:
        """Read state under the file lock. For read-only use; mutating methods
        that read-modify-write must hold the lock across the whole operation
        (see e.g. increment_review_count) rather than calling this directly."""
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project_name)
        with file_lock(self._get_lock_file(project_name), enforce_timeout=True):
            return self._load_state_unlocked(state_file)

    def get_review_count(self, project_name: str, parent_issue_number: int) -> int:
        """Get the number of review cycles completed for a parent issue."""
        data = self._load_state(project_name)
        issue_data = data.get("pr_reviews", {}).get(parent_issue_number, {})
        return issue_data.get("review_count", 0)

    def increment_review_count(
        self,
        project_name: str,
        parent_issue_number: int,
        created_issues: List[int]
    ):
        """Record a completed review cycle with the issues that were created."""
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project_name)
        with file_lock(self._get_lock_file(project_name), enforce_timeout=True):
            data = self._load_state_unlocked(state_file)
            reviews = data.setdefault("pr_reviews", {})

            issue_data = reviews.setdefault(parent_issue_number, {
                "review_count": 0,
                "iterations": []
            })

            issue_data["review_count"] = issue_data.get("review_count", 0) + 1
            now = datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            issue_data["last_review_at"] = now
            issue_data.setdefault("iterations", []).append({
                "iteration": issue_data["review_count"],
                "issues_created": created_issues,
                "timestamp": now
            })

            self._save_state_unlocked(state_file, data)
        logger.info(
            f"PR review cycle {issue_data['review_count']} recorded for "
            f"{project_name} #{parent_issue_number} "
            f"({len(created_issues)} issues created)"
        )

    def reset_review_count(self, project_name: str, parent_issue_number: int):
        """Reset the review cycle count for a parent issue.

        Called when a manual review is triggered so the cycle limit
        no longer blocks execution.  Previous iteration history is
        preserved for auditability.
        """
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project_name)
        with file_lock(self._get_lock_file(project_name), enforce_timeout=True):
            data = self._load_state_unlocked(state_file)
            reviews = data.get("pr_reviews", {})
            issue_data = reviews.get(parent_issue_number)
            if not issue_data:
                logger.debug(
                    f"No review state for {project_name} #{parent_issue_number}, nothing to reset"
                )
                return

            old_count = issue_data.get("review_count", 0)
            issue_data["review_count"] = 0
            issue_data.pop("cycle_limit_notified", None)
            issue_data.pop("cycle_limit_notified_at", None)
            self._save_state_unlocked(state_file, data)
        logger.info(
            f"Reset review count for {project_name} #{parent_issue_number} "
            f"(was {old_count}, now 0)"
        )

    def get_review_history(self, project_name: str, parent_issue_number: int) -> List[Dict]:
        """Get full review history for a parent issue."""
        data = self._load_state(project_name)
        issue_data = data.get("pr_reviews", {}).get(parent_issue_number, {})
        return issue_data.get("iterations", [])

    def get_last_review_timestamp(self, project_name: str, parent_issue_number: int) -> str:
        """
        Get the timestamp of the last PR review for a parent issue.

        Returns:
            ISO timestamp string of last review, or None if no review history exists
        """
        data = self._load_state(project_name)
        issue_data = data.get("pr_reviews", {}).get(parent_issue_number, {})
        return issue_data.get("last_review_at")

    def is_cycle_limit_notified(self, project_name: str, parent_issue_number: int) -> bool:
        """Return True if the cycle-limit-reached notification was already posted."""
        data = self._load_state(project_name)
        issue_data = data.get("pr_reviews", {}).get(parent_issue_number, {})
        return bool(issue_data.get("cycle_limit_notified", False))

    def mark_cycle_limit_notified(self, project_name: str, parent_issue_number: int):
        """Record that the cycle-limit-reached notification has been posted."""
        from utils.file_lock import file_lock

        state_file = self._get_state_file(project_name)
        with file_lock(self._get_lock_file(project_name), enforce_timeout=True):
            data = self._load_state_unlocked(state_file)
            reviews = data.setdefault("pr_reviews", {})
            issue_data = reviews.setdefault(parent_issue_number, {"review_count": 0, "iterations": []})
            issue_data["cycle_limit_notified"] = True
            issue_data["cycle_limit_notified_at"] = (
                datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
            )
            self._save_state_unlocked(state_file, data)
        logger.info(
            f"Marked cycle limit as notified for {project_name} #{parent_issue_number}"
        )

    def get_feedback_issue_ids_by_cycle(
        self, project_name: str, parent_issue_number: int
    ) -> Dict[int, List[int]]:
        """Return all feedback issue IDs grouped by review cycle number."""
        data = self._load_state(project_name)
        issue_data = data.get("pr_reviews", {}).get(parent_issue_number, {})
        result: Dict[int, List[int]] = {}
        for iteration in issue_data.get("iterations", []):
            cycle = iteration.get("iteration", 0)
            ids = iteration.get("issues_created", [])
            if ids:
                result[cycle] = ids
        return result


# Global instance
pr_review_state_manager = PRReviewStateManager()

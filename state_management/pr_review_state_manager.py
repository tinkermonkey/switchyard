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

    def _load_state(self, project_name: str) -> Dict[str, Any]:
        state_file = self._get_state_file(project_name)
        if not state_file.exists():
            return {"pr_reviews": {}}
        try:
            with open(state_file, 'r') as f:
                data = yaml.safe_load(f)
            return data or {"pr_reviews": {}}
        except yaml.YAMLError as e:
            logger.error(f"Corrupted PR review state for {project_name}: {e}", exc_info=True)
            raise
        except Exception as e:
            logger.error(f"Failed to load PR review state for {project_name}: {e}", exc_info=True)
            raise

    def _save_state(self, project_name: str, data: Dict[str, Any]):
        state_file = self._get_state_file(project_name)
        try:
            with open(state_file, 'w') as f:
                yaml.dump(data, f, default_flow_style=False, sort_keys=True)
        except Exception as e:
            logger.error(f"Failed to save PR review state for {project_name}: {e}")
            raise

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
        data = self._load_state(project_name)
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

        self._save_state(project_name, data)
        logger.info(
            f"PR review cycle {issue_data['review_count']} recorded for "
            f"{project_name} #{parent_issue_number} "
            f"({len(created_issues)} issues created)"
        )

    def get_review_history(self, project_name: str, parent_issue_number: int) -> List[Dict]:
        """Get full review history for a parent issue."""
        data = self._load_state(project_name)
        issue_data = data.get("pr_reviews", {}).get(parent_issue_number, {})
        return issue_data.get("iterations", [])


# Global instance
pr_review_state_manager = PRReviewStateManager()

"""
Feature Branch Manager

Manages hierarchical branch workflows where:
- Parent issues get shared feature branches
- Sub-issues all contribute to the parent's branch
- One PR accumulates all changes until all sub-issues complete
- Git pulls keep branches current
"""

import os
import yaml
import logging
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, asdict, field
from pathlib import Path

logger = logging.getLogger(__name__)


class MergeConflictError(Exception):
    """Raised when a merge conflict is detected"""
    def __init__(self, message: str, files: List[str]):
        super().__init__(message)
        self.files = files


@dataclass
class SubIssueState:
    """Tracks a sub-issue's progress in a feature branch"""
    number: int
    status: str  # pending, in_progress, completed, cancelled
    started_at: Optional[str] = None
    completed_at: Optional[str] = None


@dataclass
class FeatureBranch:
    """Represents a feature branch for a parent issue"""
    parent_issue: int
    branch_name: str
    created_at: str
    sub_issues: List[SubIssueState] = field(default_factory=list)
    pr_number: Optional[int] = None
    pr_status: str = "none"  # none, draft, ready
    last_pull_at: Optional[str] = None
    commits_behind_main: int = 0
    last_updated: str = field(default_factory=lambda: datetime.now().isoformat())


class FeatureBranchManager:
    """Manages feature branch lifecycle for parent/sub-issue workflows"""

    def __init__(self, workspace_root: str = "/workspace"):
        self.workspace_root = workspace_root
        self.state_dir = Path(workspace_root) / "clauditoreum" / "state" / "projects"
        # Don't create directories until actually needed (lazy creation)
        self._state_dir_initialized = False

    def _ensure_state_dir(self):
        """Ensure state directory exists (lazy initialization)"""
        if not self._state_dir_initialized:
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self._state_dir_initialized = True

    def _get_state_file(self, project: str) -> Path:
        """Get path to feature branch state file for project"""
        self._ensure_state_dir()
        project_state_dir = self.state_dir / project
        project_state_dir.mkdir(parents=True, exist_ok=True)
        return project_state_dir / "feature_branches.yaml"

    def _load_state(self, project: str) -> Dict[str, Any]:
        """Load feature branch state from YAML"""
        state_file = self._get_state_file(project)
        if not state_file.exists():
            return {"feature_branches": []}

        with open(state_file, 'r') as f:
            return yaml.safe_load(f) or {"feature_branches": []}

    def _save_state(self, project: str, state: Dict[str, Any]):
        """Save feature branch state to YAML"""
        state_file = self._get_state_file(project)
        with open(state_file, 'w') as f:
            yaml.dump(state, f, default_flow_style=False, sort_keys=False)

    def get_feature_branch_state(self, project: str, parent_issue: int) -> Optional[FeatureBranch]:
        """Get feature branch state for a parent issue"""
        state = self._load_state(project)
        for fb_data in state.get("feature_branches", []):
            if fb_data["parent_issue"] == parent_issue:
                # Convert sub_issues dicts back to SubIssueState objects
                sub_issues = [SubIssueState(**si) for si in fb_data.get("sub_issues", [])]
                fb_data["sub_issues"] = sub_issues
                return FeatureBranch(**fb_data)
        return None

    def get_feature_branch_for_issue(self, project: str, issue_number: int) -> Optional[FeatureBranch]:
        """Get feature branch for a sub-issue or parent issue"""
        state = self._load_state(project)
        for fb_data in state.get("feature_branches", []):
            # Check if this is the parent issue
            if fb_data["parent_issue"] == issue_number:
                sub_issues = [SubIssueState(**si) for si in fb_data.get("sub_issues", [])]
                fb_data["sub_issues"] = sub_issues
                return FeatureBranch(**fb_data)

            # Check if this is a sub-issue
            for si in fb_data.get("sub_issues", []):
                if si["number"] == issue_number:
                    sub_issues = [SubIssueState(**s) for s in fb_data.get("sub_issues", [])]
                    fb_data["sub_issues"] = sub_issues
                    return FeatureBranch(**fb_data)

        return None

    def get_all_feature_branches(self, project: str) -> List[FeatureBranch]:
        """Get all feature branches for a project"""
        state = self._load_state(project)
        branches = []
        for fb_data in state.get("feature_branches", []):
            sub_issues = [SubIssueState(**si) for si in fb_data.get("sub_issues", [])]
            fb_data["sub_issues"] = sub_issues
            branches.append(FeatureBranch(**fb_data))
        return branches

    def create_feature_branch_state(
        self,
        project: str,
        parent_issue: int,
        branch_name: str,
        sub_issues: List[int]
    ) -> FeatureBranch:
        """Create new feature branch state"""
        feature_branch = FeatureBranch(
            parent_issue=parent_issue,
            branch_name=branch_name,
            created_at=datetime.now().isoformat(),
            sub_issues=[SubIssueState(number=si, status="pending") for si in sub_issues]
        )

        self.save_feature_branch_state(project, feature_branch)
        logger.info(f"Created feature branch state for parent #{parent_issue}: {branch_name}")
        return feature_branch

    def save_feature_branch_state(self, project: str, feature_branch: FeatureBranch):
        """Save feature branch state"""
        feature_branch.last_updated = datetime.now().isoformat()

        state = self._load_state(project)

        # Convert to dict for serialization
        fb_dict = asdict(feature_branch)

        # Update or append
        updated = False
        for i, fb_data in enumerate(state.get("feature_branches", [])):
            if fb_data["parent_issue"] == feature_branch.parent_issue:
                state["feature_branches"][i] = fb_dict
                updated = True
                break

        if not updated:
            if "feature_branches" not in state:
                state["feature_branches"] = []
            state["feature_branches"].append(fb_dict)

        self._save_state(project, state)

    def delete_feature_branch_state(self, project: str, parent_issue: int):
        """Delete feature branch state"""
        state = self._load_state(project)
        state["feature_branches"] = [
            fb for fb in state.get("feature_branches", [])
            if fb["parent_issue"] != parent_issue
        ]
        self._save_state(project, state)
        logger.info(f"Deleted feature branch state for parent #{parent_issue}")

    def add_sub_issue_to_branch(self, project: str, feature_branch: FeatureBranch, issue_number: int):
        """Add sub-issue to feature branch tracking if not already present"""
        if not any(si.number == issue_number for si in feature_branch.sub_issues):
            feature_branch.sub_issues.append(
                SubIssueState(number=issue_number, status="pending")
            )
            self.save_feature_branch_state(project, feature_branch)
            logger.info(f"Added sub-issue #{issue_number} to feature branch {feature_branch.branch_name}")

    def mark_sub_issue_in_progress(self, project: str, feature_branch: FeatureBranch, issue_number: int):
        """Mark sub-issue as in progress"""
        for si in feature_branch.sub_issues:
            if si.number == issue_number:
                si.status = "in_progress"
                si.started_at = datetime.now().isoformat()
                break
        self.save_feature_branch_state(project, feature_branch)

    def mark_sub_issue_complete(self, project: str, feature_branch: FeatureBranch, issue_number: int):
        """Mark sub-issue as completed"""
        for si in feature_branch.sub_issues:
            if si.number == issue_number:
                si.status = "completed"
                si.completed_at = datetime.now().isoformat()
                break
        self.save_feature_branch_state(project, feature_branch)
        logger.info(f"Marked sub-issue #{issue_number} as completed in {feature_branch.branch_name}")

    def check_all_sub_issues_complete(self, feature_branch: FeatureBranch) -> bool:
        """Check if all sub-issues are completed or cancelled"""
        return all(
            si.status in ["completed", "cancelled"]
            for si in feature_branch.sub_issues
        )

    async def get_parent_issue(self, github_integration, issue_number: int) -> Optional[int]:
        """
        Get parent issue number using GitHub GraphQL API

        Returns parent issue number if this issue is tracked by a parent, None otherwise
        """
        query = """
        query($owner: String!, $repo: String!, $issueNumber: Int!) {
          repository(owner: $owner, name: $repo) {
            issue(number: $issueNumber) {
              trackedInIssues(first: 1) {
                nodes {
                  number
                }
              }
            }
          }
        }
        """

        try:
            result = await github_integration.graphql_query(
                query,
                variables={
                    "owner": github_integration.repo_owner,
                    "repo": github_integration.repo_name,
                    "issueNumber": issue_number
                }
            )

            tracked_in = result.get("data", {}).get("repository", {}).get("issue", {}).get("trackedInIssues", {})
            nodes = tracked_in.get("nodes", [])

            if nodes:
                parent_number = nodes[0]["number"]
                logger.info(f"Issue #{issue_number} is tracked by parent #{parent_number}")
                return parent_number

            logger.info(f"Issue #{issue_number} has no parent - standalone issue")
            return None

        except Exception as e:
            logger.error(f"Failed to get parent issue for #{issue_number}: {e}")
            return None

    def create_feature_branch_name(self, parent_issue: int, title: str = "") -> str:
        """Create feature branch name from parent issue"""
        sanitized_title = title.lower().replace(" ", "-")[:30] if title else "feature"
        # Remove special characters
        sanitized_title = "".join(c for c in sanitized_title if c.isalnum() or c == "-")
        return f"feature/issue-{parent_issue}-{sanitized_title}"

    async def create_branch_from_main(self, project_dir: str, branch_name: str):
        """Create a new branch from main"""
        from services.git_workflow_manager import git_workflow_manager

        # Ensure we're on main and up to date
        await git_workflow_manager.checkout_branch(project_dir, "main")
        await git_workflow_manager.pull_branch(project_dir)

        # Create and checkout new branch
        success = await git_workflow_manager.create_branch(project_dir, branch_name)

        if not success:
            # Branch might already exist (race condition with parallel sub-issue)
            # Try to checkout existing branch instead
            logger.warning(f"Failed to create branch {branch_name}, attempting checkout of existing branch")
            checkout_success = await git_workflow_manager.checkout_branch(project_dir, branch_name)

            if not checkout_success:
                raise Exception(f"Failed to create or checkout branch {branch_name}")

            logger.info(f"Checked out existing branch {branch_name} (race condition resolved)")
        else:
            await git_workflow_manager.checkout_branch(project_dir, branch_name)
            logger.info(f"Created branch {branch_name} from main")

    async def git_checkout(self, project_dir: str, branch_name: str):
        """Checkout a branch"""
        from services.git_workflow_manager import git_workflow_manager
        await git_workflow_manager.checkout_branch(project_dir, branch_name)

    async def git_pull_rebase(self, project_dir: str) -> None:
        """
        Pull latest changes with rebase

        Raises MergeConflictError if conflicts detected
        """
        from services.git_workflow_manager import git_workflow_manager

        try:
            await git_workflow_manager.pull_rebase(project_dir)
        except Exception as e:
            # Check if this is a merge conflict
            if "conflict" in str(e).lower():
                # Try to get conflicting files
                conflict_files = await git_workflow_manager.get_conflicting_files(project_dir)
                raise MergeConflictError(str(e), conflict_files)
            raise

    async def get_commits_behind_main(self, project_dir: str, branch_name: str) -> int:
        """Get number of commits this branch is behind main"""
        from services.git_workflow_manager import git_workflow_manager
        return await git_workflow_manager.get_commits_behind(project_dir, branch_name, "main")

    async def branch_exists(self, project_dir: str, branch_name: str) -> bool:
        """Check if branch exists"""
        from services.git_workflow_manager import git_workflow_manager
        return await git_workflow_manager.branch_exists(project_dir, branch_name)

    async def git_add_all(self, project_dir: str):
        """Stage all changes"""
        from services.git_workflow_manager import git_workflow_manager
        await git_workflow_manager.add_all(project_dir)

    async def git_commit(self, project_dir: str, message: str):
        """Commit changes"""
        from services.git_workflow_manager import git_workflow_manager
        await git_workflow_manager.commit(project_dir, message)

    async def git_push(self, project_dir: str, branch_name: str):
        """Push branch to remote"""
        from services.git_workflow_manager import git_workflow_manager
        await git_workflow_manager.push_branch(project_dir, branch_name)

    async def escalate_merge_conflict(
        self,
        github_integration,
        issue_number: int,
        branch_name: str,
        conflict_files: List[str]
    ):
        """Escalate merge conflict to human intervention"""
        file_list = "\n".join(f"- `{f}`" for f in conflict_files)

        message = f"""## ⚠️ Merge Conflict Detected

Work on this sub-issue cannot proceed due to merge conflicts.

**Branch:** `{branch_name}`
**Conflicting files:**
{file_list}

**Next Steps:**
1. Manually resolve conflicts in the branch
2. Commit the resolution
3. Push to remote
4. Move this issue back to the appropriate column to retry

**Resolution Command:**
```bash
git checkout {branch_name}
git pull --rebase
# Resolve conflicts manually
git add .
git commit -m "Resolve merge conflicts"
git push
```
"""

        await github_integration.post_comment(issue_number, message)
        logger.warning(f"Escalated merge conflict for issue #{issue_number}")

    async def escalate_stale_branch(
        self,
        github_integration,
        parent_issue: int,
        branch_name: str,
        commits_behind: int
    ):
        """Notify about stale branch requiring rebase"""
        message = f"""## 📅 Branch Maintenance Required

This feature branch is significantly behind the main branch.

**Branch:** `{branch_name}`
**Commits behind:** {commits_behind}
**Risk:** High - may have integration issues

**Recommended Action:**
Rebase the feature branch on latest main to incorporate recent changes.

**Rebase Command:**
```bash
git checkout {branch_name}
git fetch origin
git rebase origin/main
# Resolve any conflicts
git push --force-with-lease
```

**Note:** This is a potentially risky operation. Review changes carefully.
"""

        await github_integration.post_comment(parent_issue, message)
        logger.warning(f"Escalated stale branch for parent #{parent_issue}: {commits_behind} commits behind")

    async def prepare_feature_branch(
        self,
        project: str,
        issue_number: int,
        github_integration,
        issue_title: str = ""
    ) -> str:
        """
        Get or create feature branch, checkout, and pull latest

        Returns: branch_name
        Raises: MergeConflictError if conflict detected
        """
        project_dir = os.path.join(self.workspace_root, project)

        # Step 1: Determine parent issue
        parent_issue = await self.get_parent_issue(github_integration, issue_number)

        if not parent_issue:
            # Standalone issue - create individual branch
            logger.info(f"Issue #{issue_number} has no parent - creating standalone branch")
            branch_name = self.create_feature_branch_name(issue_number, issue_title)

            if not await self.branch_exists(project_dir, branch_name):
                await self.create_branch_from_main(project_dir, branch_name)
            else:
                await self.git_checkout(project_dir, branch_name)

            return branch_name

        # Step 2: Get or create feature branch for parent
        feature_branch = self.get_feature_branch_state(project, parent_issue)

        if not feature_branch:
            # First sub-issue - create feature branch
            parent_details = await github_integration.get_issue(parent_issue)
            branch_name = self.create_feature_branch_name(parent_issue, parent_details.get("title", ""))

            await self.create_branch_from_main(project_dir, branch_name)

            feature_branch = self.create_feature_branch_state(
                project=project,
                parent_issue=parent_issue,
                branch_name=branch_name,
                sub_issues=[issue_number]
            )
            logger.info(f"Created feature branch {branch_name} for parent #{parent_issue}")
        else:
            # Add this sub-issue to tracking if not already
            self.add_sub_issue_to_branch(project, feature_branch, issue_number)

        # Step 3: Checkout branch
        await self.git_checkout(project_dir, feature_branch.branch_name)

        # Step 4: Pull latest changes (critical for multi-sub-issue coordination)
        try:
            await self.git_pull_rebase(project_dir)
            feature_branch.last_pull_at = datetime.now().isoformat()
            self.save_feature_branch_state(project, feature_branch)

            logger.info(f"Pulled latest changes for {feature_branch.branch_name}")

        except MergeConflictError as e:
            # Don't auto-resolve - escalate to human
            logger.error(f"Merge conflict in {feature_branch.branch_name}: {e.files}")
            await self.escalate_merge_conflict(
                github_integration,
                issue_number,
                feature_branch.branch_name,
                e.files
            )
            raise

        # Step 5: Check if branch is stale
        commits_behind = await self.get_commits_behind_main(project_dir, feature_branch.branch_name)
        feature_branch.commits_behind_main = commits_behind

        if commits_behind > 50:
            await self.escalate_stale_branch(
                github_integration,
                parent_issue,
                feature_branch.branch_name,
                commits_behind
            )
        elif commits_behind > 20:
            logger.warning(
                f"Branch {feature_branch.branch_name} is {commits_behind} commits behind main"
            )

        # Mark sub-issue as in progress
        self.mark_sub_issue_in_progress(project, feature_branch, issue_number)

        return feature_branch.branch_name

    async def finalize_feature_branch_work(
        self,
        project: str,
        issue_number: int,
        commit_message: str,
        github_integration
    ) -> Dict[str, Any]:
        """
        Commit changes, push, update state, check completion

        Returns: dict with pr_url, all_complete, etc.
        """
        project_dir = os.path.join(self.workspace_root, project)

        feature_branch = self.get_feature_branch_for_issue(project, issue_number)

        if not feature_branch:
            # This is a standalone issue without parent tracking
            # Still commit and push, but skip state management
            logger.info(f"No feature branch state for issue #{issue_number} - handling as standalone")

            try:
                # Commit and push standalone branch
                await self.git_add_all(project_dir)
                await self.git_commit(project_dir, commit_message)

                # Determine standalone branch name
                from services.git_workflow_manager import git_workflow_manager
                branch_name = await git_workflow_manager.get_current_branch(project_dir)

                await self.git_push(project_dir, branch_name)

                logger.info(f"Pushed standalone changes for issue #{issue_number} to {branch_name}")

                return {
                    "success": True,
                    "branch_name": branch_name,
                    "standalone": True
                }
            except Exception as e:
                logger.error(f"Failed to finalize standalone branch for issue #{issue_number}: {e}")
                return {"success": False, "error": str(e)}

        # Step 1: Commit changes
        await self.git_add_all(project_dir)
        await self.git_commit(project_dir, commit_message)

        # Step 2: Push to remote
        await self.git_push(project_dir, feature_branch.branch_name)

        logger.info(f"Pushed changes for issue #{issue_number} to {feature_branch.branch_name}")

        # Step 3: Update sub-issue status
        self.mark_sub_issue_complete(project, feature_branch, issue_number)

        # Step 4: Create or update PR
        pr_result = await self.create_or_update_feature_pr(
            project=project,
            feature_branch=feature_branch,
            github_integration=github_integration
        )

        # Step 5: Check if all sub-issues complete
        all_complete = self.check_all_sub_issues_complete(feature_branch)

        if all_complete:
            logger.info(f"All sub-issues complete for parent #{feature_branch.parent_issue}")

            # Mark PR as ready for review
            if feature_branch.pr_number:
                await github_integration.mark_pr_ready(feature_branch.pr_number)
                feature_branch.pr_status = "ready"
                self.save_feature_branch_state(project, feature_branch)

                # Post completion comment to parent issue
                await self.post_feature_completion_comment(
                    github_integration,
                    feature_branch.parent_issue,
                    pr_result.get("pr_url")
                )

        return {
            "success": True,
            "branch_name": feature_branch.branch_name,
            "pr_url": pr_result.get("pr_url"),
            "all_complete": all_complete
        }

    async def create_or_update_feature_pr(
        self,
        project: str,
        feature_branch: FeatureBranch,
        github_integration
    ) -> Dict[str, Any]:
        """Create or update PR with current sub-issue status"""
        parent_issue = await github_integration.get_issue(feature_branch.parent_issue)

        # Build PR body with sub-issue checklist
        pr_body = await self.build_feature_pr_body(
            parent_issue,
            feature_branch,
            github_integration
        )

        if not feature_branch.pr_number:
            # Create new PR as draft
            result = await github_integration.create_pr(
                branch=feature_branch.branch_name,
                title=f"[Feature] {parent_issue.get('title', 'Feature')}",
                body=pr_body,
                draft=True
            )

            feature_branch.pr_number = result["pr_number"]
            feature_branch.pr_status = "draft"
            self.save_feature_branch_state(project, feature_branch)

            logger.info(f"Created PR #{result['pr_number']} for parent #{feature_branch.parent_issue}")

            return result
        else:
            # Update existing PR description
            await github_integration.update_pr_body(feature_branch.pr_number, pr_body)

            logger.info(f"Updated PR #{feature_branch.pr_number} with latest sub-issue status")

            return {
                "pr_number": feature_branch.pr_number,
                "pr_url": f"https://github.com/{github_integration.repo_owner}/{github_integration.repo_name}/pull/{feature_branch.pr_number}"
            }

    async def build_feature_pr_body(
        self,
        parent_issue: Dict[str, Any],
        feature_branch: FeatureBranch,
        github_integration
    ) -> str:
        """Build PR description with sub-issue checklist"""
        lines = []
        lines.append(f"# Feature: {parent_issue.get('title', 'Feature')}")
        lines.append("")
        lines.append(f"**Parent Issue:** #{feature_branch.parent_issue}")
        lines.append("")
        lines.append("## Sub-Issues Progress")

        for sub_issue in feature_branch.sub_issues:
            checkbox = "x" if sub_issue.status == "completed" else " "
            try:
                sub_details = await github_integration.get_issue(sub_issue.number)
                title = sub_details.get("title", "")
            except Exception:
                title = ""

            lines.append(f"- [{checkbox}] #{sub_issue.number} - {title}")

        lines.append("")
        lines.append("## Changes")
        lines.append("")
        lines.append("See commit history for detailed changes.")
        lines.append("")
        lines.append("---")
        lines.append("🤖 Generated by Claude Code Orchestrator")

        return "\n".join(lines)

    async def post_feature_completion_comment(
        self,
        github_integration,
        parent_issue: int,
        pr_url: Optional[str]
    ):
        """Post completion comment to parent issue"""
        message = f"""## ✅ Feature Complete

All sub-issues have been completed and changes have been committed.

**Pull Request:** {pr_url or 'Creating...'}

The PR is now ready for review and can be merged when approved.
"""

        await github_integration.post_comment(parent_issue, message)
        logger.info(f"Posted completion comment to parent issue #{parent_issue}")

    async def cleanup_orphaned_branches(self, project: str, github_integration):
        """Cleanup branches for closed parent issues (run periodically)"""
        project_dir = os.path.join(self.workspace_root, project)

        for feature_branch in self.get_all_feature_branches(project):
            try:
                parent_issue = await github_integration.get_issue(feature_branch.parent_issue)

                if parent_issue.get("state") == "closed":
                    closed_at = parent_issue.get("closed_at")
                    if closed_at:
                        from dateutil import parser
                        closed_date = parser.parse(closed_at)
                        days_closed = (datetime.now(closed_date.tzinfo) - closed_date).days

                        # Grace period before deletion
                        if days_closed > 7:
                            logger.info(f"Deleting orphaned branch {feature_branch.branch_name}")

                            # Delete remote branch
                            await github_integration.delete_branch(feature_branch.branch_name)

                            # Delete state
                            self.delete_feature_branch_state(project, feature_branch.parent_issue)

                            # Post notification
                            message = f"🧹 Deleted orphaned branch `{feature_branch.branch_name}` (parent closed {days_closed} days ago)"
                            await github_integration.post_comment(feature_branch.parent_issue, message)

            except Exception as e:
                logger.error(f"Error cleaning up branch for parent #{feature_branch.parent_issue}: {e}")
                continue


# Global instance
feature_branch_manager = FeatureBranchManager()

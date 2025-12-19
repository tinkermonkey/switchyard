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

from services.github_api_client import get_github_client

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

        # In-memory cache for branch discovery (ephemeral, process-lifetime)
        # Cache structure: {(project, parent_issue): "branch_name"}
        self._branch_cache: Dict[tuple, str] = {}

        # Initialize decision observability
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)

    def _parse_issue_from_branch_name(self, branch_name: str) -> Optional[int]:
        """
        Extract parent issue number from feature branch name.

        Handles patterns like:
        - feature/issue-53-llm-tool-use-model → 53
        - feature/issue-123 → 123

        Returns None if not a feature branch or unparseable.
        """
        import re

        if not branch_name.startswith("feature/issue-"):
            return None

        # Extract number after "issue-"
        match = re.match(r"feature/issue-(\d+)", branch_name)
        if match:
            return int(match.group(1))
        return None

    def _find_branch_for_parent(self, project_dir: str, parent_issue: int) -> Optional[str]:
        """
        Find the feature branch for a parent issue by querying git.

        Returns branch name if found, None otherwise.
        """
        try:
            all_branches = self._get_all_feature_branches_sync(project_dir)

            for branch in all_branches:
                issue_num = self._parse_issue_from_branch_name(branch)
                if issue_num == parent_issue:
                    logger.debug(f"Found branch for parent #{parent_issue}: {branch}")
                    return branch

            logger.debug(f"No branch found for parent #{parent_issue}")
            return None
        except Exception as e:
            logger.error(f"Error finding branch for parent #{parent_issue}: {e}")
            return None

    def _get_cached_branch(self, project: str, parent_issue: int) -> Optional[str]:
        """Get cached branch name for parent issue"""
        return self._branch_cache.get((project, parent_issue))

    def _cache_branch(self, project: str, parent_issue: int, branch_name: str):
        """Cache branch name for parent issue"""
        self._branch_cache[(project, parent_issue)] = branch_name
        logger.debug(f"Cached branch for {project} parent #{parent_issue}: {branch_name}")

    def _clear_cache(self, project: str, parent_issue: int):
        """Remove cached branch for parent issue"""
        key = (project, parent_issue)
        if key in self._branch_cache:
            del self._branch_cache[key]
            logger.debug(f"Cleared cache for {project} parent #{parent_issue}")

    def _get_all_feature_branches_sync(self, project_dir: str) -> List[str]:
        """
        Get all feature branches from git (synchronous version).

        This is used in contexts where we can't use async/await.
        """
        import subprocess

        try:
            # Prune stale remote references first to avoid detecting deleted branches
            subprocess.run(
                ["git", "fetch", "--prune"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False  # Don't fail if fetch has issues
            )

            result = subprocess.run(
                ["git", "branch", "-a"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=True
            )

            branches = []
            for line in result.stdout.splitlines():
                line = line.strip()
                # Remove the * for current branch
                if line.startswith("*"):
                    line = line[1:].strip()
                # Extract branch name from remotes/origin/branch-name
                if "remotes/origin/" in line:
                    line = line.split("remotes/origin/")[1]
                # Only include feature branches
                if line.startswith("feature/"):
                    branches.append(line)

            return list(set(branches))  # Remove duplicates
        except Exception as e:
            logger.error(f"Failed to get branches from {project_dir}: {e}")
            return []

    # State file methods removed - git is now the source of truth
    # Keeping empty methods for backward compatibility during transition

    def get_feature_branch_state(self, project: str, parent_issue: int) -> Optional[FeatureBranch]:
        """
        Get feature branch for a parent issue by querying git.

        This method now queries git directly instead of reading from state file.
        Returns a FeatureBranch object if found, None otherwise.
        """
        # Check cache first
        cached_branch = self._get_cached_branch(project, parent_issue)
        if cached_branch:
            return FeatureBranch(
                parent_issue=parent_issue,
                branch_name=cached_branch,
                created_at=datetime.now().isoformat(),
                sub_issues=[]
            )

        # Query git for the branch (synchronous call)
        project_dir = os.path.join(self.workspace_root, project)
        if not os.path.exists(project_dir):
            return None

        try:
            branch_name = self._find_branch_for_parent(project_dir, parent_issue)
            if branch_name:
                self._cache_branch(project, parent_issue, branch_name)
                return FeatureBranch(
                    parent_issue=parent_issue,
                    branch_name=branch_name,
                    created_at=datetime.now().isoformat(),
                    sub_issues=[]
                )
        except Exception as e:
            logger.error(f"Error getting feature branch for parent #{parent_issue}: {e}")

        return None

    async def get_feature_branch_for_issue(self, project: str, issue_number: int, github_integration) -> Optional[FeatureBranch]:
        """
        Get feature branch for a sub-issue or parent issue.

        Automatically detects if issue is a sub-issue and finds parent's branch.

        Args:
            project: Project name
            issue_number: Issue number (can be parent or sub-issue)
            github_integration: GitHubIntegration instance for API calls

        Returns:
            FeatureBranch object if found, None otherwise
        """
        # Step 1: Check if this issue itself has a branch (it's a parent)
        direct_branch = self.get_feature_branch_state(project, issue_number)
        if direct_branch:
            logger.debug(f"Found direct branch for issue #{issue_number}: {direct_branch.branch_name}")
            return direct_branch

        # Step 2: Check if it's a sub-issue - find parent
        parent_issue = await self.get_parent_issue(github_integration, issue_number, project=project)

        if parent_issue:
            # Get parent's branch
            parent_branch = self.get_feature_branch_state(project, parent_issue)
            if parent_branch:
                logger.debug(f"Found parent branch for sub-issue #{issue_number}: {parent_branch.branch_name} (parent #{parent_issue})")
            return parent_branch

        logger.debug(f"No feature branch found for issue #{issue_number}")
        return None

    def get_all_feature_branches(self, project: str) -> List[FeatureBranch]:
        """
        Get all feature branches for a project by querying git.

        Returns list of FeatureBranch objects, one per feature branch found.
        """
        project_dir = os.path.join(self.workspace_root, project)
        if not os.path.exists(project_dir):
            return []

        try:
            branch_names = self._get_all_feature_branches_sync(project_dir)
            branches = []

            for branch_name in branch_names:
                parent_issue = self._parse_issue_from_branch_name(branch_name)
                if parent_issue:
                    branches.append(FeatureBranch(
                        parent_issue=parent_issue,
                        branch_name=branch_name,
                        created_at=datetime.now().isoformat(),
                        sub_issues=[]
                    ))

            return branches
        except Exception as e:
            logger.error(f"Error getting all feature branches for {project}: {e}")
            return []

    def create_feature_branch_state(
        self,
        project: str,
        parent_issue: int,
        branch_name: str,
        sub_issues: List[int] = None
    ) -> FeatureBranch:
        """
        Create new feature branch state (in-memory only).

        No longer persists to file. Just caches the branch and returns the object.
        """
        if sub_issues is None:
            sub_issues = []

        feature_branch = FeatureBranch(
            parent_issue=parent_issue,
            branch_name=branch_name,
            created_at=datetime.now().isoformat(),
            sub_issues=[SubIssueState(number=si, status="pending") for si in sub_issues]
        )

        # Cache the branch
        self._cache_branch(project, parent_issue, branch_name)
        logger.info(f"Created feature branch state (in-memory) for parent #{parent_issue}: {branch_name}")
        return feature_branch

    def save_feature_branch_state(self, project: str, feature_branch: FeatureBranch):
        """
        Save feature branch state (NO-OP).

        State is no longer persisted. Git is the source of truth.
        This method is kept for backward compatibility but does nothing.
        """
        feature_branch.last_updated = datetime.now().isoformat()
        # Update cache
        self._cache_branch(project, feature_branch.parent_issue, feature_branch.branch_name)

    def delete_feature_branch_state(self, project: str, parent_issue: int):
        """
        Delete feature branch state (NO-OP).

        State is no longer persisted. This method is kept for backward compatibility.
        Just clears the cache.
        """
        self._clear_cache(project, parent_issue)
        logger.info(f"Cleared cached branch for parent #{parent_issue}")

    def add_sub_issue_to_branch(self, project: str, feature_branch: FeatureBranch, issue_number: int):
        """
        Add sub-issue to feature branch tracking (in-memory only).

        No longer persists to file.
        """
        if not any(si.number == issue_number for si in feature_branch.sub_issues):
            feature_branch.sub_issues.append(
                SubIssueState(number=issue_number, status="pending")
            )
            logger.info(f"Added sub-issue #{issue_number} to feature branch {feature_branch.branch_name} (in-memory)")

    def mark_sub_issue_in_progress(self, project: str, feature_branch: FeatureBranch, issue_number: int):
        """
        Mark sub-issue as in progress (in-memory only).

        No longer persists to file.
        """
        for si in feature_branch.sub_issues:
            if si.number == issue_number:
                si.status = "in_progress"
                si.started_at = datetime.now().isoformat()
                break

    def mark_sub_issue_complete(self, project: str, feature_branch: FeatureBranch, issue_number: int):
        """
        Mark sub-issue as completed (in-memory only).

        No longer persists to file.
        """
        for si in feature_branch.sub_issues:
            if si.number == issue_number:
                si.status = "completed"
                si.completed_at = datetime.now().isoformat()
                break
        logger.info(f"Marked sub-issue #{issue_number} as completed in {feature_branch.branch_name} (in-memory)")

    def check_all_sub_issues_complete(self, feature_branch: FeatureBranch) -> bool:
        """
        Check if all sub-issues are completed or cancelled.

        Note: Since we no longer track sub-issue status persistently,
        this method only works with the in-memory FeatureBranch object provided.
        """
        return all(
            si.status in ["completed", "cancelled"]
            for si in feature_branch.sub_issues
        )

    async def get_parent_issue(self, github_integration, issue_number: int, project: Optional[str] = None) -> Optional[int]:
        """
        Get parent issue number from GitHub's parent_issue_url field or issue body

        First attempts to extract parent from the `parent_issue_url` field in the REST API response.
        Falls back to parsing issue body for parent references if not found.

        Looks for patterns in body:
        - "Part of #123"
        - "Parent Issue: #123"
        - "## Parent Issue" followed by #123

        Returns parent issue number if found, None otherwise
        """
        import re

        # Step 1: Try to get parent from GitHub's parent_issue_url field (REST API)
        try:
            # Use GitHub API client to query the issue with parent_issue_url field
            github_client = get_github_client()
            endpoint = f'repos/{github_integration.github_org}/{github_integration.repo_name}/issues/{issue_number}'
            success, result = github_client.rest('GET', endpoint)
            
            if success and 'parent_issue_url' in result:
                parent_issue_url = result.get('parent_issue_url')
                
                # Extract issue number from URL like "https://api.github.com/repos/owner/repo/issues/146"
                if parent_issue_url and parent_issue_url != "null":
                    url_match = re.search(r'/issues/(\d+)$', parent_issue_url)
                    if url_match:
                        parent_num = int(url_match.group(1))
                        logger.info(f"Issue #{issue_number} is sub-issue of parent #{parent_num} (from parent_issue_url)")
                        return parent_num
        except Exception as e:
            logger.debug(f"Failed to query parent_issue_url from GitHub REST API: {e}")
            # Fall through to body parsing

        # Step 2: Fall back to parsing issue body
        try:
            issue = await github_integration.get_issue(issue_number)
            body = issue.get("body", "")

            if not body:
                logger.info(f"Issue #{issue_number} has no body - no parent detected")
                return None

            # Parse for parent references (most specific to least specific)
            patterns = [
                r'Parent Issue[:\s]+#(\d+)',  # "Parent Issue: #123" or "Parent Issue #123"
                r'Part of #(\d+)',            # "Part of #123"
                r'##\s*Parent Issue[^\d]*#(\d+)',  # "## Parent Issue\nPart of #123"
                r'##\s*Parent Issue[^\d]*Closes\s+#(\d+)',  # "## Parent Issue\nCloses #123"
                r'Sub-issue of #(\d+)',       # "Sub-issue of #123"
                r'Child of #(\d+)',           # "Child of #123"
            ]

            for pattern in patterns:
                match = re.search(pattern, body, re.IGNORECASE)
                if match:
                    parent_num = int(match.group(1))
                    logger.info(f"Issue #{issue_number} is sub-issue of parent #{parent_num} (matched pattern: {pattern})")
                    return parent_num

            logger.info(f"Issue #{issue_number} has no parent reference in body")
            return None

        except Exception as e:
            logger.error(f"Failed to get parent issue for #{issue_number}: {e}")
            return None

    def create_feature_branch_name(self, parent_issue: int, title: str = "") -> str:
        """Create feature branch name from parent issue"""
        # Validate issue number
        if parent_issue <= 0:
            raise ValueError(f"Invalid issue number: {parent_issue}. Issue numbers must be positive integers.")

        sanitized_title = title.lower().replace(" ", "-")[:30] if title else "feature"
        # Remove special characters
        sanitized_title = "".join(c for c in sanitized_title if c.isalnum() or c == "-")
        # Remove trailing/leading dashes
        sanitized_title = sanitized_title.strip("-")
        # Collapse multiple dashes
        while "--" in sanitized_title:
            sanitized_title = sanitized_title.replace("--", "-")

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

                # Only treat as merge conflict if there are actual conflicting files
                # Empty list means no real conflicts (e.g., fresh branch with no upstream)
                if conflict_files:
                    raise MergeConflictError(str(e), conflict_files)

            # Not a real merge conflict - re-raise original exception
            raise

    async def get_commits_behind_main(self, project_dir: str, branch_name: str) -> int:
        """Get number of commits this branch is behind main"""
        from services.git_workflow_manager import git_workflow_manager
        return await git_workflow_manager.get_commits_behind(project_dir, branch_name, "main")

    async def branch_exists(self, project_dir: str, branch_name: str) -> bool:
        """Check if branch exists"""
        from services.git_workflow_manager import git_workflow_manager
        return await git_workflow_manager.branch_exists(project_dir, branch_name)

    async def get_current_branch(self, project_dir: str) -> str:
        """
        Get the currently checked out branch name.

        This is useful for agents that don't need to create new branches
        and just want to work on whatever branch is currently active.

        Returns:
            Current branch name (e.g., 'main', 'feature/issue-88-...')
        """
        from services.git_workflow_manager import git_workflow_manager
        return await git_workflow_manager.get_current_branch(project_dir)

    async def get_all_feature_branches_for_project(self, project_dir: str) -> List[str]:
        """Get all feature branches from git (local and remote)"""
        from services.git_workflow_manager import git_workflow_manager
        import subprocess

        try:
            # Prune stale remote references first to avoid detecting deleted branches
            subprocess.run(
                ["git", "fetch", "--prune"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False  # Don't fail if fetch has issues
            )
            
            result = subprocess.run(
                ["git", "branch", "-a"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=True
            )

            branches = []
            for line in result.stdout.splitlines():
                line = line.strip()
                # Remove the * for current branch
                if line.startswith("*"):
                    line = line[1:].strip()
                # Extract branch name from remotes/origin/branch-name
                if "remotes/origin/" in line:
                    line = line.split("remotes/origin/")[1]
                # Only include feature branches
                if line.startswith("feature/"):
                    branches.append(line)

            return list(set(branches))  # Remove duplicates
        except Exception as e:
            logger.error(f"Failed to get branches: {e}")
            return []

    async def find_conflicting_branches(
        self,
        project_dir: str,
        issue_number: int
    ) -> List[str]:
        """Find existing branches for this issue that might conflict"""
        all_branches = await self.get_all_feature_branches_for_project(project_dir)

        # Look for branches that reference this issue number
        conflicting = []
        for branch in all_branches:
            # Match patterns like feature/issue-125 or feature/issue-125-something
            if f"issue-{issue_number}" in branch or f"issue-{issue_number}-" in branch:
                conflicting.append(branch)

        return conflicting

    async def find_related_branches(
        self,
        project: str,
        project_dir: str,
        issue_number: int,
        issue_title: str,
        parent_issue: Optional[int] = None
    ) -> List[Dict[str, Any]]:
        """
        Find existing branches that might be related to this issue

        Returns list of dicts with:
        - branch_name: str
        - match_type: str (exact_issue, parent_branch, feature_state, semantic_similarity)
        - confidence: float (0.0 to 1.0)
        - reason: str
        """
        import re
        from difflib import SequenceMatcher

        all_branches = await self.get_all_feature_branches_for_project(project_dir)
        matches = []

        # 1. Exact issue number match
        for branch in all_branches:
            if f"issue-{issue_number}" in branch or f"issue-{issue_number}-" in branch:
                matches.append({
                    "branch_name": branch,
                    "match_type": "exact_issue",
                    "confidence": 1.0,
                    "reason": f"Branch contains issue #{issue_number}"
                })

        # 2. Parent issue branch match
        if parent_issue:
            for branch in all_branches:
                if f"issue-{parent_issue}" in branch or f"issue-{parent_issue}-" in branch:
                    matches.append({
                        "branch_name": branch,
                        "match_type": "parent_branch",
                        "confidence": 0.95,
                        "reason": f"Branch for parent issue #{parent_issue}"
                    })

        # 3. Check cache for recently used branches
        cached_branch = self._get_cached_branch(project, parent_issue or issue_number)
        if cached_branch and cached_branch in all_branches:
            # Only add if not already in matches
            if not any(m["branch_name"] == cached_branch for m in matches):
                matches.append({
                    "branch_name": cached_branch,
                    "match_type": "cached",
                    "confidence": 0.90,
                    "reason": f"Recently used branch (cached)"
                })

        # 4. Semantic similarity with branch names
        if issue_title and not matches:  # Only if no strong matches found
            issue_keywords = set(re.findall(r'\w+', issue_title.lower()))
            # Remove common words
            stop_words = {'the', 'a', 'an', 'and', 'or', 'but', 'in', 'on', 'at', 'to', 'for', 'of', 'with', 'by'}
            issue_keywords = issue_keywords - stop_words

            for branch in all_branches:
                # Extract meaningful parts from branch name
                branch_parts = branch.replace('feature/', '').replace('-', ' ')
                branch_keywords = set(re.findall(r'\w+', branch_parts.lower())) - stop_words

                # Calculate keyword overlap
                overlap = len(issue_keywords & branch_keywords)
                if overlap >= 2:  # At least 2 keywords in common
                    similarity = overlap / len(issue_keywords | branch_keywords)
                    if similarity >= 0.3:  # 30% similarity threshold
                        matches.append({
                            "branch_name": branch,
                            "match_type": "semantic_similarity",
                            "confidence": min(0.7, similarity),  # Cap at 0.7 for semantic matches
                            "reason": f"Similar keywords: {', '.join(issue_keywords & branch_keywords)}"
                        })

        # Remove duplicates, keeping highest confidence
        unique_matches = {}
        for match in matches:
            branch = match["branch_name"]
            if branch not in unique_matches or match["confidence"] > unique_matches[branch]["confidence"]:
                unique_matches[branch] = match

        # Sort by confidence (highest first)
        return sorted(unique_matches.values(), key=lambda x: x["confidence"], reverse=True)

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
        return await git_workflow_manager.push_branch(project_dir, branch_name)

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

        # Step 0: Validate issue exists and is valid
        if issue_number <= 0:
            raise ValueError(f"Invalid issue number: {issue_number}. Issue numbers must be positive integers.")

        try:
            issue_data = await github_integration.get_issue(issue_number)
            if not issue_data:
                raise ValueError(f"Issue #{issue_number} does not exist or cannot be accessed")

            # Use the actual issue title if not provided
            if not issue_title:
                issue_title = issue_data.get("title", "")

            logger.info(f"Processing issue #{issue_number}: {issue_title}")
        except Exception as e:
            logger.error(f"Failed to validate issue #{issue_number}: {e}")
            raise ValueError(f"Issue #{issue_number} does not exist or cannot be accessed: {e}")

        # Step 1: Determine parent issue
        parent_issue = await self.get_parent_issue(github_integration, issue_number, project=project)

        # Step 2: Find related branches (prioritizes reuse over creation)
        related_branches = await self.find_related_branches(
            project=project,
            project_dir=project_dir,
            issue_number=issue_number,
            issue_title=issue_title,
            parent_issue=parent_issue
        )

        # Step 3: Use existing branch if high confidence match found
        if related_branches:
            best_match = related_branches[0]
            if best_match["confidence"] >= 0.8:  # High confidence threshold
                logger.info(
                    f"Found related branch for issue #{issue_number}: {best_match['branch_name']} "
                    f"(confidence: {best_match['confidence']:.2f}, reason: {best_match['reason']})"
                )
                branch_name = best_match["branch_name"]
                
                # EMIT DECISION EVENT: Branch reused
                self.decision_events.emit_branch_reused(
                    project=project,
                    issue_number=issue_number,
                    branch_name=branch_name,
                    confidence=best_match["confidence"],
                    match_reason=best_match["reason"],
                    parent_issue=parent_issue
                )

                # Get or create feature branch state
                if parent_issue:
                    feature_branch = self.get_feature_branch_state(project, parent_issue)
                    if not feature_branch:
                        # Create feature branch state for existing branch
                        feature_branch = self.create_feature_branch_state(
                            project=project,
                            parent_issue=parent_issue,
                            branch_name=branch_name,
                            sub_issues=[issue_number]
                        )
                    else:
                        # Add this sub-issue to tracking
                        self.add_sub_issue_to_branch(project, feature_branch, issue_number)

                # Checkout and continue with existing flow
                await self.git_checkout(project_dir, branch_name)

                # Pull latest (Step 4 in original flow)
                try:
                    await self.git_pull_rebase(project_dir)
                    if parent_issue and feature_branch:
                        feature_branch.last_pull_at = datetime.now().isoformat()
                        self.save_feature_branch_state(project, feature_branch)
                    logger.info(f"Pulled latest changes for {branch_name}")
                except MergeConflictError as e:
                    logger.error(f"Merge conflict in {branch_name}: {e.files}")
                    
                    # EMIT DECISION EVENT: Merge conflict detected
                    self.decision_events.emit_branch_conflict_detected(
                        project=project,
                        issue_number=issue_number,
                        branch_name=branch_name,
                        conflicting_files=e.files,
                        parent_issue=parent_issue
                    )
                    
                    await self.escalate_merge_conflict(
                        github_integration,
                        issue_number,
                        branch_name,
                        e.files
                    )
                    raise

                # Check staleness
                if parent_issue and feature_branch:
                    commits_behind = await self.get_commits_behind_main(project_dir, branch_name)
                    feature_branch.commits_behind_main = commits_behind
                    if commits_behind > 50:
                        # EMIT DECISION EVENT: Critical staleness
                        self.decision_events.emit_branch_stale_detected(
                            project=project,
                            issue_number=issue_number,
                            branch_name=branch_name,
                            commits_behind=commits_behind,
                            action_taken="escalate_stale_branch",
                            parent_issue=parent_issue
                        )
                        
                        await self.escalate_stale_branch(
                            github_integration,
                            parent_issue,
                            branch_name,
                            commits_behind
                        )
                    elif commits_behind > 20:
                        # EMIT DECISION EVENT: Warning staleness
                        self.decision_events.emit_branch_stale_detected(
                            project=project,
                            issue_number=issue_number,
                            branch_name=branch_name,
                            commits_behind=commits_behind,
                            action_taken="warn_stale_branch",
                            parent_issue=parent_issue
                        )
                        
                        logger.warning(f"Branch {branch_name} is {commits_behind} commits behind main")

                    # Mark sub-issue as in progress
                    self.mark_sub_issue_in_progress(project, feature_branch, issue_number)
                
                # EMIT BRANCH_SELECTED EVENT: Reused existing branch
                self.obs.emit_branch_selected(
                    agent="feature_branch_manager",
                    task_id=f"issue_{issue_number}",
                    project=project,
                    branch_name=branch_name,
                    reason=f"Reused existing branch (confidence: {best_match['confidence']:.2f}) - {best_match['reason']}",
                    issue_number=issue_number,
                    parent_issue=parent_issue,
                    is_new=False,
                    confidence=best_match['confidence']
                )

                return branch_name

            elif best_match["confidence"] >= 0.5:  # Medium confidence - post comment for human review
                branch_options = "\n".join([
                    f"- `{m['branch_name']}` (confidence: {m['confidence']:.0%}, {m['reason']})"
                    for m in related_branches[:3]
                ])
                
                # EMIT DECISION EVENT: Branch selection escalated
                self.decision_events.emit_branch_selection_escalated(
                    project=project,
                    issue_number=issue_number,
                    confidence=best_match["confidence"],
                    candidate_branches=related_branches[:3],
                    reason=f"Medium confidence match ({best_match['confidence']:.0%}), escalating to human"
                )

                await github_integration.post_comment(
                    issue_number,
                    f"""## Branch Selection Required

Found potentially related branches, but confidence is not high enough for automatic selection.

**Candidate branches:**
{branch_options}

**Options:**
1. To use an existing branch, update this issue body to include: `Part of #<parent-issue-number>`
2. To create a new branch, close and reopen this issue to retry
3. The orchestrator will create a new standalone branch if no action is taken

Waiting for human decision...
"""
                )
                logger.warning(
                    f"Medium confidence branch match for issue #{issue_number}. "
                    f"Posted comment for human review. Creating new branch as fallback."
                )
                # Fall through to create new branch

        # Step 4: No high-confidence match - handle parent/standalone logic
        if not parent_issue:
            # Standalone issue - create individual branch
            logger.info(f"Issue #{issue_number} has no parent and no related branches - creating standalone branch")

            branch_name = self.create_feature_branch_name(issue_number, issue_title)

            if not await self.branch_exists(project_dir, branch_name):
                # EMIT DECISION EVENT: New standalone branch created
                self.decision_events.emit_branch_created(
                    project=project,
                    issue_number=issue_number,
                    branch_name=branch_name,
                    reason="No parent issue and no related branches found - creating new standalone branch",
                    parent_issue=None,
                    is_standalone=True
                )
                
                await self.create_branch_from_main(project_dir, branch_name)
            else:
                await self.git_checkout(project_dir, branch_name)
            
            # Track branch in git workflow manager for PR management
            from services.git_workflow_manager import git_workflow_manager
            git_workflow_manager.track_branch(project, issue_number, branch_name)
            logger.info(f"Tracked branch {branch_name} for issue #{issue_number} in GitWorkflowManager")
            
            # EMIT BRANCH_SELECTED EVENT: New standalone branch
            self.obs.emit_branch_selected(
                agent="feature_branch_manager",
                task_id=f"issue_{issue_number}",
                project=project,
                branch_name=branch_name,
                reason="Created new standalone branch (no parent issue, no related branches found)",
                issue_number=issue_number,
                parent_issue=None,
                is_new=True
            )

            return branch_name

        # Step 5: Parent issue detected - get or create feature branch
        feature_branch = self.get_feature_branch_state(project, parent_issue)

        if not feature_branch:
            # First sub-issue - create feature branch
            parent_details = await github_integration.get_issue(parent_issue)
            branch_name = self.create_feature_branch_name(parent_issue, parent_details.get("title", ""))
            
            # Validate branch name matches parent issue
            import re
            branch_issue_match = re.search(r'issue-(\d+)', branch_name)
            if branch_issue_match:
                branch_issue_num = int(branch_issue_match.group(1))
                if branch_issue_num != parent_issue:
                    logger.error(
                        f"CRITICAL: Branch name mismatch! Created branch '{branch_name}' for parent "
                        f"issue #{parent_issue} but branch contains issue #{branch_issue_num}. "
                        f"This is a bug in create_feature_branch_name or issue data."
                    )
                    # Force correct branch name
                    safe_title = parent_details.get("title", "feature")[:30].lower().replace(" ", "-")
                    safe_title = "".join(c for c in safe_title if c.isalnum() or c == "-").strip("-")
                    while "--" in safe_title:
                        safe_title = safe_title.replace("--", "-")
                    branch_name = f"feature/issue-{parent_issue}-{safe_title}"
                    logger.warning(f"Corrected branch name to: {branch_name}")

            # EMIT DECISION EVENT: New feature branch created
            self.decision_events.emit_branch_created(
                project=project,
                issue_number=issue_number,
                branch_name=branch_name,
                reason=f"First sub-issue of parent #{parent_issue} - creating shared feature branch",
                parent_issue=parent_issue,
                is_standalone=False
            )

            await self.create_branch_from_main(project_dir, branch_name)

            feature_branch = self.create_feature_branch_state(
                project=project,
                parent_issue=parent_issue,
                branch_name=branch_name,
                sub_issues=[issue_number]
            )
            
            # Track branch in git workflow manager for PR management
            # Note: Track against the PARENT issue since that's what the PR will be for
            from services.git_workflow_manager import git_workflow_manager
            git_workflow_manager.track_branch(project, parent_issue, branch_name)
            logger.info(f"Created feature branch {branch_name} for parent #{parent_issue} and tracked in GitWorkflowManager")
        else:
            # Add this sub-issue to tracking if not already
            self.add_sub_issue_to_branch(project, feature_branch, issue_number)
            
            # Ensure branch is tracked in git workflow manager (in case it wasn't tracked before)
            from services.git_workflow_manager import git_workflow_manager
            if not git_workflow_manager.get_branch_info(project, parent_issue):
                git_workflow_manager.track_branch(project, parent_issue, feature_branch.branch_name)
                logger.info(f"Ensured branch {feature_branch.branch_name} is tracked for parent #{parent_issue} in GitWorkflowManager")

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
            
            # EMIT DECISION EVENT: Merge conflict detected
            self.decision_events.emit_branch_conflict_detected(
                project=project,
                issue_number=issue_number,
                branch_name=feature_branch.branch_name,
                conflicting_files=e.files,
                parent_issue=parent_issue
            )
            
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
            # EMIT DECISION EVENT: Critical staleness detected
            self.decision_events.emit_branch_stale_detected(
                project=project,
                issue_number=issue_number,
                branch_name=feature_branch.branch_name,
                commits_behind=commits_behind,
                action_taken="escalate_stale_branch",
                parent_issue=parent_issue
            )
            
            await self.escalate_stale_branch(
                github_integration,
                parent_issue,
                feature_branch.branch_name,
                commits_behind
            )
        elif commits_behind > 20:
            # EMIT DECISION EVENT: Warning staleness detected
            self.decision_events.emit_branch_stale_detected(
                project=project,
                issue_number=issue_number,
                branch_name=feature_branch.branch_name,
                commits_behind=commits_behind,
                action_taken="warn_stale_branch",
                parent_issue=parent_issue
            )
            
            logger.warning(
                f"Branch {feature_branch.branch_name} is {commits_behind} commits behind main"
            )

        # Mark sub-issue as in progress
        self.mark_sub_issue_in_progress(project, feature_branch, issue_number)
        
        # EMIT BRANCH_SELECTED EVENT: Parent feature branch
        # Determine if this is a newly created branch or existing
        is_new_branch = len(feature_branch.sub_issues) == 1 and feature_branch.sub_issues[0].number == issue_number
        reason_suffix = "first sub-issue" if is_new_branch else f"continuing work on parent #{parent_issue}"
        self.obs.emit_branch_selected(
            agent="feature_branch_manager",
            task_id=f"issue_{issue_number}",
            project=project,
            branch_name=feature_branch.branch_name,
            reason=f"Using parent feature branch for issue #{issue_number} ({reason_suffix})",
            issue_number=issue_number,
            parent_issue=parent_issue,
            is_new=is_new_branch
        )

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

        feature_branch = await self.get_feature_branch_for_issue(project, issue_number, github_integration)

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

        # Step 1: Get the actual current branch (git is source of truth)
        current_branch = await self.get_current_branch(project_dir)

        # Step 2: Trust git - use whatever branch we're currently on
        # The feature_branch object now comes from git queries, so it should match
        # But if there's any mismatch, git wins
        if current_branch != feature_branch.branch_name:
            logger.warning(
                f"Current branch '{current_branch}' doesn't match feature branch '{feature_branch.branch_name}'. "
                f"Git is the source of truth - using current branch '{current_branch}'."
            )
            feature_branch.branch_name = current_branch

        # Step 2: Commit changes
        await self.git_add_all(project_dir)
        await self.git_commit(project_dir, commit_message)

        # Step 3: Verify branch exists before pushing
        branch_exists = await self.branch_exists(project_dir, feature_branch.branch_name)
        if not branch_exists:
            error_msg = f"Branch {feature_branch.branch_name} does not exist locally, cannot push"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

        # Step 4: Push to remote
        push_success = await self.git_push(project_dir, feature_branch.branch_name)
        
        if not push_success:
            error_msg = f"Failed to push branch {feature_branch.branch_name}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

        logger.info(f"Pushed changes for issue #{issue_number} to {feature_branch.branch_name}")

        # Step 5: Update sub-issue status
        self.mark_sub_issue_complete(project, feature_branch, issue_number)

        # Step 6: Create or update PR
        pr_result = await self.create_or_update_feature_pr(
            project=project,
            feature_branch=feature_branch,
            github_integration=github_integration
        )

        # Step 7: Check if all sub-issues complete
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

    async def detect_and_clean_invalid_branches(
        self,
        project: str,
        project_dir: str,
        github_integration
    ) -> Dict[str, List[str]]:
        """
        Detect and optionally clean invalid branches (issues that don't exist, etc.)

        Returns dict with 'cleaned' and 'errors' lists
        """
        import re

        all_branches = await self.get_all_feature_branches_for_project(project_dir)
        cleaned = []
        errors = []

        for branch in all_branches:
            # Extract issue number from branch name
            match = re.search(r'issue-(\d+)', branch)
            if not match:
                continue

            issue_num = int(match.group(1))

            # Skip if issue number is invalid
            if issue_num <= 0:
                logger.warning(f"Branch {branch} has invalid issue number: {issue_num}")
                try:
                    # Try to delete both local and remote
                    from services.git_workflow_manager import git_workflow_manager
                    await git_workflow_manager.checkout_branch(project_dir, "main")

                    # Delete local branch
                    import subprocess
                    subprocess.run(
                        ["git", "branch", "-D", branch],
                        cwd=project_dir,
                        capture_output=True,
                        check=False
                    )

                    # Delete remote branch
                    try:
                        await github_integration.delete_branch(branch)
                    except Exception:
                        pass  # Remote might not exist

                    cleaned.append(branch)
                    logger.info(f"Cleaned invalid branch: {branch}")
                except Exception as e:
                    errors.append(f"{branch}: {e}")
                    logger.error(f"Failed to clean branch {branch}: {e}")
                continue

            # Check if issue exists
            try:
                issue = await github_integration.get_issue(issue_num)
                if not issue:
                    logger.warning(f"Branch {branch} references non-existent issue #{issue_num}")
            except Exception as e:
                logger.error(f"Cannot verify issue #{issue_num} for branch {branch}: {e}")

        return {"cleaned": cleaned, "errors": errors}


# Global instance
feature_branch_manager = FeatureBranchManager()

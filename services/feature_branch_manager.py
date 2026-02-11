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
import time
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple
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

        # In-memory cache for parent issue lookups with TTL
        # Cache structure: {issue_number: (parent_number, cached_at_timestamp)}
        # Reduces GitHub API usage by caching stable parent relationships
        self._parent_cache: Dict[int, Tuple[Optional[int], float]] = {}
        self._parent_cache_ttl = 3600  # 1 hour in seconds
        self._parent_cache_max_size = 1000  # Prevent unbounded growth

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
            fetch_result = subprocess.run(
                ["git", "fetch", "--prune"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False  # Don't fail if fetch has issues
            )

            if fetch_result.returncode != 0:
                logger.warning(
                    f"git fetch --prune failed in {project_dir}: {fetch_result.stderr.strip()}. "
                    f"Branch detection may be incomplete. Continuing with local branches only."
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

        DEPRECATED: This method is unreliable because sub_issues list may be empty.
        Use _verify_all_sub_issues_complete instead to check GitHub directly.
        """
        return all(
            si.status in ["completed", "cancelled"]
            for si in feature_branch.sub_issues
        )

    async def _get_sub_issues_from_parent(self, github_integration, parent_issue_data: dict) -> List[dict]:
        """
        Extract sub-issues from GitHub's native sub-issue API.

        Uses GitHub's structured subIssues field via GraphQL to query child issues.
        This is reliable structured data, not parsed from issue body checkboxes.

        Args:
            github_integration: GitHubIntegration instance
            parent_issue_data: Parent issue data from GitHub API (must contain 'number' key)

        Returns:
            List of issue data dicts for each sub-issue
        """
        parent_number = parent_issue_data.get('number')

        if not parent_number:
            logger.error("parent_issue_data missing 'number' key, cannot query sub-issues")
            return []

        # Query GitHub's structured subIssues field via GraphQL
        try:
            from services.github_api_client import get_github_client
            github_client = get_github_client()

            query = '''
            query($owner: String!, $repo: String!, $issueNumber: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $issueNumber) {
                  number
                  subIssues(first: 100) {
                    totalCount
                    nodes {
                      number
                      title
                      state
                      url
                    }
                  }
                }
              }
            }
            '''

            variables = {
                "owner": github_integration.github_org,
                "repo": github_integration.repo_name,
                "issueNumber": parent_number
            }

            success, result = github_client.graphql(query, variables)

            if not success:
                logger.error(f"GraphQL query failed for issue #{parent_number} sub-issues: {result}")
                return []

            # Extract sub-issues from response
            # Note: github_client.graphql() already extracts 'data' field, so access directly
            issue_data = result.get('repository', {}).get('issue', {})
            sub_issues_data = issue_data.get('subIssues', {})
            total_count = sub_issues_data.get('totalCount', 0)
            sub_issues = sub_issues_data.get('nodes', [])

            if sub_issues:
                logger.info(
                    f"Found {len(sub_issues)} sub-issues for parent #{parent_number} "
                    f"(total: {total_count}) via GitHub structured API"
                )
                for sub_issue in sub_issues:
                    logger.debug(
                        f"  Sub-issue #{sub_issue['number']}: {sub_issue.get('title', 'N/A')} "
                        f"(state: {sub_issue.get('state', 'unknown')})"
                    )
            else:
                logger.debug(f"Issue #{parent_number} has no sub-issues")

            return sub_issues

        except Exception as e:
            logger.error(f"Failed to query sub-issues for parent #{parent_number}: {e}")
            return []

    async def _verify_all_sub_issues_complete(
        self,
        github_integration,
        sub_issues: List[dict],
        project_name: Optional[str] = None,
        workflow_template = None,
        project_monitor = None,
        triggering_issue: Optional[int] = None
    ) -> bool:
        """
        Verify that all sub-issues are complete (closed OR in exit columns).

        An issue is considered complete if:
        1. Its GitHub state is 'closed' (case-insensitive), OR
        2. It is the triggering issue (just moved to exit column), OR
        3. It's currently in a pipeline exit column (Done, Staged, etc.)

        Args:
            github_integration: GitHubIntegration instance
            sub_issues: List of issue data dicts from GitHub
            project_name: Project name (optional, for exit column check)
            workflow_template: Workflow template with exit columns (optional)
            project_monitor: ProjectMonitor instance (optional, for querying issue columns)
            triggering_issue: Issue number that just moved to an exit column (optional).
                Skip re-querying this issue's column to avoid GitHub API eventual consistency lag.

        Returns:
            True if all sub-issues are complete, False otherwise
        """
        if not sub_issues:
            # No sub-issues means nothing to complete
            return False

        # Performance optimization: Fetch board name and issue columns once before the loop
        board_name = None
        issue_columns = {}  # Cache of issue_number -> column_name

        if project_name and workflow_template and project_monitor:
            if hasattr(workflow_template, 'pipeline_exit_columns') and workflow_template.pipeline_exit_columns:
                try:
                    # Find the board name for this project's dev workflow
                    from config.manager import config_manager
                    project_config = config_manager.get_project_config(project_name)

                    # Use consistent lookup strategy: check for 'sdlc' or 'dev' in pipeline name/workflow
                    for pipeline in project_config.pipelines:
                        if 'sdlc' in pipeline.name.lower() or 'dev' in pipeline.workflow.lower():
                            board_name = pipeline.board_name
                            break

                    if not board_name:
                        logger.warning(
                            f"Could not find dev/SDLC board for workflow '{workflow_template.name}' "
                            f"in project '{project_name}' - exit column check will be skipped"
                        )
                    else:
                        # Batch fetch: Get columns for all sub-issues at once
                        # Skip the triggering issue — its column may not be consistent yet
                        issues_to_fetch = [
                            i for i in sub_issues
                            if triggering_issue is None or i.get('number') != triggering_issue
                        ]
                        logger.debug(f"Fetching columns for {len(issues_to_fetch)} sub-issues from board '{board_name}'")
                        for issue in issues_to_fetch:
                            try:
                                column_name = await project_monitor.get_issue_column_async(
                                    project_name,
                                    board_name,
                                    issue.get('number')
                                )
                                if column_name:
                                    issue_columns[issue.get('number')] = column_name
                            except Exception as e:
                                logger.debug(f"Could not get column for sub-issue #{issue.get('number')}: {e}")

                except Exception as e:
                    logger.warning(f"Error setting up exit column check: {e}")

        # Now check each sub-issue for completion
        for issue in sub_issues:
            issue_number = issue.get('number')
            state = issue.get('state')

            # Check 1: Issue is closed (case-insensitive — GitHub GraphQL returns uppercase)
            if state and state.upper() == 'CLOSED':
                logger.debug(f"Sub-issue #{issue_number} is closed - treating as complete")
                continue

            # Check 2: Issue is the one that just triggered this check (skip API re-query
            # to avoid GitHub Projects v2 eventual consistency lag)
            if triggering_issue is not None and issue_number == triggering_issue:
                logger.info(
                    f"Sub-issue #{issue_number} is the triggering issue "
                    f"(just moved to exit column) - treating as complete"
                )
                continue

            # Check 3: Issue is in an exit column (using pre-fetched data)
            if issue_number in issue_columns:
                column_name = issue_columns[issue_number]
                if column_name in workflow_template.pipeline_exit_columns:
                    logger.info(
                        f"Sub-issue #{issue_number} is in exit column '{column_name}' "
                        f"(state={state}) - treating as complete"
                    )
                    continue  # Treat as complete

            # If we get here, issue is neither closed nor in exit column
            logger.debug(f"Sub-issue #{issue_number} is not complete (state={state}, not in exit column)")
            return False

        return True

    async def get_parent_issue(self, github_integration, issue_number: int, project: Optional[str] = None) -> Optional[int]:
        """
        Get parent issue number from GitHub's structured parent field

        Uses GitHub's native sub-issues API via GraphQL to query the parent field.
        This is reliable structured data, not parsed from issue body text.

        Caches results with 1-hour TTL to reduce GitHub API usage.

        Returns parent issue number if found, None otherwise
        """
        # Check cache with TTL before making API call
        if issue_number in self._parent_cache:
            parent_num, cached_at = self._parent_cache[issue_number]
            age = time.time() - cached_at

            if age < self._parent_cache_ttl:
                logger.debug(
                    f"Cache hit for issue #{issue_number} parent: #{parent_num} "
                    f"(age: {age:.0f}s, TTL: {self._parent_cache_ttl}s)"
                )
                return parent_num
            else:
                logger.debug(
                    f"Cache expired for issue #{issue_number} parent "
                    f"(age: {age:.0f}s > TTL: {self._parent_cache_ttl}s)"
                )
                del self._parent_cache[issue_number]  # Clean up expired entry

        # Validate repository information before making API calls
        if not github_integration.github_org or not github_integration.repo_name:
            logger.warning(
                f"Cannot get parent issue for #{issue_number}: "
                f"github_org={github_integration.github_org}, repo_name={github_integration.repo_name}"
            )
            return None

        # Query GitHub's structured parent field via GraphQL
        try:
            github_client = get_github_client()

            query = '''
            query($owner: String!, $repo: String!, $issueNumber: Int!) {
              repository(owner: $owner, name: $repo) {
                issue(number: $issueNumber) {
                  number
                  parent {
                    ... on Issue {
                      number
                      title
                    }
                  }
                }
              }
            }
            '''

            variables = {
                "owner": github_integration.github_org,
                "repo": github_integration.repo_name,
                "issueNumber": issue_number
            }

            success, result = github_client.graphql(query, variables)

            if not success:
                logger.error(f"GraphQL query failed for issue #{issue_number} parent: {result}")
                return None

            # Extract parent from response
            # Note: github_client.graphql() already extracts 'data' field, so access directly
            issue_data = result.get('repository', {}).get('issue', {})
            parent_data = issue_data.get('parent')

            if parent_data and 'number' in parent_data:
                parent_num = parent_data['number']
                parent_title = parent_data.get('title', 'Unknown')
                logger.info(
                    f"Issue #{issue_number} is sub-issue of parent #{parent_num} "
                    f"('{parent_title}') via GitHub structured API"
                )

                # Cache the result with current timestamp
                self._parent_cache[issue_number] = (parent_num, time.time())

                # Enforce max cache size with simple FIFO eviction
                if len(self._parent_cache) > self._parent_cache_max_size:
                    # Remove oldest 10% of entries
                    entries_to_remove = len(self._parent_cache) - self._parent_cache_max_size + 100
                    oldest_keys = sorted(
                        self._parent_cache.keys(),
                        key=lambda k: self._parent_cache[k][1]  # Sort by timestamp
                    )[:entries_to_remove]
                    for key in oldest_keys:
                        del self._parent_cache[key]
                    logger.debug(f"Evicted {len(oldest_keys)} old parent cache entries")

                return parent_num

            # No parent found - cache this result too (None with timestamp)
            logger.debug(f"Issue #{issue_number} has no parent (structured API returned null)")
            self._parent_cache[issue_number] = (None, time.time())
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
        import subprocess
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

            # Verify tracking is set up (checkout_branch should have done this, but verify)
            # If remote doesn't exist, push it now to ensure tracking works
            check_tracking = subprocess.run(
                ['git', 'config', '--get', f'branch.{branch_name}.remote'],
                cwd=project_dir,
                capture_output=True,
                timeout=10
            )

            if check_tracking.returncode != 0:
                # No tracking configured - push to set it up
                logger.warning(f"Existing branch {branch_name} has no tracking, pushing to remote")
                push_success = await git_workflow_manager.push_branch(project_dir, branch_name)
                if not push_success:
                    raise Exception(f"Failed to push existing branch {branch_name} to set up tracking")
                logger.info(f"Pushed existing branch {branch_name} and configured tracking")
        else:
            await git_workflow_manager.checkout_branch(project_dir, branch_name)
            logger.info(f"Created branch {branch_name} from main")

            # Push branch to remote with -u to set up tracking
            # This is critical - without it, git pull will fail with "no tracking information"
            push_success = await git_workflow_manager.push_branch(project_dir, branch_name)
            if not push_success:
                raise Exception(f"Failed to push branch {branch_name} to remote")
            logger.info(f"Pushed branch {branch_name} to remote with upstream tracking")

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

        # Get pipeline_run_id for event tracking
        pipeline_run_id = None
        try:
            from services.pipeline_run import get_pipeline_run_manager
            prm = get_pipeline_run_manager()
            active_run = prm.get_active_pipeline_run(project, issue_number)
            if active_run:
                pipeline_run_id = active_run.id
        except Exception:
            pass  # pipeline_run_id remains None

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
        logger.info(
            f"Parent detection result for issue #{issue_number}: "
            f"parent_issue={parent_issue}, "
            f"org={github_integration.github_org}, "
            f"repo={github_integration.repo_name}"
        )

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
                    parent_issue=parent_issue,
                    pipeline_run_id=pipeline_run_id
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

                # Clean up any prompt file artifacts before pull rebase
                # These files can cause "uncommitted changes" errors during git pull --rebase
                try:
                    import glob
                    prompt_files = glob.glob(os.path.join(project_dir, '.claude_prompt_*.txt'))
                    for prompt_file in prompt_files:
                        try:
                            os.remove(prompt_file)
                            logger.info(f"Cleaned up prompt artifact before pull: {os.path.basename(prompt_file)}")
                        except Exception as e:
                            logger.warning(f"Failed to clean prompt file {prompt_file}: {e}")
                except Exception as e:
                    logger.warning(f"Error during prompt file cleanup: {e}")

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
                        parent_issue=parent_issue,
                        pipeline_run_id=pipeline_run_id
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
                            parent_issue=parent_issue,
                            pipeline_run_id=pipeline_run_id
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
                            parent_issue=parent_issue,
                            pipeline_run_id=pipeline_run_id
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
                    reason=f"Medium confidence match ({best_match['confidence']:.0%}), escalating to human",
                    pipeline_run_id=pipeline_run_id
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
                    is_standalone=True,
                    pipeline_run_id=pipeline_run_id
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

        # DEFENSIVE CHECK: If state lookup failed, verify branch doesn't exist in git
        # This prevents duplicate branch creation when get_feature_branch_state()
        # returns None due to cache miss or fetch failure
        if not feature_branch:
            logger.info(
                f"No cached state found for parent #{parent_issue}. "
                f"Performing direct git check before creating new branch."
            )

            # Direct git query bypassing cache
            all_branches = self._get_all_feature_branches_sync(project_dir)
            existing_parent_branch = None

            for branch in all_branches:
                # Match pattern: feature/issue-{parent_issue} or feature/issue-{parent_issue}-*
                issue_num = self._parse_issue_from_branch_name(branch)
                if issue_num == parent_issue:
                    existing_parent_branch = branch
                    logger.info(
                        f"✓ Found existing parent branch in git: {branch}. "
                        f"Issue #{issue_number} will reuse this branch instead of creating new one."
                    )
                    break

            if existing_parent_branch:
                # Parent branch exists - create state object and reuse it
                feature_branch = FeatureBranch(
                    parent_issue=parent_issue,
                    branch_name=existing_parent_branch,
                    created_at=datetime.now().isoformat(),
                    sub_issues=[issue_number]
                )

                # Update cache to prevent future misses
                self._cache_branch(project, parent_issue, existing_parent_branch)

                # Checkout existing branch
                await self.git_checkout(project_dir, existing_parent_branch)

                # Track in git workflow manager
                from services.git_workflow_manager import git_workflow_manager
                git_workflow_manager.track_branch(project, parent_issue, existing_parent_branch)

                # Emit observability event
                self.decision_events.emit_branch_reused(
                    project=project,
                    issue_number=issue_number,
                    branch_name=existing_parent_branch,
                    reason=f"Found existing parent #{parent_issue} branch via direct git check",
                    parent_issue=parent_issue,
                    pipeline_run_id=pipeline_run_id
                )

                logger.info(
                    f"Successfully attached issue #{issue_number} to existing parent branch: {existing_parent_branch}"
                )

        # Original "if not feature_branch:" block continues here
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
                is_standalone=False,
                pipeline_run_id=pipeline_run_id
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

        # Clean up any prompt file artifacts before pull rebase
        # These files can cause "uncommitted changes" errors during git pull --rebase
        try:
            import glob
            prompt_files = glob.glob(os.path.join(project_dir, '.claude_prompt_*.txt'))
            for prompt_file in prompt_files:
                try:
                    os.remove(prompt_file)
                    logger.info(f"Cleaned up prompt artifact before pull: {os.path.basename(prompt_file)}")
                except Exception as e:
                    logger.warning(f"Failed to clean prompt file {prompt_file}: {e}")
        except Exception as e:
            logger.warning(f"Error during prompt file cleanup: {e}")

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
                parent_issue=parent_issue,
                pipeline_run_id=pipeline_run_id
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
                parent_issue=parent_issue,
                pipeline_run_id=pipeline_run_id
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
                parent_issue=parent_issue,
                pipeline_run_id=pipeline_run_id
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
                # Clean up ALL prompt files BEFORE staging to prevent accidental commits
                try:
                    import glob
                    prompt_files = glob.glob(os.path.join(project_dir, '.claude_prompt_*.txt'))
                    for prompt_file in prompt_files:
                        try:
                            os.remove(prompt_file)
                            logger.info(f"Cleaned up prompt file before commit: {os.path.basename(prompt_file)}")
                        except Exception as e:
                            logger.warning(f"Failed to remove prompt file {prompt_file}: {e}")
                    if prompt_files:
                        logger.info(f"Removed {len(prompt_files)} prompt file(s) before staging changes")
                except Exception as e:
                    logger.warning(f"Error during pre-commit prompt file cleanup: {e}")

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

        # Step 2: Clean up ALL prompt files BEFORE staging to prevent accidental commits
        # This is CRITICAL: git add . will stage prompt files if they exist
        try:
            import glob
            prompt_files = glob.glob(os.path.join(project_dir, '.claude_prompt_*.txt'))
            for prompt_file in prompt_files:
                try:
                    os.remove(prompt_file)
                    logger.info(f"Cleaned up prompt file before commit: {os.path.basename(prompt_file)}")
                except Exception as e:
                    logger.warning(f"Failed to remove prompt file {prompt_file}: {e}")
            if prompt_files:
                logger.info(f"Removed {len(prompt_files)} prompt file(s) before staging changes")
        except Exception as e:
            logger.warning(f"Error during pre-commit prompt file cleanup: {e}")

        # Step 3: Commit changes
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

        # Handle PR creation/update failure gracefully
        if not pr_result.get('success', True):  # Default True for backward compat
            logger.warning(
                f"PR operation failed for issue #{issue_number}: {pr_result.get('error', 'Unknown')}"
            )
            # Return partial success - changes were committed and pushed, but PR failed
            return {
                "success": True,  # Git operations succeeded
                "branch_name": feature_branch.branch_name,
                "pr_failed": True,
                "pr_error": pr_result.get('error', 'Unknown error'),
                "all_complete": False  # Can't mark complete without PR
            }

        # Step 7: Check if all sub-issues complete
        # CRITICAL: Check completion after EVERY finalization (parent or sub-issue)
        # This ensures PRs are marked ready as soon as the last sub-issue completes
        is_parent_issue = (issue_number == feature_branch.parent_issue)

        # Query GitHub to get the ACTUAL current state of all sub-issues
        try:
            parent_issue_data = await github_integration.get_issue(feature_branch.parent_issue)
            actual_sub_issues = await self._get_sub_issues_from_parent(github_integration, parent_issue_data)

            # Get workflow template for exit column check
            # Note: We don't pass project_monitor here, so exit column check won't work
            # The delayed check in project_monitor._check_pr_ready_on_issue_exit() handles it
            workflow_template = None
            try:
                from config.manager import config_manager
                project_config = config_manager.get_project_config(project)
                # Find the SDLC/dev pipeline workflow (consistent lookup strategy)
                for pipeline in project_config.pipelines:
                    if 'sdlc' in pipeline.name.lower() or 'dev' in pipeline.workflow.lower():
                        workflow_template = config_manager.get_workflow_template(pipeline.workflow)
                        break
            except Exception as e:
                logger.debug(f"Could not get workflow template for exit column check: {e}")

            # Check if ALL sub-issues are actually complete in GitHub
            # Note: project_monitor=None here means exit column check won't work in this path
            # The delayed check in project_monitor._check_pr_ready_on_issue_exit() will handle it
            all_complete = await self._verify_all_sub_issues_complete(
                github_integration,
                actual_sub_issues,
                project_name=project,
                workflow_template=workflow_template,
                project_monitor=None
            )

            # Issue is complete if: no sub-issues defined (standalone work) OR all sub-issues are complete
            if len(actual_sub_issues) == 0 or all_complete:
                if len(actual_sub_issues) == 0:
                    logger.info(
                        f"Parent issue #{feature_branch.parent_issue} has no sub-issues (standalone work) - marking PR ready"
                    )
                else:
                    logger.info(
                        f"All {len(actual_sub_issues)} sub-issues complete for parent #{feature_branch.parent_issue} "
                        f"(triggered by finalizing issue #{issue_number})"
                    )

                # Mark PR as ready for review
                if feature_branch.pr_number:
                    success = await github_integration.mark_pr_ready(feature_branch.pr_number)

                    if success:
                        feature_branch.pr_status = "ready"
                        self.save_feature_branch_state(project, feature_branch)
                        logger.info(f"✓ Successfully marked PR #{feature_branch.pr_number} as ready for review")

                        # Post completion comment to parent issue
                        # Only post once - check if we already posted by looking for existing comment
                        await self.post_feature_completion_comment(
                            github_integration,
                            feature_branch.parent_issue,
                            pr_result.get("pr_url")
                        )
                    else:
                        # Log prominent error and post warning to parent issue
                        logger.error(
                            f"✗ FAILED to mark PR #{feature_branch.pr_number} as ready for review. "
                            f"All sub-issues are complete but GitHub API call failed. "
                            f"Manual intervention required."
                        )

                        # Post warning comment to parent issue
                        await github_integration.add_comment(
                            feature_branch.parent_issue,
                            f"⚠️ **Warning**: All sub-issues have been completed, but the system failed to mark "
                            f"PR #{feature_branch.pr_number} as ready for review. Please manually mark it ready:\n\n"
                            f"```\ngh pr ready {feature_branch.pr_number}\n```"
                        )

                        # Keep PR status as draft so we can retry later
                        feature_branch.pr_status = "draft"
                        self.save_feature_branch_state(project, feature_branch)
            else:
                logger.debug(
                    f"Not all sub-issues complete for parent #{feature_branch.parent_issue} "
                    f"(complete: {sum(1 for si in actual_sub_issues if (si.get('state') or '').upper() == 'CLOSED')}/{len(actual_sub_issues)}) "
                    f"- just finalized issue #{issue_number}"
                )
                all_complete = False
        except Exception as e:
            logger.error(f"Failed to check sub-issue completion for parent #{feature_branch.parent_issue}: {e}")
            all_complete = False

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
            # Attempt to create new PR (will reuse existing if found)
            result = await github_integration.create_pr(
                branch=feature_branch.branch_name,
                title=f"[Feature] {parent_issue.get('title', 'Feature')}",
                body=pr_body,
                draft=True
            )

            if not result.get('success'):
                logger.error(
                    f"Failed to create/find PR for {feature_branch.branch_name}: "
                    f"{result.get('error', 'Unknown error')}"
                )
                return result

            # Update state with PR number (whether it was created or found)
            feature_branch.pr_number = result["pr_number"]
            feature_branch.pr_status = "draft"
            self.save_feature_branch_state(project, feature_branch)

            if result.get('already_existed'):
                logger.info(
                    f"Found existing PR #{result['pr_number']} for parent #{feature_branch.parent_issue}"
                )
            else:
                logger.info(
                    f"Created PR #{result['pr_number']} for parent #{feature_branch.parent_issue}"
                )

            return result
        else:
            # Update existing PR description
            update_success = await github_integration.update_pr_body(feature_branch.pr_number, pr_body)

            if not update_success:
                logger.error(f"Failed to update PR #{feature_branch.pr_number}")
                return {
                    'success': False,
                    'error': f"Failed to update PR #{feature_branch.pr_number}",
                    'pr_number': feature_branch.pr_number
                }

            logger.info(f"Updated PR #{feature_branch.pr_number} with latest sub-issue status")

            return {
                'success': True,
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

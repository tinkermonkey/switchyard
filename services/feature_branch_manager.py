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


class StaleBranchError(Exception):
    """Raised when a feature branch is too many commits behind main to proceed safely."""
    def __init__(self, message: str, branch_name: str, commits_behind: int, parent_issue: int = None):
        super().__init__(message)
        self.branch_name = branch_name
        self.commits_behind = commits_behind
        self.parent_issue = parent_issue


class BranchPullFailedError(Exception):
    """Raised when git pull of main fails during branch creation, leaving local main potentially stale."""
    pass


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


# ---------------------------------------------------------------------------
# Batched / aliased cross-parent sub-issue query builder (GitHub issue #95,
# sub-issue of #36)
#
# FeatureBranchManager._get_sub_issues_from_parent() below issues one
# GraphQL request per parent epic. get_sub_issues_for_parents_batched()
# fetches sub-issues for multiple parent issues in as few GraphQL requests
# as possible by aliasing each parent's `issue(number: ...)` field under one
# `repository(owner, name)` root, mirroring the aliased-batch pattern
# build_batched_projects_v2_query() / parse_batched_projects_v2_response()
# established in services/github_owner_utils.py for issue #92's board-query
# batching (same partial-failure error-attribution-by-path approach).
#
# Unlike issue #92's board queries - which batch across *owners* because a
# single poll cycle can hold boards belonging to many different GitHub
# owners at once - a GitHubIntegration instance is single-repo scoped (one
# github_org/repo_name pair, see GitHubIntegration.__init__ in
# services/github_integration.py) and every caller of
# _get_sub_issues_from_parent() found in this codebase
# (services/project_monitor.py, services/scheduled_tasks.py) constructs one
# GitHubIntegration per project/repo. A batch of parent_issue_numbers is
# therefore already scoped to a single owner/repo by construction of the
# github_integration argument, so there is no (owner, repo) grouping step
# here - only chunking.
#
# These two helpers are pure/free functions (no GitHub client access) so
# they can be unit tested directly, independent of
# get_sub_issues_for_parents_batched()'s network call.
# ---------------------------------------------------------------------------

# GitHub's GraphQL API enforces per-query node-count, response-size and
# execution-time limits, same rationale as MAX_BOARDS_PER_BATCH in
# services/github_owner_utils.py. 12 is a conservative starting point
# chosen without a production token to test against; tunable and should be
# re-verified against GitHub's actual limits once one is available.
MAX_SUB_ISSUE_PARENTS_PER_BATCH = 12


def _build_batched_sub_issues_query(parent_issue_numbers: List[int]) -> str:
    """
    Build ONE aliased GraphQL query document fetching sub-issues for
    multiple parent issues (in one repo) in a single request.

    Each parent issue number is exposed under its own alias (`i<number>`)
    so all parents can be selected under one `repository(owner, name)` root
    field, e.g.:

        query($owner: String!, $repo: String!) {
          repository(owner: $owner, name: $repo) {
            i123: issue(number: 123) { number subIssues(first: 100) { totalCount nodes { number title state url } } }
            i456: issue(number: 456) { number subIssues(first: 100) { totalCount nodes { number title state url } } }
          }
        }

    This does not chunk `parent_issue_numbers` itself - callers with more
    than MAX_SUB_ISSUE_PARENTS_PER_BATCH parents should chunk before calling
    (see get_sub_issues_for_parents_batched(), which does this).

    Args:
        parent_issue_numbers: Parent issue numbers to alias into this query.
            Must be non-empty.

    Returns:
        GraphQL query string, parameterized on $owner/$repo (values are
        supplied as query variables by the caller, not interpolated here).
    """
    aliased_fields = "\n".join(
        f'''    i{number}: issue(number: {number}) {{
      number
      subIssues(first: 100) {{
        totalCount
        nodes {{
          number
          title
          state
          url
        }}
      }}
    }}'''
        for number in parent_issue_numbers
    )

    return f'''query($owner: String!, $repo: String!) {{
  repository(owner: $owner, name: $repo) {{
{aliased_fields}
  }}
}}'''


def _parse_batched_sub_issues_response(
    response: Optional[dict],
    parent_issue_numbers: List[int],
) -> Tuple[Dict[int, List[dict]], Dict[int, str]]:
    """
    Parse one batched/aliased sub-issues GraphQL response into per-parent
    sub-issue lists.

    Handles partial-batch failure the same way
    parse_batched_projects_v2_response() (services/github_owner_utils.py)
    does: a response for a multi-alias query can have `data` populated for
    some aliases and a top-level `errors` array naming the alias(es) that
    failed via each error's `path`. Every alias not implicated by an error
    is still parsed and returned, so one bad parent never loses the rest of
    the batch.

    Args:
        response: The raw response for the query built from
            `parent_issue_numbers` via _build_batched_sub_issues_query().
            Accepts either shape GitHubAPIClient.graphql() can hand back:
            - the full GraphQL envelope `{'data': {...}, 'errors': [...]}`
              (returned on partial/total GraphQL failure, success=False), or
            - just the unwrapped data payload, e.g. `{'repository': {...}}`
              (returned on full success, success=True).
        parent_issue_numbers: The parent issue numbers the query was built
            from (same list passed to _build_batched_sub_issues_query()).

    Returns:
        Tuple of:
        - results: dict of parent_issue_number -> list of sub-issue dicts
          (each {'number', 'title', 'state', 'url'}), matching exactly what
          _get_sub_issues_from_parent() returns for one parent, for every
          alias that parsed successfully.
        - errors: dict of parent_issue_number -> error message string for
          every alias that failed, whether via an attributable top-level
          GraphQL error or because the alias was simply missing from the
          response.
    """
    results: Dict[int, List[dict]] = {}
    errors: Dict[int, str] = {}

    if not parent_issue_numbers:
        return results, errors

    if response is None:
        for number in parent_issue_numbers:
            errors[number] = "No response received"
        return results, errors

    # Normalize both response shapes described above into (data, errors_list).
    # Checking for 'errors' as well as 'data' matters: a pre-execution
    # GraphQL validation error (e.g. a malformed alias) comes back as
    # {'errors': [...]} with NO 'data' key at all - treating that as the
    # unwrapped-success shape (the old `if 'data' in response` check) would
    # silently drop the real error and report every parent in the chunk as
    # generically "missing", masking the actual failure.
    if 'data' in response or 'errors' in response:
        data = response.get('data') or {}
        errors_list = response.get('errors') or []
    else:
        data = response
        errors_list = []

    alias_to_parent = {f'i{number}': number for number in parent_issue_numbers}

    # Attribute each top-level GraphQL error to the parent(s) named in its path.
    for error in errors_list:
        if isinstance(error, dict):
            message = error.get('message', 'Unknown GraphQL error')
            path = error.get('path') or []
        else:
            message = str(error)
            path = []

        matched = False
        for segment in path:
            if isinstance(segment, str) and segment in alias_to_parent:
                parent_number = alias_to_parent[segment]
                errors.setdefault(parent_number, message)  # keep the first error per parent
                matched = True
                break

        if not matched:
            logger.warning(f"Batched sub-issues query returned an error with no attributable parent path: {message}")

    repository = data.get('repository') if isinstance(data, dict) else None
    if not isinstance(repository, dict):
        repository = {}

    for number in parent_issue_numbers:
        if number in errors:
            continue  # already recorded via error path attribution above

        alias = f'i{number}'
        issue_data = repository.get(alias)

        if issue_data is None:
            errors[number] = (
                f"No data returned for alias '{alias}' (issue #{number} may not exist, "
                f"or the repository root field was missing from the response)"
            )
            continue

        sub_issues_data = issue_data.get('subIssues', {}) if isinstance(issue_data, dict) else {}
        sub_issues = sub_issues_data.get('nodes', []) if isinstance(sub_issues_data, dict) else []
        results[number] = sub_issues

    return results, errors


class FeatureBranchManager:
    """Manages feature branch lifecycle for parent/sub-issue workflows"""

    def __init__(self, workspace_root: str = "/workspace"):
        self.workspace_root = workspace_root

        # In-memory cache for branch discovery (ephemeral, process-lifetime)
        # Cache structure: {(project, parent_issue): "branch_name"}
        self._branch_cache: Dict[tuple, str] = {}

        # In-memory cache for parent issue lookups with TTL
        # Cache structure: {(repo_owner, repo_name, issue_number): (parent_number, cached_at_timestamp)}
        # Keyed by repo identity, not just issue_number: this is a shared module-level
        # singleton across every monitored project, and issue numbers are only unique
        # within a single repo -- a bare issue_number key would let two different
        # projects' issue #N collide and return each other's cached parent.
        self._parent_cache: Dict[Tuple[str, str, int], Tuple[Optional[int], float]] = {}
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
        # NOTE: GitHub's GraphQL API may cache responses. If stale data is suspected,
        # consider adding cache control headers or using conditional requests.
        # The query timestamp logged below helps debug cache-related issues.
        try:
            from services.github_api_client import get_github_client
            github_client = get_github_client()

            query_start = time.time()
            logger.debug(
                f"🔍 Querying GitHub GraphQL for sub-issues of #{parent_number} "
                f"(query_ts={query_start:.3f})"
            )

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
            query_duration = (time.time() - query_start) * 1000  # milliseconds

            if not success:
                logger.error(
                    f"GraphQL query failed for issue #{parent_number} sub-issues "
                    f"(duration={query_duration:.0f}ms): {result}"
                )
                return []

            # Extract sub-issues from response
            # Note: github_client.graphql() already extracts 'data' field, so access directly
            issue_data = result.get('repository', {}).get('issue', {})
            sub_issues_data = issue_data.get('subIssues', {})
            total_count = sub_issues_data.get('totalCount', 0)
            sub_issues = sub_issues_data.get('nodes', [])

            if sub_issues:
                logger.info(
                    f"🔍 Found {len(sub_issues)} sub-issues for parent #{parent_number} "
                    f"(total: {total_count}, duration={query_duration:.0f}ms) via GitHub structured API"
                )
                for sub_issue in sub_issues:
                    logger.debug(
                        f"  Sub-issue #{sub_issue['number']}: {sub_issue.get('title', 'N/A')} "
                        f"(state: {sub_issue.get('state', 'unknown')})"
                    )
            else:
                logger.info(
                    f"🔍 Issue #{parent_number} has no sub-issues "
                    f"(duration={query_duration:.0f}ms, totalCount={total_count})"
                )

            return sub_issues

        except Exception as e:
            logger.error(f"Failed to query sub-issues for parent #{parent_number}: {e}")
            return []

    async def get_sub_issues_for_parents_batched(
        self, github_integration, parent_issue_numbers: List[int]
    ) -> Dict[int, List[dict]]:
        """
        Fetch sub-issues for multiple parent epics in as few GraphQL
        requests as possible, instead of issuing one
        _get_sub_issues_from_parent()-style request per parent.

        Additive: does not change _get_sub_issues_from_parent()'s existing
        behavior/signature or any of its existing callers - this is a
        separate, opt-in batched primitive. Returns the exact same
        per-parent list shape _get_sub_issues_from_parent() returns for one
        parent (a list of {'number', 'title', 'state', 'url'} dicts), so
        _verify_all_sub_issues_complete() (or any other consumer of that
        shape) can be handed either this function's per-parent values or
        _get_sub_issues_from_parent()'s return value interchangeably.

        Requests are chunked at MAX_SUB_ISSUE_PARENTS_PER_BATCH aliases per
        query (see module-level comment above FeatureBranchManager for why
        no (owner, repo) grouping is needed - github_integration is already
        single-repo scoped). A parent whose alias fails within an otherwise
        successful chunk (deleted/renamed issue, attributable GraphQL
        error) is simply omitted from the returned dict rather than raising
        - every other parent, in that chunk and every other chunk, is still
        returned. A parent in a chunk that fails entirely (rate limit,
        timeout, transport error) is likewise omitted. Callers should treat
        a missing key the same way they already tolerate
        _get_sub_issues_from_parent() returning an empty list, e.g. via
        `.get(parent_number, [])`.

        Args:
            github_integration: GitHubIntegration instance (single-repo
                scoped - github_org/repo_name). All parent_issue_numbers are
                queried against this one repo.
            parent_issue_numbers: Parent issue numbers to fetch sub-issues
                for. Duplicates are de-duplicated (first-seen order kept).
                Empty input returns an empty dict without making a request.

        Returns:
            Dict of parent_issue_number -> list of sub-issue dicts, for
            every parent that was successfully fetched. Parents that failed
            (see above) are absent from the dict.
        """
        deduped_numbers: List[int] = []
        seen_numbers = set()
        for number in parent_issue_numbers:
            # Matches _get_sub_issues_from_parent()'s own `if not parent_number`
            # guard: a falsy/invalid number interpolated into an aliased
            # GraphQL field name (e.g. `i-5: issue(number: -5)`) would produce
            # an invalid alias and break parsing of the WHOLE chunk it's in,
            # not just itself - violating this function's own per-parent
            # isolation guarantee - so invalid numbers are dropped here,
            # before ever reaching the query builder.
            if not number or not isinstance(number, int) or number <= 0:
                logger.error(f"Skipping invalid parent issue number in batched sub-issues request: {number!r}")
                continue
            if number in seen_numbers:
                continue
            seen_numbers.add(number)
            deduped_numbers.append(number)

        if not deduped_numbers:
            return {}

        results: Dict[int, List[dict]] = {}

        try:
            github_client = get_github_client()

            for start in range(0, len(deduped_numbers), MAX_SUB_ISSUE_PARENTS_PER_BATCH):
                chunk = deduped_numbers[start:start + MAX_SUB_ISSUE_PARENTS_PER_BATCH]
                query = _build_batched_sub_issues_query(chunk)
                variables = {
                    "owner": github_integration.github_org,
                    "repo": github_integration.repo_name,
                }

                query_start = time.time()
                logger.debug(
                    f"🔍 Querying GitHub GraphQL (batched) for sub-issues of parents {chunk} "
                    f"(query_ts={query_start:.3f})"
                )

                success, response = github_client.graphql(query, variables)
                query_duration = (time.time() - query_start) * 1000  # milliseconds

                if not success and not (isinstance(response, dict) and ('data' in response or 'errors' in response)):
                    # Total failure for this chunk (rate limit, timeout,
                    # transport error, JSON parse error, etc.) with no
                    # per-alias data to salvage - every parent in this chunk is
                    # simply omitted from the result, matching
                    # _get_sub_issues_from_parent()'s failure mode of returning
                    # [] on total failure.
                    message = response.get('error', str(response)) if isinstance(response, dict) else str(response)
                    logger.error(
                        f"Batched sub-issues GraphQL query failed for parents {chunk} "
                        f"(duration={query_duration:.0f}ms): {message}"
                    )
                    continue

                chunk_results, chunk_errors = _parse_batched_sub_issues_response(response, chunk)

                for parent_number, error_message in chunk_errors.items():
                    logger.error(
                        f"Batched sub-issues query: parent #{parent_number} failed "
                        f"(duration={query_duration:.0f}ms): {error_message}"
                    )

                if chunk_results:
                    total_found = sum(len(v) for v in chunk_results.values())
                    logger.info(
                        f"🔍 Batched sub-issues query fetched sub-issues for {len(chunk_results)}/{len(chunk)} "
                        f"parents ({total_found} sub-issues total, duration={query_duration:.0f}ms) "
                        f"via GitHub structured API"
                    )

                results.update(chunk_results)

        except Exception as e:
            # Matches _get_sub_issues_from_parent()'s own guarantee that this
            # never raises out to its caller (e.g. a malformed
            # github_integration missing github_org/repo_name) - degrade to
            # whatever chunks already succeeded rather than crashing.
            logger.error(f"Unexpected error in batched sub-issues fetch for parents {deduped_numbers}: {e}")

        return results

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
        # Check cache with TTL before making API call. Keyed by (repo_owner, repo_name,
        # issue_number) -- this manager is a shared singleton across every monitored
        # project, so a bare issue_number key would collide across repos.
        cache_key = (github_integration.repo_owner, github_integration.repo_name, issue_number)
        if cache_key in self._parent_cache:
            parent_num, cached_at = self._parent_cache[cache_key]
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
                del self._parent_cache[cache_key]  # Clean up expired entry

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
                self._parent_cache[cache_key] = (parent_num, time.time())

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
            self._parent_cache[cache_key] = (None, time.time())
            return None

        except Exception as e:
            logger.error(f"Failed to get parent issue for #{issue_number}: {e}")
            return None

    async def resolve_epic_id(
        self, github_integration, issue_number: int, project: Optional[str] = None
    ) -> str:
        """
        Resolve the epic id that should scope a per-epic git worktree for issue_number.

        Sub-issues (sdlc_execution dispatch) resolve to their parent epic's issue
        number. An issue with no parent (a planning_design epic dispatched directly,
        or a standalone issue not part of any epic) resolves to its own number, so
        every call site gets a consistent, non-empty epic_id to isolate a worktree by.

        Uses the same 1-hour-TTL-cached get_parent_issue lookup used everywhere else
        parent resolution happens, so calling this from multiple call sites in the
        same dispatch costs at most one real GitHub API call.
        """
        parent_issue = await self.get_parent_issue(github_integration, issue_number, project=project)
        return str(parent_issue) if parent_issue else str(issue_number)

    def resolve_epic_branch_name(self, project: str, epic_id: str) -> Optional[str]:
        """
        Read-only lookup of the epic's already-established shared branch, if any.

        Pure git query (cached branch state, or a plain `git branch` listing) with
        no side effects on the shared base clone -- never checks out or creates
        anything. Returns None when no branch has been created for this epic yet;
        callers creating the epic's worktree for the first time should fall back to
        create_feature_branch_name(int(epic_id), ...) for the canonical name. That
        fallback is safe to use without inventing a new naming scheme: the epic
        worktree's own `git worktree add -b` call is what actually creates the
        branch. (#124/WI-E, #119: prepare_feature_branch() and its shared-base-
        clone checkout, the only other thing that used to independently rediscover
        an epic's branch this way, was removed -- resolve_workspace() [pipeline_run.py]
        is now the sole caller of this method.)
        """
        existing = self.get_feature_branch_state(project, int(epic_id))
        return existing.branch_name if existing else None

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

    async def git_add_all(self, project_dir: str):
        """Stage all changes"""
        from services.git_workflow_manager import git_workflow_manager
        await git_workflow_manager.add_all(project_dir)

    async def git_commit(self, project_dir: str, message: str) -> bool:
        """Commit changes. Returns True if commit succeeded, False otherwise."""
        from services.git_workflow_manager import git_workflow_manager
        return await git_workflow_manager.commit(project_dir, message)

    async def git_push(self, project_dir: str, branch_name: str) -> None:
        """Push branch to remote. Raises PushFailedError on failure."""
        from services.git_workflow_manager import git_workflow_manager
        await git_workflow_manager.push_branch(project_dir, branch_name)


    async def escalate_stale_branch(
        self,
        github_integration,
        parent_issue: int,
        branch_name: str,
        commits_behind: int,
        pipeline_run_id: Optional[str] = None,
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

        await github_integration.post_comment(parent_issue, message, pipeline_run_id=pipeline_run_id)
        logger.warning(f"Escalated stale branch for parent #{parent_issue}: {commits_behind} commits behind")

    async def finalize_feature_branch_work(
        self,
        project: str,
        issue_number: int,
        commit_message: str,
        github_integration,
        project_dir_override: Optional[str] = None
    ) -> Dict[str, Any]:
        """
        Commit changes, push, update state, check completion

        Args:
            project_dir_override: Commit/push from this directory instead of the
                shared base clone (services/pipeline_run.py's resolve_workspace(),
                issue #122/WI-C -- when the caller's agent actually did its work in
                an isolated epic worktree rather than the shared base clone, this
                MUST be passed, or this method silently commits/pushes from the
                wrong directory: none of the agent's real changes, and possibly on
                whatever branch the base clone happens to be on at the time).

        Returns: dict with pr_url, all_complete, etc.
        """
        project_dir = project_dir_override or os.path.join(self.workspace_root, project)

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
                commit_succeeded = await self.git_commit(project_dir, commit_message)
                if not commit_succeeded:
                    logger.warning(f"Standalone commit was blocked for issue #{issue_number}. Continuing with push of prior commits.")

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
                from services.git_workflow_manager import PushFailedError
                if isinstance(e, PushFailedError):
                    raise  # Let PushFailedError propagate — agent_executor handles it
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
        commit_succeeded = await self.git_commit(project_dir, commit_message)
        if not commit_succeeded:
            logger.warning(f"Commit was blocked (likely unwanted docs validation). Continuing with push of prior commits.")

        # Step 3: Verify branch exists before pushing
        branch_exists = await self.branch_exists(project_dir, feature_branch.branch_name)
        if not branch_exists:
            error_msg = f"Branch {feature_branch.branch_name} does not exist locally, cannot push"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

        # Step 4: Push to remote — raises PushFailedError on failure
        await self.git_push(project_dir, feature_branch.branch_name)

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
                        finalize_pipeline_run_id = None
                        try:
                            from services.pipeline_run import get_pipeline_run_manager
                            active_run = get_pipeline_run_manager().get_active_pipeline_run(project, issue_number)
                            if active_run:
                                finalize_pipeline_run_id = active_run.id
                        except Exception:
                            pass
                        await self.post_feature_completion_comment(
                            github_integration,
                            feature_branch.parent_issue,
                            pr_result.get("pr_url"),
                            pipeline_run_id=finalize_pipeline_run_id,
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
        lines.append("🤖 Generated by Switchyard")

        return "\n".join(lines)

    async def post_feature_completion_comment(
        self,
        github_integration,
        parent_issue: int,
        pr_url: Optional[str],
        pipeline_run_id: Optional[str] = None,
    ):
        """Post completion comment to parent issue"""
        message = f"""## ✅ Feature Complete

All sub-issues have been completed and changes have been committed.

**Pull Request:** {pr_url or 'Creating...'}

The PR is now ready for review and can be merged when approved.
"""

        await github_integration.post_comment(parent_issue, message, pipeline_run_id=pipeline_run_id)
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
                    # NOTE (#49): project_dir here is whatever the caller passes --
                    # currently this method (detect_and_clean_invalid_branches) has
                    # no in-repo callers, but it is a general maintenance utility,
                    # not something verified to run only against a worktree. Treat
                    # it as base-clone-scoped like every other checkout_branch()
                    # call site in this file until proven otherwise; do not drop
                    # checkout_branch()'s defensive logic here either.
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

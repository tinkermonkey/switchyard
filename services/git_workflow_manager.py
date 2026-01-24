"""
Git Workflow Manager

Manages the complete git workflow lifecycle:
- Branch creation and tracking
- Pull request creation and management
- PR status updates based on review cycle
- Integration with code review process
"""

import subprocess
import logging
from pathlib import Path
from typing import Optional, Dict, Any, List
from dataclasses import dataclass
from datetime import datetime

from services.github_api_client import get_github_client

logger = logging.getLogger(__name__)


@dataclass
class BranchInfo:
    """Information about a feature branch"""
    branch_name: str
    issue_number: int
    created_at: str
    last_commit_sha: Optional[str] = None
    pr_number: Optional[int] = None
    pr_url: Optional[str] = None
    pr_state: Optional[str] = None  # 'draft', 'open', 'merged', 'closed'


class GitWorkflowManager:
    """Manages git workflow and pull request lifecycle"""

    def __init__(self):
        self.branch_cache: Dict[str, Dict[int, BranchInfo]] = {}  # project -> issue_number -> BranchInfo

    def get_branch_info(self, project: str, issue_number: int) -> Optional[BranchInfo]:
        """Get cached branch info for an issue"""
        if project in self.branch_cache:
            return self.branch_cache[project].get(issue_number)
        return None

    def track_branch(self, project: str, issue_number: int, branch_name: str) -> BranchInfo:
        """Track a new branch for an issue"""
        if project not in self.branch_cache:
            self.branch_cache[project] = {}

        branch_info = BranchInfo(
            branch_name=branch_name,
            issue_number=issue_number,
            created_at=datetime.utcnow().isoformat() + 'Z'
        )

        self.branch_cache[project][issue_number] = branch_info
        logger.info(f"Tracking branch {branch_name} for issue #{issue_number} in {project}")

        return branch_info

    async def create_or_update_pr(
        self,
        project: str,
        issue_number: int,
        project_dir: Path,
        org: str,
        repo: str,
        issue_title: str,
        issue_body: str = "",
        draft: bool = True
    ) -> Dict[str, Any]:
        """
        Create or update a pull request for an issue.

        Args:
            project: Project name
            issue_number: GitHub issue number
            project_dir: Path to project directory
            org: GitHub organization
            repo: GitHub repository
            issue_title: Issue title for PR title
            issue_body: Issue body for PR description
            draft: Whether to create as draft PR

        Returns:
            Dict with pr_number, pr_url, created (bool)
        """
        branch_info = self.get_branch_info(project, issue_number)

        # If no branch found, try to find feature branch for this issue (parent or sub-issue)
        if not branch_info:
            try:
                from services.feature_branch_manager import feature_branch_manager
                from services.github_integration import GitHubIntegration

                # Create GitHubIntegration for parent detection
                github_integration = GitHubIntegration(repo_owner=org, repo_name=repo)

                feature_branch = await feature_branch_manager.get_feature_branch_for_issue(project, issue_number, github_integration)
                if feature_branch:
                    logger.info(f"Found feature branch for issue #{issue_number}: {feature_branch.branch_name}")
                    self.track_branch(project, issue_number, feature_branch.branch_name)
                    branch_info = self.get_branch_info(project, issue_number)
            except Exception as e:
                logger.warning(f"Could not check for feature branch for issue #{issue_number}: {e}", exc_info=True)

        if not branch_info:
            logger.error(f"No branch tracked for issue #{issue_number}")
            return {'success': False, 'error': 'No branch tracked'}

        branch_name = branch_info.branch_name

        # Check if PR already exists
        if branch_info.pr_number:
            logger.info(f"PR #{branch_info.pr_number} already exists for issue #{issue_number}")
            return {
                'success': True,
                'pr_number': branch_info.pr_number,
                'pr_url': branch_info.pr_url,
                'created': False
            }

        try:
            # Build PR title and body
            pr_title = issue_title if not issue_title.startswith('#') else issue_title[issue_title.find(' ')+1:]
            pr_body = self._build_pr_body(issue_number, issue_body, org, repo)

            # Create PR using gh CLI
            cmd = [
                'gh', 'pr', 'create',
                '--repo', f"{org}/{repo}",
                '--base', 'main',
                '--head', branch_name,
                '--title', pr_title,
                '--body', pr_body
            ]

            if draft:
                cmd.append('--draft')

            result = subprocess.run(
                cmd,
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                # Parse PR URL from output (gh pr create returns URL)
                pr_url = result.stdout.strip()

                # Extract PR number from URL
                pr_number = self._extract_pr_number_from_url(pr_url)

                # Update branch info
                branch_info.pr_number = pr_number
                branch_info.pr_url = pr_url
                branch_info.pr_state = 'draft' if draft else 'open'

                # Track the API call
                github_client = get_github_client()
                github_client.track_gh_operation(
                    'gh_pr_create',
                    f"Created PR #{pr_number} for issue #{issue_number} in {org}/{repo}"
                )

                logger.info(f"Created PR #{pr_number} for issue #{issue_number}: {pr_url}")

                return {
                    'success': True,
                    'pr_number': pr_number,
                    'pr_url': pr_url,
                    'created': True
                }
            else:
                error_msg = result.stderr.strip()

                # Check if PR already exists (common error)
                if 'already exists' in error_msg.lower():
                    logger.warning(f"PR already exists for branch {branch_name}, fetching details")
                    pr_info = await self._get_existing_pr(project_dir, branch_name, org, repo)
                    if pr_info:
                        branch_info.pr_number = pr_info['number']
                        branch_info.pr_url = pr_info['url']
                        branch_info.pr_state = pr_info['state']
                        return {
                            'success': True,
                            'pr_number': pr_info['number'],
                            'pr_url': pr_info['url'],
                            'created': False
                        }

                logger.error(f"Failed to create PR: {error_msg}")
                return {'success': False, 'error': error_msg}

        except Exception as e:
            logger.error(f"Failed to create PR for issue #{issue_number}: {e}")
            return {'success': False, 'error': str(e)}

    async def update_pr_status(
        self,
        project: str,
        issue_number: int,
        project_dir: Path,
        status: str,  # 'draft', 'ready', 'approved', 'merged'
        org: str,
        repo: str
    ) -> bool:
        """
        Update PR status based on review cycle state.

        Args:
            project: Project name
            issue_number: Issue number
            project_dir: Project directory path
            status: Target status
            org: GitHub org
            repo: GitHub repo

        Returns:
            True if successful
        """
        branch_info = self.get_branch_info(project, issue_number)
        if not branch_info or not branch_info.pr_number:
            logger.error(f"No PR found for issue #{issue_number}")
            return False

        pr_number = branch_info.pr_number

        try:
            if status == 'ready' and branch_info.pr_state == 'draft':
                # Mark PR as ready for review (remove draft status)
                result = subprocess.run(
                    ['gh', 'pr', 'ready', str(pr_number), '--repo', f"{org}/{repo}"],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode == 0:
                    branch_info.pr_state = 'open'
                    
                    # Track the API call
                    github_client = get_github_client()
                    github_client.track_gh_operation(
                        'gh_pr_ready',
                        f"Marked PR #{pr_number} as ready for review in {org}/{repo}"
                    )
                    
                    logger.info(f"Marked PR #{pr_number} as ready for review")
                    return True
                else:
                    logger.error(f"Failed to mark PR ready: {result.stderr}")
                    return False

            elif status == 'approved':
                # Add approval label
                result = subprocess.run(
                    ['gh', 'pr', 'edit', str(pr_number), '--add-label', 'approved', '--repo', f"{org}/{repo}"],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if result.returncode == 0:
                    # Track the API call
                    github_client = get_github_client()
                    github_client.track_gh_operation(
                        'gh_pr_edit_add_label',
                        f"Added 'approved' label to PR #{pr_number} in {org}/{repo}"
                    )

                    logger.info(f"Added 'approved' label to PR #{pr_number}")
                    return True
                else:
                    error_msg = result.stderr.strip()

                    # Check if label doesn't exist - if so, create it and retry
                    if "'approved' not found" in error_msg:
                        logger.info(f"Creating 'approved' label in {org}/{repo}")
                        create_result = subprocess.run(
                            ['gh', 'label', 'create', 'approved',
                             '--color', '0e8a16',  # Green
                             '--description', 'PR approved and ready to merge',
                             '--repo', f"{org}/{repo}"],
                            cwd=project_dir,
                            capture_output=True,
                            text=True,
                            timeout=30
                        )

                        if create_result.returncode == 0:
                            logger.info(f"Created 'approved' label, retrying add to PR #{pr_number}")
                            # Retry adding the label
                            retry_result = subprocess.run(
                                ['gh', 'pr', 'edit', str(pr_number), '--add-label', 'approved', '--repo', f"{org}/{repo}"],
                                cwd=project_dir,
                                capture_output=True,
                                text=True,
                                timeout=30
                            )

                            if retry_result.returncode == 0:
                                github_client = get_github_client()
                                github_client.track_gh_operation(
                                    'gh_pr_edit_add_label',
                                    f"Created and added 'approved' label to PR #{pr_number} in {org}/{repo}"
                                )
                                logger.info(f"Successfully added 'approved' label to PR #{pr_number}")
                                return True
                            else:
                                logger.error(f"Failed to add 'approved' label after creation: {retry_result.stderr}")
                                return False
                        else:
                            logger.error(f"Failed to create 'approved' label: {create_result.stderr}")
                            return False
                    else:
                        logger.error(f"Failed to add approved label: {error_msg}")
                        return False

            elif status == 'merged':
                # PR was merged
                branch_info.pr_state = 'merged'
                logger.info(f"PR #{pr_number} has been merged")

                # Delete branch after merge
                await self._delete_branch(project_dir, branch_info.branch_name, org, repo)
                return True

            return True

        except Exception as e:
            logger.error(f"Failed to update PR status: {e}")
            return False

    async def _get_existing_pr(
        self,
        project_dir: Path,
        branch_name: str,
        org: str,
        repo: str
    ) -> Optional[Dict[str, Any]]:
        """Get existing PR for a branch"""
        try:
            result = subprocess.run(
                ['gh', 'pr', 'list', '--head', branch_name, '--repo', f"{org}/{repo}", '--json', 'number,url,state'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0 and result.stdout.strip():
                import json
                prs = json.loads(result.stdout)
                if prs:
                    # Track the API call
                    github_client = get_github_client()
                    github_client.track_gh_operation(
                        'gh_pr_list',
                        f"Retrieved existing PR for branch {branch_name} in {org}/{repo}"
                    )
                    
                    return {
                        'number': prs[0]['number'],
                        'url': prs[0]['url'],
                        'state': prs[0]['state'].lower()
                    }

        except Exception as e:
            logger.error(f"Failed to get existing PR: {e}")

        return None

    async def _delete_branch(self, project_dir: Path, branch_name: str, org: str, repo: str) -> bool:
        """Delete a remote branch after merge"""
        try:
            result = subprocess.run(
                ['git', 'push', 'origin', '--delete', branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Deleted remote branch {branch_name}")
                return True
            else:
                logger.warning(f"Failed to delete branch {branch_name}: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to delete branch: {e}")
            return False

    def _build_pr_body(self, issue_number: int, issue_body: str, org: str, repo: str) -> str:
        """Build PR body with link to issue"""
        body_parts = []

        # Link to issue
        body_parts.append(f"Closes #{issue_number}")
        body_parts.append("")

        # Include issue body if available
        if issue_body:
            body_parts.append("## Description")
            body_parts.append(issue_body)
            body_parts.append("")

        # Add attribution
        body_parts.append("---")
        body_parts.append("🤖 Generated with [Claude Code](https://claude.com/claude-code)")

        return '\n'.join(body_parts)

    def _extract_pr_number_from_url(self, pr_url: str) -> int:
        """Extract PR number from GitHub URL"""
        # URL format: https://github.com/org/repo/pull/123
        try:
            return int(pr_url.rstrip('/').split('/')[-1])
        except (ValueError, IndexError):
            logger.warning(f"Could not extract PR number from URL: {pr_url}")
            return 0

    async def create_branch(self, project_dir: str, branch_name: str) -> bool:
        """Create a new git branch"""
        try:
            result = subprocess.run(
                ['git', 'checkout', '-b', branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Created branch {branch_name}")
                return True
            else:
                logger.error(f"Failed to create branch {branch_name}: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to create branch: {e}")
            return False

    async def checkout_branch(self, project_dir: str, branch_name: str) -> bool:
        """Checkout a git branch, handling dirty working directory"""
        try:
            # Check for uncommitted changes
            status_result = subprocess.run(
                ['git', 'status', '--porcelain'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            has_changes = bool(status_result.stdout.strip())

            if has_changes:
                logger.info(f"Working directory has uncommitted changes, stashing before checkout")

                # Stash changes including untracked files
                stash_result = subprocess.run(
                    ['git', 'stash', 'push', '--include-untracked', '-m', f'Auto-stash before checkout {branch_name}'],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                if stash_result.returncode != 0:
                    logger.error(f"Failed to stash changes: {stash_result.stderr}")
                    raise Exception(f"Failed to stash changes: {stash_result.stderr}")

                logger.info("Successfully stashed uncommitted changes")

            # Check if branch exists locally first
            check_local = subprocess.run(
                ['git', 'rev-parse', '--verify', branch_name],
                cwd=project_dir,
                capture_output=True,
                timeout=10
            )

            if check_local.returncode != 0:
                # Branch doesn't exist locally - check if it exists remotely
                logger.info(f"Branch {branch_name} doesn't exist locally, checking remote...")

                # Fetch to ensure we have latest remote refs
                subprocess.run(
                    ['git', 'fetch', 'origin'],
                    cwd=project_dir,
                    capture_output=True,
                    timeout=30
                )

                # Check if remote branch exists
                check_remote = subprocess.run(
                    ['git', 'rev-parse', '--verify', f'origin/{branch_name}'],
                    cwd=project_dir,
                    capture_output=True,
                    timeout=10
                )

                if check_remote.returncode == 0:
                    # Remote branch exists - create local tracking branch
                    logger.info(f"Creating local tracking branch for remote origin/{branch_name}")
                    result = subprocess.run(
                        ['git', 'checkout', '-b', branch_name, '--track', f'origin/{branch_name}'],
                        cwd=project_dir,
                        capture_output=True,
                        text=True,
                        timeout=30
                    )

                    # Verify tracking was set up correctly
                    if result.returncode == 0:
                        verify_tracking = subprocess.run(
                            ['git', 'config', '--get', f'branch.{branch_name}.remote'],
                            cwd=project_dir,
                            capture_output=True,
                            timeout=10
                        )

                        if verify_tracking.returncode != 0:
                            logger.error(
                                f"Created branch {branch_name} from remote with --track, "
                                f"but tracking was not configured. This is unexpected."
                            )
                        else:
                            logger.info(f"Verified tracking configured for {branch_name}")
                else:
                    # Neither local nor remote exists - use regular checkout (will fail with clear error)
                    result = subprocess.run(
                        ['git', 'checkout', branch_name],
                        cwd=project_dir,
                        capture_output=True,
                        text=True,
                        timeout=30
                    )
            else:
                # Branch exists locally - regular checkout
                result = subprocess.run(
                    ['git', 'checkout', branch_name],
                    cwd=project_dir,
                    capture_output=True,
                    text=True,
                    timeout=30
                )

                # After checkout, verify tracking is configured
                # This handles legacy branches created before the push fix was applied
                if result.returncode == 0:
                    check_tracking = subprocess.run(
                        ['git', 'config', '--get', f'branch.{branch_name}.remote'],
                        cwd=project_dir,
                        capture_output=True,
                        timeout=10
                    )

                    if check_tracking.returncode != 0:
                        # No tracking configured - set it up if remote branch exists
                        logger.warning(f"Branch {branch_name} exists locally but has no tracking configured")

                        # Check if remote branch exists
                        fetch_result = subprocess.run(
                            ['git', 'fetch', 'origin'],
                            cwd=project_dir,
                            capture_output=True,
                            timeout=30
                        )

                        check_remote = subprocess.run(
                            ['git', 'rev-parse', '--verify', f'origin/{branch_name}'],
                            cwd=project_dir,
                            capture_output=True,
                            timeout=10
                        )

                        if check_remote.returncode == 0:
                            # Remote exists - set up tracking
                            logger.info(f"Setting up tracking for {branch_name} -> origin/{branch_name}")
                            set_upstream = subprocess.run(
                                ['git', 'branch', '--set-upstream-to', f'origin/{branch_name}', branch_name],
                                cwd=project_dir,
                                capture_output=True,
                                text=True,
                                timeout=10
                            )

                            if set_upstream.returncode == 0:
                                logger.info(f"Successfully configured tracking for {branch_name}")
                            else:
                                logger.warning(
                                    f"Failed to set upstream tracking for {branch_name}: {set_upstream.stderr}. "
                                    f"This may cause git pull to fail."
                                )
                        else:
                            # Remote doesn't exist - this will be pushed later by push_branch
                            # The caller (feature_branch_manager) should ensure a push happens
                            logger.info(f"Remote branch origin/{branch_name} doesn't exist yet (will be created on first push)")

            if result.returncode == 0:
                logger.info(f"Checked out branch {branch_name}")

                if has_changes:
                    logger.info("Restoring stashed changes...")
                    try:
                        pop_result = subprocess.run(
                            ['git', 'stash', 'pop'],
                            cwd=project_dir,
                            capture_output=True,
                            text=True,
                            timeout=30
                        )

                        if pop_result.returncode != 0:
                            logger.warning(f"Failed to pop stash (changes are saved in stash list): {pop_result.stderr}")
                        else:
                            logger.info("Successfully restored stashed changes")
                    except Exception as e:
                        logger.warning(f"Exception while popping stash: {e}")

                return True
            else:
                logger.error(f"Failed to checkout branch {branch_name}: {result.stderr}")
                raise Exception(f"Failed to checkout branch {branch_name}: {result.stderr}")

        except Exception as e:
            logger.error(f"Failed to checkout branch: {e}")
            raise

    async def pull_branch(self, project_dir: str) -> bool:
        """Pull latest changes from remote"""
        try:
            result = subprocess.run(
                ['git', 'pull'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.info(f"Pulled latest changes")
                return True
            else:
                logger.error(f"Failed to pull: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to pull: {e}")
            return False

    async def pull_rebase(self, project_dir: str):
        """
        Pull latest changes with rebase

        Raises exception if conflicts detected
        """
        try:
            result = subprocess.run(
                ['git', 'pull', '--rebase'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.info(f"Pulled with rebase successfully")
            else:
                error_msg = result.stderr
                if 'conflict' in error_msg.lower() or 'rebase' in error_msg.lower():
                    raise Exception(f"Merge conflict detected: {error_msg}")
                else:
                    raise Exception(f"Pull rebase failed: {error_msg}")

        except subprocess.TimeoutExpired:
            raise Exception("Pull rebase timed out")

    async def get_current_branch(self, project_dir: str) -> str:
        """Get the current branch name"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--abbrev-ref', 'HEAD'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10,
                check=True
            )

            return result.stdout.strip()

        except Exception as e:
            logger.error(f"Failed to get current branch: {e}")
            raise

    async def branch_exists(self, project_dir: str, branch_name: str) -> bool:
        """Check if a branch exists locally"""
        try:
            result = subprocess.run(
                ['git', 'rev-parse', '--verify', branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=10
            )

            return result.returncode == 0

        except Exception as e:
            logger.error(f"Failed to check if branch exists: {e}")
            return False

    async def add_all(self, project_dir: str) -> bool:
        """Stage all changes"""
        try:
            result = subprocess.run(
                ['git', 'add', '.'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Staged all changes")
                return True
            else:
                logger.error(f"Failed to stage changes: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to add all: {e}")
            return False

    async def commit(self, project_dir: str, message: str) -> bool:
        """Commit staged changes"""
        try:
            result = subprocess.run(
                ['git', 'commit', '-m', message],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                logger.info(f"Committed changes: {message[:50]}...")
                return True
            else:
                # Check if nothing to commit
                if 'nothing to commit' in result.stdout.lower():
                    logger.info("Nothing to commit")
                    return True

                logger.error(f"Failed to commit: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to commit: {e}")
            return False

    async def push_branch(self, project_dir: str, branch_name: str) -> bool:
        """Push branch to remote"""
        try:
            result = subprocess.run(
                ['git', 'push', '-u', 'origin', branch_name],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=60
            )

            if result.returncode == 0:
                logger.info(f"Pushed branch {branch_name}")
                return True
            else:
                logger.error(f"Failed to push branch: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to push branch: {e}")
            return False

    async def get_commits_behind(self, project_dir: str, branch_name: str, base_branch: str) -> int:
        """Get number of commits a branch is behind base branch"""
        try:
            # Fetch latest
            subprocess.run(
                ['git', 'fetch', 'origin'],
                cwd=project_dir,
                capture_output=True,
                timeout=30
            )

            # Count commits behind
            result = subprocess.run(
                ['git', 'rev-list', '--count', f'{branch_name}..origin/{base_branch}'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                count = int(result.stdout.strip())
                return count
            else:
                logger.warning(f"Failed to get commits behind: {result.stderr}")
                return 0

        except Exception as e:
            logger.error(f"Failed to get commits behind: {e}")
            return 0

    async def get_conflicting_files(self, project_dir: str) -> List[str]:
        """Get list of files with merge conflicts"""
        try:
            result = subprocess.run(
                ['git', 'diff', '--name-only', '--diff-filter=U'],
                cwd=project_dir,
                capture_output=True,
                text=True,
                timeout=30
            )

            if result.returncode == 0:
                files = [f.strip() for f in result.stdout.split('\n') if f.strip()]
                return files
            else:
                return []

        except Exception as e:
            logger.error(f"Failed to get conflicting files: {e}")
            return []


# Global instance
git_workflow_manager = GitWorkflowManager()

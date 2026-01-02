"""
GitHub Integration Service for Agent Collaboration
"""

import subprocess
import json
import os
import logging
import asyncio
import requests
from typing import Dict, Any, List, Optional

from services.github_api_client import get_github_client

logger = logging.getLogger(__name__)

class GitHubIntegration:
    """Handles GitHub API interactions for agent collaboration"""

    def __init__(self, repo_owner: Optional[str] = None, repo_name: Optional[str] = None):
        self.github_org = os.environ.get('GITHUB_ORG')
        self.repo_owner = repo_owner or self.github_org
        self.repo_name = repo_name

        # Try to use GitHub App auth, fall back to PAT
        from services.github_app_auth import get_github_app_auth
        self.github_app = get_github_app_auth()

        if self.github_app.is_configured():
            logger.debug("Using GitHub App authentication")
            self.auth_type = "github_app"
        else:
            logger.warning("Using Personal Access Token authentication")
            self.auth_type = "pat"

    def _get_gh_env(self) -> dict:
        """Get environment variables for gh CLI, using GitHub App token if available"""
        env = os.environ.copy()

        if self.auth_type == "github_app":
            token = self.github_app.get_installation_token()
            if token:
                env['GH_TOKEN'] = token
                # Remove GITHUB_TOKEN to prevent conflicts
                env.pop('GITHUB_TOKEN', None)

        return env

    async def post_issue_comment(self, issue_number: int, comment: str, repo: Optional[str] = None) -> Dict[str, Any]:
        """Post a comment to a GitHub issue using REST API with rate limiting"""
        try:
            repo_name = repo or self.repo_name
            endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{issue_number}/comments"
            
            success, response = get_github_client().rest(
                method='POST',
                endpoint=endpoint,
                data={'body': comment}
            )
            
            if not success:
                logger.error(f"Failed to post issue comment: {response}")
                return {'success': False, 'error': response.get('error', 'Unknown error')}
            
            return {
                'success': True,
                'html_url': response.get('html_url'),
                'id': response.get('id')
            }

        except Exception as e:
            logger.error(f"Failed to post GitHub comment: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def post_pr_comment(self, pr_number: int, comment: str, repo: Optional[str] = None) -> Dict[str, Any]:
        """Post a comment to a GitHub pull request using REST API with rate limiting"""
        try:
            repo_name = repo or self.repo_name
            # PR comments are posted to issues endpoint (PRs are issues in GitHub API)
            endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{pr_number}/comments"
            
            success, response = get_github_client().rest(
                method='POST',
                endpoint=endpoint,
                data={'body': comment}
            )
            
            if not success:
                logger.error(f"Failed to post PR comment: {response}")
                return {'success': False, 'error': response.get('error', 'Unknown error')}
            
            return {
                'success': True,
                'html_url': response.get('html_url'),
                'id': response.get('id')
            }

        except Exception as e:
            logger.error(f"Failed to post PR comment: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def create_pr_review(
        self,
        pr_number: int,
        review_type: str,  # "approve", "request-changes", "comment"
        body: str,
        repo: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a formal PR review using REST API with rate limiting"""
        try:
            repo_name = repo or self.repo_name
            endpoint = f"/repos/{self.github_org}/{repo_name}/pulls/{pr_number}/reviews"
            
            # Map review_type to GitHub API event
            event_map = {
                'approve': 'APPROVE',
                'request-changes': 'REQUEST_CHANGES',
                'comment': 'COMMENT'
            }
            event = event_map.get(review_type, review_type.upper())
            
            success, response = get_github_client().rest(
                method='POST',
                endpoint=endpoint,
                data={
                    'body': body,
                    'event': event
                }
            )
            
            if not success:
                logger.error(f"Failed to create PR review: {response}")
                return {'success': False, 'error': response.get('error', 'Unknown error')}
            
            return {'success': True, 'review_id': response.get('id')}

        except Exception as e:
            logger.error(f"Failed to create PR review: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def get_issue_details(self, issue_number: int, repo: Optional[str] = None) -> Dict[str, Any]:
        """Get issue details using REST API with rate limiting"""
        try:
            repo_name = repo or self.repo_name
            endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{issue_number}"
            
            success, response = get_github_client().rest(
                method='GET',
                endpoint=endpoint
            )
            
            if not success:
                logger.error(f"Failed to get issue details: {response}")
                return {}
            
            return response

        except Exception as e:
            logger.error(f"Failed to get issue details: {e}", exc_info=True)
            return {}

    async def has_agent_processed_issue(self, issue_number: int, agent_name: str, repo: Optional[str] = None) -> bool:
        """Check if an agent has already processed this issue by looking for its signature in comments"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'issue', 'view', str(issue_number), '--json', 'comments']

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())
            data = json.loads(result.stdout)

            # Look for the agent processing signature in comments
            signature = f"_Processed by the {agent_name} agent_"

            for comment in data.get('comments', []):
                if signature in comment.get('body', ''):
                    return True

            return False

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to check issue comments: {e.stderr}")
            return False  # Default to not processed if we can't check

    async def has_agent_processed_discussion(self, discussion_id: str, agent_name: str) -> bool:
        """
        Check if an agent has already processed this discussion.
        Returns True if the agent has processed it AND there are no subsequent user comments.
        Returns False if the agent hasn't processed it OR if there are new user comments.
        """
        try:
            from services.github_app import github_app

            # GraphQL query to get discussion comments with replies
            query = """
            query($discussionId: ID!) {
              node(id: $discussionId) {
                ... on Discussion {
                  number
                  comments(first: 100) {
                    nodes {
                      id
                      body
                      author {
                        login
                      }
                      createdAt
                      replies(first: 50) {
                        nodes {
                          id
                          body
                          author {
                            login
                          }
                          createdAt
                        }
                      }
                    }
                  }
                }
              }
            }
            """

            result = github_app.graphql_request(query, {'discussionId': discussion_id})

            if not result or 'node' not in result:
                logger.error(f"Failed to get discussion {discussion_id} for processing check")
                return False

            comments = result['node']['comments']['nodes']
            signature = f"_Processed by the {agent_name} agent_"

            # Collect all messages with timestamps
            all_messages = []
            for comment in comments:
                all_messages.append({
                    'body': comment.get('body', ''),
                    'author': comment.get('author', {}).get('login', ''),
                    'createdAt': comment.get('createdAt', ''),
                    'type': 'comment'
                })
                for reply in comment.get('replies', {}).get('nodes', []):
                    all_messages.append({
                        'body': reply.get('body', ''),
                        'author': reply.get('author', {}).get('login', ''),
                        'createdAt': reply.get('createdAt', ''),
                        'type': 'reply'
                    })
            
            # Sort by createdAt
            all_messages.sort(key=lambda x: x['createdAt'])
            
            last_agent_idx = -1
            last_user_idx = -1
            
            for i, msg in enumerate(all_messages):
                if signature in msg['body']:
                    last_agent_idx = i
                elif msg['author'] != 'orchestrator-bot' and '[bot]' not in msg['author']:
                    last_user_idx = i

            if last_agent_idx == -1:
                return False # Agent never processed it
            
            if last_user_idx > last_agent_idx:
                logger.info(f"New user comment found after agent signature (User idx: {last_user_idx}, Agent idx: {last_agent_idx})")
                return False # New user comment exists
                
            logger.debug(f"Found agent signature for {agent_name} and no new user comments")
            return True

        except Exception as e:
            logger.error(f"Failed to check discussion comments: {e}")
            return False  # Default to not processed if we can't check

    async def get_feedback_comments(self, issue_number: int, repo: Optional[str] = None,
                                    since_timestamp: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get comments that mention @orchestrator-bot for feedback using REST API with rate limiting"""
        try:
            repo_name = repo or self.repo_name
            endpoint = f"/repos/{self.github_org}/{repo_name}/issues/{issue_number}/comments"
            
            success, response = get_github_client().rest(
                method='GET',
                endpoint=endpoint
            )
            
            if not success:
                logger.error(f"Failed to fetch feedback comments: {response}")
                return []
            
            # Handle both list and dict responses
            comments_list = response if isinstance(response, list) else response.get('comments', [])
            
            feedback_comments = []
            for comment in comments_list:
                body = comment.get('body', '')
                created_at = comment.get('created_at', '')
                comment_id = comment.get('id', '')

                # Check if comment mentions @orchestrator-bot
                if '@orchestrator-bot' in body:
                    logger.debug(f"Found @orchestrator-bot mention in comment {comment_id} created_at={created_at}, checking timestamp filter: {since_timestamp}")
                    # If timestamp filter provided, only include newer comments
                    if since_timestamp:
                        try:
                            from dateutil import parser as date_parser
                            from datetime import timezone

                            comment_time = date_parser.parse(created_at)
                            since_time = date_parser.parse(since_timestamp)

                            logger.debug(f"Comparing timestamps - comment_time: {comment_time} (tz: {comment_time.tzinfo}), since_time: {since_time} (tz: {since_time.tzinfo})")

                            # Make both timezone-aware if one is naive
                            if comment_time.tzinfo is None:
                                comment_time = comment_time.replace(tzinfo=timezone.utc)
                                logger.debug(f"Made comment_time timezone-aware: {comment_time}")
                            if since_time.tzinfo is None:
                                since_time = since_time.replace(tzinfo=timezone.utc)
                                logger.debug(f"Made since_time timezone-aware: {since_time}")

                            if comment_time <= since_time:
                                continue
                        except Exception as e:
                            logger.warning(f"Could not compare timestamps (comment: {created_at}, since: {since_timestamp}): {e}. Including comment anyway.")
                            # Include the comment if we can't compare timestamps
                            pass

                    feedback_comments.append({
                        'id': comment_id,
                        'body': body,
                        'author': comment.get('user', {}).get('login', 'unknown'),
                        'created_at': created_at,
                        'is_bot': comment.get('user', {}).get('type') == 'Bot'
                    })

            return feedback_comments

        except Exception as e:
            import traceback
            logger.warning(f"Error processing feedback comments: {e}")
            logger.info(f"Full traceback: {traceback.format_exc()}")
            return []

    async def get_pr_details(self, pr_number: int, repo: Optional[str] = None) -> Dict[str, Any]:
        """Get pull request details using REST API with rate limiting"""
        try:
            repo_name = repo or self.repo_name
            endpoint = f"/repos/{self.github_org}/{repo_name}/pulls/{pr_number}"
            
            success, response = get_github_client().rest(
                method='GET',
                endpoint=endpoint
            )
            
            if not success:
                logger.error(f"Failed to get PR details: {response}")
                return {}
            
            return response

        except Exception as e:
            logger.error(f"Failed to get PR details: {e}", exc_info=True)
            return {}

    async def find_pr_by_branch(
        self,
        branch: str,
        base: str = "main",
        state: str = "open"
    ) -> Optional[Dict[str, Any]]:
        """
        Find an existing PR for the given branch

        Args:
            branch: The head branch name (e.g., "feature/issue-85-...")
            base: The base branch (default: "main")
            state: PR state filter - "open", "closed", "all" (default: "open")

        Returns:
            PR details dict if found, None otherwise
        """
        try:
            repo_arg = f"{self.repo_owner}/{self.repo_name}"

            # Use gh CLI to list PRs for this branch
            cmd = [
                'gh', 'pr', 'list',
                '--repo', repo_arg,
                '--head', branch,
                '--base', base,
                '--state', state,
                '--json', 'number,title,url,state,isDraft',
                '--limit', '1'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._get_gh_env()
            )

            if result.returncode == 0 and result.stdout.strip():
                import json
                prs = json.loads(result.stdout)
                if prs:
                    pr = prs[0]
                    logger.info(f"Found existing PR #{pr['number']} for branch {branch}")
                    return {
                        'success': True,
                        'pr_number': pr['number'],
                        'pr_url': pr['url'],
                        'state': pr['state'],
                        'is_draft': pr.get('isDraft', False),
                        'title': pr['title']
                    }

            logger.debug(f"No existing PR found for branch {branch}")
            return None

        except Exception as e:
            logger.error(f"Failed to find PR by branch: {e}")
            return None

    async def mention_user(self, username: str) -> str:
        """Format user mention"""
        return f"@{username}"

    async def add_issue_label(self, issue_number: int, labels: List[str], repo: Optional[str] = None):
        """Add labels to an issue"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            for label in labels:
                cmd = ['gh', 'issue', 'edit', str(issue_number), '--add-label', label]

                if repo:
                    cmd.extend(['--repo', repo_arg])

                subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to add labels: {e.stderr}")

    async def create_issue_from_agent(
        self,
        title: str,
        body: str,
        labels: Optional[List[str]] = None,
        assignees: Optional[List[str]] = None,
        repo: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a new issue from agent work"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'issue', 'create', '--title', title, '--body', body]

            if repo:
                cmd.extend(['--repo', repo_arg])

            if labels:
                for label in labels:
                    cmd.extend(['--label', label])

            if assignees:
                for assignee in assignees:
                    cmd.extend(['--assignee', assignee])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

            # Extract issue number from output
            issue_url = result.stdout.strip()
            issue_number = issue_url.split('/')[-1]

            return {
                'success': True,
                'issue_number': int(issue_number),
                'url': issue_url
            }

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create issue: {e.stderr}")
            return {'success': False, 'error': str(e)}

    async def post_agent_output(self, context: Dict[str, Any], comment: str,
                               reply_to_id: Optional[str] = None) -> Dict[str, Any]:
        """
        Post agent output to the appropriate workspace (Issues or Discussions)

        Args:
            context: Task context containing workspace information
            comment: Formatted comment body
            reply_to_id: Optional comment ID to reply to (for discussions)

        Returns:
            Dict with success status and comment metadata
        """
        workspace_type = context.get('workspace_type', 'issues')
        discussion_id = context.get('discussion_id')

        # Debug logging
        logger.info(f"post_agent_output - workspace_type: {workspace_type}, discussion_id: {discussion_id}, context keys: {list(context.keys())}")

        # If we have a discussion_id, use discussions regardless of workspace_type
        # (hybrid workspaces have discussions for some issues)
        if discussion_id or workspace_type in ['discussions', 'hybrid']:
            if discussion_id:
                return await self._post_discussion_comment(context, comment, reply_to_id)
            else:
                logger.warning(f"workspace_type is '{workspace_type}' but no discussion_id provided, falling back to issues")
                return await self._post_issue_comment(context, comment)
        else:
            return await self._post_issue_comment(context, comment)

    async def _post_discussion_comment(self, context: Dict[str, Any], comment: str,
                                      reply_to_id: Optional[str] = None) -> Dict[str, Any]:
        """Post comment to a GitHub Discussion"""
        try:
            from services.github_discussions import GitHubDiscussions

            discussion_id = context.get('discussion_id')
            if not discussion_id:
                logger.error("No discussion_id in context for discussion post")
                return {'success': False, 'error': 'No discussion_id'}

            discussions = GitHubDiscussions()
            comment_id = discussions.add_discussion_comment(
                discussion_id=discussion_id,
                body=comment,
                reply_to_id=reply_to_id
            )

            if comment_id:
                logger.info(f"Posted comment to discussion {discussion_id}")
                return {
                    'success': True,
                    'comment_id': comment_id,
                    'workspace_type': 'discussions'
                }
            else:
                logger.error(f"Failed to post to discussion {discussion_id}")
                return {'success': False, 'error': 'API call failed'}

        except Exception as e:
            logger.error(f"Error posting discussion comment: {e}")
            return {'success': False, 'error': str(e)}

    async def _post_issue_comment(self, context: Dict[str, Any], comment: str) -> Dict[str, Any]:
        """Post comment to a GitHub Issue"""
        try:
            issue_number = context.get('issue_number')
            repository = context.get('repository')

            if not issue_number:
                logger.error("No issue_number in context for issue post")
                return {'success': False, 'error': 'No issue_number'}

            return await self.post_issue_comment(issue_number, comment, repository)

        except Exception as e:
            logger.error(f"Error posting issue comment: {e}")
            return {'success': False, 'error': str(e)}

    async def graphql_query(self, query: str, variables: Dict[str, Any]) -> Dict[str, Any]:
        """Execute a GraphQL query using the GitHub API client"""
        try:
            github_client = get_github_client()
            success, result = github_client.graphql(query, variables)
            
            if success:
                return result
            else:
                logger.error(f"GraphQL query failed: {result}")
                return {}

        except Exception as e:
            logger.error(f"GraphQL query failed: {e}")
            return {}

    async def get_issue(self, issue_number: int) -> Dict[str, Any]:
        """Get issue details (simplified wrapper)"""
        return await self.get_issue_details(issue_number, self.repo_name)

    async def post_comment(self, issue_number: int, comment: str) -> Dict[str, Any]:
        """Post comment to issue (simplified wrapper)"""
        return await self.post_issue_comment(issue_number, comment, self.repo_name)

    async def create_pr(
        self,
        branch: str,
        title: str,
        body: str,
        draft: bool = True
    ) -> Dict[str, Any]:
        """
        Create a pull request (idempotent - returns existing PR if already exists)

        Args:
            branch: The head branch name
            title: PR title
            body: PR description
            draft: Whether to create as draft (default: True)

        Returns:
            Dict with success, pr_number, pr_url
        """
        try:
            # Check if PR already exists for this branch
            existing_pr = await self.find_pr_by_branch(branch)

            if existing_pr:
                logger.info(
                    f"PR already exists for branch {branch}: #{existing_pr['pr_number']}. "
                    f"Updating body instead of creating new PR."
                )

                # Update the existing PR body to reflect current state
                update_success = await self.update_pr_body(
                    existing_pr['pr_number'],
                    body
                )

                return {
                    'success': True,
                    'pr_number': existing_pr['pr_number'],
                    'pr_url': existing_pr['pr_url'],
                    'already_existed': True,
                    'updated': update_success
                }

            # No existing PR - create new one
            repo_arg = f"{self.repo_owner}/{self.repo_name}"

            cmd = [
                'gh', 'pr', 'create',
                '--repo', repo_arg,
                '--base', 'main',
                '--head', branch,
                '--title', title,
                '--body', body
            ]

            if draft:
                cmd.append('--draft')

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._get_gh_env()
            )

            if result.returncode == 0:
                pr_url = result.stdout.strip()
                pr_number = int(pr_url.rstrip('/').split('/')[-1])

                logger.info(f"Created PR #{pr_number}: {pr_url}")

                return {
                    'success': True,
                    'pr_number': pr_number,
                    'pr_url': pr_url,
                    'already_existed': False
                }
            else:
                # Even after checking, creation might fail (race condition)
                # Try to parse the error to see if it's "already exists"
                error_msg = result.stderr

                if "already exists" in error_msg.lower():
                    logger.warning(
                        f"Race condition detected: PR created between check and create. "
                        f"Attempting to find the PR..."
                    )

                    # Retry the lookup
                    existing_pr = await self.find_pr_by_branch(branch)
                    if existing_pr:
                        return {
                            'success': True,
                            'pr_number': existing_pr['pr_number'],
                            'pr_url': existing_pr['pr_url'],
                            'already_existed': True,
                            'race_condition': True
                        }

                logger.error(f"Failed to create PR: {error_msg}")
                return {'success': False, 'error': error_msg}

        except Exception as e:
            logger.error(f"Failed to create PR: {e}")
            return {'success': False, 'error': str(e)}

    async def update_pr_body(self, pr_number: int, body: str) -> bool:
        """Update PR description"""
        try:
            repo_arg = f"{self.repo_owner}/{self.repo_name}"

            cmd = [
                'gh', 'pr', 'edit', str(pr_number),
                '--repo', repo_arg,
                '--body', body
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._get_gh_env()
            )

            if result.returncode == 0:
                logger.info(f"Updated PR #{pr_number} body")
                return True
            else:
                logger.error(f"Failed to update PR body: {result.stderr}")
                return False

        except Exception as e:
            logger.error(f"Failed to update PR body: {e}")
            return False

    async def mark_pr_ready(self, pr_number: int, max_retries: int = 3) -> bool:
        """
        Mark a draft PR as ready for review with exponential backoff retry.

        Args:
            pr_number: PR number to mark ready
            max_retries: Maximum retry attempts (default 3)

        Returns:
            True if successfully marked ready, False otherwise
        """
        repo_arg = f"{self.repo_owner}/{self.repo_name}"

        for attempt in range(1, max_retries + 1):
            try:
                cmd = ['gh', 'pr', 'ready', str(pr_number), '--repo', repo_arg]

                result = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    env=self._get_gh_env()
                )

                if result.returncode == 0:
                    logger.info(
                        f"Marked PR #{pr_number} as ready for review "
                        f"(attempt {attempt}/{max_retries})"
                    )
                    return True
                else:
                    error_msg = result.stderr.strip()

                    # Check if already ready (not an error)
                    if "is not a draft" in error_msg.lower():
                        logger.info(f"PR #{pr_number} is already ready for review")
                        return True

                    # Log error and retry if not last attempt
                    logger.warning(
                        f"Failed to mark PR #{pr_number} ready (attempt {attempt}/{max_retries}): "
                        f"{error_msg}"
                    )

                    if attempt < max_retries:
                        # Exponential backoff: 2s, 4s, 8s
                        backoff = 2 ** attempt
                        logger.info(f"Retrying in {backoff}s...")
                        await asyncio.sleep(backoff)
                    else:
                        logger.error(
                            f"Failed to mark PR #{pr_number} ready after {max_retries} attempts: "
                            f"{error_msg}"
                        )
                        return False

            except subprocess.TimeoutExpired:
                logger.warning(f"PR ready command timed out (attempt {attempt}/{max_retries})")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"Failed to mark PR #{pr_number} ready: timeout after {max_retries} attempts")
                    return False

            except Exception as e:
                logger.error(f"Failed to mark PR #{pr_number} ready (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(2 ** attempt)
                else:
                    return False

        return False

    async def delete_branch(self, branch_name: str) -> bool:
        """Delete a remote branch"""
        try:
            repo_arg = f"{self.repo_owner}/{self.repo_name}"

            cmd = [
                'gh', 'api',
                f'repos/{repo_arg}/git/refs/heads/{branch_name}',
                '-X', 'DELETE'
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=30,
                env=self._get_gh_env()
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


class AgentCommentFormatter:
    """Formats agent communications for GitHub"""

    @staticmethod
    def format_agent_completion(
        agent_name: str,
        output: str,
        summary_stats: Dict[str, Any],
        next_steps: str = None
    ) -> str:
        """
        Format agent completion comment with actual output

        Args:
            agent_name: Name of the agent (e.g., 'business_analyst', 'idea_researcher')
            output: The actual output/analysis from Claude
            summary_stats: Dict with counts and metrics (e.g., user_stories_count, quality_score)
            next_steps: Optional next step description
        """
        # Agent-specific emoji and title mapping
        agent_info = {
            'idea_researcher': ('🔬', 'Idea Research'),
            'business_analyst': ('📊', 'Business Analysis'),
            'software_architect': ('🏗️', 'Architecture Design'),
            'senior_software_engineer': ('💻', 'Implementation'),
            'code_reviewer': ('🔍', 'Code Review'),
            'senior_qa_engineer': ('🧪', 'QA Testing'),
            'qa_reviewer': ('✅', 'QA Review'),
            'technical_writer': ('📝', 'Documentation'),
            'documentation_editor': ('✏️', 'Documentation Review')
        }

        emoji, title = agent_info.get(agent_name, ('📋', agent_name.replace('_', ' ').title()))

        # Agent header with H1 and horizontal rule (no emoji)
        agent_header = f"""# {title}

---
"""

        # Build summary section (only if stats are provided)
        summary_section = ""
        if summary_stats:
            summary_section = "**Summary:**\n"
            for key, value in summary_stats.items():
                # Format the key nicely
                label = key.replace('_', ' ').title()
                if isinstance(value, float) and value < 1:
                    # Assume it's a percentage
                    summary_section += f"- {label}: {value * 100:.1f}%\n"
                else:
                    summary_section += f"- {label}: {value}\n"
            summary_section += "---\n\n"

        # Build next steps section
        next_steps_section = ""
        if next_steps:
            next_steps_section = f"\n**Next Steps:**\n{next_steps}\n---\n"

        # Convert output to markdown if it's JSON/dict
        formatted_output = AgentCommentFormatter._format_output_as_markdown(output)

        return f"""{agent_header}
{formatted_output}

---
_Generated by Orchestrator Bot 🤖_
_Processed by the {agent_name} agent_
""".strip()

    @staticmethod
    def _format_output_as_markdown(output: Any) -> str:
        """
        Convert output to markdown format, handling JSON/dict structures

        Args:
            output: Can be str, dict, or JSON string

        Returns:
            Markdown-formatted string
        """
        # If it's already a string and doesn't look like JSON, return as-is
        if isinstance(output, str):
            # Try to parse as JSON
            try:
                output = json.loads(output)
            except (json.JSONDecodeError, TypeError):
                # Not JSON, return as-is
                return output

        # If it's a dict, convert to markdown
        if isinstance(output, dict):
            return AgentCommentFormatter._dict_to_markdown(output)

        # Fallback: convert to string
        return str(output)

    @staticmethod
    def _dict_to_markdown(data: dict, level: int = 0) -> str:
        """
        Recursively convert a dictionary to markdown format

        Args:
            data: Dictionary to convert
            level: Current nesting level for headers

        Returns:
            Markdown-formatted string
        """
        md = []
        indent = "  " * level

        for key, value in data.items():
            # Format the key nicely
            formatted_key = key.replace('_', ' ').title()

            if isinstance(value, dict):
                # Nested dict: use header and recurse
                header_level = min(level + 3, 6)  # Start at h3, max at h6
                md.append(f"\n{'#' * header_level} {formatted_key}\n")
                md.append(AgentCommentFormatter._dict_to_markdown(value, level + 1))
            elif isinstance(value, list):
                # List: format as markdown list
                md.append(f"\n**{formatted_key}:**\n")
                for item in value:
                    if isinstance(item, dict):
                        # List of dicts: format each as a sub-section
                        md.append(f"\n{indent}- **Item:**")
                        for sub_key, sub_value in item.items():
                            formatted_sub_key = sub_key.replace('_', ' ').title()
                            if isinstance(sub_value, list):
                                md.append(f"\n{indent}  - **{formatted_sub_key}:**")
                                for sub_item in sub_value:
                                    md.append(f"\n{indent}    - {sub_item}")
                            else:
                                md.append(f"\n{indent}  - **{formatted_sub_key}:** {sub_value}")
                    else:
                        md.append(f"\n{indent}- {item}")
            else:
                # Simple key-value pair
                if level == 0:
                    md.append(f"\n**{formatted_key}:** {value}")
                else:
                    md.append(f"\n{indent}- **{formatted_key}:** {value}")

        return "".join(md)

    @staticmethod
    def format_agent_status_update(agent_name: str, status: str, details: Dict[str, Any]) -> str:
        """
        Format agent status update (for progress updates, not completion)
        DEPRECATED: Use format_agent_completion for completion messages
        """

        emoji_map = {
            'started': '🚀',
            'in_progress': '⚙️',
            'completed': '✅',
            'failed': '❌',
            'blocked': '🚫',
            'review_requested': '👀'
        }

        emoji = emoji_map.get(status, '📋')
        agent_display = agent_name.replace('_', ' ').title()

        summary = details.get('summary', 'No summary provided')

        # Format findings if present
        findings_section = ""
        if 'findings' in details:
            findings_section = "\n### Key Findings\n"
            for finding in details['findings'][:5]:  # Limit to 5 findings
                # Handle both string and dict formats
                if isinstance(finding, str):
                    findings_section += f"• {finding}\n"
                else:
                    findings_section += f"• {finding.get('message', 'No message')}\n"

        # Format next steps if present
        next_steps_section = ""
        if 'next_steps' in details:
            next_steps_section = "\n### Next Steps\n"
            for step in details['next_steps']:
                next_steps_section += f"- [ ] {step}\n"

        # Format artifacts if present
        artifacts_section = ""
        if 'artifacts' in details:
            artifacts_section = "\n### Generated Artifacts\n"
            for artifact_name in details['artifacts']:
                artifacts_section += f"📄 {artifact_name}\n"

        return f"""## {emoji} {agent_display} - {status.replace('_', ' ').title()}

{summary}
{findings_section}
{next_steps_section}
{artifacts_section}

---
*Updated by Claude Code Orchestrator at {details.get('timestamp', 'unknown time')}*"""

    @staticmethod
    def format_cross_agent_discussion(conversation: List[Dict[str, Any]]) -> str:
        """Format multi-agent conversation"""

        discussion = "## 🤝 Agent Collaboration Summary\n\n"

        for entry in conversation:
            agent = entry.get('agent', 'Unknown').replace('_', ' ').title()
            message = entry.get('message', '')
            timestamp = entry.get('timestamp', '')

            discussion += f"**{agent}** _{timestamp}_:\n{message}\n\n"

        return discussion

    @staticmethod
    def format_decision_log(decisions: List[Dict[str, Any]]) -> str:
        """Format decision log for GitHub"""

        if not decisions:
            return "No decisions recorded."

        log = "## 📋 Decision Log\n\n"

        for i, decision in enumerate(decisions, 1):
            log += f"### Decision {i}: {decision.get('topic', 'Unnamed Decision')}\n"
            log += f"**Decision**: {decision.get('decision', 'Not specified')}\n"
            log += f"**Rationale**: {decision.get('rationale', 'Not provided')}\n"
            log += f"**Made by**: {decision.get('agent', 'Unknown').replace('_', ' ').title()}\n"

            if decision.get('alternatives'):
                log += f"**Alternatives considered**: {', '.join(decision['alternatives'])}\n"

            log += "\n"

        return log
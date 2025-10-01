"""
GitHub Integration Service for Agent Collaboration
"""

import subprocess
import json
import os
import logging
from typing import Dict, Any, List, Optional

logger = logging.getLogger(__name__)

class GitHubIntegration:
    """Handles GitHub API interactions for agent collaboration"""

    def __init__(self):
        self.github_org = os.environ.get('GITHUB_ORG')

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
        """Post a comment to a GitHub issue"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = [
                'gh', 'issue', 'comment', str(issue_number),
                '--body', comment
            ]

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

            # Get the comment URL (GitHub CLI doesn't return it directly)
            # So we'll construct it manually
            issue_url = f"https://github.com/{self.github_org}/{repo}/issues/{issue_number}"

            return {
                'success': True,
                'html_url': issue_url,
                'id': None  # Would need API call to get actual comment ID
            }

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to post GitHub comment: {e.stderr}")
            return {'success': False, 'error': str(e)}

    async def post_pr_comment(self, pr_number: int, comment: str, repo: Optional[str] = None) -> Dict[str, Any]:
        """Post a comment to a GitHub pull request"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = [
                'gh', 'pr', 'comment', str(pr_number),
                '--body', comment
            ]

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

            return {'success': True}

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to post PR comment: {e.stderr}")
            return {'success': False, 'error': str(e)}

    async def create_pr_review(
        self,
        pr_number: int,
        review_type: str,  # "approve", "request-changes", "comment"
        body: str,
        repo: Optional[str] = None
    ) -> Dict[str, Any]:
        """Create a formal PR review"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = [
                'gh', 'pr', 'review', str(pr_number),
                f'--{review_type}',
                '--body', body
            ]

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

            return {'success': True}

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create PR review: {e.stderr}")
            return {'success': False, 'error': str(e)}

    async def get_issue_details(self, issue_number: int, repo: Optional[str] = None) -> Dict[str, Any]:
        """Get issue details"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'issue', 'view', str(issue_number), '--json', 'title,body,state,labels,assignees']

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

            return json.loads(result.stdout)

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get issue details: {e.stderr}")
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

    async def get_feedback_comments(self, issue_number: int, repo: Optional[str] = None,
                                    since_timestamp: Optional[str] = None) -> List[Dict[str, Any]]:
        """Get comments that mention @orchestrator-bot for feedback"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'issue', 'view', str(issue_number), '--json', 'comments']

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())
            data = json.loads(result.stdout)

            feedback_comments = []
            for comment in data.get('comments', []):
                body = comment.get('body', '')
                created_at = comment.get('createdAt', '')
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
                        'author': comment.get('author', {}).get('login', 'unknown'),
                        'created_at': created_at,
                        'is_bot': comment.get('author', {}).get('isBot', False)
                    })

            return feedback_comments

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to fetch feedback comments: {e.stderr}")
            return []
        except Exception as e:
            import traceback
            logger.warning(f"Error processing feedback comments: {e}")
            logger.info(f"Full traceback: {traceback.format_exc()}")
            return []

    async def get_pr_details(self, pr_number: int, repo: Optional[str] = None) -> Dict[str, Any]:
        """Get pull request details"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'pr', 'view', str(pr_number), '--json', 'title,body,state,headRefName,baseRefName']

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True, env=self._get_gh_env())

            return json.loads(result.stdout)

        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to get PR details: {e.stderr}")
            return {}

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
            'product_manager': ('🎯', 'Product Planning'),
            'requirements_reviewer': ('✅', 'Requirements Review'),
            'software_architect': ('🏗️', 'Architecture Design'),
            'design_reviewer': ('👁️', 'Design Review'),
            'senior_software_engineer': ('💻', 'Implementation'),
            'code_reviewer': ('🔍', 'Code Review'),
            'senior_qa_engineer': ('🧪', 'Testing'),
            'test_planner': ('📋', 'Test Planning'),
            'test_reviewer': ('✔️', 'Test Review'),
            'technical_writer': ('📝', 'Documentation'),
            'documentation_editor': ('✏️', 'Documentation Review')
        }

        emoji, title = agent_info.get(agent_name, ('📋', agent_name.replace('_', ' ').title()))

        # Build summary section
        summary_section = "**Summary:**\n"
        for key, value in summary_stats.items():
            # Format the key nicely
            label = key.replace('_', ' ').title()
            if isinstance(value, float) and value < 1:
                # Assume it's a percentage
                summary_section += f"- {label}: {value * 100:.1f}%\n"
            else:
                summary_section += f"- {label}: {value}\n"

        # Build next steps section
        next_steps_section = ""
        if next_steps:
            next_steps_section = f"\n**Next Steps:**\n{next_steps}\n"

        # Convert output to markdown if it's JSON/dict
        formatted_output = AgentCommentFormatter._format_output_as_markdown(output)

        return f"""## {emoji} {title} Complete

{title} has been completed by the orchestrator.

{summary_section}
---

## Analysis Output

{formatted_output}

---
{next_steps_section}
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
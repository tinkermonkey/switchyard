"""
GitHub Integration Service for Agent Collaboration
"""

import subprocess
import json
import os
from typing import Dict, Any, List, Optional

class GitHubIntegration:
    """Handles GitHub API interactions for agent collaboration"""

    def __init__(self):
        self.github_org = os.environ.get('GITHUB_ORG')

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

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Get the comment URL (GitHub CLI doesn't return it directly)
            # So we'll construct it manually
            issue_url = f"https://github.com/{self.github_org}/{repo}/issues/{issue_number}"

            return {
                'success': True,
                'html_url': issue_url,
                'id': None  # Would need API call to get actual comment ID
            }

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to post GitHub comment: {e.stderr}")
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

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            return {'success': True}

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to post PR comment: {e.stderr}")
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

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            return {'success': True}

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to create PR review: {e.stderr}")
            return {'success': False, 'error': str(e)}

    async def get_issue_details(self, issue_number: int, repo: Optional[str] = None) -> Dict[str, Any]:
        """Get issue details"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'issue', 'view', str(issue_number), '--json', 'title,body,state,labels,assignees']

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            return json.loads(result.stdout)

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to get issue details: {e.stderr}")
            return {}

    async def get_pr_details(self, pr_number: int, repo: Optional[str] = None) -> Dict[str, Any]:
        """Get pull request details"""
        try:
            repo_arg = f"{self.github_org}/{repo}" if repo else ""

            cmd = ['gh', 'pr', 'view', str(pr_number), '--json', 'title,body,state,headRefName,baseRefName']

            if repo:
                cmd.extend(['--repo', repo_arg])

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            return json.loads(result.stdout)

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to get PR details: {e.stderr}")
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

                subprocess.run(cmd, capture_output=True, text=True, check=True)

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to add labels: {e.stderr}")

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

            result = subprocess.run(cmd, capture_output=True, text=True, check=True)

            # Extract issue number from output
            issue_url = result.stdout.strip()
            issue_number = issue_url.split('/')[-1]

            return {
                'success': True,
                'issue_number': int(issue_number),
                'url': issue_url
            }

        except subprocess.CalledProcessError as e:
            print(f"⚠️ Failed to create issue: {e.stderr}")
            return {'success': False, 'error': str(e)}

class AgentCommentFormatter:
    """Formats agent communications for GitHub"""

    @staticmethod
    def format_agent_status_update(agent_name: str, status: str, details: Dict[str, Any]) -> str:
        """Format agent status update"""

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
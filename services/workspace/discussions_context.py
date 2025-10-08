"""
Discussions workspace context - handles GitHub Discussions without git operations.
"""

from pathlib import Path
from typing import Dict, Any
from .context import WorkspaceContext
import logging

logger = logging.getLogger(__name__)


class DiscussionsWorkspaceContext(WorkspaceContext):
    """
    Workspace context for GitHub Discussions (no git operations).

    This workspace type:
    - Does NOT perform git operations
    - Posts output to discussion comments
    - Used for ideation, requirements gathering, design discussions
    """

    def __init__(self, project, issue_number, task_context, github_integration):
        super().__init__(project, issue_number, task_context, github_integration)
        self.discussion_id = task_context.get('discussion_id')

    @property
    def supports_git_operations(self) -> bool:
        return False

    @property
    def workspace_type(self) -> str:
        return 'discussions'

    async def prepare_execution(self) -> Dict[str, Any]:
        """
        Prepare discussions workspace (no git operations needed).

        Returns:
            Dict containing:
                - discussion_id: GitHub discussion ID
                - work_dir: Temporary working directory
        """
        self._logger.info(
            f"Preparing discussions workspace for issue #{self.issue_number} "
            f"(discussion: {self.discussion_id})"
        )

        if not self.discussion_id:
            self._logger.warning(
                f"No discussion_id provided for issue #{self.issue_number}"
            )

        return {
            'discussion_id': self.discussion_id,
            'work_dir': str(self.get_working_directory())
        }

    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """
        Finalize discussions workspace (no git operations).

        For discussions, there's nothing to finalize - all output
        is posted via comments.

        Args:
            result: Agent execution result (unused)
            commit_message: Commit message (unused)

        Returns:
            Dict with success status
        """
        self._logger.info(
            f"Finalizing discussions workspace for issue #{self.issue_number} "
            "(no git operations)"
        )

        return {
            'success': True,
            'message': 'Discussions workspace requires no finalization',
            'workspace_type': 'discussions'
        }

    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """
        Post output as discussion comment.

        Args:
            agent_name: Name of the agent
            markdown_output: Markdown-formatted output

        Returns:
            Dict with success status and posted location
        """
        from services.github_discussions import GitHubDiscussions

        if not self.discussion_id:
            self._logger.error(
                f"Cannot post to discussion - no discussion_id for issue #{self.issue_number}"
            )
            return {
                'success': False,
                'error': 'No discussion_id provided'
            }

        self._logger.info(
            f"Posting {agent_name} output to discussion {self.discussion_id}"
        )

        discussions = GitHubDiscussions(
            self.github.repo_owner,
            self.github.repo_name,
            self.github.token
        )

        await discussions.add_comment(
            self.discussion_id,
            markdown_output
        )

        return {
            'success': True,
            'posted_to': f'discussion {self.discussion_id}',
            'issue_number': self.issue_number
        }

    def get_working_directory(self) -> Path:
        """
        Get temporary working directory for discussions.

        Discussions don't need a git repo, so we use a temporary workspace.

        Returns:
            Path to temporary working directory
        """
        # Create a temporary workspace for discussions
        # This allows agents to read/write files if needed, but changes aren't committed
        temp_workspace = Path(f"/tmp/discussions/{self.project}")
        temp_workspace.mkdir(parents=True, exist_ok=True)
        return temp_workspace

    async def get_execution_metadata(self) -> Dict[str, Any]:
        """
        Get metadata for observability.

        Returns:
            Dict with workspace type, discussion ID, and issue number
        """
        return {
            'workspace_type': 'discussions',
            'discussion_id': self.discussion_id,
            'issue_number': self.issue_number,
            'supports_git': False
        }

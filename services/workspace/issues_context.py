"""
Issues workspace context - handles GitHub Issues with git operations.
"""

from pathlib import Path
from typing import Dict, Any
from .context import WorkspaceContext
import logging

logger = logging.getLogger(__name__)


class IssuesWorkspaceContext(WorkspaceContext):
    """
    Workspace context for GitHub Issues with git operations.

    This workspace type:
    - Prepares feature branches for development work
    - Commits and pushes changes
    - Creates/updates pull requests
    - Posts output to issue comments
    """

    def __init__(self, project, issue_number, task_context, github_integration):
        super().__init__(project, issue_number, task_context, github_integration)
        self.branch_name = None

    @property
    def supports_git_operations(self) -> bool:
        return True

    @property
    def workspace_type(self) -> str:
        return 'issues'

    async def prepare_execution(self) -> Dict[str, Any]:
        """
        Prepare feature branch and checkout for development work.

        Returns:
            Dict containing:
                - branch_name: Name of the prepared feature branch
                - work_dir: Working directory path
        """
        from services.feature_branch_manager import feature_branch_manager

        issue_title = self.task_context.get('issue_title', '')

        self._logger.info(
            f"Preparing feature branch for issue #{self.issue_number} in project {self.project}"
        )

        self.branch_name = await feature_branch_manager.prepare_feature_branch(
            project=self.project,
            issue_number=self.issue_number,
            github_integration=self.github,
            issue_title=issue_title
        )

        self._logger.info(f"Prepared feature branch: {self.branch_name}")

        return {
            'branch_name': self.branch_name,
            'work_dir': str(self.get_working_directory())
        }

    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """
        Commit changes, push, and create/update PR.

        Args:
            result: Agent execution result
            commit_message: Commit message for changes

        Returns:
            Dict containing:
                - success: Whether finalization succeeded
                - branch_name: Branch name
                - pr_url: Pull request URL (if created)
                - all_complete: Whether all sub-issues are complete
        """
        from services.feature_branch_manager import feature_branch_manager

        self._logger.info(
            f"Finalizing feature branch work for issue #{self.issue_number}"
        )

        finalize_result = await feature_branch_manager.finalize_feature_branch_work(
            project=self.project,
            issue_number=self.issue_number,
            commit_message=commit_message,
            github_integration=self.github
        )

        if finalize_result.get('success'):
            self._logger.info(
                f"Successfully finalized work: PR {finalize_result.get('pr_url', 'N/A')}"
            )
        else:
            self._logger.warning(f"Finalization had issues: {finalize_result}")

        return finalize_result

    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """
        Post output as issue comment.

        Args:
            agent_name: Name of the agent
            markdown_output: Markdown-formatted output

        Returns:
            Dict with success status and posted location
        """
        self._logger.info(
            f"Posting {agent_name} output to issue #{self.issue_number}"
        )

        await self.github.post_comment(
            self.issue_number,
            markdown_output,
            pipeline_run_id=self.task_context.get('pipeline_run_id'),
        )

        return {
            'success': True,
            'posted_to': f'issue #{self.issue_number}'
        }

    def get_working_directory(self) -> Path:
        """
        Get project git repository directory.

        Returns:
            Path to the project's git repository
        """
        from services.project_workspace import workspace_manager
        return workspace_manager.get_project_dir(self.project)

    async def get_execution_metadata(self) -> Dict[str, Any]:
        """
        Get metadata for observability.

        Returns:
            Dict with workspace type, issue number, and branch name
        """
        return {
            'workspace_type': 'issues',
            'issue_number': self.issue_number,
            'branch_name': self.branch_name,
            'supports_git': True
        }

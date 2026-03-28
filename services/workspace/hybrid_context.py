"""
Hybrid workspace context - handles workflows that span discussions and issues.
"""

from pathlib import Path
from typing import Dict, Any, Optional
from .context import WorkspaceContext
import logging

logger = logging.getLogger(__name__)


class HybridWorkspaceContext(WorkspaceContext):
    """
    Workspace context for hybrid workflows (discussions + issues).

    This workspace type:
    - Starts in discussions for early-stage work
    - Transitions to issues for implementation
    - Determines workspace dynamically based on workflow stage
    - Posts to appropriate location based on current stage
    """

    def __init__(self, project, issue_number, task_context, github_integration):
        super().__init__(project, issue_number, task_context, github_integration)
        self.discussion_id = task_context.get('discussion_id')
        self.branch_name = None
        self._current_workspace = None  # Will be 'discussions' or 'issues'

    @property
    def supports_git_operations(self) -> bool:
        """Git operations support depends on current stage"""
        return self._current_workspace == 'issues'

    @property
    def workspace_type(self) -> str:
        return 'hybrid'

    def _determine_current_workspace(self) -> str:
        """
        Determine which workspace to use based on task context.

        Logic:
        - If discussion_id is present and no branch work needed -> discussions
        - If implementation/code work needed -> issues
        - Default to discussions for early stages

        Returns:
            'discussions' or 'issues'
        """
        # Check if we have indicators of implementation work
        column = self.task_context.get('column', '').lower()
        agent_name = self.task_context.get('agent_name', '').lower()

        # Implementation-focused columns/agents use issues workspace
        implementation_columns = ['development', 'code review', 'testing', 'qa']
        implementation_agents = [
            'senior_software_engineer', 'code_reviewer',
            'senior_qa_engineer', 'qa_reviewer'
        ]

        if any(col in column for col in implementation_columns):
            return 'issues'

        if any(agent in agent_name for agent in implementation_agents):
            return 'issues'

        # Early-stage work uses discussions
        return 'discussions'

    async def prepare_execution(self) -> Dict[str, Any]:
        """
        Prepare workspace based on current stage.

        Returns:
            Dict containing workspace-specific context
        """
        self._current_workspace = self._determine_current_workspace()

        self._logger.info(
            f"Preparing hybrid workspace for issue #{self.issue_number} "
            f"(current workspace: {self._current_workspace})"
        )

        if self._current_workspace == 'issues':
            # Prepare git branch for implementation work
            from services.feature_branch_manager import feature_branch_manager

            issue_title = self.task_context.get('issue_title', '')
            self.branch_name = await feature_branch_manager.prepare_feature_branch(
                project=self.project,
                issue_number=self.issue_number,
                github_integration=self.github,
                issue_title=issue_title
            )

            self._logger.info(f"Prepared feature branch: {self.branch_name}")

            return {
                'branch_name': self.branch_name,
                'work_dir': str(self.get_working_directory()),
                'current_workspace': 'issues'
            }
        else:
            # Discussions workspace - no git operations
            return {
                'discussion_id': self.discussion_id,
                'work_dir': str(self.get_working_directory()),
                'current_workspace': 'discussions'
            }

    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """
        Finalize workspace based on current stage.

        Args:
            result: Agent execution result
            commit_message: Commit message for changes

        Returns:
            Dict with finalization results
        """
        self._logger.info(
            f"Finalizing hybrid workspace for issue #{self.issue_number} "
            f"(current workspace: {self._current_workspace})"
        )

        if self._current_workspace == 'issues':
            # Finalize git operations
            from services.feature_branch_manager import feature_branch_manager

            finalize_result = await feature_branch_manager.finalize_feature_branch_work(
                project=self.project,
                issue_number=self.issue_number,
                commit_message=commit_message,
                github_integration=self.github
            )

            return finalize_result
        else:
            # Discussions - no finalization needed
            return {
                'success': True,
                'message': 'Hybrid workspace in discussions mode - no git finalization',
                'current_workspace': 'discussions'
            }

    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """
        Post output to appropriate location based on current stage.

        Args:
            agent_name: Name of the agent
            markdown_output: Markdown-formatted output

        Returns:
            Dict with posting results
        """
        if self._current_workspace == 'discussions' and self.discussion_id:
            # Post to discussions
            from services.github_discussions import GitHubDiscussions

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
                'workspace': 'discussions'
            }
        else:
            # Post to issue
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
                'posted_to': f'issue #{self.issue_number}',
                'workspace': 'issues'
            }

    def get_working_directory(self) -> Path:
        """
        Get working directory based on current stage.

        Returns:
            Path to working directory
        """
        if self._current_workspace == 'issues':
            # Use actual git repository
            from services.project_workspace import workspace_manager
            return workspace_manager.get_project_dir(self.project)
        else:
            # Use temporary workspace for discussions
            temp_workspace = Path(f"/tmp/discussions/{self.project}")
            temp_workspace.mkdir(parents=True, exist_ok=True)
            return temp_workspace

    async def get_execution_metadata(self) -> Dict[str, Any]:
        """
        Get metadata for observability.

        Returns:
            Dict with workspace information
        """
        return {
            'workspace_type': 'hybrid',
            'current_workspace': self._current_workspace,
            'discussion_id': self.discussion_id,
            'issue_number': self.issue_number,
            'branch_name': self.branch_name,
            'supports_git': self.supports_git_operations
        }

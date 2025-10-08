"""
Abstract base class for workspace contexts.

This module defines the interface that all workspace types must implement.
"""

from abc import ABC, abstractmethod
from typing import Dict, Any, Optional
from pathlib import Path
import logging

logger = logging.getLogger(__name__)


class WorkspaceContext(ABC):
    """
    Abstract base class for workspace-specific execution contexts.

    Each workspace type (issues, discussions, hybrid) implements this
    interface to provide workspace-specific behavior without conditionals
    in the orchestration layer.
    """

    def __init__(
        self,
        project: str,
        issue_number: int,
        task_context: Dict[str, Any],
        github_integration
    ):
        """
        Initialize workspace context.

        Args:
            project: Project name
            issue_number: GitHub issue/discussion number
            task_context: Task execution context
            github_integration: GitHub integration instance
        """
        self.project = project
        self.issue_number = issue_number
        self.task_context = task_context
        self.github = github_integration
        self._logger = logging.getLogger(f"{__name__}.{self.__class__.__name__}")

    @property
    @abstractmethod
    def supports_git_operations(self) -> bool:
        """Whether this workspace supports git branch operations"""
        pass

    @property
    @abstractmethod
    def workspace_type(self) -> str:
        """The type of workspace ('issues', 'discussions', 'hybrid')"""
        pass

    @abstractmethod
    async def prepare_execution(self) -> Dict[str, Any]:
        """
        Prepare workspace for agent execution.

        This may include:
        - Creating/checking out git branches (issues workspace)
        - Validating discussion access (discussions workspace)
        - Setting up hybrid workflow state

        Returns:
            Dict with workspace-specific context (e.g., branch_name, discussion_id)

        Raises:
            Exception if preparation fails
        """
        pass

    @abstractmethod
    async def finalize_execution(
        self,
        result: Dict[str, Any],
        commit_message: str
    ) -> Dict[str, Any]:
        """
        Finalize workspace after agent execution.

        This may include:
        - Committing changes and creating PRs (issues workspace)
        - No-op for discussions (discussions workspace)
        - Conditional finalization based on state (hybrid workspace)

        Args:
            result: Agent execution result
            commit_message: Commit message if git operations needed

        Returns:
            Dict with finalization results (e.g., pr_url, comment_id)

        Raises:
            Exception if finalization fails
        """
        pass

    @abstractmethod
    async def post_output(
        self,
        agent_name: str,
        markdown_output: str
    ) -> Dict[str, Any]:
        """
        Post agent output to the appropriate location.

        This may include:
        - Posting to issue comments (issues workspace)
        - Posting to discussion comments (discussions workspace)
        - Posting to both (hybrid workspace)

        Args:
            agent_name: Name of the agent
            markdown_output: Markdown-formatted output

        Returns:
            Dict with posting results

        Raises:
            Exception if posting fails
        """
        pass

    @abstractmethod
    def get_working_directory(self) -> Path:
        """
        Get the working directory for this workspace.

        Returns:
            Path to working directory
        """
        pass

    @abstractmethod
    async def get_execution_metadata(self) -> Dict[str, Any]:
        """
        Get workspace-specific metadata for logging/observability.

        Returns:
            Dict with metadata (workspace_type, identifiers, etc.)
        """
        pass


class WorkspaceContextFactory:
    """Factory for creating workspace contexts"""

    @staticmethod
    def create(
        workspace_type: str,
        project: str,
        issue_number: int,
        task_context: Dict[str, Any],
        github_integration
    ) -> WorkspaceContext:
        """
        Create appropriate workspace context based on type.

        Args:
            workspace_type: Type of workspace ('issues', 'discussions', 'hybrid')
            project: Project name
            issue_number: GitHub issue/discussion number
            task_context: Task execution context
            github_integration: GitHub integration instance

        Returns:
            Appropriate WorkspaceContext instance

        Raises:
            ValueError: If workspace_type is unknown
        """
        if workspace_type == 'issues':
            from .issues_context import IssuesWorkspaceContext
            return IssuesWorkspaceContext(
                project, issue_number, task_context, github_integration
            )
        elif workspace_type == 'discussions':
            from .discussions_context import DiscussionsWorkspaceContext
            return DiscussionsWorkspaceContext(
                project, issue_number, task_context, github_integration
            )
        elif workspace_type == 'hybrid':
            from .hybrid_context import HybridWorkspaceContext
            return HybridWorkspaceContext(
                project, issue_number, task_context, github_integration
            )
        else:
            raise ValueError(f"Unknown workspace type: {workspace_type}")

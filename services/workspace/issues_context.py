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

    def __init__(self, project, issue_number, task_context, github_integration, pipeline_run=None):
        super().__init__(project, issue_number, task_context, github_integration)
        self.branch_name = None
        # The dispatch's PipelineRun (#122) -- resolve_workspace() reads/writes
        # branch_name/project_dir/epic_id directly onto this same instance, so
        # prepare_execution()/get_working_directory() below need it in scope
        # rather than resolving their own, independent branch (the pre-#122
        # behavior this replaced: FeatureBranchManager.prepare_feature_branch(),
        # checking out directly on the shared base clone).
        self.pipeline_run = pipeline_run

    @property
    def supports_git_operations(self) -> bool:
        return True

    @property
    def workspace_type(self) -> str:
        return 'issues'

    async def prepare_execution(self) -> Dict[str, Any]:
        """
        Resolve this issue's epic branch and isolated git worktree.

        Reads (and, on first call for this run, resolves) branch_name/project_dir
        via PipelineRunManager.resolve_workspace() against self.pipeline_run --
        the same centralized resolver repair-cycle uses for the same epic (#122,
        WI-C of #119), instead of independently resolving and checking out a
        branch on the shared base clone via FeatureBranchManager.
        prepare_feature_branch() (the pre-#122 behavior this replaced). Idempotent:
        a second call against an already-resolved pipeline_run is a cheap no-op.

        Returns:
            Dict containing:
                - branch_name: Name of the resolved feature branch
                - work_dir: Working directory path (the epic's isolated worktree)

        Raises:
            ValueError: No pipeline_run was supplied to construct this context, or
                (propagated from resolve_workspace()) no resolvable parent epic.
            RuntimeError: (propagated from resolve_workspace()) the worktree add
                failed.
        """
        if self.pipeline_run is None:
            raise ValueError(
                f"IssuesWorkspaceContext for {self.project}/#{self.issue_number} has no "
                "pipeline_run to resolve a workspace against -- WorkspaceContextFactory."
                "create() must be given the dispatch's PipelineRun instance for the "
                "'issues' workspace type (#122)."
            )

        from services.pipeline_run import get_pipeline_run_manager

        self._logger.info(
            f"Resolving workspace for issue #{self.issue_number} in project {self.project} "
            f"via pipeline run {self.pipeline_run.id}"
        )

        self.pipeline_run = await get_pipeline_run_manager().resolve_workspace(
            self.pipeline_run, self.github, self.workspace_type
        )
        self.branch_name = self.pipeline_run.branch_name

        self._logger.info(f"Resolved feature branch: {self.branch_name}")

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

        if self.pipeline_run is None or not self.pipeline_run.project_dir:
            # Fail loud, matching prepare_execution()'s guard above -- silently
            # passing None here would make finalize_feature_branch_work() default
            # to the shared base clone (code review finding, issue #122), exactly
            # the silent-wrong-directory bug class this migration exists to fix.
            raise ValueError(
                f"Cannot finalize issue #{self.issue_number} ({self.project}) -- no "
                "resolved project_dir. prepare_execution() must run (and succeed) "
                "before finalize_execution() is called."
            )

        finalize_result = await feature_branch_manager.finalize_feature_branch_work(
            project=self.project,
            issue_number=self.issue_number,
            commit_message=commit_message,
            github_integration=self.github,
            # Commit/push from the SAME isolated epic worktree prepare_execution()
            # resolved and the agent actually worked in (issue #122/WI-C review
            # finding) -- without this, finalize_feature_branch_work() defaults to
            # the shared base clone, which has none of the agent's real changes.
            project_dir_override=self.pipeline_run.project_dir
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
        Get the epic's isolated git worktree directory.

        Returns:
            Path to the epic worktree resolved onto self.pipeline_run by
            resolve_workspace() -- see prepare_execution() above (#122, WI-C of
            #119). Only valid once prepare_execution() has run.
        """
        if self.pipeline_run is None or not self.pipeline_run.project_dir:
            # Fail with a clear diagnostic, not Path(None)'s opaque TypeError
            # (code review finding, issue #122) -- this means prepare_execution()
            # either wasn't called first, or didn't successfully resolve.
            raise ValueError(
                f"IssuesWorkspaceContext for {self.project}/#{self.issue_number} has no "
                "resolved project_dir -- prepare_execution() must run (and succeed) "
                "before get_working_directory() is called."
            )
        return Path(self.pipeline_run.project_dir)

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

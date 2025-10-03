"""
Workspace Router

Determines whether work should happen in GitHub Issues or Discussions
based on project configuration and pipeline stage.
"""

import logging
from typing import Dict, Any, Optional, Tuple
from config.manager import config_manager
from services.github_discussions import GitHubDiscussions

logger = logging.getLogger(__name__)


class WorkspaceRouter:
    """Routes work to appropriate GitHub workspace (Issues or Discussions)"""

    def __init__(self):
        self.discussions = GitHubDiscussions()

    def determine_workspace(self, project: str, board: str, stage: str) -> Tuple[str, Optional[str]]:
        """
        Determine which workspace to use for this stage

        Args:
            project: Project name
            board: Board/pipeline name
            stage: Current pipeline stage

        Returns:
            Tuple of (workspace_type, category_id_or_none)
            workspace_type: "issues" or "discussions"
            category_id: Discussion category ID if workspace is discussions, None otherwise
        """
        try:
            project_config = config_manager.get_project_config(project)

            # Find pipeline config
            pipeline_config = None
            for pipeline in project_config.pipelines:
                if pipeline.board_name == board:
                    pipeline_config = pipeline
                    break

            if not pipeline_config:
                logger.warning(f"No pipeline config found for {project}/{board}, defaulting to issues")
                return ("issues", None)

            # Check workspace configuration
            workspace = pipeline_config.workspace

            if workspace == "issues":
                return ("issues", None)

            elif workspace == "discussions":
                # Get or determine category ID
                category_id = self._get_discussion_category(
                    project_config.github['org'],
                    project_config.github['repo'],
                    pipeline_config.discussion_category
                )
                return ("discussions", category_id)

            elif workspace == "hybrid":
                # Check which list this stage belongs to
                if pipeline_config.discussion_stages and stage in pipeline_config.discussion_stages:
                    category_id = self._get_discussion_category(
                        project_config.github['org'],
                        project_config.github['repo'],
                        pipeline_config.discussion_category
                    )
                    return ("discussions", category_id)
                else:
                    # Default to issues for implementation stages
                    return ("issues", None)

            else:
                logger.warning(f"Unknown workspace type: {workspace}, defaulting to issues")
                return ("issues", None)

        except Exception as e:
            logger.error(f"Error determining workspace: {e}")
            return ("issues", None)

    def _get_discussion_category(self, owner: str, repo: str,
                                 preferred_category: Optional[str] = None) -> Optional[str]:
        """
        Get discussion category ID

        Args:
            owner: Repository owner
            repo: Repository name
            preferred_category: Preferred category name (e.g. "Ideas")

        Returns:
            Category ID or None
        """
        # Default to "Ideas" if no preference specified
        category_name = preferred_category or "Ideas"

        category_id = self.discussions.find_category_by_name(owner, repo, category_name)

        if not category_id:
            logger.warning(f"Category '{category_name}' not found, will use first available")
            categories = self.discussions.get_discussion_categories(owner, repo)
            if categories:
                category_id = categories[0]['id']
                logger.info(f"Using category: {categories[0]['name']}")

        return category_id

    def get_workspace_identifier(self, context: Dict[str, Any]) -> Tuple[str, Any]:
        """
        Extract workspace identifier from context

        Args:
            context: Task context

        Returns:
            Tuple of (workspace_type, identifier)
            For issues: ("issues", issue_number)
            For discussions: ("discussions", discussion_id)
        """
        workspace_type = context.get('workspace_type', 'issues')

        if workspace_type == 'discussions':
            discussion_id = context.get('discussion_id')
            return ('discussions', discussion_id)
        else:
            issue_number = context.get('issue_number')
            return ('issues', issue_number)

    def create_or_get_workspace(self, project: str, board: str,
                               issue_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Create or get workspace for this work item

        Args:
            project: Project name
            board: Board name
            issue_data: Issue/requirement data

        Returns:
            Dict with workspace_type, workspace_id, and other metadata
        """
        try:
            project_config = config_manager.get_project_config(project)

            # Determine workspace
            workspace_type, category_id = self.determine_workspace(project, board, "initial")

            if workspace_type == "discussions":
                # Check if discussion already exists for this issue
                # For now, create new discussion
                discussion_id = self.discussions.create_discussion(
                    owner=project_config.github['org'],
                    repo=project_config.github['repo'],
                    category_id=category_id,
                    title=issue_data.get('title', 'Untitled'),
                    body=issue_data.get('body', '')
                )

                if discussion_id:
                    return {
                        'workspace_type': 'discussions',
                        'discussion_id': discussion_id,
                        'category_id': category_id
                    }
                else:
                    logger.error("Failed to create discussion, falling back to issues")
                    return {
                        'workspace_type': 'issues',
                        'issue_number': issue_data.get('number')
                    }
            else:
                # Use existing issue
                return {
                    'workspace_type': 'issues',
                    'issue_number': issue_data.get('number')
                }

        except Exception as e:
            logger.error(f"Error creating/getting workspace: {e}")
            return {
                'workspace_type': 'issues',
                'issue_number': issue_data.get('number')
            }


# Global singleton
workspace_router = WorkspaceRouter()

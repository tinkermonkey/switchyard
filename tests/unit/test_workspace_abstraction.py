"""
Tests for workspace abstraction layer.

These tests verify the workspace context implementations work correctly.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from pathlib import Path

from services.workspace.context import WorkspaceContext, WorkspaceContextFactory
from services.workspace.issues_context import IssuesWorkspaceContext
from services.workspace.discussions_context import DiscussionsWorkspaceContext
from services.workspace.hybrid_context import HybridWorkspaceContext


class TestWorkspaceContextFactory:
    """Test the workspace context factory"""

    def test_create_issues_context(self):
        """Factory should create IssuesWorkspaceContext for 'issues' type"""
        mock_gh = MagicMock()
        context = WorkspaceContextFactory.create(
            workspace_type='issues',
            project='test-project',
            issue_number=123,
            task_context={'issue_title': 'Test'},
            github_integration=mock_gh
        )

        assert isinstance(context, IssuesWorkspaceContext)
        assert context.workspace_type == 'issues'
        assert context.supports_git_operations == True

    def test_create_discussions_context(self):
        """Factory should create DiscussionsWorkspaceContext for 'discussions' type"""
        mock_gh = MagicMock()
        context = WorkspaceContextFactory.create(
            workspace_type='discussions',
            project='test-project',
            issue_number=88,
            task_context={'discussion_id': 'D_test123'},
            github_integration=mock_gh
        )

        assert isinstance(context, DiscussionsWorkspaceContext)
        assert context.workspace_type == 'discussions'
        assert context.supports_git_operations == False

    def test_create_hybrid_context(self):
        """Factory should create HybridWorkspaceContext for 'hybrid' type"""
        mock_gh = MagicMock()
        context = WorkspaceContextFactory.create(
            workspace_type='hybrid',
            project='test-project',
            issue_number=99,
            task_context={'discussion_id': 'D_test'},
            github_integration=mock_gh
        )

        assert isinstance(context, HybridWorkspaceContext)
        assert context.workspace_type == 'hybrid'

    def test_create_unknown_type_raises_error(self):
        """Factory should raise ValueError for unknown workspace type"""
        mock_gh = MagicMock()

        with pytest.raises(ValueError, match="Unknown workspace type: invalid"):
            WorkspaceContextFactory.create(
                workspace_type='invalid',
                project='test-project',
                issue_number=123,
                task_context={},
                github_integration=mock_gh
            )


class TestIssuesWorkspaceContext:
    """Test Issues workspace context"""

    @pytest.mark.asyncio
    async def test_prepare_execution_creates_branch(self):
        """Issues workspace should prepare feature branch"""
        mock_gh = MagicMock()
        context = IssuesWorkspaceContext(
            project='test-project',
            issue_number=123,
            task_context={'issue_title': 'Test feature'},
            github_integration=mock_gh
        )

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.project_workspace.workspace_manager') as mock_wm:

            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value='feature/issue-123-test'
            )
            mock_wm.get_project_dir.return_value = Path('/workspace/test-project')

            result = await context.prepare_execution()

            # Verify branch was prepared
            mock_fbm.prepare_feature_branch.assert_called_once_with(
                project='test-project',
                issue_number=123,
                github_integration=mock_gh,
                issue_title='Test feature'
            )

            # Verify result
            assert result['branch_name'] == 'feature/issue-123-test'
            assert '/workspace/test-project' in result['work_dir']
            assert context.branch_name == 'feature/issue-123-test'

    @pytest.mark.asyncio
    async def test_finalize_execution_commits_and_creates_pr(self):
        """Issues workspace should finalize with git operations"""
        mock_gh = MagicMock()
        context = IssuesWorkspaceContext(
            project='test-project',
            issue_number=123,
            task_context={},
            github_integration=mock_gh
        )

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm:
            mock_fbm.finalize_feature_branch_work = AsyncMock(
                return_value={
                    'success': True,
                    'pr_url': 'https://github.com/org/repo/pull/1',
                    'branch_name': 'feature/test'
                }
            )

            result = await context.finalize_execution(
                result={'status': 'success'},
                commit_message='Test commit'
            )

            # Verify finalization was called
            mock_fbm.finalize_feature_branch_work.assert_called_once_with(
                project='test-project',
                issue_number=123,
                commit_message='Test commit',
                github_integration=mock_gh
            )

            # Verify result
            assert result['success'] == True
            assert 'pr_url' in result

    @pytest.mark.asyncio
    async def test_post_output_to_issue(self):
        """Issues workspace should post to issue comments"""
        mock_gh = MagicMock()
        mock_gh.post_comment = AsyncMock()

        context = IssuesWorkspaceContext(
            project='test-project',
            issue_number=123,
            task_context={},
            github_integration=mock_gh
        )

        result = await context.post_output(
            agent_name='test_agent',
            markdown_output='# Test Output'
        )

        # Verify comment was posted
        mock_gh.post_comment.assert_called_once_with(
            123,
            '# Test Output'
        )

        # Verify result
        assert result['success'] == True
        assert 'issue #123' in result['posted_to']

    @pytest.mark.asyncio
    async def test_get_execution_metadata(self):
        """Issues workspace should provide correct metadata"""
        mock_gh = MagicMock()
        context = IssuesWorkspaceContext(
            project='test-project',
            issue_number=123,
            task_context={},
            github_integration=mock_gh
        )
        context.branch_name = 'feature/test'

        metadata = await context.get_execution_metadata()

        assert metadata['workspace_type'] == 'issues'
        assert metadata['issue_number'] == 123
        assert metadata['branch_name'] == 'feature/test'
        assert metadata['supports_git'] == True


class TestDiscussionsWorkspaceContext:
    """Test Discussions workspace context"""

    @pytest.mark.asyncio
    async def test_prepare_execution_no_git_ops(self):
        """Discussions workspace should NOT perform git operations"""
        mock_gh = MagicMock()
        context = DiscussionsWorkspaceContext(
            project='test-project',
            issue_number=88,
            task_context={'discussion_id': 'D_test123'},
            github_integration=mock_gh
        )

        result = await context.prepare_execution()

        # Verify no git operations
        assert result['discussion_id'] == 'D_test123'
        assert '/tmp/discussions/test-project' in result['work_dir']

    @pytest.mark.asyncio
    async def test_finalize_execution_no_op(self):
        """Discussions workspace should not finalize (no git ops)"""
        mock_gh = MagicMock()
        context = DiscussionsWorkspaceContext(
            project='test-project',
            issue_number=88,
            task_context={'discussion_id': 'D_test123'},
            github_integration=mock_gh
        )

        result = await context.finalize_execution(
            result={'status': 'success'},
            commit_message='Ignored'
        )

        # Verify it's a no-op
        assert result['success'] == True
        assert 'no finalization' in result['message'].lower()

    @pytest.mark.asyncio
    async def test_post_output_to_discussion(self):
        """Discussions workspace should post to discussion comments"""
        mock_gh = MagicMock()
        mock_gh.repo_owner = 'test-org'
        mock_gh.repo_name = 'test-repo'
        mock_gh.token = 'test-token'

        context = DiscussionsWorkspaceContext(
            project='test-project',
            issue_number=88,
            task_context={'discussion_id': 'D_test123'},
            github_integration=mock_gh
        )

        with patch('services.github_discussions.GitHubDiscussions') as mock_gd_class:
            mock_gd = MagicMock()
            mock_gd.add_comment = AsyncMock()
            mock_gd_class.return_value = mock_gd

            result = await context.post_output(
                agent_name='business_analyst',
                markdown_output='# Requirements'
            )

            # Verify GitHubDiscussions was created
            mock_gd_class.assert_called_once_with('test-org', 'test-repo', 'test-token')

            # Verify comment was posted
            mock_gd.add_comment.assert_called_once_with(
                'D_test123',
                '# Requirements'
            )

            # Verify result
            assert result['success'] == True
            assert 'discussion D_test123' in result['posted_to']

    @pytest.mark.asyncio
    async def test_post_output_no_discussion_id_fails(self):
        """Discussions workspace should fail gracefully without discussion_id"""
        mock_gh = MagicMock()
        context = DiscussionsWorkspaceContext(
            project='test-project',
            issue_number=88,
            task_context={},  # No discussion_id
            github_integration=mock_gh
        )

        result = await context.post_output(
            agent_name='test_agent',
            markdown_output='Test'
        )

        # Should fail gracefully
        assert result['success'] == False
        assert 'error' in result

    @pytest.mark.asyncio
    async def test_get_execution_metadata(self):
        """Discussions workspace should provide correct metadata"""
        mock_gh = MagicMock()
        context = DiscussionsWorkspaceContext(
            project='test-project',
            issue_number=88,
            task_context={'discussion_id': 'D_test123'},
            github_integration=mock_gh
        )

        metadata = await context.get_execution_metadata()

        assert metadata['workspace_type'] == 'discussions'
        assert metadata['discussion_id'] == 'D_test123'
        assert metadata['issue_number'] == 88
        assert metadata['supports_git'] == False


class TestHybridWorkspaceContext:
    """Test Hybrid workspace context"""

    @pytest.mark.asyncio
    async def test_determine_workspace_discussions_for_early_stage(self):
        """Hybrid should use discussions for early-stage work"""
        mock_gh = MagicMock()
        context = HybridWorkspaceContext(
            project='test-project',
            issue_number=99,
            task_context={
                'column': 'Requirements',
                'discussion_id': 'D_test'
            },
            github_integration=mock_gh
        )

        workspace = context._determine_current_workspace()
        assert workspace == 'discussions'

    @pytest.mark.asyncio
    async def test_determine_workspace_issues_for_development(self):
        """Hybrid should use issues for implementation work"""
        mock_gh = MagicMock()
        context = HybridWorkspaceContext(
            project='test-project',
            issue_number=99,
            task_context={
                'column': 'Development',
                'discussion_id': 'D_test'
            },
            github_integration=mock_gh
        )

        workspace = context._determine_current_workspace()
        assert workspace == 'issues'

    @pytest.mark.asyncio
    async def test_prepare_execution_discussions_mode(self):
        """Hybrid in discussions mode should not prepare branch"""
        mock_gh = MagicMock()
        context = HybridWorkspaceContext(
            project='test-project',
            issue_number=99,
            task_context={
                'column': 'Requirements',
                'discussion_id': 'D_test'
            },
            github_integration=mock_gh
        )

        result = await context.prepare_execution()

        assert result['current_workspace'] == 'discussions'
        assert result['discussion_id'] == 'D_test'
        assert 'branch_name' not in result

    @pytest.mark.asyncio
    async def test_prepare_execution_issues_mode(self):
        """Hybrid in issues mode should prepare branch"""
        mock_gh = MagicMock()
        context = HybridWorkspaceContext(
            project='test-project',
            issue_number=99,
            task_context={
                'column': 'Development',
                'issue_title': 'Test'
            },
            github_integration=mock_gh
        )

        with patch('services.feature_branch_manager.feature_branch_manager') as mock_fbm, \
             patch('services.project_workspace.workspace_manager') as mock_wm:

            mock_fbm.prepare_feature_branch = AsyncMock(
                return_value='feature/issue-99-test'
            )
            mock_wm.get_project_dir.return_value = Path('/workspace/test-project')

            result = await context.prepare_execution()

            assert result['current_workspace'] == 'issues'
            assert result['branch_name'] == 'feature/issue-99-test'
            mock_fbm.prepare_feature_branch.assert_called_once()

    @pytest.mark.asyncio
    async def test_supports_git_operations_varies_by_mode(self):
        """Hybrid git support should depend on current workspace"""
        mock_gh = MagicMock()

        # Discussions mode - no git
        context_discussions = HybridWorkspaceContext(
            project='test-project',
            issue_number=99,
            task_context={'column': 'Requirements'},
            github_integration=mock_gh
        )
        context_discussions._current_workspace = 'discussions'
        assert context_discussions.supports_git_operations == False

        # Issues mode - git enabled
        context_issues = HybridWorkspaceContext(
            project='test-project',
            issue_number=99,
            task_context={'column': 'Development'},
            github_integration=mock_gh
        )
        context_issues._current_workspace = 'issues'
        assert context_issues.supports_git_operations == True


class TestWorkspaceBehaviorEquivalence:
    """Verify workspace contexts provide expected behavior"""

    @pytest.mark.asyncio
    async def test_issues_always_does_git_operations(self):
        """Issues workspace MUST always do git operations"""
        mock_gh = MagicMock()
        context = IssuesWorkspaceContext(
            project='test',
            issue_number=1,
            task_context={},
            github_integration=mock_gh
        )

        assert context.supports_git_operations == True
        assert context.workspace_type == 'issues'

    @pytest.mark.asyncio
    async def test_discussions_never_does_git_operations(self):
        """Discussions workspace MUST never do git operations"""
        mock_gh = MagicMock()
        context = DiscussionsWorkspaceContext(
            project='test',
            issue_number=1,
            task_context={},
            github_integration=mock_gh
        )

        assert context.supports_git_operations == False
        assert context.workspace_type == 'discussions'

    @pytest.mark.asyncio
    async def test_all_contexts_have_required_methods(self):
        """All workspace contexts MUST implement required interface"""
        mock_gh = MagicMock()

        for workspace_type in ['issues', 'discussions', 'hybrid']:
            context = WorkspaceContextFactory.create(
                workspace_type=workspace_type,
                project='test',
                issue_number=1,
                task_context={},
                github_integration=mock_gh
            )

            # Verify all required methods exist
            assert hasattr(context, 'prepare_execution')
            assert hasattr(context, 'finalize_execution')
            assert hasattr(context, 'post_output')
            assert hasattr(context, 'get_working_directory')
            assert hasattr(context, 'get_execution_metadata')
            assert hasattr(context, 'supports_git_operations')
            assert hasattr(context, 'workspace_type')

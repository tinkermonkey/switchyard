"""
Integration tests for feature branch workflow

Tests the complete lifecycle:
1. Parent issue detection
2. Feature branch creation
3. Sub-issue contributions
4. PR creation and updates
5. Completion detection
"""

import pytest
import asyncio
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from pathlib import Path
import tempfile
import shutil
import yaml

from services.feature_branch_manager import (
    FeatureBranchManager,
    FeatureBranch,
    SubIssueState,
    MergeConflictError
)


@pytest.fixture
def temp_workspace():
    """Create a temporary workspace directory"""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)


@pytest.fixture
def feature_branch_manager(temp_workspace):
    """Create a FeatureBranchManager instance with temp workspace"""
    return FeatureBranchManager(workspace_root=temp_workspace)


@pytest.fixture
def mock_github_integration():
    """Create a mock GitHub integration"""
    mock_gh = AsyncMock()

    # Mock graphql_query for parent issue detection
    mock_gh.graphql_query = AsyncMock(return_value={
        "data": {
            "repository": {
                "issue": {
                    "trackedInIssues": {
                        "nodes": [{"number": 50}]
                    }
                }
            }
        }
    })

    # Mock get_issue
    mock_gh.get_issue = AsyncMock(return_value={
        "number": 50,
        "title": "User Authentication Feature",
        "state": "open"
    })

    # Mock post_comment
    mock_gh.post_comment = AsyncMock(return_value={"success": True})

    # Mock create_pr
    mock_gh.create_pr = AsyncMock(return_value={
        "success": True,
        "pr_number": 123,
        "pr_url": "https://github.com/org/repo/pull/123"
    })

    # Mock update_pr_body
    mock_gh.update_pr_body = AsyncMock(return_value=True)

    # Mock mark_pr_ready
    mock_gh.mark_pr_ready = AsyncMock(return_value=True)

    # Set repo info
    mock_gh.repo_owner = "test-org"
    mock_gh.repo_name = "test-repo"

    return mock_gh


@pytest.fixture
def mock_git_workflow():
    """Mock git workflow manager operations"""
    with patch('services.git_workflow_manager.git_workflow_manager') as mock_git:
        mock_git.checkout_branch = AsyncMock(return_value=True)
        mock_git.pull_branch = AsyncMock(return_value=True)
        mock_git.create_branch = AsyncMock(return_value=True)
        mock_git.pull_rebase = AsyncMock(return_value=None)
        mock_git.branch_exists = AsyncMock(return_value=False)
        mock_git.add_all = AsyncMock(return_value=True)
        mock_git.commit = AsyncMock(return_value=True)
        mock_git.push_branch = AsyncMock(return_value=True)
        mock_git.get_commits_behind = AsyncMock(return_value=5)
        mock_git.get_conflicting_files = AsyncMock(return_value=[])
        yield mock_git


class TestFeatureBranchState:
    """Test feature branch state management"""

    def test_create_feature_branch_state(self, feature_branch_manager):
        """Test creating feature branch state"""
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52, 53]
        )

        assert fb.parent_issue == 50
        assert fb.branch_name == "feature/issue-50-auth"
        assert len(fb.sub_issues) == 3
        assert all(si.status == "pending" for si in fb.sub_issues)

    def test_get_feature_branch_state(self, feature_branch_manager):
        """Test retrieving feature branch state"""
        # Create state
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        # Retrieve state
        fb = feature_branch_manager.get_feature_branch_state("test-project", 50)

        assert fb is not None
        assert fb.parent_issue == 50
        assert fb.branch_name == "feature/issue-50-auth"

    def test_get_feature_branch_for_sub_issue(self, feature_branch_manager):
        """Test retrieving feature branch by sub-issue number"""
        # Create state
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52, 53]
        )

        # Retrieve by sub-issue
        fb = feature_branch_manager.get_feature_branch_for_issue("test-project", 52)

        assert fb is not None
        assert fb.parent_issue == 50
        assert any(si.number == 52 for si in fb.sub_issues)

    def test_add_sub_issue_to_branch(self, feature_branch_manager):
        """Test adding a sub-issue to an existing feature branch"""
        # Create initial state
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        # Add new sub-issue
        feature_branch_manager.add_sub_issue_to_branch("test-project", fb, 53)

        # Verify
        updated_fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        assert len(updated_fb.sub_issues) == 3
        assert any(si.number == 53 for si in updated_fb.sub_issues)

    def test_mark_sub_issue_complete(self, feature_branch_manager):
        """Test marking sub-issue as completed"""
        # Create state
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        # Mark as complete
        feature_branch_manager.mark_sub_issue_complete("test-project", fb, 51)

        # Verify
        updated_fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        completed_issue = next(si for si in updated_fb.sub_issues if si.number == 51)
        assert completed_issue.status == "completed"
        assert completed_issue.completed_at is not None

    def test_check_all_sub_issues_complete(self, feature_branch_manager):
        """Test checking if all sub-issues are complete"""
        fb = FeatureBranch(
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            created_at="2025-01-01T00:00:00Z",
            sub_issues=[
                SubIssueState(number=51, status="completed"),
                SubIssueState(number=52, status="completed"),
                SubIssueState(number=53, status="pending")
            ]
        )

        assert not feature_branch_manager.check_all_sub_issues_complete(fb)

        # Complete the last one
        fb.sub_issues[2].status = "completed"
        assert feature_branch_manager.check_all_sub_issues_complete(fb)


class TestFeatureBranchLifecycle:
    """Test complete feature branch lifecycle"""

    @pytest.mark.asyncio
    async def test_prepare_feature_branch_first_sub_issue(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test preparing feature branch for first sub-issue"""
        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Prepare branch
        branch_name = await feature_branch_manager.prepare_feature_branch(
            project="test-project",
            issue_number=51,
            github_integration=mock_github_integration,
            issue_title="Login form UI"
        )

        # Verify branch created
        assert branch_name.startswith("feature/issue-50-")
        assert "user-authentication" in branch_name.lower()

        # Verify state saved
        fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        assert fb is not None
        assert fb.branch_name == branch_name
        assert any(si.number == 51 for si in fb.sub_issues)

        # Verify git operations called
        mock_git_workflow.create_branch.assert_called()
        mock_git_workflow.checkout_branch.assert_called()
        mock_git_workflow.pull_rebase.assert_called()

    @pytest.mark.asyncio
    async def test_prepare_feature_branch_subsequent_sub_issue(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test preparing feature branch for subsequent sub-issue"""
        # Create existing feature branch state
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-user-authentication",
            sub_issues=[51]
        )

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Prepare branch for second sub-issue
        branch_name = await feature_branch_manager.prepare_feature_branch(
            project="test-project",
            issue_number=52,
            github_integration=mock_github_integration,
            issue_title="Password validation"
        )

        # Verify same branch used
        assert branch_name == "feature/issue-50-user-authentication"

        # Verify sub-issue added
        fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        assert len(fb.sub_issues) == 2
        assert any(si.number == 52 for si in fb.sub_issues)

        # Verify git pull happened
        mock_git_workflow.pull_rebase.assert_called()

    @pytest.mark.asyncio
    async def test_prepare_feature_branch_standalone_issue(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test preparing branch for standalone issue (no parent)"""
        # Mock no parent issue
        mock_github_integration.graphql_query = AsyncMock(return_value={
            "data": {
                "repository": {
                    "issue": {
                        "trackedInIssues": {
                            "nodes": []
                        }
                    }
                }
            }
        })

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Prepare branch
        branch_name = await feature_branch_manager.prepare_feature_branch(
            project="test-project",
            issue_number=100,
            github_integration=mock_github_integration,
            issue_title="Standalone feature"
        )

        # Verify standalone branch created
        assert branch_name.startswith("feature/issue-100-")

        # Verify no parent state created
        fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        assert fb is None

    @pytest.mark.asyncio
    async def test_finalize_feature_branch_work(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test finalizing feature branch work after agent completion"""
        # Create feature branch state
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Finalize work for first sub-issue
        result = await feature_branch_manager.finalize_feature_branch_work(
            project="test-project",
            issue_number=51,
            commit_message="Complete login form UI",
            github_integration=mock_github_integration
        )

        # Verify success
        assert result['success'] is True
        assert 'pr_url' in result
        assert result['all_complete'] is False

        # Verify git operations
        mock_git_workflow.add_all.assert_called()
        mock_git_workflow.commit.assert_called()
        mock_git_workflow.push_branch.assert_called()

        # Verify PR created
        mock_github_integration.create_pr.assert_called()

        # Verify sub-issue marked complete
        fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        completed = next(si for si in fb.sub_issues if si.number == 51)
        assert completed.status == "completed"

    @pytest.mark.asyncio
    async def test_finalize_all_sub_issues_complete(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test finalizing when all sub-issues are complete"""
        # Create feature branch with one pending sub-issue
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        # Mark first as complete
        feature_branch_manager.mark_sub_issue_complete("test-project", fb, 51)

        # Set PR number
        fb.pr_number = 123
        feature_branch_manager.save_feature_branch_state("test-project", fb)

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Finalize last sub-issue
        result = await feature_branch_manager.finalize_feature_branch_work(
            project="test-project",
            issue_number=52,
            commit_message="Complete password validation",
            github_integration=mock_github_integration
        )

        # Verify all complete
        assert result['all_complete'] is True

        # Verify PR marked ready
        mock_github_integration.mark_pr_ready.assert_called_with(123)

        # Verify completion comment posted
        mock_github_integration.post_comment.assert_called()


class TestConflictHandling:
    """Test merge conflict detection and escalation"""

    @pytest.mark.asyncio
    async def test_merge_conflict_detection(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test that merge conflicts are detected and escalated"""
        # Create existing feature branch
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51]
        )

        # Mock conflict on pull
        mock_git_workflow.pull_rebase.side_effect = Exception("Merge conflict detected")
        mock_git_workflow.get_conflicting_files.return_value = ["auth.py", "config.js"]

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Attempt to prepare branch - should raise MergeConflictError
        with pytest.raises(MergeConflictError) as exc_info:
            await feature_branch_manager.prepare_feature_branch(
                project="test-project",
                issue_number=52,
                github_integration=mock_github_integration,
                issue_title="Password validation"
            )

        # Verify escalation comment posted
        mock_github_integration.post_comment.assert_called()
        call_args = mock_github_integration.post_comment.call_args
        assert "Merge Conflict Detected" in call_args[0][1]


class TestStaleBranchDetection:
    """Test stale branch detection and warnings"""

    @pytest.mark.asyncio
    async def test_stale_branch_warning(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test that stale branches trigger warnings"""
        # Create feature branch
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51]
        )

        # Mock 25 commits behind (warning threshold)
        mock_git_workflow.get_commits_behind.return_value = 25

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Prepare branch
        await feature_branch_manager.prepare_feature_branch(
            project="test-project",
            issue_number=52,
            github_integration=mock_github_integration,
            issue_title="Password validation"
        )

        # Verify state updated with commits_behind
        fb = feature_branch_manager.get_feature_branch_state("test-project", 50)
        assert fb.commits_behind_main == 25

    @pytest.mark.asyncio
    async def test_very_stale_branch_escalation(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test that very stale branches trigger escalation"""
        # Create feature branch
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51]
        )

        # Mock 60 commits behind (escalation threshold)
        mock_git_workflow.get_commits_behind.return_value = 60

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # Prepare branch
        await feature_branch_manager.prepare_feature_branch(
            project="test-project",
            issue_number=52,
            github_integration=mock_github_integration,
            issue_title="Password validation"
        )

        # Verify escalation comment posted
        mock_github_integration.post_comment.assert_called()
        call_args = mock_github_integration.post_comment.call_args
        assert "Branch Maintenance Required" in call_args[0][1]


class TestPRManagement:
    """Test PR creation and management"""

    @pytest.mark.asyncio
    async def test_create_pr_with_checklist(
        self,
        feature_branch_manager,
        mock_github_integration
    ):
        """Test PR creation with sub-issue checklist"""
        # Create feature branch state
        fb = FeatureBranch(
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            created_at="2025-01-01T00:00:00Z",
            sub_issues=[
                SubIssueState(number=51, status="completed"),
                SubIssueState(number=52, status="in_progress"),
                SubIssueState(number=53, status="pending")
            ]
        )

        # Mock issue details for sub-issues
        async def mock_get_issue(issue_number):
            titles = {
                50: "User Authentication Feature",
                51: "Login form UI",
                52: "Password validation",
                53: "Session management"
            }
            return {"number": issue_number, "title": titles.get(issue_number, "")}

        mock_github_integration.get_issue = mock_get_issue

        # Create PR
        result = await feature_branch_manager.create_or_update_feature_pr(
            project="test-project",
            feature_branch=fb,
            github_integration=mock_github_integration
        )

        # Verify PR created
        assert result['success'] is True
        assert result['pr_number'] == 123

        # Verify PR body contains checklist
        call_args = mock_github_integration.create_pr.call_args
        pr_body = call_args[1]['body']

        assert "User Authentication Feature" in pr_body
        assert "[x] #51" in pr_body  # Completed
        assert "[ ] #52" in pr_body  # In progress
        assert "[ ] #53" in pr_body  # Pending


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

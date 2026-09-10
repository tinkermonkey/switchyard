"""
Integration tests for feature branch workflow

Covers FeatureBranch state tracking (creation, sub-issue attach/complete),
finalize_feature_branch_work()'s commit/push/PR/completion-detection flow, and
PR checklist body generation.

Does NOT cover branch resolution/checkout (prepare_feature_branch(),
find_related_branches(), parent-issue-triggered branch creation) -- that
end-to-end flow was removed as dead code, zero production callers, #124/WI-E
of #119 (resolve_workspace()/get_or_create_epic_worktree() replaced it; see
tests/unit/services/test_pipeline_run_workspace_resolver.py for that path's
coverage).
Parent-issue detection alone is still covered independently in
tests/unit/services/test_feature_branch_parent_detection.py.
"""

import pytest
import asyncio
from contextlib import contextmanager
from unittest.mock import Mock, AsyncMock, patch, MagicMock
from pathlib import Path
import tempfile
import shutil
import yaml

from services.feature_branch_manager import (
    FeatureBranchManager,
    FeatureBranch,
    SubIssueState,
)

PARENT_ISSUE = 50
FEATURE_BRANCH = "feature/issue-50-auth"


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

    # Mock get_issue - returns different issues based on number
    async def mock_get_issue(issue_number):
        issues = {
            50: {
                "number": 50,
                "title": "User Authentication Feature",
                "state": "open",
                "body": "Parent issue for authentication"
            },
            51: {
                "number": 51,
                "title": "Login form UI",
                "state": "open",
                "body": "Part of #50"  # References parent
            },
            52: {
                "number": 52,
                "title": "Password validation",
                "state": "open",
                "body": "Part of #50"  # References parent
            },
            53: {
                "number": 53,
                "title": "Session management",
                "state": "open",
                "body": "Part of #50"  # References parent
            },
            100: {
                "number": 100,
                "title": "Standalone feature",
                "state": "open",
                "body": "No parent"
            }
        }
        return issues.get(issue_number, {
            "number": issue_number,
            "title": f"Issue {issue_number}",
            "state": "open",
            "body": ""
        })

    mock_gh.get_issue = mock_get_issue

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

    # Set repo info. github_org as well as repo_owner/repo_name (#132): the
    # production code reads github_org for the GraphQL `owner` variable and
    # repo_owner for the parent-lookup cache key, and leaving one of them an
    # un-configured AsyncMock attribute sent a non-serializable mock into
    # json.dumps() -- which surfaced as ParentIssueLookupError several frames
    # away from the fixture that caused it. (The two attributes naming the same
    # thing is #131's subject, not this file's.)
    mock_gh.github_org = "test-org"
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
        # True, not False (#132): the only remaining reader is
        # finalize_feature_branch_work()'s pre-push existence check, and these
        # tests are all "the agent worked on this branch and is now pushing
        # it". False belonged to the branch-creation flow removed as dead code
        # in #124/WI-E, and left every finalize test refusing to push.
        mock_git.branch_exists = AsyncMock(return_value=True)
        mock_git.add_all = AsyncMock(return_value=True)
        mock_git.commit = AsyncMock(return_value=True)
        mock_git.push_branch = AsyncMock(return_value=True)
        mock_git.get_commits_behind = AsyncMock(return_value=5)
        mock_git.get_conflicting_files = AsyncMock(return_value=[])
        # get_current_branch was missing (#132): FeatureBranchManager.
        # get_current_branch() delegates straight here, and an unconfigured
        # attribute on this MagicMock returns a non-awaitable, so
        # finalize_feature_branch_work()'s "git is the source of truth"
        # reconciliation step raised instead of running.
        mock_git.get_current_branch = AsyncMock(return_value=FEATURE_BRANCH)
        yield mock_git


@contextmanager
def _finalization_without_github(sub_issues):
    """The two GitHub GraphQL round trips finalize_feature_branch_work() makes,
    replaced by fixed answers.

    Both go through services.github_api_client's real client rather than the
    injected github_integration mock, so they are not reachable from the
    fixture (#132) -- and this file deliberately does not cover parent
    detection (see the module docstring). `sub_issues` is what GitHub would
    report for the parent's children, in the GraphQL shape
    _verify_all_sub_issues_complete() reads: state 'CLOSED' means done.
    """
    with patch.object(
        FeatureBranchManager, 'get_parent_issue', AsyncMock(return_value=PARENT_ISSUE)
    ), patch.object(
        FeatureBranchManager, '_get_sub_issues_from_parent',
        AsyncMock(return_value=sub_issues),
    ):
        yield


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

    @pytest.mark.asyncio
    async def test_get_feature_branch_for_sub_issue(
        self, feature_branch_manager, mock_github_integration
    ):
        """A sub-issue resolves to its parent's branch.

        #132: this called get_feature_branch_for_issue("test-project", 52)
        with two positional arguments against a method that has been async and
        taken a third (github_integration) for a long time, so it had been
        raising TypeError rather than testing anything.

        get_parent_issue is patched rather than left to hit GitHub: parent
        detection is covered on its own in
        tests/unit/services/test_feature_branch_parent_detection.py, and this
        module's docstring already scopes it out. What is under test here is
        the resolution step that runs AFTER the parent is known.
        """
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52, 53]
        )

        with patch.object(
            FeatureBranchManager, 'get_parent_issue', AsyncMock(return_value=50)
        ):
            fb = await feature_branch_manager.get_feature_branch_for_issue(
                "test-project", 52, mock_github_integration
            )

        assert fb is not None
        assert fb.parent_issue == 50
        assert fb.branch_name == "feature/issue-50-auth"
        # NOT `any(si.number == 52 ...)`: a FeatureBranch resolved from git
        # carries no sub-issue list at all -- see
        # test_sub_issue_status_is_in_memory_only_and_does_not_survive_a_re_read.
        assert fb.sub_issues == []

    def test_add_sub_issue_to_branch(self, feature_branch_manager):
        """Test adding a sub-issue to an existing feature branch"""
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        feature_branch_manager.add_sub_issue_to_branch("test-project", fb, 53)

        # The object the caller passed in, not a re-read: add_sub_issue_to_branch
        # is documented in-memory-only (#132).
        assert len(fb.sub_issues) == 3
        assert any(si.number == 53 for si in fb.sub_issues)

    def test_add_sub_issue_to_branch_is_idempotent(self, feature_branch_manager):
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        feature_branch_manager.add_sub_issue_to_branch("test-project", fb, 52)

        assert [si.number for si in fb.sub_issues] == [51, 52]

    def test_mark_sub_issue_complete(self, feature_branch_manager):
        """Test marking sub-issue as completed"""
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )

        feature_branch_manager.mark_sub_issue_complete("test-project", fb, 51)

        completed_issue = next(si for si in fb.sub_issues if si.number == 51)
        assert completed_issue.status == "completed"
        assert completed_issue.completed_at is not None

    def test_sub_issue_status_is_in_memory_only_and_does_not_survive_a_re_read(
        self, feature_branch_manager
    ):
        """Pins the contract the two tests above used to contradict (#132).

        save_feature_branch_state() is a documented NO-OP and
        get_feature_branch_state() reconstructs a FeatureBranch from git with
        sub_issues=[] -- git is the source of truth, and per-sub-issue status
        is deliberately not persisted anywhere. The old versions of those tests
        re-read after mutating and asserted the mutation had survived, which
        the issue read as a serialization bug; it is the documented design, and
        restoring persistence to satisfy them would be adding durable on-disk
        state to make a stale test pass.

        finalize_feature_branch_work() does not depend on the dropped status:
        it re-derives completion from GitHub via _get_sub_issues_from_parent()/
        _verify_all_sub_issues_complete(), which is what
        check_all_sub_issues_complete()'s own DEPRECATED note points at.
        """
        fb = feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=50,
            branch_name="feature/issue-50-auth",
            sub_issues=[51, 52]
        )
        feature_branch_manager.mark_sub_issue_complete("test-project", fb, 51)
        feature_branch_manager.save_feature_branch_state("test-project", fb)

        re_read = feature_branch_manager.get_feature_branch_state("test-project", 50)

        assert re_read is not None
        assert re_read.branch_name == "feature/issue-50-auth"
        assert re_read.sub_issues == []

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
            parent_issue=PARENT_ISSUE,
            branch_name=FEATURE_BRANCH,
            sub_issues=[51, 52]
        )

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # #51 is being finalized; #52 is still open, so the feature as a whole
        # is not done.
        with _finalization_without_github([
            {'number': 51, 'state': 'OPEN'},
            {'number': 52, 'state': 'OPEN'},
        ]):
            result = await feature_branch_manager.finalize_feature_branch_work(
                project="test-project",
                issue_number=51,
                commit_message="Complete login form UI",
                github_integration=mock_github_integration
            )

        # Verify success
        assert result['success'] is True
        assert result['pr_url'] == "https://github.com/org/repo/pull/123"
        assert result['branch_name'] == FEATURE_BRANCH
        assert result['all_complete'] is False

        # Verify git operations
        mock_git_workflow.add_all.assert_called()
        mock_git_workflow.commit.assert_called()
        mock_git_workflow.push_branch.assert_called()

        # Verify a draft PR was opened against the feature branch
        mock_github_integration.create_pr.assert_called()
        assert mock_github_integration.create_pr.call_args[1]['branch'] == FEATURE_BRANCH
        assert mock_github_integration.create_pr.call_args[1]['draft'] is True

        # The feature is not complete, so the PR stays a draft.
        mock_github_integration.mark_pr_ready.assert_not_called()

    @pytest.mark.asyncio
    async def test_finalize_all_sub_issues_complete(
        self,
        feature_branch_manager,
        mock_github_integration,
        mock_git_workflow,
        temp_workspace
    ):
        """Test finalizing when all sub-issues are complete.

        #132: this used to set fb.pr_number = 123 and call
        save_feature_branch_state() to "carry it in", which is a no-op -- the
        FeatureBranch finalization works from is reconstructed from git on
        every call, with pr_number None. The 123 the assertions below check
        for is the one create_pr() returns, which is where it genuinely comes
        from at runtime.
        """
        feature_branch_manager.create_feature_branch_state(
            project="test-project",
            parent_issue=PARENT_ISSUE,
            branch_name=FEATURE_BRANCH,
            sub_issues=[51, 52]
        )

        # Create project directory
        project_dir = Path(temp_workspace) / "test-project"
        project_dir.mkdir(parents=True)

        # #51 already closed, #52 closes with this finalization -- the last one.
        with _finalization_without_github([
            {'number': 51, 'state': 'CLOSED'},
            {'number': 52, 'state': 'CLOSED'},
        ]):
            result = await feature_branch_manager.finalize_feature_branch_work(
                project="test-project",
                issue_number=52,
                commit_message="Complete password validation",
                github_integration=mock_github_integration
            )

        # Verify all complete
        assert result['success'] is True
        assert result['all_complete'] is True

        # Verify PR marked ready
        mock_github_integration.mark_pr_ready.assert_called_with(123)

        # Verify completion comment posted, on the PARENT issue
        mock_github_integration.post_comment.assert_called()
        assert mock_github_integration.post_comment.call_args[0][0] == PARENT_ISSUE


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

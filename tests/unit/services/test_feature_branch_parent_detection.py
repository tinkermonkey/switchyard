"""
Unit tests for parent issue detection via GraphQL.

Tests cover the fix for:
- GraphQL response parsing was incorrectly accessing result.get('data', {})
- github_client.graphql() already extracts 'data' field before returning
- Parent detection should access result.get('repository', {}) directly
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from services.feature_branch_manager import FeatureBranchManager


class TestParentIssueDetection:
    """Test parent issue detection via GitHub's GraphQL API"""

    @pytest.fixture
    def manager(self):
        """Create a feature branch manager for testing"""
        return FeatureBranchManager()

    @pytest.fixture
    def mock_github_integration(self):
        """Create a mock GitHub integration with valid org/repo"""
        mock = Mock()
        mock.github_org = "test-org"
        mock.repo_name = "test-repo"
        return mock

    @pytest.mark.asyncio
    async def test_parent_detection_with_parent_present(self, manager, mock_github_integration):
        """
        Test that get_parent_issue() correctly extracts parent when it exists.

        This tests the FIX: result.get('repository', {}) instead of result.get('data', {})
        The GraphQL client already extracts 'data' before returning.
        """
        # Mock GraphQL response that matches what github_client.graphql() returns
        # Note: The 'data' field is already extracted by github_client.graphql()
        mock_response = {
            'repository': {
                'issue': {
                    'number': 214,
                    'parent': {
                        'number': 188,
                        'title': 'Update changesets to be staged explicitly'
                    }
                }
            }
        }

        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, mock_response)
            mock_get_client.return_value = mock_client

            parent_number = await manager.get_parent_issue(
                mock_github_integration,
                issue_number=214,
                project="documentation_robotics"
            )

            # Should correctly extract parent #188
            assert parent_number == 188, \
                "Should extract parent issue number from GraphQL response"

            # Verify GraphQL was called with correct query
            assert mock_client.graphql.called
            call_args = mock_client.graphql.call_args
            query = call_args[0][0]
            variables = call_args[0][1]

            # Verify query structure
            assert 'parent {' in query, "Query should include parent field"
            assert 'Issue {' in query, "Query should specify Issue type"

            # Verify variables
            assert variables['owner'] == 'test-org'
            assert variables['repo'] == 'test-repo'
            assert variables['issueNumber'] == 214

    @pytest.mark.asyncio
    async def test_parent_detection_without_parent(self, manager, mock_github_integration):
        """
        Test that get_parent_issue() returns None when no parent exists.
        """
        # Mock GraphQL response with null parent
        mock_response = {
            'repository': {
                'issue': {
                    'number': 188,
                    'parent': None  # No parent
                }
            }
        }

        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, mock_response)
            mock_get_client.return_value = mock_client

            parent_number = await manager.get_parent_issue(
                mock_github_integration,
                issue_number=188,
                project="documentation_robotics"
            )

            # Should return None for no parent
            assert parent_number is None, \
                "Should return None when parent field is null"

    @pytest.mark.asyncio
    async def test_parent_detection_graphql_failure(self, manager, mock_github_integration):
        """
        Test that get_parent_issue() handles GraphQL failures gracefully.
        """
        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (False, {'error': 'rate_limited'})
            mock_get_client.return_value = mock_client

            parent_number = await manager.get_parent_issue(
                mock_github_integration,
                issue_number=214,
                project="documentation_robotics"
            )

            # Should return None on GraphQL failure
            assert parent_number is None, \
                "Should return None when GraphQL query fails"

    @pytest.mark.asyncio
    async def test_parent_detection_missing_org_repo(self, manager):
        """
        Test that get_parent_issue() validates org/repo before making API calls.
        """
        # Mock GitHub integration with missing org/repo
        mock_integration = Mock()
        mock_integration.github_org = None
        mock_integration.repo_name = None

        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            parent_number = await manager.get_parent_issue(
                mock_integration,
                issue_number=214,
                project="documentation_robotics"
            )

            # Should return None without making GraphQL call
            assert parent_number is None, \
                "Should return None when org/repo not configured"

            # Verify no GraphQL call was made
            assert not mock_client.graphql.called, \
                "Should not call GraphQL when org/repo missing"

    @pytest.mark.asyncio
    async def test_old_buggy_parsing_would_fail(self, manager, mock_github_integration):
        """
        Demonstrate that the OLD buggy parsing (result.get('data', {})) would fail.

        This test documents the bug that was fixed in commit 2d5c9f9.
        """
        # Mock response that matches what github_client.graphql() returns
        mock_response = {
            'repository': {
                'issue': {
                    'number': 214,
                    'parent': {
                        'number': 188,
                        'title': 'Update changesets'
                    }
                }
            }
        }

        # OLD BUGGY CODE: result.get('data', {}).get('repository', {})
        # This would return {} because 'data' doesn't exist (already extracted)
        buggy_issue_data = mock_response.get('data', {}).get('repository', {}).get('issue', {})
        buggy_parent_data = buggy_issue_data.get('parent')

        # OLD CODE would get None
        assert buggy_parent_data is None, \
            "OLD buggy code would fail to extract parent"

        # FIXED CODE: result.get('repository', {}).get('issue', {})
        fixed_issue_data = mock_response.get('repository', {}).get('issue', {})
        fixed_parent_data = fixed_issue_data.get('parent')

        # FIXED CODE correctly extracts parent
        assert fixed_parent_data is not None, \
            "FIXED code correctly extracts parent"
        assert fixed_parent_data['number'] == 188, \
            "FIXED code gets correct parent number"


# Note: _get_sub_issues_from_parent() uses a different signature and data flow
# The critical bug fix was in get_parent_issue() which is fully tested above


class TestResolveEpicWorktreeTarget:
    """Issue #46: resolve_epic_id()/resolve_epic_branch_name() -- the two
    helpers the 3 Docker-mount-source call sites (claude_integration.py,
    agent_executor.py, project_monitor.py's repair cycle) use to scope a
    per-epic git worktree instead of the shared base clone."""

    @pytest.fixture
    def manager(self):
        return FeatureBranchManager()

    @pytest.mark.asyncio
    async def test_resolve_epic_id_uses_the_parent_when_one_exists(self, manager):
        """sdlc_execution dispatch: a sub-issue resolves to its PARENT
        epic's number, not its own."""
        with patch.object(manager, 'get_parent_issue', new_callable=AsyncMock) as mock_get_parent:
            mock_get_parent.return_value = 42

            epic_id = await manager.resolve_epic_id(Mock(), 101, project='test-project')

            assert epic_id == '42'
            mock_get_parent.assert_awaited_once_with(mock_get_parent.call_args.args[0], 101, project='test-project')

    @pytest.mark.asyncio
    async def test_resolve_epic_id_falls_back_to_self_with_no_parent(self, manager):
        """planning_design dispatch (the board item IS the epic) and
        standalone issues both resolve to their own number."""
        with patch.object(manager, 'get_parent_issue', new_callable=AsyncMock) as mock_get_parent:
            mock_get_parent.return_value = None

            epic_id = await manager.resolve_epic_id(Mock(), 200, project='test-project')

            assert epic_id == '200'

    def test_resolve_epic_branch_name_returns_the_existing_branch(self, manager):
        """A read-only lookup: when the epic already has a tracked branch,
        that exact name must be reused, not regenerated."""
        existing = Mock()
        existing.branch_name = 'feature/issue-42-existing-epic'
        with patch.object(manager, 'get_feature_branch_state', return_value=existing) as mock_state:
            branch_name = manager.resolve_epic_branch_name('test-project', '42')

            assert branch_name == 'feature/issue-42-existing-epic'
            mock_state.assert_called_once_with('test-project', 42)

    def test_resolve_epic_branch_name_returns_none_when_nothing_exists_yet(self, manager):
        """No git side effects, no invented name -- callers creating the
        epic's worktree for the first time fall back to
        create_feature_branch_name() themselves."""
        with patch.object(manager, 'get_feature_branch_state', return_value=None):
            branch_name = manager.resolve_epic_branch_name('test-project', '42')

            assert branch_name is None


class TestFindBranchForParentNumericPrefixRegression:
    """
    Regression coverage for a real past production incident (originally covered
    by the now-deleted tests/unit/services/test_feature_branch_related_branch_matching.py,
    which tested the since-removed FeatureBranchManager.find_related_branches()
    directly -- code review finding, issue #124): issue #2 on phone-home had its
    work silently attached to `feature/issue-216-token-efficiency-program-trac`,
    a fully-merged, unrelated branch, because an OLDER matching implementation
    used a plain substring check (`f"issue-{n}" in branch`), and "issue-2" is a
    substring of "issue-216".

    _find_branch_for_parent()/_parse_issue_from_branch_name() (below) are the
    fixed primitives that replaced that substring check -- and, since #122/#124,
    are the SOLE production path for resolving an epic's branch
    (get_feature_branch_state() -> resolve_epic_branch_name(), no independent
    caller remains). Unlike the tests above (which mock get_feature_branch_state
    itself), these exercise the real matching logic against an adversarial
    branch listing, unmocked past the git-subprocess boundary -- the actual
    regression case, not just resolve_epic_branch_name()'s pass-through.
    """

    @pytest.fixture
    def manager(self):
        return FeatureBranchManager()

    def test_does_not_match_a_branch_whose_number_starts_with_the_same_digits(self, manager):
        """The exact regression case: parent #2 must not match
        feature/issue-216-... (or issue-20-, issue-200-, etc.)."""
        with patch.object(
            manager, '_get_all_feature_branches_sync',
            return_value=[
                'feature/issue-216-token-efficiency-program-trac',
                'feature/issue-20-something-else',
                'feature/issue-200-yet-another-thing',
            ],
        ):
            result = manager._find_branch_for_parent('/workspace/phone-home', 2)

        assert result is None

    def test_still_finds_the_correctly_named_branch(self, manager):
        """Control case: the fix must not break the actual intended match --
        a genuine feature/issue-2-... branch must still be found, even
        alongside the numeric-prefix decoys."""
        with patch.object(
            manager, '_get_all_feature_branches_sync',
            return_value=[
                'feature/issue-2-deterministic-health-check',
                'feature/issue-216-token-efficiency-program-trac',
                'feature/issue-20-something-else',
            ],
        ):
            result = manager._find_branch_for_parent('/workspace/phone-home', 2)

        assert result == 'feature/issue-2-deterministic-health-check'

    @pytest.mark.parametrize("branch,expected", [
        ('feature/issue-216-token-efficiency-program-trac', 216),
        ('feature/issue-2-deterministic-health-check', 2),
        ('feature/issue-123', 123),
        ('main', None),
        ('feature/some-other-branch', None),
    ])
    def test_parse_issue_from_branch_name_exact_match_only(self, manager, branch, expected):
        """The underlying primitive: extracts the FULL number, never a prefix
        substring match."""
        assert manager._parse_issue_from_branch_name(branch) == expected

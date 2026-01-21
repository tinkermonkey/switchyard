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

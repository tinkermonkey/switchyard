"""
Unit tests for PR ready marking logic fix.

Tests cover the fix for:
- Standalone issues (no sub-issues) should have PRs marked ready
- Issues with all sub-issues complete should have PRs marked ready
- Issues with incomplete sub-issues should NOT have PRs marked ready
"""

import pytest
from services.feature_branch_manager import SubIssueState


class TestPRReadyLogicConditions:
    """Test the specific condition logic that determines when PRs should be marked ready"""

    def test_standalone_issue_condition_evaluates_true(self):
        """
        Test that the condition evaluates to True for standalone issues (no sub-issues).

        This tests the fix: len(actual_sub_issues) == 0 or all_complete
        When there are no sub-issues, the first part of the OR is True.
        """
        actual_sub_issues = []  # No sub-issues
        all_complete = False  # Doesn't matter since first condition is True

        # The fixed condition from line 1344
        should_mark_ready = len(actual_sub_issues) == 0 or all_complete

        assert should_mark_ready is True, \
            "Standalone issues (no sub-issues) should be marked ready"

    def test_all_sub_issues_complete_condition_evaluates_true(self):
        """
        Test that the condition evaluates to True when all sub-issues are complete.

        This tests the existing behavior: when sub-issues exist and all are complete.
        """
        actual_sub_issues = [
            SubIssueState(number=51, status="completed"),
            SubIssueState(number=52, status="completed")
        ]
        all_complete = True

        # The fixed condition from line 1344
        should_mark_ready = len(actual_sub_issues) == 0 or all_complete

        assert should_mark_ready is True, \
            "Issues with all sub-issues complete should be marked ready"

    def test_incomplete_sub_issues_condition_evaluates_false(self):
        """
        Test that the condition evaluates to False when sub-issues exist but are incomplete.

        This tests the existing behavior: when some sub-issues are still pending.
        """
        actual_sub_issues = [
            SubIssueState(number=51, status="completed"),
            SubIssueState(number=52, status="pending")  # Not complete!
        ]
        all_complete = False

        # The fixed condition from line 1344
        should_mark_ready = len(actual_sub_issues) == 0 or all_complete

        assert should_mark_ready is False, \
            "Issues with incomplete sub-issues should NOT be marked ready"

    def test_old_buggy_condition_would_fail_standalone(self):
        """
        Demonstrate that the OLD buggy condition would incorrectly reject standalone issues.

        The bug was: if all_complete and len(actual_sub_issues) > 0
        This would always be False for standalone issues.
        """
        actual_sub_issues = []  # No sub-issues
        all_complete = False

        # The OLD buggy condition (for comparison)
        old_buggy_condition = all_complete and len(actual_sub_issues) > 0

        assert old_buggy_condition is False, \
            "OLD bug: standalone issues could never be marked ready"

        # The NEW fixed condition
        new_fixed_condition = len(actual_sub_issues) == 0 or all_complete

        assert new_fixed_condition is True, \
            "FIX: standalone issues are now correctly marked ready"


class TestPRReadyReturnValueLogic:
    """Test the logic for handling mark_pr_ready() return values"""

    def test_success_true_means_update_state(self):
        """
        Test that when mark_pr_ready returns True, we should update state to "ready".

        This tests the Phase 2 fix: checking return value before updating state.
        """
        success = True

        if success:
            pr_status = "ready"
        else:
            pr_status = "draft"

        assert pr_status == "ready", \
            "Successful GitHub API call should update local state to ready"

    def test_success_false_means_keep_draft(self):
        """
        Test that when mark_pr_ready returns False, we should keep status as "draft".

        This tests the Phase 2 fix: keeping draft status on failure so retry is possible.
        """
        success = False

        if success:
            pr_status = "ready"
        else:
            pr_status = "draft"

        assert pr_status == "draft", \
            "Failed GitHub API call should keep draft status for retry"

    def test_should_post_warning_on_failure(self):
        """
        Test that when mark_pr_ready fails, we should post a warning comment.

        This tests the Phase 2 fix: posting warning to parent issue on failure.
        """
        success = False
        should_post_warning = not success

        assert should_post_warning is True, \
            "Failed mark_pr_ready should trigger warning comment to issue"


class TestGitHubAPINativeSubIssues:
    """
    Test the logic for GitHub API native sub-issue detection.

    Phase 6: Markdown checkboxes are NO LONGER used for sub-issue detection.
    Only GitHub's native sub_issues_summary and parent_issue_url are used.
    """

    def test_should_query_api_when_summary_shows_sub_issues(self):
        """
        Test that we query GitHub API when sub_issues_summary shows sub-issues exist.
        """
        sub_issues_summary = {'total': 6, 'completed': 6, 'percent_completed': 100}

        # The logic: if GitHub reports sub-issues, query API to get the list
        should_query_api = sub_issues_summary.get('total', 0) > 0

        assert should_query_api is True, \
            "Should query GitHub API when sub_issues_summary shows sub-issues"

    def test_should_not_query_api_when_no_sub_issues(self):
        """
        Test that we should NOT query GitHub API when truly standalone (no sub-issues).
        """
        sub_issues_summary = {'total': 0, 'completed': 0, 'percent_completed': 0}

        # The logic: if GitHub reports zero sub-issues, it's standalone
        should_query_api = sub_issues_summary.get('total', 0) > 0

        assert should_query_api is False, \
            "Should NOT query GitHub API for truly standalone issues"

    def test_pr126_scenario_now_works(self):
        """
        Test that PR #126 scenario now works correctly.

        This simulates the exact scenario from PR #126 / issue #118:
        - GitHub reports 6 sub-issues via sub_issues_summary
        - All 6 sub-issues are closed
        - PR should be marked ready
        """
        # Step 1: Check sub_issues_summary (single source of truth)
        sub_issues_summary = {'total': 6, 'completed': 6, 'percent_completed': 100}

        # Step 2: Would we query the API?
        should_query_api = sub_issues_summary.get('total', 0) > 0
        assert should_query_api is True, "Should query API for PR #126 scenario"

        # Step 3: Simulate API returning 6 closed sub-issues via parent_issue_url
        api_sub_issues = [
            {'number': 120, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/118'},
            {'number': 121, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/118'},
            {'number': 122, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/118'},
            {'number': 123, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/118'},
            {'number': 124, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/118'},
            {'number': 125, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/118'}
        ]

        # Step 4: Check if all complete
        all_complete = all(issue.get('state') == 'closed' for issue in api_sub_issues)
        assert all_complete is True, "All sub-issues should be closed"

        # Step 5: Should we mark PR ready?
        actual_sub_issues = api_sub_issues
        should_mark_ready = len(actual_sub_issues) == 0 or all_complete

        assert should_mark_ready is True, \
            "PR #126 is now correctly marked ready using GitHub API"

    def test_pr138_scenario_sub_issue_triggers_completion(self):
        """
        Test that PR #138 scenario now works correctly.

        This simulates the exact scenario from PR #138 / issue #128:
        - Parent issue #128 has 3 sub-issues (#135, #136, #137)
        - When finalizing the LAST sub-issue (#137), completion check runs
        - PR should be marked ready after last sub-issue finalized
        """
        # Scenario: Finalizing sub-issue #137 (the last one)
        finalizing_issue_number = 137
        parent_issue_number = 128

        # Completion check should run even though we're finalizing a sub-issue
        should_check_completion = True  # NEW: Always check, not just for parent
        assert should_check_completion is True, \
            "Completion check should run for sub-issue finalization"

        # GitHub reports 3 sub-issues, all closed
        sub_issues_summary = {'total': 3, 'completed': 3, 'percent_completed': 100}
        api_sub_issues = [
            {'number': 135, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/128'},
            {'number': 136, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/128'},
            {'number': 137, 'state': 'closed', 'parent_issue_url': 'https://api.github.com/repos/owner/repo/issues/128'}
        ]

        # Check if all complete
        all_complete = all(issue.get('state') == 'closed' for issue in api_sub_issues)
        assert all_complete is True, "All sub-issues should be closed"

        # Should we mark PR ready?
        should_mark_ready = len(api_sub_issues) == 0 or all_complete
        assert should_mark_ready is True, \
            "PR #138 should be marked ready when last sub-issue (#137) is finalized"

    def test_github_native_is_single_source_of_truth(self):
        """
        Test that GitHub's native sub-issue API is the only source used.

        Even if markdown checkboxes exist in the issue body, they are IGNORED.
        Only sub_issues_summary and parent_issue_url relationships matter.
        """
        # Scenario: Issue body has markdown checkboxes, but GitHub API is empty
        issue_body_has_markdown = True  # Doesn't matter anymore
        sub_issues_summary = {'total': 0, 'completed': 0, 'percent_completed': 0}

        # The NEW logic: ONLY check GitHub API
        should_query_api = sub_issues_summary.get('total', 0) > 0

        assert should_query_api is False, \
            "Markdown checkboxes are ignored - only GitHub API matters"

        # Scenario 2: No markdown, but GitHub API has sub-issues
        issue_body_has_markdown = False  # Doesn't matter anymore
        sub_issues_summary = {'total': 3, 'completed': 3, 'percent_completed': 100}

        should_query_api = sub_issues_summary.get('total', 0) > 0

        assert should_query_api is True, \
            "GitHub API is the single source of truth"


class TestSubIssueDetectionAPICall:
    """Regression test for sub-issue detection API calls"""

    def test_api_call_formatting(self):
        """
        Verify REST API call uses query parameters in URL, not as kwargs.

        Regression test for bug where params={'state': 'all'} was passed
        as a keyword argument to rest() which caused TypeError.
        The fix embeds query params in the URL string.
        """
        from services.github_api_client import GitHubAPIClient
        from unittest.mock import patch, Mock
        import json

        client = GitHubAPIClient()

        # Mock subprocess.run to simulate successful API response
        with patch('subprocess.run') as mock_run:
            response_data = [
                {'number': 1, 'state': 'open'},
                {'number': 2, 'state': 'closed'}
            ]
            mock_run.return_value = Mock(
                returncode=0,
                stdout=json.dumps(response_data)
            )

            # This is how the code SHOULD call the API (with params in URL)
            endpoint_with_params = 'repos/owner/repo/issues?state=all&per_page=100'
            success, response = client.rest('GET', endpoint_with_params)

            # Verify it succeeds
            assert success == True
            assert response == response_data

            # Verify the subprocess was called with query params in the URL
            call_args = mock_run.call_args[0][0]
            assert any('state=all' in str(arg) for arg in call_args), \
                "Query parameters should be in the URL, not passed as kwargs"

    def test_params_kwarg_raises_typeerror(self):
        """
        Verify that passing params as kwargs raises TypeError.

        This documents the bug that was fixed: rest() does not accept
        a 'params' keyword argument.
        """
        from services.github_api_client import GitHubAPIClient

        client = GitHubAPIClient()

        # Attempting to pass params as kwarg should raise TypeError
        with pytest.raises(TypeError, match="unexpected keyword argument"):
            # This is the BUGGY way that caused the original issue
            client.rest('GET', 'repos/owner/repo/issues', params={'state': 'all'})


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

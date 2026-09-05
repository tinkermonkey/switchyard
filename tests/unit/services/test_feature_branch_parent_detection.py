"""
Unit tests for parent issue detection via GraphQL.

Tests cover the fix for:
- GraphQL response parsing was incorrectly accessing result.get('data', {})
- github_client.graphql() already extracts 'data' field before returning
- Parent detection should access result.get('repository', {}) directly
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch
from services.feature_branch_manager import FeatureBranchManager, ParentIssueLookupError


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
        Issue #126: a GraphQL failure must RAISE ParentIssueLookupError, not
        return None -- returning None here is indistinguishable from a
        confirmed "this issue has no parent," which let resolve_epic_id()
        silently mis-scope an epic worktree on a transient API error (see
        TestGetParentIssueLookupFailures below for the full regression suite).
        """
        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (False, {'error': 'rate_limited'})
            mock_get_client.return_value = mock_client

            with pytest.raises(ParentIssueLookupError):
                await manager.get_parent_issue(
                    mock_github_integration,
                    issue_number=214,
                    project="documentation_robotics"
                )

    @pytest.mark.asyncio
    async def test_parent_detection_missing_org_repo(self, manager):
        """
        Issue #126: missing org/repo config must RAISE ParentIssueLookupError,
        not return None -- a misconfiguration is a lookup failure, not a
        confirmed "no parent."
        """
        # Mock GitHub integration with missing org/repo
        mock_integration = Mock()
        mock_integration.github_org = None
        mock_integration.repo_name = None

        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            with pytest.raises(ParentIssueLookupError):
                await manager.get_parent_issue(
                    mock_integration,
                    issue_number=214,
                    project="documentation_robotics"
                )

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


class TestGetParentIssueLookupFailures:
    """Issue #126: get_parent_issue() used to swallow EVERY failure mode to a bare
    None, making a genuine lookup failure indistinguishable from GitHub's structured
    API confirming the issue has no parent. These tests cover the remaining failure
    mode not already exercised above (an unexpected exception from the GraphQL call
    itself, e.g. a network error) and the caching contract for failures."""

    @pytest.fixture
    def manager(self):
        return FeatureBranchManager()

    @pytest.fixture
    def mock_github_integration(self):
        mock = Mock()
        mock.github_org = "test-org"
        mock.repo_name = "test-repo"
        return mock

    @pytest.mark.asyncio
    async def test_unexpected_exception_is_wrapped_and_raised_not_swallowed(
        self, manager, mock_github_integration
    ):
        """A network error (or any other unexpected exception) inside the GraphQL
        call must surface as ParentIssueLookupError, not vanish into a None that
        looks like a confirmed no-parent answer."""
        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.side_effect = ConnectionError("network unreachable")
            mock_get_client.return_value = mock_client

            with pytest.raises(ParentIssueLookupError) as exc_info:
                await manager.get_parent_issue(
                    mock_github_integration, issue_number=214, project="documentation_robotics"
                )

            # The original exception must still be discoverable (chained), not lost.
            assert isinstance(exc_info.value.__cause__, ConnectionError)

    @pytest.mark.asyncio
    async def test_failed_lookup_is_not_cached(self, manager, mock_github_integration):
        """A failed attempt must never poison the cache -- the next call has to
        actually retry against GitHub, not silently replay a stale failure (or,
        worse, a stale None that looks like a confirmed no-parent)."""
        with patch('services.feature_branch_manager.get_github_client') as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (False, {'error': 'rate_limited'})
            mock_get_client.return_value = mock_client

            with pytest.raises(ParentIssueLookupError):
                await manager.get_parent_issue(
                    mock_github_integration, issue_number=214, project="documentation_robotics"
                )

            cache_key = ("test-org", "test-repo", 214)
            assert cache_key not in manager._parent_cache, \
                "A failed lookup must not be cached -- the next call must retry"

            # Second call, now succeeding, must actually hit GraphQL again (not
            # return a cached failure) and get the real answer.
            mock_client.graphql.return_value = (True, {
                'repository': {'issue': {'number': 214, 'parent': {'number': 188, 'title': 'Epic'}}}
            })
            parent_number = await manager.get_parent_issue(
                mock_github_integration, issue_number=214, project="documentation_robotics"
            )
            assert parent_number == 188
            assert mock_client.graphql.call_count == 2


class TestParentIssueLookupErrorPerCallerHandling:
    """Issue #126's core fix: get_parent_issue() no longer collapses "lookup
    failed" and "confirmed no parent" into the same None -- so each caller must
    now make its own explicit choice about which behavior is correct for it.
    Both of resolve_epic_id()/get_feature_branch_for_issue() below turn out to
    need the SAME choice (propagate) -- an initial version of this PR had
    get_feature_branch_for_issue() catch and fall back to None instead, reasoned
    to be safe because it has no persisted state of its own to poison. Code
    review found that reasoning incomplete: its own most consequential caller,
    finalize_feature_branch_work() (see TestFinalizeFeatureBranchWorkPropagatesLookupFailure
    below), treats a falsy result as a confirmed standalone issue and skips
    completion tracking/PR creation entirely -- silently reintroducing this
    issue's exact ambiguity one call frame up. These tests confirm the
    (corrected) propagation, on the real (unmocked) methods rather than
    re-testing get_parent_issue() itself."""

    @pytest.fixture
    def manager(self):
        return FeatureBranchManager()

    @pytest.mark.asyncio
    async def test_resolve_epic_id_propagates_lookup_failure_instead_of_guessing(self, manager):
        """The critical regression case: resolve_epic_id() feeds
        PipelineRunManager.resolve_workspace(), whose idempotency guard makes a
        wrong answer PERMANENT for the pipeline run's lifetime. On a transient
        lookup failure it must raise -- letting the caller's own retry/threshold
        handling get a real second attempt -- rather than silently falling back
        to the sub-issue's own number as if it had confirmed there was no parent."""
        with patch.object(manager, 'get_parent_issue', new_callable=AsyncMock) as mock_get_parent:
            mock_get_parent.side_effect = ParentIssueLookupError("GraphQL rate limited")

            with pytest.raises(ParentIssueLookupError):
                await manager.resolve_epic_id(Mock(), 101, project='test-project')

    @pytest.mark.asyncio
    async def test_get_feature_branch_for_issue_propagates_lookup_failure(self, manager):
        """Must propagate, not degrade to "no feature branch found" -- see
        TestFinalizeFeatureBranchWorkPropagatesLookupFailure below for why a
        swallowed failure here is unsafe."""
        with patch.object(manager, 'get_feature_branch_state', return_value=None), \
             patch.object(manager, 'get_parent_issue', new_callable=AsyncMock) as mock_get_parent:
            mock_get_parent.side_effect = ParentIssueLookupError("GraphQL rate limited")

            with pytest.raises(ParentIssueLookupError):
                await manager.get_feature_branch_for_issue('test-project', 101, Mock())


class TestFinalizeFeatureBranchWorkPropagatesLookupFailure:
    """The actual regression case that overturned get_feature_branch_for_issue()'s
    original "fall back to None" design (code review finding on this PR):
    finalize_feature_branch_work() -- the live production finalize step for every
    'issues'/'hybrid' dispatch (services/workspace/issues_context.py and
    hybrid_context.py both call it) -- treats a falsy get_feature_branch_for_issue()
    result as "this issue is genuinely standalone" and skips
    mark_sub_issue_complete()/create_or_update_feature_pr() entirely, returning
    success with no error. A caught-and-swallowed ParentIssueLookupError here
    would have silently reproduced that exact failure mode for a REAL sub-issue
    hitting a transient lookup failure at exactly the wrong moment."""

    @pytest.fixture
    def manager(self):
        return FeatureBranchManager()

    @pytest.mark.asyncio
    async def test_lookup_failure_during_finalize_raises_not_silently_treated_as_standalone(
        self, manager, tmp_path
    ):
        with patch.object(manager, 'get_feature_branch_state', return_value=None), \
             patch.object(manager, 'get_parent_issue', new_callable=AsyncMock) as mock_get_parent, \
             patch.object(manager, 'git_add_all', new_callable=AsyncMock) as mock_add, \
             patch.object(manager, 'git_commit', new_callable=AsyncMock) as mock_commit:
            mock_get_parent.side_effect = ParentIssueLookupError("GraphQL rate limited")

            with pytest.raises(ParentIssueLookupError):
                await manager.finalize_feature_branch_work(
                    project='test-project',
                    issue_number=101,
                    commit_message='test commit',
                    github_integration=Mock(),
                    project_dir_override=str(tmp_path),
                )

            # Must fail before ever touching git -- no commit/push attempted
            # against a workspace whose completion status couldn't be determined.
            mock_add.assert_not_called()
            mock_commit.assert_not_called()


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

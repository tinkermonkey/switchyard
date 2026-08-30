"""
Unit tests for the chunked, aliased cross-parent sub-issues GraphQL query
builder in services/feature_branch_manager.py (GitHub issue #95, sub-issue
of #36).

Covers:
- _build_batched_sub_issues_query(): aliased query construction for one
  and multiple parents
- _parse_batched_sub_issues_response(): happy-path parsing (single and
  multi-parent), partial-batch GraphQL error attribution, and a missing
  alias with no attributable error
- FeatureBranchManager.get_sub_issues_for_parents_batched(): end-to-end
  wiring over a mocked GitHub client, including chunking beyond
  MAX_SUB_ISSUE_PARENTS_PER_BATCH, de-duplication, a fully-failed chunk,
  and drop-in shape parity with _get_sub_issues_from_parent()

No live network access - all GitHub API calls are mocked.
"""

import pytest
from unittest.mock import Mock, patch

from services.feature_branch_manager import (
    MAX_SUB_ISSUE_PARENTS_PER_BATCH,
    FeatureBranchManager,
    _build_batched_sub_issues_query,
    _parse_batched_sub_issues_response,
)


def _sub_issue(number: int, state: str = "OPEN") -> dict:
    """Minimal but shaped-like-real sub-issue node."""
    return {
        "number": number,
        "title": f"Sub-issue {number}",
        "state": state,
        "url": f"https://github.com/test-org/test-repo/issues/{number}",
    }


class TestBuildBatchedSubIssuesQuery:
    def test_single_parent_aliases_correctly(self):
        query = _build_batched_sub_issues_query([123])

        assert "query($owner: String!, $repo: String!)" in query
        assert "repository(owner: $owner, name: $repo)" in query
        assert "i123: issue(number: 123)" in query
        assert "subIssues(first: 100)" in query
        assert "totalCount" in query
        assert "number" in query and "title" in query and "state" in query and "url" in query

    def test_multiple_parents_each_get_own_alias(self):
        query = _build_batched_sub_issues_query([123, 456, 789])

        for number in (123, 456, 789):
            assert f"i{number}: issue(number: {number})" in query
        # Shared field selection present under every alias
        assert query.count("subIssues(first: 100)") == 3
        assert query.count("totalCount") == 3

    def test_query_is_parameterized_not_interpolated_for_owner_repo(self):
        query = _build_batched_sub_issues_query([1])
        # owner/repo must stay as GraphQL variables, never string-interpolated
        assert "$owner" in query
        assert "$repo" in query


class TestParseBatchedSubIssuesResponse:
    def test_empty_parent_list_returns_empty(self):
        results, errors = _parse_batched_sub_issues_response({"repository": {}}, [])
        assert results == {}
        assert errors == {}

    def test_none_response_errors_every_parent(self):
        results, errors = _parse_batched_sub_issues_response(None, [1, 2])
        assert results == {}
        assert errors == {1: "No response received", 2: "No response received"}

    def test_errors_only_response_with_no_data_key_is_not_mistaken_for_success(self):
        """
        Regression test: a pre-execution GraphQL validation error (e.g. a
        malformed alias) comes back as {'errors': [...]} with NO 'data' key
        at all. Treating that as the unwrapped-success shape (checking only
        `if 'data' in response`) would silently drop the real error and
        report every parent as generically "missing" instead of surfacing
        the actual failure.
        """
        response = {"errors": [{"message": "Field 'issue' argument 'number' has invalid value.", "path": ["repository", "i50"]}]}
        results, errors = _parse_batched_sub_issues_response(response, [50])
        assert results == {}
        assert errors == {50: "Field 'issue' argument 'number' has invalid value."}

    def test_single_parent_happy_path(self):
        response = {
            "repository": {
                "i123": {
                    "number": 123,
                    "subIssues": {
                        "totalCount": 2,
                        "nodes": [_sub_issue(124), _sub_issue(125, state="CLOSED")],
                    },
                }
            }
        }

        results, errors = _parse_batched_sub_issues_response(response, [123])

        assert errors == {}
        assert results == {123: [_sub_issue(124), _sub_issue(125, state="CLOSED")]}

    def test_multi_parent_happy_path_matches_per_parent_shape(self):
        response = {
            "repository": {
                "i1": {"number": 1, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(11)]}},
                "i2": {"number": 2, "subIssues": {"totalCount": 0, "nodes": []}},
                "i3": {"number": 3, "subIssues": {"totalCount": 2, "nodes": [_sub_issue(31), _sub_issue(32)]}},
            }
        }

        results, errors = _parse_batched_sub_issues_response(response, [1, 2, 3])

        assert errors == {}
        assert results[1] == [_sub_issue(11)]
        assert results[2] == []
        assert results[3] == [_sub_issue(31), _sub_issue(32)]

    def test_accepts_full_envelope_shape_with_data_key(self):
        # success=False path from GitHubAPIClient.graphql() hands back the
        # full envelope {'data': ..., 'errors': [...]}, not the unwrapped
        # payload - parsing must handle both shapes.
        response = {
            "data": {
                "repository": {
                    "i1": {"number": 1, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(11)]}},
                }
            },
            "errors": [],
        }

        results, errors = _parse_batched_sub_issues_response(response, [1])

        assert errors == {}
        assert results == {1: [_sub_issue(11)]}

    def test_partial_batch_error_attributed_by_path_leaves_rest_intact(self):
        response = {
            "data": {
                "repository": {
                    "i1": {"number": 1, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(11)]}},
                    "i2": None,
                }
            },
            "errors": [
                {
                    "type": "NOT_FOUND",
                    "message": "Could not resolve to an issue with the number of 2.",
                    "path": ["repository", "i2"],
                }
            ],
        }

        results, errors = _parse_batched_sub_issues_response(response, [1, 2])

        # Parent #1 unaffected by parent #2's error
        assert results == {1: [_sub_issue(11)]}
        assert 1 not in errors
        assert errors[2] == "Could not resolve to an issue with the number of 2."

    def test_missing_alias_with_no_error_recorded_as_error(self):
        # Alias absent from the response and not named by any top-level
        # error - still must not silently disappear or crash.
        response = {"repository": {}}

        results, errors = _parse_batched_sub_issues_response(response, [999])

        assert results == {}
        assert 999 in errors

    def test_unattributable_error_does_not_lose_other_parents(self):
        response = {
            "data": {
                "repository": {
                    "i1": {"number": 1, "subIssues": {"totalCount": 0, "nodes": []}},
                }
            },
            "errors": [
                {"message": "Something went wrong", "path": ["someOtherField"]}
            ],
        }

        results, errors = _parse_batched_sub_issues_response(response, [1])

        assert results == {1: []}
        # The unattributable error should not be silently assigned to parent 1
        assert 1 not in errors


class TestGetSubIssuesForParentsBatched:
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
    async def test_empty_input_returns_empty_without_network_call(self, manager, mock_github_integration):
        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, [])

            assert result == {}
            mock_client.graphql.assert_not_called()

    @pytest.mark.asyncio
    async def test_single_parent_one_request_matches_get_sub_issues_from_parent_shape(
        self, manager, mock_github_integration
    ):
        response = {
            "repository": {
                "i50": {
                    "number": 50,
                    "subIssues": {"totalCount": 1, "nodes": [_sub_issue(51)]},
                }
            }
        }

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, response)
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, [50])

            assert mock_client.graphql.call_count == 1
            assert result == {50: [_sub_issue(51)]}

            # Same per-parent shape _get_sub_issues_from_parent() returns:
            # a plain list of dicts with number/title/state/url.
            sub_issues = result[50]
            assert isinstance(sub_issues, list)
            assert sub_issues[0]["number"] == 51
            assert sub_issues[0]["state"] == "OPEN"

            # Variables passed through correctly
            call_args = mock_client.graphql.call_args
            variables = call_args[0][1]
            assert variables["owner"] == "test-org"
            assert variables["repo"] == "test-repo"

    @pytest.mark.asyncio
    async def test_invalid_parent_numbers_dropped_not_sent_to_query_builder(self, manager, mock_github_integration):
        """
        Regression test: a falsy/negative/non-int parent number interpolated
        into an aliased GraphQL field (e.g. `i-5: issue(number: -5)`) would
        be an invalid alias, breaking parsing of the whole chunk it's in -
        such numbers must be dropped before ever reaching the query.
        """
        response = {
            "repository": {
                "i50": {"number": 50, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(51)]}},
            }
        }

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, response)
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(
                mock_github_integration, [50, 0, -5, None, 50]
            )

            assert mock_client.graphql.call_count == 1
            # Only the valid, deduplicated parent made it into the query.
            query = mock_client.graphql.call_args[0][0]
            assert "i50:" in query
            assert "i0:" not in query
            assert "i-5:" not in query
            assert result == {50: [_sub_issue(51)]}

    @pytest.mark.asyncio
    async def test_unexpected_exception_degrades_to_partial_result_not_raise(self, manager, mock_github_integration):
        """
        Regression test: matches _get_sub_issues_from_parent()'s own
        guarantee that this never raises out to its caller - an unexpected
        error (e.g. a malformed github_integration) must degrade to
        whatever was already fetched, not crash the caller.
        """
        bad_integration = Mock()
        bad_integration.github_org = "test-org"
        del bad_integration.repo_name  # accessing .repo_name raises AttributeError

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_get_client.return_value = Mock()

            result = await manager.get_sub_issues_for_parents_batched(bad_integration, [50])  # must not raise

        assert result == {}

    @pytest.mark.asyncio
    async def test_multiple_parents_within_cap_issue_single_request(self, manager, mock_github_integration):
        response = {
            "repository": {
                "i1": {"number": 1, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(11)]}},
                "i2": {"number": 2, "subIssues": {"totalCount": 0, "nodes": []}},
                "i3": {"number": 3, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(31)]}},
            }
        }

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, response)
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, [1, 2, 3])

            # ONE GraphQL call replaces what would have been 3 sequential
            # _get_sub_issues_from_parent() calls.
            assert mock_client.graphql.call_count == 1
            assert result == {1: [_sub_issue(11)], 2: [], 3: [_sub_issue(31)]}

    @pytest.mark.asyncio
    async def test_duplicate_parent_numbers_deduplicated(self, manager, mock_github_integration):
        response = {
            "repository": {
                "i1": {"number": 1, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(11)]}},
            }
        }

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, response)
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, [1, 1, 1])

            assert mock_client.graphql.call_count == 1
            query = mock_client.graphql.call_args[0][0]
            # Only one alias for parent #1, not three
            assert query.count("i1: issue(number: 1)") == 1
            assert result == {1: [_sub_issue(11)]}

    @pytest.mark.asyncio
    async def test_chunks_beyond_cap_into_multiple_requests(self, manager, mock_github_integration):
        parent_numbers = list(range(1, MAX_SUB_ISSUE_PARENTS_PER_BATCH + 5))  # cap + 4 extra

        def fake_graphql(query, variables=None, retries=0):
            # Return an empty-but-successful sub-issues list for every
            # parent alias actually present in this chunk's query.
            repo = {}
            for number in parent_numbers:
                alias = f"i{number}"
                if f"{alias}: issue(number: {number})" in query:
                    repo[alias] = {"number": number, "subIssues": {"totalCount": 0, "nodes": []}}
            return True, {"repository": repo}

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.side_effect = fake_graphql
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, parent_numbers)

            # 5 extra parents beyond the cap -> 2 chunks
            assert mock_client.graphql.call_count == 2
            assert set(result.keys()) == set(parent_numbers)
            for number in parent_numbers:
                assert result[number] == []

            # Verify no single query exceeded the cap
            for call in mock_client.graphql.call_args_list:
                query = call[0][0]
                alias_count = sum(
                    1 for number in parent_numbers if f"i{number}: issue(number: {number})" in query
                )
                assert alias_count <= MAX_SUB_ISSUE_PARENTS_PER_BATCH

    @pytest.mark.asyncio
    async def test_partial_batch_error_does_not_lose_other_parents_end_to_end(
        self, manager, mock_github_integration
    ):
        response = {
            "data": {
                "repository": {
                    "i1": {"number": 1, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(11)]}},
                    "i2": None,
                }
            },
            "errors": [
                {
                    "message": "Could not resolve to an issue with the number of 2.",
                    "path": ["repository", "i2"],
                }
            ],
        }

        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            # success=False (GraphQL errors present) but data is salvageable
            mock_client.graphql.return_value = (False, response)
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, [1, 2])

            assert result == {1: [_sub_issue(11)]}
            assert 2 not in result

    @pytest.mark.asyncio
    async def test_total_chunk_failure_omits_all_parents_in_that_chunk(self, manager, mock_github_integration):
        with patch("services.feature_branch_manager.get_github_client") as mock_get_client:
            mock_client = Mock()
            # Total failure: no 'data'/'errors' envelope to salvage from,
            # e.g. rate limit / transport failure.
            mock_client.graphql.return_value = (False, {"error": "GitHub API rate limit exceeded"})
            mock_get_client.return_value = mock_client

            result = await manager.get_sub_issues_for_parents_batched(mock_github_integration, [1, 2])

            assert result == {}

    @pytest.mark.asyncio
    async def test_does_not_change_get_sub_issues_from_parent_behavior(self, manager, mock_github_integration):
        """
        Sanity check that the additive batched method coexists with the
        original single-parent method without altering its behavior.
        """
        single_response = {
            "repository": {
                "issue": {
                    "number": 7,
                    "subIssues": {"totalCount": 1, "nodes": [_sub_issue(8)]},
                }
            }
        }

        # _get_sub_issues_from_parent() re-imports get_github_client locally
        # from services.github_api_client (unlike the new batched method,
        # which uses the module-level import) - patch it at its actual
        # source so this pre-existing behavior is exercised unmodified.
        with patch("services.github_api_client.get_github_client") as mock_get_client:
            mock_client = Mock()
            mock_client.graphql.return_value = (True, single_response)
            mock_get_client.return_value = mock_client

            result = await manager._get_sub_issues_from_parent(
                mock_github_integration, {"number": 7}
            )

            assert result == [_sub_issue(8)]
            # Unaliased single-parent query shape, unchanged
            query = mock_client.graphql.call_args[0][0]
            assert "issue(number: $issueNumber)" in query

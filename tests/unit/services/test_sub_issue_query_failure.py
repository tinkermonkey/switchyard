"""
Regression tests for the silent failure in pipeline run 4cf816cf: a sub-issue
query that FAILED being reported as a parent that HAS NO SUB-ISSUES.

What happened
-------------
With the GitHub circuit breaker open on an exhausted GraphQL budget:

    Failed to get issue details: circuit breaker open
    parent_issue_data missing 'number' key, cannot query sub-issues
    Parent issue #1016 has no sub-issues (standalone work) - marking PR ready
    Marked PR #1051 as ready for review

Epic #1016 had five sub-issues, four of them unfinished; only Phase 1 was on
the branch. A rate-limit error had been turned into a positive claim about the
world and then acted on irreversibly.

The fix is a type distinction, not a log-level change:
_get_sub_issues_from_parent() returns [] ONLY for "the query succeeded and
there are none", and raises SubIssueQueryError for every "could not answer".
These tests pin that boundary, because the two answers are opposite and the
PR-ready decision downstream turns on exactly which one it got.

No live network access - all GitHub API calls are mocked.
"""

import os
import pytest
from unittest.mock import AsyncMock, Mock, patch

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from services.feature_branch_manager import (
    FeatureBranchManager,
    SubIssueQueryError,
)


def _sub_issue(number: int, state: str = "OPEN") -> dict:
    return {
        "number": number,
        "title": f"Sub-issue {number}",
        "state": state,
        "url": f"https://github.com/test-org/test-repo/issues/{number}",
    }


@pytest.fixture
def manager():
    return FeatureBranchManager()


@pytest.fixture
def github():
    mock = Mock()
    mock.github_org = "test-org"
    mock.repo_name = "test-repo"
    return mock


class TestEmptyStillMeansEmpty:
    """The distinction is only useful if the ordinary answer is unchanged."""

    @pytest.mark.asyncio
    async def test_a_parent_with_no_sub_issues_still_returns_an_empty_list(
        self, manager, github
    ):
        response = {
            "repository": {"issue": {"number": 7, "subIssues": {"totalCount": 0, "nodes": []}}}
        }
        with patch("services.github_api_client.get_github_client") as mock_get_client:
            mock_get_client.return_value = Mock(graphql=Mock(return_value=(True, response)))

            assert await manager._get_sub_issues_from_parent(github, {"number": 7}) == []

    @pytest.mark.asyncio
    async def test_a_parent_with_sub_issues_still_returns_them(self, manager, github):
        response = {
            "repository": {
                "issue": {"number": 7, "subIssues": {"totalCount": 1, "nodes": [_sub_issue(8)]}}
            }
        }
        with patch("services.github_api_client.get_github_client") as mock_get_client:
            mock_get_client.return_value = Mock(graphql=Mock(return_value=(True, response)))

            assert await manager._get_sub_issues_from_parent(github, {"number": 7}) == [
                _sub_issue(8)
            ]


class TestFailureIsRaisedNotReturnedAsEmpty:
    @pytest.mark.asyncio
    async def test_the_incidents_own_input_raises(self, manager, github):
        """An open circuit breaker makes get_issue() return an error dict, not
        an issue. That dict has no 'number' key — the exact value that reached
        this method at 14:07:21 and came back as []."""
        breaker_error = {"error": "GitHub API rate limit exceeded - circuit breaker open"}

        with pytest.raises(SubIssueQueryError) as excinfo:
            await manager._get_sub_issues_from_parent(github, breaker_error)

        assert "number" in str(excinfo.value)

    @pytest.mark.asyncio
    @pytest.mark.parametrize("parent_data", [None, {}, {"title": "no number here"}])
    async def test_every_unusable_parent_shape_raises(self, manager, github, parent_data):
        with pytest.raises(SubIssueQueryError):
            await manager._get_sub_issues_from_parent(github, parent_data)

    @pytest.mark.asyncio
    async def test_a_failed_graphql_call_raises(self, manager, github):
        with patch("services.github_api_client.get_github_client") as mock_get_client:
            mock_get_client.return_value = Mock(
                graphql=Mock(return_value=(False, {"error": "rate_limited"}))
            )

            with pytest.raises(SubIssueQueryError) as excinfo:
                await manager._get_sub_issues_from_parent(github, {"number": 1016})

        assert "1016" in str(excinfo.value)

    @pytest.mark.asyncio
    async def test_an_unexpected_exception_is_still_a_failure_to_answer(
        self, manager, github
    ):
        """Not a failure to have sub-issues. The distinction has to hold for
        the unanticipated case too, or the laundering just moves."""
        with patch("services.github_api_client.get_github_client") as mock_get_client:
            mock_get_client.return_value = Mock(
                graphql=Mock(side_effect=ConnectionError("transport blew up"))
            )

            with pytest.raises(SubIssueQueryError) as excinfo:
                await manager._get_sub_issues_from_parent(github, {"number": 1016})

        assert isinstance(excinfo.value.__cause__, ConnectionError)

    @pytest.mark.asyncio
    async def test_the_error_is_never_an_empty_list(self, manager, github):
        """Stated as its own test because returning [] here is the whole bug,
        and a future refactor that 'helpfully' restores a fallback return would
        pass every other test in this file."""
        with patch("services.github_api_client.get_github_client") as mock_get_client:
            mock_get_client.return_value = Mock(
                graphql=Mock(return_value=(False, {"error": "rate_limited"}))
            )

            result = None
            try:
                result = await manager._get_sub_issues_from_parent(github, {"number": 1016})
            except SubIssueQueryError:
                pass

            assert result is None, "a failed query must not produce a returnable answer"


class TestSubIssueQueryErrorType:
    def test_it_is_catchable_as_a_runtime_error(self):
        """Every caller sits inside a broad `except Exception` already, and each
        of those already defaults to the conservative branch (skip, or
        all_complete=False). So the cost of a caller FORGETTING the specific
        handler is a mislabelled log line, not a wrong decision — which is what
        makes raising safe here, and is the property a sentinel or result type
        would not have given us (`if not result.items` is the natural thing to
        write, and Python offers no exhaustiveness check to stop it)."""
        assert issubclass(SubIssueQueryError, RuntimeError)


class TestPrReadyDoesNotActOnAnUnansweredQuestion:
    """The irreversible call site: finalize_feature_branch_work() reads
    `len(actual_sub_issues) == 0` as "standalone work" and marks the feature PR
    ready for review.

    STRUCTURAL, and deliberately labelled as such. Driving that method end to
    end needs the project_checkout lock, a real git commit, a push and PR
    creation — a stub surface large enough that the test would mostly assert
    its own mocks. What these pin instead is the property that actually
    regressed: that the specific handler EXISTS, sits ahead of the pre-existing
    broad `except Exception` (behind it, it would be dead code), and cannot
    reach the mark-ready branch.

    The previous version of this class claimed to be end-to-end and was
    vacuous: it called _get_sub_issues_from_parent() directly inside
    pytest.raises and then asserted mark_pr_ready.assert_not_called() on a Mock
    nothing could ever have reached. It passed by construction, and would have
    kept passing with the handler deleted.
    """

    @pytest.fixture
    def finalize_source(self):
        import inspect
        return inspect.getsource(FeatureBranchManager.finalize_feature_branch_work)

    def test_the_specific_handler_exists(self, finalize_source):
        assert 'except SubIssueQueryError' in finalize_source

    def test_it_precedes_the_broad_handler(self, finalize_source):
        """Behind `except Exception` it would never run — the specific handling
        would be dead code while looking entirely correct."""
        specific = finalize_source.index('except SubIssueQueryError')
        broad = finalize_source.index('except Exception as e:\n            logger.error(f"Failed to check sub-issue completion')
        assert specific < broad

    def test_the_handler_leaves_the_pr_a_draft(self, finalize_source):
        """all_complete=False is what keeps the mark-ready branch unreachable."""
        import re
        handler = re.search(
            r'except SubIssueQueryError as e:(.*?)except Exception as e:',
            finalize_source,
            re.DOTALL,
        )
        assert handler, "expected a SubIssueQueryError handler"
        body = handler.group(1)
        assert re.search(r'all_complete\s*=\s*False', body)
        assert 'mark_pr_ready' not in body

    def test_the_mark_ready_branch_is_gated_on_a_real_answer(self, finalize_source):
        """mark_pr_ready() must sit behind the len()==0-or-all_complete gate, so
        that a raised query cannot reach it by any path."""
        gate = finalize_source.index('if len(actual_sub_issues) == 0 or all_complete:')
        mark_ready = finalize_source.index('mark_pr_ready(')
        assert gate < mark_ready



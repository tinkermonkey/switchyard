"""
Unit tests for GitHubAPIClient's dual REST/GraphQL rate-limit buckets (issue #103).

Tests cover:
- _parse_gh_api_include_output() - the pure parser that recovers REST-bucket
  x-ratelimit-* headers from `gh api --include`'s stdout shape.
- Bucket independence - a REST call updates rate_limit_rest without
  touching rate_limit_graphql, and vice versa for a GraphQL call. This is
  the literal bug #103 reports (a single conflated `rate_limit` field
  whose resource_type got silently overwritten by whichever call type
  reported last), so it's asserted directly rather than only indirectly
  via get_status().
"""

import json
import subprocess
from unittest.mock import MagicMock, patch

import pytest

from services.github_api_client import GitHubAPIClient, GitHubRateLimitStatus


class TestParseGhApiIncludeOutput:
    """Unit tests for _parse_gh_api_include_output (no instance state needed)."""

    @pytest.fixture
    def parser(self):
        # Pure function of its `stdout` argument - doesn't touch self, so
        # __new__ avoids the constructor's Redis/background-thread setup.
        return GitHubAPIClient.__new__(GitHubAPIClient)

    def test_normal_include_shape(self, parser):
        stdout = (
            "HTTP/2.0 200 OK\n"
            "Content-Type: application/json; charset=utf-8\n"
            "X-Ratelimit-Limit: 5000\n"
            "X-Ratelimit-Remaining: 4242\n"
            "X-Ratelimit-Reset: 1700000000\n"
            "X-Ratelimit-Resource: core\n"
            "\n"
            '{"login": "octocat"}'
        )
        headers, body = parser._parse_gh_api_include_output(stdout)

        assert headers["x-ratelimit-limit"] == "5000"
        assert headers["x-ratelimit-remaining"] == "4242"
        assert headers["x-ratelimit-reset"] == "1700000000"
        assert headers["x-ratelimit-resource"] == "core"
        assert json.loads(body) == {"login": "octocat"}

    def test_header_value_containing_colon_is_preserved(self, parser):
        # e.g. a Date or Link header - partition(':') must split on the
        # first colon only, keeping the rest of the value intact.
        stdout = (
            "HTTP/2.0 200 OK\n"
            "Date: Mon, 31 Aug 2026 10:25:13 GMT\n"
            "\n"
            "{}"
        )
        headers, body = parser._parse_gh_api_include_output(stdout)
        assert headers["date"] == "Mon, 31 Aug 2026 10:25:13 GMT"
        assert body == "{}"

    def test_no_separator_falls_back_to_whole_body(self, parser):
        # Plain `gh api` output (no --include), or a test mock that returns
        # bare JSON with no header block at all.
        plain = json.dumps({"foo": "bar"})
        headers, body = parser._parse_gh_api_include_output(plain)
        assert headers == {}
        assert body == plain

    def test_non_http_first_line_falls_back_to_whole_body(self, parser):
        # A blank line is present, but the first line doesn't look like an
        # HTTP status line - must not be mistaken for a real header block.
        malformed = 'not-a-status-line\n\n{"a": 1}'
        headers, body = parser._parse_gh_api_include_output(malformed)
        assert headers == {}
        assert body == malformed

    def test_empty_body_after_real_headers_is_preserved_as_empty(self, parser):
        # A genuine empty-body success (e.g. 204 No Content) must not be
        # treated as malformed - rest() relies on getting back an empty
        # string here so its own empty-response handling can take over.
        stdout = "HTTP/2.0 204 No Content\nX-Ratelimit-Remaining: 100\n\n"
        headers, body = parser._parse_gh_api_include_output(stdout)
        assert headers["x-ratelimit-remaining"] == "100"
        assert body == ""

    def test_error_response_headers_still_recovered(self, parser):
        # Verified against a live 404 from `gh api --include`: headers are
        # present on error responses too, so the REST bucket still gets
        # updated even when the call itself fails.
        stdout = (
            "HTTP/2.0 404 Not Found\n"
            "X-Ratelimit-Remaining: 4761\n"
            "\n"
            '{"message": "Not Found"}'
        )
        headers, body = parser._parse_gh_api_include_output(stdout)
        assert headers["x-ratelimit-remaining"] == "4761"
        assert json.loads(body) == {"message": "Not Found"}


class TestRateLimitBucketIndependence:
    """A REST call must update rate_limit_rest only; a GraphQL call must
    update rate_limit_graphql only. This is the exact conflation bug
    issue #103 reports - previously there was one `rate_limit` field
    whose `resource_type` got overwritten by whichever call type reported
    response headers last.
    """

    @pytest.fixture
    def client(self):
        return GitHubAPIClient()

    def test_rest_call_updates_only_rest_bucket(self, client):
        graphql_before = client.rate_limit_graphql.to_dict()

        include_stdout = (
            "HTTP/2.0 200 OK\n"
            "X-Ratelimit-Limit: 5000\n"
            "X-Ratelimit-Remaining: 111\n"
            "X-Ratelimit-Reset: 1700000000\n"
            "\n"
            '{"ok": true}'
        )
        mock_result = MagicMock(returncode=0, stdout=include_stdout, stderr="")

        with patch("subprocess.run", return_value=mock_result):
            success, data = client.rest("GET", "/some/endpoint")

        assert success is True
        assert data == {"ok": True}
        assert client.rate_limit_rest.remaining == 111
        assert client.rate_limit_rest.resource_type == "rest"
        # GraphQL bucket must be completely untouched by a REST call.
        assert client.rate_limit_graphql.to_dict() == graphql_before

    def test_graphql_call_updates_only_graphql_bucket(self, client):
        rest_before = client.rate_limit_rest.to_dict()

        graphql_response = {
            "data": {"viewer": {"login": "octocat"}},
            "extensions": {
                "cost": {
                    "rateLimit": {
                        "remaining": 222,
                        "limit": 5000,
                        "resetAt": "2026-08-31T12:00:00Z",
                    }
                }
            },
        }
        mock_result = MagicMock(returncode=0, stdout=json.dumps(graphql_response), stderr="")

        with patch("subprocess.run", return_value=mock_result):
            success, data = client.graphql("query { viewer { login } }")

        assert success is True
        assert client.rate_limit_graphql.remaining == 222
        assert client.rate_limit_graphql.resource_type == "graphql"
        # REST bucket must be completely untouched by a GraphQL call.
        assert client.rate_limit_rest.to_dict() == rest_before

    def test_rate_limit_alias_tracks_graphql_bucket_only(self, client):
        # self.rate_limit is a backward-compat alias for the GraphQL
        # bucket (same object, not a copy) - a REST-bucket update must not
        # be visible through it.
        assert client.rate_limit is client.rate_limit_graphql

        include_stdout = (
            "HTTP/2.0 200 OK\n"
            "X-Ratelimit-Remaining: 333\n"
            "\n"
            "{}"
        )
        mock_result = MagicMock(returncode=0, stdout=include_stdout, stderr="")
        with patch("subprocess.run", return_value=mock_result):
            client.rest("GET", "/x")

        assert client.rate_limit_rest.remaining == 333
        assert client.rate_limit.remaining != 333


class TestRateLimitStatusStaleness:
    """A bucket sitting at its 5000/5000 defaults looks identical to a
    genuinely healthy '0% used' bucket unless something distinguishes
    'never actually populated' from 'just refreshed and healthy'.
    """

    def test_fresh_instance_is_stale(self):
        status = GitHubRateLimitStatus()
        assert status.ever_updated is False
        assert status.is_stale() is True

    def test_marked_updated_and_recent_is_not_stale(self):
        status = GitHubRateLimitStatus()
        status.ever_updated = True
        assert status.is_stale() is False

    def test_updated_long_ago_is_stale(self):
        status = GitHubRateLimitStatus()
        status.ever_updated = True
        status.last_updated = status.last_updated.replace(year=2020)
        assert status.is_stale() is True

    def test_to_dict_includes_ever_updated_and_stale(self):
        status = GitHubRateLimitStatus()
        d = status.to_dict()
        assert d["ever_updated"] is False
        assert d["stale"] is True


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

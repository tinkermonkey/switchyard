"""
Unit tests for the cross-process rate-limit mirror added to
services/github_api_client.py (GitHubAPIClient._mirror_rate_limit_to_redis
and get_shared_rate_limit_status()), and the fixes made after review found
this untested core logic had real bugs.

Tests cover:
- The out-of-order-write guard, including the window-rollover case a
  remaining-only comparison can't catch (a delayed pre-rollover write with
  LOWER remaining than a fresh post-rollover write must still be
  discarded, because its reset_time is older).
- get_shared_rate_limit_status()'s three-way never_observed / unavailable /
  stale distinction - conflating "Redis is down" with "genuinely never
  observed" was found to silently disable degraded-health detection.
- The GraphQL `gh api graphql --include` header-parsing path, including on
  an error (rate-limited) response - the critical bug this regression-tests
  is that headers on a returncode==1 response used to never be parsed at
  all, and _extract_reset_time() received raw --include stdout that always
  failed to json.loads(), so the breaker always fell back to a full 1-hour
  backoff regardless of GitHub's real (often much shorter) reset time.
- _extract_reset_time() preferring the response headers over regex-parsing
  an "available in N seconds" message out of the error body.

See tests/unit/services/test_github_api_rate_limit_buckets.py for the
pre-existing bucket-independence and staleness tests this file
complements.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import redis as redis_lib

from services.github_api_client import (
    GitHubAPIClient,
    GitHubRateLimitStatus,
    RATE_LIMIT_REDIS_KEYS,
    get_shared_rate_limit_status,
)


def _redis_client():
    return redis_lib.Redis(
        host=os.environ.get('REDIS_HOST', 'redis'), port=6379,
        decode_responses=True, socket_timeout=2,
    )


@pytest.fixture
def clean_redis():
    """The two rate-limit mirror keys are real, global, non-namespaced
    Redis keys shared with the live dashboard (see tests/conftest.py's
    session-level purge fixture) - tests here must not leak into, or be
    polluted by, anything else touching the same keys mid-session.
    """
    r = _redis_client()
    r.delete(*RATE_LIMIT_REDIS_KEYS.values())
    yield r
    r.delete(*RATE_LIMIT_REDIS_KEYS.values())


@pytest.fixture
def client():
    return GitHubAPIClient()


def _make_bucket(remaining, limit, reset_time, last_updated=None):
    bucket = GitHubRateLimitStatus()
    bucket.remaining = remaining
    bucket.limit = limit
    bucket.reset_time = reset_time
    bucket.last_updated = last_updated or datetime.now(timezone.utc)
    return bucket


class TestMirrorRateLimitOutOfOrderGuard:
    def test_lower_remaining_in_same_window_is_applied(self, client, clean_redis):
        """The common, important case: a sibling process made real calls
        and depleted quota further within the same window - must be
        applied, not discarded."""
        reset = datetime.now(timezone.utc) + timedelta(minutes=30)
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], json.dumps({
            'remaining': 4000, 'limit': 5000,
            'reset_time': reset.isoformat(), 'last_updated': datetime.now(timezone.utc).isoformat(),
        }))

        bucket = _make_bucket(3500, 5000, reset)
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 3500

    def test_higher_remaining_in_same_window_is_discarded(self, client, clean_redis):
        """An out-of-order write from a slower process reporting MORE
        remaining than what's already stored, within the same window, must
        be discarded - remaining only decreases within a window."""
        reset = datetime.now(timezone.utc) + timedelta(minutes=30)
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], json.dumps({
            'remaining': 100, 'limit': 5000,
            'reset_time': reset.isoformat(), 'last_updated': datetime.now(timezone.utc).isoformat(),
        }))

        bucket = _make_bucket(4999, 5000, reset)  # higher than stored, same window
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 100  # unchanged

    def test_newer_window_is_applied_even_with_higher_remaining(self, client, clean_redis):
        """A fresh window rolling over (higher remaining, newer reset_time)
        must be applied - this is a real reset, not an out-of-order
        write."""
        old_reset = datetime.now(timezone.utc) - timedelta(minutes=1)
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], json.dumps({
            'remaining': 50, 'limit': 5000,
            'reset_time': old_reset.isoformat(), 'last_updated': datetime.now(timezone.utc).isoformat(),
        }))

        new_reset = datetime.now(timezone.utc) + timedelta(hours=1)
        bucket = _make_bucket(5000, 5000, new_reset)
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 5000

    def test_delayed_write_from_old_window_after_rollover_is_discarded(self, client, clean_redis):
        """The window-rollover gap this fix closes: a fresh post-rollover
        write lands first (full remaining, new window). A DELAYED write
        from before the rollover then arrives, with LOWER remaining than
        what's stored - the remaining-only check alone would have let it
        through - but its reset_time is strictly older, so it must still
        be discarded."""
        new_reset = datetime.now(timezone.utc) + timedelta(hours=1)
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], json.dumps({
            'remaining': 5000, 'limit': 5000,
            'reset_time': new_reset.isoformat(), 'last_updated': datetime.now(timezone.utc).isoformat(),
        }))

        old_reset = datetime.now(timezone.utc) - timedelta(minutes=1)
        bucket = _make_bucket(87, 5000, old_reset)  # lower remaining, but a stale window
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 5000  # unchanged - the correct, newer value survives

    def test_no_existing_value_always_applies(self, client, clean_redis):
        bucket = _make_bucket(42, 5000, datetime.now(timezone.utc) + timedelta(hours=1))
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 42

    def test_naive_bucket_reset_time_does_not_raise(self, client, clean_redis):
        """bucket.reset_time is naive everywhere else in this file
        (datetime.now()/datetime.fromtimestamp() with no tzinfo) - the
        out-of-order comparison against the UTC-aware stored value must
        not raise TypeError on naive-vs-aware comparison."""
        reset = datetime.now(timezone.utc) + timedelta(minutes=30)
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], json.dumps({
            'remaining': 4000, 'limit': 5000,
            'reset_time': reset.isoformat(), 'last_updated': datetime.now(timezone.utc).isoformat(),
        }))

        bucket = _make_bucket(3500, 5000, datetime.now() + timedelta(minutes=30))  # naive
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])  # must not raise

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 3500

    def test_written_value_has_utc_offset(self, client, clean_redis):
        """Cross-browser-timezone bug regression guard: the serialized
        reset_time/last_updated must carry an explicit UTC offset, not a
        naive string a browser would parse as local time."""
        bucket = _make_bucket(
            42, 5000,
            reset_time=datetime.now() + timedelta(hours=1),  # naive, as produced elsewhere
            last_updated=datetime.now(),  # naive
        )
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['reset_time'].endswith('+00:00')
        assert stored['last_updated'].endswith('+00:00')
        # And must round-trip back to an aware datetime without raising.
        assert datetime.fromisoformat(stored['reset_time']).tzinfo is not None

    def test_no_ttl_set(self, client, clean_redis):
        """The 2h TTL that used to undermine the never_observed/stale
        distinction is gone - the key persists indefinitely."""
        bucket = _make_bucket(42, 5000, datetime.now(timezone.utc) + timedelta(hours=1))
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])

        assert clean_redis.ttl(RATE_LIMIT_REDIS_KEYS['rest']) == -1  # -1 means "no TTL"

    def test_corrupted_existing_value_is_overwritten_not_fatal(self, client, clean_redis):
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], "not valid json{{{")

        bucket = _make_bucket(42, 5000, datetime.now(timezone.utc) + timedelta(hours=1))
        client._mirror_rate_limit_to_redis(bucket, RATE_LIMIT_REDIS_KEYS['rest'])  # must not raise

        stored = json.loads(clean_redis.get(RATE_LIMIT_REDIS_KEYS['rest']))
        assert stored['remaining'] == 42


class TestGetSharedRateLimitStatus:
    def test_never_observed_when_key_absent(self, clean_redis):
        result = get_shared_rate_limit_status('rest')
        assert result['never_observed'] is True
        assert result['unavailable'] is False
        assert result['stale'] is False
        assert result['percentage_used'] is None

    def test_fresh_reading_is_not_stale(self, clean_redis):
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['graphql'], json.dumps({
            'remaining': 4000, 'limit': 5000,
            'reset_time': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            'last_updated': datetime.now(timezone.utc).isoformat(),
        }))
        result = get_shared_rate_limit_status('graphql')
        assert result['never_observed'] is False
        assert result['unavailable'] is False
        assert result['stale'] is False
        assert result['remaining'] == 4000
        assert result['percentage_used'] == 20.0

    def test_old_reading_is_stale(self, clean_redis):
        old = datetime.now(timezone.utc) - timedelta(seconds=1000)  # > 900s threshold
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['graphql'], json.dumps({
            'remaining': 4000, 'limit': 5000,
            'reset_time': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            'last_updated': old.isoformat(),
        }))
        result = get_shared_rate_limit_status('graphql')
        assert result['never_observed'] is False
        assert result['unavailable'] is False
        assert result['stale'] is True

    def test_reading_just_under_threshold_is_not_stale(self, clean_redis):
        recent = datetime.now(timezone.utc) - timedelta(seconds=800)  # < 900s threshold
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['graphql'], json.dumps({
            'remaining': 4000, 'limit': 5000,
            'reset_time': (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
            'last_updated': recent.isoformat(),
        }))
        result = get_shared_rate_limit_status('graphql')
        assert result['stale'] is False

    def test_unavailable_on_redis_error_not_conflated_with_never_observed(self, clean_redis):
        """The core fix this class regression-tests: a Redis read failure
        must NOT look identical to a genuinely quiet bucket - conflating
        them silently hides an outage of the mechanism itself, and (per
        HealthMonitor.check_github()) used to silently disable
        degraded-health detection during exactly that outage."""
        with patch('services.github_api_client._get_shared_redis_client') as mock_get_client:
            mock_get_client.return_value.get.side_effect = ConnectionError("boom")
            result = get_shared_rate_limit_status('rest')
        assert result['unavailable'] is True
        assert result['never_observed'] is False

    def test_unavailable_on_corrupted_data(self, clean_redis):
        clean_redis.set(RATE_LIMIT_REDIS_KEYS['rest'], "not valid json{{{")
        result = get_shared_rate_limit_status('rest')
        assert result['unavailable'] is True
        assert result['never_observed'] is False

    def test_unknown_resource_type_raises(self, clean_redis):
        with pytest.raises(ValueError):
            get_shared_rate_limit_status('not_a_real_bucket')


class TestGraphqlIncludeHeaderPath:
    """graphql() always passes --include now; the header-parsing path must
    correctly update the GraphQL bucket from a realistic
    `gh api graphql --include` output, including on an error response."""

    def test_success_response_headers_update_graphql_bucket(self, client, clean_redis):
        include_stdout = (
            "HTTP/2.0 200 OK\n"
            "X-Ratelimit-Limit: 5000\n"
            "X-Ratelimit-Remaining: 4500\n"
            "X-Ratelimit-Reset: 1900000000\n"
            "\n"
            '{"data": {"viewer": {"login": "octocat"}}}'
        )
        mock_result = MagicMock(returncode=0, stdout=include_stdout, stderr="")

        with patch("subprocess.run", return_value=mock_result):
            success, data = client.graphql("query { viewer { login } }")

        assert success is True
        assert client.rate_limit_graphql.remaining == 4500
        assert client.rate_limit_graphql.limit == 5000

    def test_rate_limited_error_response_still_recovers_headers_and_reset_time(self, client, clean_redis):
        """The critical bug this fix closes: before, headers on a
        returncode==1 (error) response were never parsed at all, and
        _extract_reset_time() received raw --include stdout (headers +
        JSON) which always failed to json.loads(), so reset_time was
        always None and the breaker always fell back to a full 1-hour
        backoff regardless of GitHub's real, much shorter reset time."""
        reset_epoch = int(datetime.now(timezone.utc).timestamp()) + 42  # 42s from now
        include_stdout = (
            "HTTP/2.0 403 Forbidden\n"
            "X-Ratelimit-Remaining: 0\n"
            f"X-Ratelimit-Reset: {reset_epoch}\n"
            "\n"
            '{"errors": [{"message": "API rate limit exceeded"}]}'
        )
        mock_result = MagicMock(returncode=1, stdout=include_stdout, stderr="rate limit exceeded")

        with patch("subprocess.run", return_value=mock_result):
            success, data = client.graphql("query { viewer { login } }")

        assert success is False
        # The GraphQL bucket itself was updated from headers despite the error.
        assert client.rate_limit_graphql.remaining == 0
        # And the breaker was tripped using the real header-derived reset
        # time (~42s away), not the ~1-hour fallback a None reset_time
        # (the pre-fix behavior) would have produced.
        assert client.breaker.reset_time is not None
        seconds_until_reset = (client.breaker.reset_time - datetime.now()).total_seconds()
        assert 0 < seconds_until_reset < 120


class TestExtractResetTime:
    @pytest.fixture
    def bare_client(self):
        # Pure-ish function of its arguments - __new__ avoids the
        # constructor's Redis/background-thread setup.
        return GitHubAPIClient.__new__(GitHubAPIClient)

    def test_prefers_header_reset_time(self, bare_client):
        reset_epoch = int(datetime.now(timezone.utc).timestamp()) + 100
        result = bare_client._extract_reset_time(
            stdout='{"errors": [{"message": "API rate limit will be available in 9999 seconds"}]}',
            stderr="",
            headers={"x-ratelimit-reset": str(reset_epoch)},
        )
        assert result is not None
        seconds_until = (result - datetime.now()).total_seconds()
        assert 90 < seconds_until < 110  # from the header, not the 9999s in the body

    def test_falls_back_to_body_message_without_headers(self, bare_client):
        result = bare_client._extract_reset_time(
            stdout='{"errors": [{"message": "API rate limit will be available in 60 seconds"}]}',
            stderr="",
            headers=None,
        )
        assert result is not None
        seconds_until = (result - datetime.now()).total_seconds()
        assert 50 < seconds_until < 70

    def test_falls_back_to_body_message_when_headers_lack_reset(self, bare_client):
        result = bare_client._extract_reset_time(
            stdout='{"errors": [{"message": "API rate limit will be available in 60 seconds"}]}',
            stderr="",
            headers={"x-ratelimit-remaining": "0"},  # present, but no x-ratelimit-reset
        )
        assert result is not None
        seconds_until = (result - datetime.now()).total_seconds()
        assert 50 < seconds_until < 70

    def test_returns_none_when_nothing_parseable(self, bare_client):
        result = bare_client._extract_reset_time(stdout="not json", stderr="", headers=None)
        assert result is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

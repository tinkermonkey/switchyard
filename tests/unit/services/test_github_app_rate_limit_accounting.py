"""
Regression tests for #168: the GitHub App's rate-limit failures must be visible.

/health's api_call_stats reported `failed_requests: 0, rate_limited_requests: 0,
total_requests: 106` across a window that contained 16 logged
`services.github_app: GraphQL errors: [{'type': 'RATE_LIMIT', ...}]` lines. Two
distinct blind spots produced that:

  1. services/github_app.py authenticates as the GitHub App installation and
     talks to api.github.com directly, bypassing GitHubAPIClient entirely -- so
     none of its traffic, successful or not, ever reached those counters.
  2. GitHub answers a primary GraphQL rate limit with HTTP 200 and a RATE_LIMIT
     error in the response BODY. GitHubAPIClient.graphql()'s own body-error
     branch logged it and returned, incrementing neither counter.

A failure mode the circuit breaker and every rate-based alert are structurally
blind to is worse than the log noise it also causes, so the counting is pinned
first. The App-scoped hold is the second half: once GitHub says the budget is
exhausted every further query in that window is certain to fail, and issuing
them anyway is what produced 484 ERROR lines in three hours.

The App's budget is SEPARATE from the PAT budget every `gh` call spends -- an
operator can read 5000/5000 from `gh api rate_limit` while the App is fully
exhausted -- so the App's readings get their own bucket, and that separation is
pinned here too.
"""

import os
import pytest

if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

import logging
import time
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import requests
from requests.structures import CaseInsensitiveDict

from services.github_api_client import GitHubAPIClient, is_graphql_rate_limit_error
from services.github_app import CREDENTIAL_APP, CREDENTIAL_PAT, GitHubApp

RATE_LIMIT_ERRORS = [{
    'type': 'RATE_LIMIT',
    'code': 'graphql_rate_limit',
    'message': 'API rate limit already exceeded for installation ID 1234.',
}]


class TestRateLimitErrorClassification:

    def test_a_graphql_rate_limit_body_error_is_recognised(self):
        assert is_graphql_rate_limit_error(RATE_LIMIT_ERRORS)

    def test_a_mixed_error_array_is_not_reclassified_as_throttling(self):
        """A response that is partly rate-limited and partly something else
        still contains a real error that must not be downgraded."""
        assert not is_graphql_rate_limit_error(
            RATE_LIMIT_ERRORS + [{'type': 'FORBIDDEN', 'message': 'nope'}]
        )

    def test_other_errors_are_not_rate_limits(self):
        assert not is_graphql_rate_limit_error([{'type': 'NOT_FOUND'}])
        assert not is_graphql_rate_limit_error([])
        assert not is_graphql_rate_limit_error(None)


class TestClientBodyErrorAccounting:
    """GitHubAPIClient.graphql()'s own HTTP-200-with-errors path."""

    @staticmethod
    def _client():
        with patch.object(GitHubAPIClient, '_start_call_trace_summarizer'):
            return GitHubAPIClient()

    def _run(self, body):
        client = self._client()
        result = MagicMock(returncode=0, stdout=body, stderr='')
        with patch('services.github_api_client.subprocess.run', return_value=result):
            success, _ = client.graphql('query { viewer { login } }')
        assert success is False
        return client

    def test_a_body_rate_limit_increments_both_counters(self):
        """The regression: neither counter moved, so the client's own
        accounting could not see a rate limit it had just logged."""
        client = self._run(
            '{"errors": [{"type": "RATE_LIMIT", "code": "graphql_rate_limit"}]}'
        )

        assert client.rate_limited_requests == 1
        assert client.failed_requests == 1

    def test_a_non_rate_limit_body_error_counts_only_as_a_failure(self):
        client = self._run('{"errors": [{"type": "FORBIDDEN"}]}')

        assert client.rate_limited_requests == 0
        assert client.failed_requests == 1


def _app(enabled=True):
    """A GitHubApp holding a live cached installation token, so _get_token()
    returns it without touching the filesystem or GitHub's token endpoint --
    and so every request below is genuinely on the App credential, which is
    what decides whether a reading belongs to the App bucket."""
    app = object.__new__(GitHubApp)
    app.app_id = '1'
    app.installation_id = '2'
    app.private_key_path = '/dev/null'
    app.enabled = enabled
    app._installation_token = 'app-token' if enabled else None
    app._token_expires_at = (
        datetime.now(timezone.utc) + timedelta(hours=1) if enabled else None
    )
    app._graphql_holds = {
        CREDENTIAL_APP: {'until': None, 'reset_at': None, 'suppressed': 0},
        CREDENTIAL_PAT: {'until': None, 'reset_at': None, 'suppressed': 0},
    }
    return app


def _hold(app, credential=CREDENTIAL_APP):
    """The rate-limit hold state for one credential. Held per credential
    because the App installation's budget and the PAT's are separate: holding
    one on the other's exhaustion suppresses calls that would have worked."""
    return app._graphql_holds[credential]


def _response(status_code=200, json_body=None, headers=None):
    response = MagicMock()
    response.status_code = status_code
    response.headers = headers or {}
    response.json.return_value = json_body or {}
    response.raise_for_status.return_value = None
    return response


class TestAppCallsReachTheSharedAccounting:

    @staticmethod
    def _client():
        with patch.object(GitHubAPIClient, '_start_call_trace_summarizer'):
            return GitHubAPIClient()

    def _run_graphql(self, app, response, client):
        with patch('services.github_app.requests.post', return_value=response) as post, \
             patch('services.github_api_client.get_github_client', return_value=client):
            result = app.graphql_request('query { viewer { login } }')
        return result, post

    def test_a_rate_limited_app_query_is_counted(self):
        """The regression: this module never reported anything, so /health read
        rate_limited_requests: 0 through a window full of RATE_LIMIT errors."""
        app = _app()
        client = self._client()
        response = _response(json_body={'errors': RATE_LIMIT_ERRORS})

        result, _ = self._run_graphql(app, response, client)

        assert result is None
        assert client.rate_limited_requests == 1
        assert client.failed_requests == 1
        assert client.total_requests == 1

    def test_a_successful_app_query_is_counted_as_traffic(self):
        app = _app()
        client = self._client()
        response = _response(json_body={'data': {'viewer': {'login': 'bot'}}})

        result, _ = self._run_graphql(app, response, client)

        assert result == {'viewer': {'login': 'bot'}}
        assert client.total_requests == 1
        assert client.failed_requests == 0

    def test_the_app_budget_is_tracked_separately_from_the_pat_budget(self):
        """`gh api rate_limit` can read 5000/5000 off the PAT while the App is
        exhausted. Merging the two is the misdiagnosis #168 records."""
        app = _app()
        client = self._client()
        response = _response(
            json_body={'data': {}},
            headers={'x-ratelimit-limit': '5000', 'x-ratelimit-remaining': '11',
                     'x-ratelimit-resource': 'graphql'},
        )

        self._run_graphql(app, response, client)

        assert client.rate_limit_app_graphql.remaining == 11
        assert client.rate_limit_app_graphql.resource_type == 'graphql_app'
        # The PAT bucket must be untouched by an App reading.
        assert client.rate_limit_graphql.ever_updated is False
        assert client.rate_limit_graphql.remaining == 5000

    def test_the_app_bucket_is_exposed_in_the_client_status(self):
        """What /health renders as api_rate_limit_app_graphql."""
        status = self._client().get_status()

        assert status['rate_limit_app_graphql']['resource_type'] == 'graphql_app'


class TestAppGraphqlRateLimitHold:

    def _post_rate_limited(self, app, reset_header=None):
        headers = {'x-ratelimit-remaining': '0'}
        if reset_header is not None:
            headers['x-ratelimit-reset'] = reset_header
        response = _response(json_body={'errors': RATE_LIMIT_ERRORS}, headers=headers)
        with patch('services.github_app.requests.post', return_value=response) as post, \
             patch.object(app, '_report_call'):
            app.graphql_request('query { viewer { login } }')
        return post

    def test_further_queries_are_not_even_attempted_while_held(self):
        """The regression: every query in the exhausted window went out anyway
        and logged its own ERROR -- 484 of them in three hours."""
        app = _app()
        self._post_rate_limited(app)

        with patch('services.github_app.requests.post') as post, \
             patch.object(app, '_report_call'):
            assert app.graphql_request('query { viewer { login } }') is None

        post.assert_not_called()
        assert _hold(app)['suppressed'] == 1

    def test_the_exhaustion_is_reported_once_not_once_per_query(self, caplog):
        app = _app()

        with caplog.at_level(logging.DEBUG, logger='services.github_app'):
            self._post_rate_limited(app)
            with patch('services.github_app.requests.post'), \
                 patch.object(app, '_report_call'):
                for _ in range(5):
                    app.graphql_request('query { viewer { login } }')

        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        # And it names the distinction an operator needs to debug this.
        assert 'separate from the PAT budget' in warnings[0].getMessage()
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    def test_the_hold_expires_and_requests_resume(self):
        app = _app()
        self._post_rate_limited(app)

        # Wind the hold into the past rather than sleeping through it.
        _hold(app)['until'] = time.monotonic() - 1

        response = _response(json_body={'data': {'ok': True}})
        with patch('services.github_app.requests.post', return_value=response) as post, \
             patch.object(app, '_report_call'):
            assert app.graphql_request('query { ok }') == {'ok': True}

        post.assert_called_once()
        assert _hold(app)['until'] is None

    def test_the_reset_header_drives_the_hold_length(self):
        app = _app()
        reset_at = int(time.time()) + 600

        self._post_rate_limited(app, reset_header=str(reset_at))

        remaining = _hold(app)['until'] - time.monotonic()
        assert 500 < remaining <= 600

    def test_an_absurd_reset_header_is_capped(self):
        """A malformed header must not park the App offline indefinitely."""
        app = _app()

        self._post_rate_limited(app, reset_header=str(int(time.time()) + 86400))

        assert _hold(app)['until'] - time.monotonic() <= 3600

    def test_a_not_found_response_does_not_start_a_hold(self):
        """Only rate limiting suppresses traffic; ordinary errors must not."""
        app = _app()
        response = _response(json_body={'errors': [{'type': 'NOT_FOUND'}]})

        with patch('services.github_app.requests.post', return_value=response), \
             patch.object(app, '_report_call'):
            app.graphql_request('query { viewer { login } }')

        assert _hold(app)['until'] is None


class TestGithubsRealHeaderCasingReachesTheAppBucket:
    """GitHub sends `X-RateLimit-Remaining`; `requests` returns a
    CaseInsensitiveDict where the lowercase lookups in
    update_from_response_headers() work. A plain dict() copy of one keeps
    GitHub's casing, so every lookup misses -- silently, because that method
    stamps ever_updated regardless. The bucket then reports its 5000/5000
    constructor defaults as a fresh reading, i.e. a healthy App budget for a
    fully exhausted one: verbatim the misdiagnosis rate_limit_app_graphql was
    added to prevent.

    Every fixture in this file used to hand-build a lowercase plain dict, which
    no real response ever looks like -- so the casing was untested from both
    directions. These use GitHub's actual casing.
    """

    @staticmethod
    def _client():
        with patch.object(GitHubAPIClient, '_start_call_trace_summarizer'):
            return GitHubAPIClient()

    GITHUB_HEADERS = CaseInsensitiveDict({
        'X-RateLimit-Limit': '5000',
        'X-RateLimit-Remaining': '11',
        'X-RateLimit-Resource': 'graphql',
    })

    def test_an_app_reading_survives_the_trip_through_report_call(self):
        app = _app()
        client = self._client()
        response = _response(json_body={'data': {}}, headers=self.GITHUB_HEADERS)

        with patch('services.github_app.requests.post', return_value=response), \
             patch('services.github_api_client.get_github_client', return_value=client):
            app.graphql_request('query { viewer { login } }')

        assert client.rate_limit_app_graphql.limit == 5000
        assert client.rate_limit_app_graphql.remaining == 11
        # The tell that made this invisible: the bucket looked freshly updated
        # either way, so only the VALUE distinguishes broken from working.
        assert client.rate_limit_app_graphql.ever_updated is True
        assert client.rate_limit_app_graphql.resource_type == 'graphql_app'

    def test_the_bucket_normalises_casing_itself(self):
        """update_from_response_headers() is now reachable with a plain dict
        from more than one direction, so it must not depend on its caller
        having lowercased first."""
        from services.github_api_client import GitHubRateLimitStatus

        bucket = GitHubRateLimitStatus()
        bucket.update_from_response_headers(dict(self.GITHUB_HEADERS.items()))

        assert bucket.remaining == 11
        assert bucket.limit == 5000


class TestBrokenAppAuthDoesNotWriteTheAppBucket:
    """A 401 whose refresh fails leaves _installation_token None (the refresh
    invalidates it FIRST). With no PAT configured _get_token() also returns
    None, so a bare `token == self._installation_token` evaluates None == None
    and claims the App credential -- attributing the response's headers, which
    are GitHub's UNAUTHENTICATED 60/hr bucket, to the App.

    /health's api_rate_limit_app_graphql would then read a healthy-looking
    59/60 at the exact moment App auth is broken: the same wrong-but-plausible
    reading #168 was filed about, relocated.
    """

    @staticmethod
    def _client():
        with patch.object(GitHubAPIClient, '_start_call_trace_summarizer'):
            return GitHubAPIClient()

    UNAUTHENTICATED_HEADERS = CaseInsensitiveDict({
        'X-RateLimit-Limit': '60',
        'X-RateLimit-Remaining': '59',
    })

    def _run_401(self, app, client):
        response = _response(status_code=401, headers=self.UNAUTHENTICATED_HEADERS)
        response.raise_for_status.side_effect = requests.exceptions.HTTPError('401')

        env = {k: v for k, v in os.environ.items() if k != 'GITHUB_TOKEN'}
        # The first call resolves the cached installation token (so the request
        # genuinely goes out on the App credential); the refresh after the 401
        # fails, which is what leaves _installation_token None.
        with patch('services.github_app.requests.post', return_value=response), \
             patch.dict(os.environ, env, clear=True), \
             patch.object(app, 'get_installation_token',
                          side_effect=['app-token', None]), \
             patch('services.github_api_client.get_github_client', return_value=client):
            return app.graphql_request('query { viewer { login } }')

    def test_a_failed_refresh_does_not_attribute_the_60_hr_bucket_to_the_app(self):
        app = _app()
        client = self._client()

        assert self._run_401(app, client) is None

        # Never written at all -- an unauthenticated reading describes no
        # credential's real budget, least of all the App's.
        assert client.rate_limit_app_graphql.ever_updated is False
        assert client.rate_limit_app_graphql.limit == 5000

    def test_the_failure_is_still_counted(self):
        """Suppressing the bucket write must not suppress the failure itself --
        /health being blind to App failures is the other half of #168."""
        app = _app()
        client = self._client()

        self._run_401(app, client)

        assert client.failed_requests == 1
        assert client.total_requests == 1

    def test_a_pat_fallback_401_does_not_write_the_app_bucket_either(self):
        """The leg that already set used_app_token = False. A PAT's headers
        describe the budget `gh` spends; merging them is what the separate
        bucket exists to prevent."""
        app = _app()
        client = self._client()

        first = _response(status_code=401, headers=self.UNAUTHENTICATED_HEADERS)
        pat_response = _response(
            status_code=401,
            headers=CaseInsensitiveDict({'X-RateLimit-Limit': '5000',
                                         'X-RateLimit-Remaining': '4999'}),
        )
        pat_response.raise_for_status.side_effect = requests.exceptions.HTTPError('401')

        with patch('services.github_app.requests.post',
                   side_effect=[first, first, pat_response]), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'pat-token'}), \
             patch('services.github_api_client.get_github_client', return_value=client):
            assert app.graphql_request('query { viewer { login } }') is None

        assert client.rate_limit_app_graphql.ever_updated is False


class TestThePatLegIsHeldToo:
    """graphql_request() never checks self.enabled, so a PAT-configured
    deployment (a supported mode per CLAUDE.md) routes every discussions /
    human_feedback_loop / review_cycle / pr_review_stage query through it.
    Leaving that leg unheld reproduces the whole of #168 -- one ERROR per
    query, no backoff -- in a configuration the fix would not have covered.
    """

    def _post_rate_limited(self, app):
        response = _response(
            json_body={'errors': RATE_LIMIT_ERRORS},
            headers={'x-ratelimit-remaining': '0'},
        )
        with patch('services.github_app.requests.post', return_value=response), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'pat-token'}), \
             patch.object(app, '_report_call'):
            app.graphql_request('query { viewer { login } }')

    def test_five_rate_limited_pat_queries_produce_one_warning_and_one_request(self, caplog):
        app = _app(enabled=False)

        with caplog.at_level(logging.DEBUG, logger='services.github_app'):
            self._post_rate_limited(app)
            with patch('services.github_app.requests.post') as post, \
                 patch.dict(os.environ, {'GITHUB_TOKEN': 'pat-token'}), \
                 patch.object(app, '_report_call'):
                for _ in range(4):
                    app.graphql_request('query { viewer { login } }')
                post.assert_not_called()

        # The regression: five ERROR lines, one per query, and five requests.
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert len(warnings) == 1
        assert 'this is the PAT budget' in warnings[0].getMessage()
        assert _hold(app, CREDENTIAL_PAT)['suppressed'] == 4

    def test_the_two_budgets_are_held_independently(self):
        """Exhausting the PAT says nothing about the App's 5000/hr, and a
        shared hold would park a working credential offline."""
        app = _app(enabled=False)

        self._post_rate_limited(app)

        assert _hold(app, CREDENTIAL_PAT)['until'] is not None
        assert _hold(app, CREDENTIAL_APP)['until'] is None

    def test_a_pat_rate_limit_does_not_write_the_app_bucket(self):
        app = _app(enabled=False)
        with patch.object(GitHubAPIClient, '_start_call_trace_summarizer'):
            client = GitHubAPIClient()

        response = _response(
            json_body={'errors': RATE_LIMIT_ERRORS},
            headers=CaseInsensitiveDict({'X-RateLimit-Limit': '5000',
                                         'X-RateLimit-Remaining': '0'}),
        )
        with patch('services.github_app.requests.post', return_value=response), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'pat-token'}), \
             patch('services.github_api_client.get_github_client', return_value=client):
            app.graphql_request('query { viewer { login } }')

        assert client.rate_limited_requests == 1
        assert client.rate_limit_app_graphql.ever_updated is False


class TestHoldStatusIsReportable:
    """The hold, not either bucket, is what decides whether this module's
    GraphQL works while one is in force -- and on the PAT leg it is the ONLY
    signal, since a PAT-credential response updates no bucket at all. /health
    reported `degraded: false` for the entire hold window because it had no
    machine-readable view of this; get_graphql_hold_status() is that view."""

    def test_no_hold_reports_both_credentials_inactive(self):
        status = _app().get_graphql_hold_status()

        assert status[CREDENTIAL_APP]['active'] is False
        assert status[CREDENTIAL_PAT]['active'] is False
        assert status[CREDENTIAL_APP]['remaining_seconds'] is None
        assert status[CREDENTIAL_APP]['reset_at'] is None

    def test_an_active_hold_reports_its_remaining_time_and_suppression_count(self):
        app = _app()
        reset_at = datetime(2026, 9, 1, 1, 0, tzinfo=timezone.utc)
        _hold(app)['until'] = time.monotonic() + 1800
        _hold(app)['reset_at'] = reset_at
        _hold(app)['suppressed'] = 41

        status = app.get_graphql_hold_status()

        assert status[CREDENTIAL_APP]['active'] is True
        assert 1790 < status[CREDENTIAL_APP]['remaining_seconds'] <= 1800
        assert status[CREDENTIAL_APP]['reset_at'] == reset_at.isoformat()
        assert status[CREDENTIAL_APP]['suppressed_requests'] == 41
        # Independent budgets: the App's exhaustion says nothing about the PAT's.
        assert status[CREDENTIAL_PAT]['active'] is False

    def test_reading_the_status_does_not_clear_an_expired_hold(self):
        """Deliberately not routed through _graphql_hold_remaining(): that
        clears the hold and logs the 'expired, N requests skipped' summary,
        and a /health probe must not consume a state transition the request
        path is the one meant to report."""
        app = _app()
        _hold(app)['until'] = time.monotonic() - 5
        _hold(app)['suppressed'] = 7

        status = app.get_graphql_hold_status()

        assert status[CREDENTIAL_APP]['active'] is False
        assert status[CREDENTIAL_APP]['remaining_seconds'] is None
        # State untouched -- the next real request still reports the expiry.
        assert _hold(app)['until'] is not None
        assert _hold(app)['suppressed'] == 7

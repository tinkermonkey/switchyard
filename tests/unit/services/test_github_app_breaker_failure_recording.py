"""
Regression tests for failure-recording bugs found in code review of the
GitHub circuit breaker consolidation (PR #270), all in services/github_app.py
and services/github_app_auth.py's use of the shared github_app_breaker:

1. get_installation_token() (both modules), and get_app_info()/
   get_installation_info() (github_app_auth.py) only called
   record_github_app_failure() inside a `requests.exceptions.RequestException`
   handler -- a broken/revoked private key (jwt.encode() raising), or a
   malformed response body (KeyError/ValueError on data['token']/
   data['expires_at']), fell into a bare `except Exception` that never
   recorded a failure at all. This is exactly the scenario the breaker's own
   module docstring cites as motivating its existence, and it silently never
   tripped.

2. github_app.py's rest_request() and get_installation_token() (and, before
   this fix, all App-auth methods in both modules) called
   record_github_app_success() BEFORE parsing the response body. If
   response.json() (or a missing field) then raised, execution fell into the
   except block and ALSO recorded a failure -- a single call recording both
   outcomes. In CLOSED state this silently WIPES the accumulated
   failure_count (CircuitBreaker._on_success() resets it on any success), so
   a sustained run of malformed responses could never reach
   failure_threshold and never trip the breaker at all -- not just a
   half-open corruption, an outright "never trips" bug. A first version of
   this fix reordered rest_request() and github_app_auth.py's three methods
   but MISSED github_app.py's own get_installation_token(), which still had
   the bug; caught in a second round of review.

3. get_app_info()/get_installation_info()/graphql_request()/rest_request()
   count every requests.exceptions.HTTPError as a breaker failure EXCEPT for
   ordinary 4xx responses (a specific, expected answer about one GitHub
   resource, not outage evidence -- mirrors GitHubAPIClient.http_request()'s
   >=500-only rule). get_installation_token() in BOTH modules is
   deliberately NOT given that same exemption: it mints a credential, not a
   resource, so every 4xx there (401 bad JWT, 403 suspended/no permission,
   404 misconfigured installation ID, 422 validation) is a persistent,
   unrecoverable verdict on the App credential itself -- exactly the
   "revoked/expired private key, misconfigured installation" scenario the
   breaker module's own docstring names as its motivating case. A first
   version of this fix applied the >=500-only rule to get_installation_token()
   too, which would have left the breaker unable to trip for a revoked key;
   caught in a second round of review.

Assertion note: several tests below check `github_app_breaker.success_count`
was NOT incremented as proof a spurious success wasn't recorded.
CircuitBreaker.success_count (services/circuit_breaker.py) only increments
in HALF_OPEN state -- in the CLOSED state these tests run in, it is always 0
whether or not the bug is present, making `success_count == 0` a no-op
assertion that cannot catch the double-record bug. The correct signal is
`total_successes`, which increments in every state. This file was corrected
to use `total_successes` after review found the original assertions passed
against the still-buggy code.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests

from services.circuit_breaker import CircuitState
from services.github_app import GitHubApp
from services.github_app_auth import GitHubAppAuth
from services.github_app_breaker import github_app_breaker


@pytest.fixture(autouse=True)
def reset_breaker():
    github_app_breaker.state = CircuitState.CLOSED
    github_app_breaker.failure_count = 0
    github_app_breaker.success_count = 0
    github_app_breaker.total_failures = 0
    github_app_breaker.total_successes = 0
    github_app_breaker.last_failure_time = None
    yield
    github_app_breaker.state = CircuitState.CLOSED
    github_app_breaker.failure_count = 0
    github_app_breaker.success_count = 0
    github_app_breaker.total_failures = 0
    github_app_breaker.total_successes = 0
    github_app_breaker.last_failure_time = None


def _app():
    app = object.__new__(GitHubApp)
    app.app_id = '1'
    app.installation_id = '2'
    app.private_key_path = '/dev/null'
    app.private_key = 'not-a-real-key'
    app.enabled = True
    app._installation_token = None
    app._token_expires_at = None
    app._installation_permissions = None
    from services.github_app_credentials import CREDENTIAL_APP, CREDENTIAL_PAT
    app._graphql_holds = {
        CREDENTIAL_APP: {'until': None, 'reset_at': None, 'suppressed': 0},
        CREDENTIAL_PAT: {'until': None, 'reset_at': None, 'suppressed': 0},
    }
    return app


def _app_with_token():
    app = _app()
    app._installation_token = 'app-token'
    app._token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
    return app


def _auth():
    auth = object.__new__(GitHubAppAuth)
    auth.app_id = '1'
    auth.installation_id = '2'
    auth.private_key_path = None
    auth.private_key_content = None
    auth.private_key = 'not-a-real-key'
    auth.installation_token = None
    auth.token_expires_at = None
    return auth


def _http_error(status_code):
    response = MagicMock(status_code=status_code, text='error body')
    error = requests.exceptions.HTTPError(response=response)
    return error


class TestGithubAppFourXXNotCountedAsOutageForResourceEndpoints:
    """A 4xx is a specific, expected answer about one GitHub resource --
    must not trip the shared breaker, mirroring
    GitHubAPIClient.http_request()'s >=500-only rule. Does NOT apply to
    get_installation_token() -- see TestTokenMintCountsEvery4xx below."""

    @patch('services.github_app.requests.post')
    def test_graphql_request_404_does_not_record_failure(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=404, raise_for_status=MagicMock(side_effect=_http_error(404)),
        )
        app = _app_with_token()
        assert app.graphql_request('query { viewer { login } }') is None
        assert github_app_breaker.failure_count == 0

    @patch('services.github_app.requests.post')
    def test_graphql_request_500_records_failure(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=500, raise_for_status=MagicMock(side_effect=_http_error(500)),
        )
        app = _app_with_token()
        assert app.graphql_request('query { viewer { login } }') is None
        assert github_app_breaker.failure_count == 1

    @patch('services.github_app.requests.get')
    def test_rest_request_404_does_not_record_failure(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=404, text='not found',
            raise_for_status=MagicMock(side_effect=_http_error(404)),
        )
        app = _app_with_token()
        assert app.rest_request('GET', '/repos/o/r/issues/1') is None
        assert github_app_breaker.failure_count == 0

    @patch('services.github_app.requests.get')
    def test_rest_request_500_records_failure(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=500, text='server error',
            raise_for_status=MagicMock(side_effect=_http_error(500)),
        )
        app = _app_with_token()
        assert app.rest_request('GET', '/repos/o/r/issues/1') is None
        assert github_app_breaker.failure_count == 1

    @patch('services.github_app_auth.requests.get')
    def test_get_app_info_404_does_not_record_failure(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=404, raise_for_status=MagicMock(side_effect=_http_error(404)),
        )
        auth = _auth()
        with patch.object(auth, 'generate_jwt', return_value='dummy-jwt'):
            assert auth.get_app_info() is None
        assert github_app_breaker.failure_count == 0

    @patch('services.github_app_auth.requests.get')
    def test_get_app_info_500_records_failure(self, mock_get):
        mock_get.return_value = MagicMock(
            status_code=500, raise_for_status=MagicMock(side_effect=_http_error(500)),
        )
        auth = _auth()
        with patch.object(auth, 'generate_jwt', return_value='dummy-jwt'):
            assert auth.get_app_info() is None
        assert github_app_breaker.failure_count == 1


class TestTokenMintCountsEvery4xx:
    """get_installation_token() mints a credential, not a resource -- unlike
    the resource-fetching methods above, a 4xx here always means the App
    credential itself is broken (bad JWT, suspended/no permission,
    misconfigured installation, validation failure), so it must count even
    though it's a 4xx. Regression test for a gap found in a second round of
    code review of PR #270 (the first fix wrongly applied the >=500-only
    resource rule here too)."""

    @patch('services.github_app.requests.post')
    def test_github_app_installation_token_404_records_failure(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=404, raise_for_status=MagicMock(side_effect=_http_error(404)),
        )
        app = _app()
        with patch.object(app, '_generate_jwt', return_value='dummy-jwt'):
            assert app.get_installation_token() is None
        assert github_app_breaker.failure_count == 1

    @patch('services.github_app.requests.post')
    def test_github_app_installation_token_500_records_failure(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=500, raise_for_status=MagicMock(side_effect=_http_error(500)),
        )
        app = _app()
        with patch.object(app, '_generate_jwt', return_value='dummy-jwt'):
            assert app.get_installation_token() is None
        assert github_app_breaker.failure_count == 1

    @patch('services.github_app_auth.requests.post')
    def test_github_app_auth_installation_token_404_records_failure(self, mock_post):
        mock_post.return_value = MagicMock(
            status_code=404, raise_for_status=MagicMock(side_effect=_http_error(404)),
        )
        auth = _auth()
        with patch.object(auth, 'generate_jwt', return_value='dummy-jwt'):
            assert auth.get_installation_token() is None
        assert github_app_breaker.failure_count == 1


class TestGithubAppDoesNotDoubleRecord:
    """A malformed body after a successful HTTP response must record exactly
    one outcome (failure), never a success immediately followed by a
    failure for the same call. Asserts on total_successes/total_failures,
    not success_count/failure_count -- see module docstring."""

    @patch('services.github_app.requests.get')
    def test_rest_request_malformed_json_body_records_only_failure(self, mock_get):
        response = MagicMock(status_code=200, text='not-json')
        response.raise_for_status = MagicMock()
        response.json = MagicMock(side_effect=ValueError("Expecting value"))
        mock_get.return_value = response

        app = _app_with_token()
        assert app.rest_request('GET', '/repos/o/r/issues/1') is None
        assert github_app_breaker.total_failures == 1
        assert github_app_breaker.total_successes == 0

    @patch('services.github_app.requests.post')
    def test_installation_token_missing_field_records_only_failure(self, mock_post):
        response = MagicMock(status_code=200)
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={'no_token_here': True})
        mock_post.return_value = response

        app = _app()
        with patch.object(app, '_generate_jwt', return_value='dummy-jwt'):
            assert app.get_installation_token() is None
        assert github_app_breaker.total_failures == 1
        assert github_app_breaker.total_successes == 0

    def test_installation_token_sustained_malformed_responses_trip_the_breaker(self):
        """The behavior that actually matters: if a spurious success were
        still being recorded alongside each failure, its CLOSED-state
        failure_count reset would prevent the breaker from EVER reaching
        failure_threshold, no matter how many consecutive malformed
        responses occurred. Drives failure_threshold consecutive malformed
        responses and confirms the breaker actually opens."""
        app = _app()
        response = MagicMock(status_code=200)
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={'no_token_here': True})

        with patch('services.github_app.requests.post', return_value=response), \
             patch.object(app, '_generate_jwt', return_value='dummy-jwt'):
            for _ in range(github_app_breaker.failure_threshold):
                assert app.get_installation_token() is None

        assert github_app_breaker.state == CircuitState.OPEN
        assert github_app_breaker.total_successes == 0


class TestGithubAppGenericExceptionNowRecordsFailure:
    """A non-RequestException failure (a bad private key, a malformed
    successful response) must count -- this is the exact class of sustained
    App-auth failure the breaker exists to catch."""

    def test_broken_private_key_records_failure(self):
        app = _app()
        with patch.object(app, '_generate_jwt', side_effect=ValueError("bad key")):
            assert app.get_installation_token() is None
        assert github_app_breaker.failure_count == 1


class TestGithubAppAuthGenericExceptionNowRecordsFailure:
    """Same class of gap in the sibling module services/github_app_auth.py."""

    def test_get_installation_token_broken_key_records_failure(self):
        auth = _auth()
        with patch.object(auth, 'generate_jwt', side_effect=ValueError("bad key")):
            assert auth.get_installation_token() is None
        assert github_app_breaker.failure_count == 1

    @patch('services.github_app_auth.requests.post')
    def test_get_installation_token_missing_field_records_only_failure(self, mock_post):
        response = MagicMock(status_code=200)
        response.raise_for_status = MagicMock()
        response.json = MagicMock(return_value={'no_token_here': True})
        mock_post.return_value = response

        auth = _auth()
        with patch.object(auth, 'generate_jwt', return_value='dummy-jwt'):
            assert auth.get_installation_token() is None
        assert github_app_breaker.total_failures == 1
        assert github_app_breaker.total_successes == 0

    @patch('services.github_app_auth.requests.get')
    def test_get_app_info_malformed_json_records_only_failure(self, mock_get):
        response = MagicMock(status_code=200)
        response.raise_for_status = MagicMock()
        response.json = MagicMock(side_effect=ValueError("Expecting value"))
        mock_get.return_value = response

        auth = _auth()
        with patch.object(auth, 'generate_jwt', return_value='dummy-jwt'):
            assert auth.get_app_info() is None
        assert github_app_breaker.total_failures == 1
        assert github_app_breaker.total_successes == 0

    @patch('services.github_app_auth.requests.get')
    def test_get_installation_info_malformed_json_records_only_failure(self, mock_get):
        response = MagicMock(status_code=200)
        response.raise_for_status = MagicMock()
        response.json = MagicMock(side_effect=ValueError("Expecting value"))
        mock_get.return_value = response

        auth = _auth()
        auth.installation_token = 'tok'
        auth.token_expires_at = datetime.now(timezone.utc) + timedelta(hours=1)
        assert auth.get_installation_info() is None
        assert github_app_breaker.total_failures == 1
        assert github_app_breaker.total_successes == 0

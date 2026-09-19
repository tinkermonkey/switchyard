"""
Tests for the GitHub App auth path's circuit breaker (services/github_app_breaker.py),
wired into services/github_app.py and services/github_app_auth.py.

Context: audit found both modules made raw `requests.*` calls (JWT token
minting, GraphQL, REST) with NO breaker protection at all -- github_app.py
reported to GitHubAPIClient's accounting for metrics, but nothing ever gated
a call on breaker state, and github_app_auth.py had no accounting or
protection whatsoever. This is a fundamentally different credential flow
(JWT exchange for an installation token) than the PAT-routed calls
GitHubBreaker/gh_cli() protect, so it gets its own dedicated breaker instance
that both modules share (they spend the same App installation's quota).
"""
import os
from unittest.mock import MagicMock, patch

import pytest
import requests

from services.github_app_breaker import (
    github_app_breaker,
    check_github_app_breaker,
    record_github_app_success,
    record_github_app_failure,
)
from services.circuit_breaker import CircuitBreakerOpen, CircuitState


@pytest.fixture(autouse=True)
def reset_breaker():
    """The breaker is a module-level singleton shared across tests/modules --
    reset it before and after every test so state doesn't leak."""
    github_app_breaker.state = CircuitState.CLOSED
    github_app_breaker.failure_count = 0
    github_app_breaker.success_count = 0
    github_app_breaker.last_failure_time = None
    yield
    github_app_breaker.state = CircuitState.CLOSED
    github_app_breaker.failure_count = 0
    github_app_breaker.success_count = 0
    github_app_breaker.last_failure_time = None


class TestCheckGithubAppBreaker:
    def test_closed_breaker_does_not_raise(self):
        check_github_app_breaker()  # should not raise

    def test_open_breaker_raises(self):
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()
        assert github_app_breaker.state == CircuitState.OPEN
        with pytest.raises(CircuitBreakerOpen):
            check_github_app_breaker()

    def test_open_breaker_transitions_to_half_open_after_recovery_window(self):
        import datetime as dt
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()
        assert github_app_breaker.state == CircuitState.OPEN
        # Simulate the recovery window having elapsed.
        github_app_breaker.last_failure_time = (
            dt.datetime.now() - dt.timedelta(seconds=github_app_breaker.recovery_timeout + 1)
        )
        check_github_app_breaker()  # should not raise -- flips to half-open
        assert github_app_breaker.state == CircuitState.HALF_OPEN

    def test_success_resets_failure_count(self):
        record_github_app_failure()
        record_github_app_failure()
        record_github_app_success()
        assert github_app_breaker.failure_count == 0
        assert github_app_breaker.state == CircuitState.CLOSED


class TestGithubAppUsesSharedBreaker:
    """services/github_app.py's GitHubApp must actually check/record against
    the shared breaker, not just import it decoratively."""

    def _make_app(self):
        from services.github_app import GitHubApp
        app = GitHubApp.__new__(GitHubApp)
        app.app_id = "1"
        app.installation_id = "1"
        app.private_key_path = "/dev/null"
        app.private_key = "fake-key"
        app.enabled = True
        app._installation_token = None
        app._token_expires_at = None
        app._installation_permissions = None
        app._graphql_holds = {}
        return app

    def test_open_breaker_short_circuits_installation_token_request(self):
        app = self._make_app()
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()

        with patch("services.github_app.requests.post") as mock_post:
            result = app.get_installation_token()

        assert result is None
        mock_post.assert_not_called()

    def test_request_exception_trips_breaker_toward_threshold(self):
        app = self._make_app()
        with patch.object(app, "_generate_jwt", return_value="fake-jwt"), \
             patch("services.github_app.requests.post", side_effect=requests.exceptions.ConnectionError("down")):
            for _ in range(github_app_breaker.failure_threshold):
                assert app.get_installation_token() is None

        assert github_app_breaker.state == CircuitState.OPEN

    def test_successful_token_mint_records_success(self):
        app = self._make_app()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "token": "ghs_abc",
            "expires_at": "2099-01-01T00:00:00Z",
            "permissions": {},
        }
        mock_response.raise_for_status.return_value = None
        record_github_app_failure()
        record_github_app_failure()
        with patch.object(app, "_generate_jwt", return_value="fake-jwt"), \
             patch("services.github_app.requests.post", return_value=mock_response):
            token = app.get_installation_token()
        assert token == "ghs_abc"
        assert github_app_breaker.failure_count == 0


class TestGithubAppGraphqlAndRestBreakerWiring:
    """graphql_request()/rest_request() have their own internal 401-refresh
    and PAT-fallback retry logic -- these must still gate on the breaker at
    entry and record success/failure once per outer call, not per internal
    attempt."""

    def _make_app(self):
        from services.github_app import GitHubApp
        from services.github_app_credentials import CREDENTIAL_APP, CREDENTIAL_PAT

        app = GitHubApp.__new__(GitHubApp)
        app.app_id = None
        app.installation_id = None
        app.private_key_path = None
        app.enabled = False  # forces _get_token() straight to the PAT env var
        app._installation_token = None
        app._token_expires_at = None
        app._installation_permissions = None
        app._graphql_holds = {
            CREDENTIAL_APP: {'until': None, 'reset_at': None, 'suppressed': 0},
            CREDENTIAL_PAT: {'until': None, 'reset_at': None, 'suppressed': 0},
        }
        return app

    def test_open_breaker_short_circuits_graphql_request(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "pat-token")
        app = self._make_app()
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()

        with patch("services.github_app.requests.post") as mock_post:
            result = app.graphql_request("query { viewer { login } }")

        assert result is None
        mock_post.assert_not_called()

    def test_graphql_request_connection_error_trips_breaker(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "pat-token")
        app = self._make_app()
        with patch("services.github_app.requests.post", side_effect=requests.exceptions.ConnectionError("down")), \
             patch.object(app, "_report_call"):
            for _ in range(github_app_breaker.failure_threshold):
                assert app.graphql_request("query { viewer { login } }") is None

        assert github_app_breaker.state == CircuitState.OPEN

    def test_graphql_request_success_records_success(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "pat-token")
        app = self._make_app()
        record_github_app_failure()
        record_github_app_failure()
        mock_response = MagicMock(status_code=200)
        mock_response.json.return_value = {"data": {"viewer": {"login": "octo"}}}
        mock_response.raise_for_status.return_value = None

        with patch("services.github_app.requests.post", return_value=mock_response), \
             patch.object(app, "_report_call"):
            result = app.graphql_request("query { viewer { login } }")

        assert result == {"viewer": {"login": "octo"}}
        assert github_app_breaker.failure_count == 0

    def test_open_breaker_short_circuits_rest_request(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "pat-token")
        app = self._make_app()
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()

        with patch.object(app, "_rest_call") as mock_rest_call:
            result = app.rest_request("GET", "/repos/x/y")

        assert result is None
        mock_rest_call.assert_not_called()

    def test_rest_request_connection_error_trips_breaker(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "pat-token")
        app = self._make_app()
        with patch.object(app, "_rest_call", side_effect=requests.exceptions.ConnectionError("down")):
            for _ in range(github_app_breaker.failure_threshold):
                assert app.rest_request("GET", "/repos/x/y") is None

        assert github_app_breaker.state == CircuitState.OPEN

    def test_rest_request_success_records_success(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "pat-token")
        app = self._make_app()
        record_github_app_failure()
        record_github_app_failure()
        mock_response = MagicMock(status_code=200, text='{"ok": true}')
        mock_response.json.return_value = {"ok": True}
        mock_response.raise_for_status.return_value = None

        with patch.object(app, "_rest_call", return_value=mock_response):
            result = app.rest_request("GET", "/repos/x/y")

        assert result == {"ok": True}
        assert github_app_breaker.failure_count == 0


class TestGithubAppAuthGetAppInfoAndInstallationInfoBreakerWiring:
    """get_app_info()/get_installation_info() only got a test for
    get_installation_token() the first time around -- these close that gap."""

    def _make_auth(self):
        from services.github_app_auth import GitHubAppAuth
        auth = GitHubAppAuth.__new__(GitHubAppAuth)
        auth.app_id = "1"
        auth.installation_id = "1"
        auth.private_key_path = None
        auth.private_key_content = None
        auth.private_key = "fake-key"
        auth.installation_token = None
        auth.token_expires_at = None
        return auth

    def test_open_breaker_short_circuits_get_app_info(self):
        auth = self._make_auth()
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()

        with patch("services.github_app_auth.requests.get") as mock_get:
            result = auth.get_app_info()

        assert result is None
        mock_get.assert_not_called()

    def test_get_app_info_connection_error_trips_breaker(self):
        auth = self._make_auth()
        with patch.object(auth, "generate_jwt", return_value="fake-jwt"), \
             patch("services.github_app_auth.requests.get", side_effect=requests.exceptions.ConnectionError("down")):
            for _ in range(github_app_breaker.failure_threshold):
                assert auth.get_app_info() is None

        assert github_app_breaker.state == CircuitState.OPEN

    def test_get_app_info_success_records_success(self):
        auth = self._make_auth()
        record_github_app_failure()
        record_github_app_failure()
        mock_response = MagicMock()
        mock_response.json.return_value = {"name": "orchestrator-bot"}
        mock_response.raise_for_status.return_value = None

        with patch.object(auth, "generate_jwt", return_value="fake-jwt"), \
             patch("services.github_app_auth.requests.get", return_value=mock_response):
            result = auth.get_app_info()

        assert result == {"name": "orchestrator-bot"}
        assert github_app_breaker.failure_count == 0

    def test_open_breaker_short_circuits_get_installation_info_via_token_fetch(self):
        """get_installation_info() calls get_installation_token() first --
        with the breaker open, that inner call already short-circuits, so no
        request is ever made for either step."""
        auth = self._make_auth()
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()

        with patch("services.github_app_auth.requests.post") as mock_post, \
             patch("services.github_app_auth.requests.get") as mock_get:
            result = auth.get_installation_info()

        assert result is None
        mock_post.assert_not_called()
        mock_get.assert_not_called()

    def test_installation_info_own_breaker_check_when_token_already_cached(self):
        """Isolates get_installation_info()'s OWN check_github_app_breaker()
        call from get_installation_token()'s, by short-circuiting token
        retrieval entirely."""
        auth = self._make_auth()
        with patch.object(auth, "get_installation_token", return_value="cached-token"):
            for _ in range(github_app_breaker.failure_threshold):
                record_github_app_failure()
            with patch("services.github_app_auth.requests.get") as mock_get:
                result = auth.get_installation_info()

        assert result is None
        mock_get.assert_not_called()

    def test_installation_info_success_records_success(self):
        auth = self._make_auth()
        record_github_app_failure()
        record_github_app_failure()
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 123}
        mock_response.raise_for_status.return_value = None

        with patch.object(auth, "get_installation_token", return_value="cached-token"), \
             patch("services.github_app_auth.requests.get", return_value=mock_response):
            result = auth.get_installation_info()

        assert result == {"id": 123}
        assert github_app_breaker.failure_count == 0


class TestGithubAppAuthUsesSharedBreaker:
    """services/github_app_auth.py's GitHubAppAuth shares the SAME breaker
    instance as github_app.py -- both spend the same App installation quota."""

    def _make_auth(self):
        from services.github_app_auth import GitHubAppAuth
        auth = GitHubAppAuth.__new__(GitHubAppAuth)
        auth.app_id = "1"
        auth.installation_id = "1"
        auth.private_key_path = None
        auth.private_key_content = None
        auth.private_key = "fake-key"
        auth.installation_token = None
        auth.token_expires_at = None
        return auth

    def test_breaker_tripped_by_github_app_also_blocks_github_app_auth(self):
        """The whole point of sharing one instance: a sustained failure minting
        tokens via github_app.py's GitHubApp must also protect
        github_app_auth.py's GitHubAppAuth, since they spend the same quota."""
        for _ in range(github_app_breaker.failure_threshold):
            record_github_app_failure()

        auth = self._make_auth()
        with patch("services.github_app_auth.requests.post") as mock_post:
            result = auth.get_installation_token()

        assert result is None
        mock_post.assert_not_called()

    def test_connection_error_trips_shared_breaker(self):
        auth = self._make_auth()
        with patch.object(auth, "generate_jwt", return_value="fake-jwt"), \
             patch("services.github_app_auth.requests.post", side_effect=requests.exceptions.ConnectionError("down")):
            for _ in range(github_app_breaker.failure_threshold):
                assert auth.get_installation_token() is None
        assert github_app_breaker.state == CircuitState.OPEN

"""
Tests for GitHubAPIClient credential routing (WI-1..WI-6).

The behaviour under test is the thing #168 was filed about, generalised: a
rate-limit reading belongs to the (credential, resource) pair whose budget was
actually spent, and NOT to whichever transport produced it. Once
GitHubAPIClient can authenticate as either the PAT or a GitHub App
installation, "graphql headers -> the GraphQL bucket" is no longer a correct
rule, and every test here exists to keep the corrected rule from regressing.

The companion guard (WI-5) is covered at the bottom: a credential without
Projects permission must be detected from its GRANT, because the operation it
protects fails silently rather than loudly.
"""

import os
import pytest
from unittest.mock import patch, MagicMock

pytest.importorskip("requests")

from services.github_api_client import (  # noqa: E402
    GitHubAPIClient,
    CREDENTIAL_PREFERENCE_ENV,
    RATE_LIMIT_REDIS_KEYS,
    RATE_LIMIT_REDIS_KEYS_APP,
)
from services.github_app_credentials import CREDENTIAL_APP, CREDENTIAL_PAT  # noqa: E402


HEADERS = {
    'x-ratelimit-limit': '6000',
    'x-ratelimit-remaining': '4242',
    'x-ratelimit-reset': '1789070000',
}


@pytest.fixture
def client():
    """A client with the credential preference neutralised and Redis mirroring
    stubbed - these tests are about bucket selection, not about persistence."""
    saved = os.environ.get(CREDENTIAL_PREFERENCE_ENV)
    os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
    c = GitHubAPIClient()
    with patch.object(c, '_mirror_rate_limit_to_redis'):
        yield c
    if saved is None:
        os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
    else:
        os.environ[CREDENTIAL_PREFERENCE_ENV] = saved


# --------------------------------------------------------------- WI-4: preference

class TestCredentialPreference:
    def test_default_is_pat_so_existing_deployments_are_unchanged(self, client):
        """The whole change must be a no-op until explicitly opted into."""
        os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
        assert client._credential_preference() == CREDENTIAL_PAT
        assert client._resolve_credential() == CREDENTIAL_PAT

    def test_app_preference_resolves_to_app_when_app_is_configured(self, client):
        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'app'
        app = MagicMock(enabled=True)
        with patch.dict('sys.modules'), \
             patch('services.github_app.github_app', app):
            assert client._resolve_credential() == CREDENTIAL_APP

    def test_app_preference_falls_back_when_app_is_not_configured(self, client):
        """A deployment that sets the preference before finishing App setup
        must keep working on the PAT, not fail every call."""
        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'app'
        app = MagicMock(enabled=False)
        with patch('services.github_app.github_app', app):
            assert client._resolve_credential() == CREDENTIAL_PAT

    def test_unrecognised_preference_falls_back_rather_than_raising(self, client):
        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'not-a-credential'
        assert client._credential_preference() == CREDENTIAL_PAT


# ------------------------------------------------------- WI-3: bucket separation

class TestBucketGrid:
    def test_four_distinct_buckets_exist(self, client):
        buckets = [
            client._bucket(CREDENTIAL_PAT, 'graphql'),
            client._bucket(CREDENTIAL_PAT, 'rest'),
            client._bucket(CREDENTIAL_APP, 'graphql'),
            client._bucket(CREDENTIAL_APP, 'rest'),
        ]
        assert len({id(b) for b in buckets}) == 4

    def test_legacy_attributes_are_aliases_not_copies(self, client):
        """health_monitor, get_status() and the #168 tests all read the plain
        attributes; they must stay the same objects the index hands out."""
        assert client.rate_limit_graphql is client._bucket(CREDENTIAL_PAT, 'graphql')
        assert client.rate_limit_rest is client._bucket(CREDENTIAL_PAT, 'rest')
        assert client.rate_limit_app_graphql is client._bucket(CREDENTIAL_APP, 'graphql')
        assert client.rate_limit_app_rest is client._bucket(CREDENTIAL_APP, 'rest')
        assert client.rate_limit is client.rate_limit_graphql

    def test_app_rest_headers_do_not_touch_the_pat_rest_bucket(self, client):
        """The core #168 invariant, in the direction WI-3 newly makes possible."""
        before = client.rate_limit_rest.remaining
        client._update_rate_limit_from_http_headers(HEADERS, CREDENTIAL_APP)
        assert client.rate_limit_app_rest.remaining == 4242
        assert client.rate_limit_app_rest.resource_type == 'rest_app'
        assert client.rate_limit_rest.remaining == before
        assert client.rate_limit_rest.ever_updated is False

    def test_app_graphql_headers_do_not_touch_the_pat_graphql_bucket(self, client):
        before = client.rate_limit_graphql.remaining
        client._update_rate_limit_from_graphql_headers(HEADERS, CREDENTIAL_APP)
        assert client.rate_limit_app_graphql.remaining == 4242
        assert client.rate_limit_app_graphql.resource_type == 'graphql_app'
        assert client.rate_limit_graphql.remaining == before

    def test_pat_headers_still_land_in_the_pat_buckets(self, client):
        """Default argument keeps every pre-existing caller correct."""
        client._update_rate_limit_from_http_headers(HEADERS)
        client._update_rate_limit_from_graphql_headers(HEADERS)
        assert client.rate_limit_rest.remaining == 4242
        assert client.rate_limit_graphql.remaining == 4242
        assert client.rate_limit_app_rest.ever_updated is False
        assert client.rate_limit_app_graphql.ever_updated is False

    def test_redis_keys_are_credential_scoped(self, client):
        """App readings must never be published under the keys other processes
        read as 'the PAT budget'."""
        for resource in ('rest', 'graphql'):
            pat_key = client._redis_key_for(CREDENTIAL_PAT, resource)
            app_key = client._redis_key_for(CREDENTIAL_APP, resource)
            assert pat_key == RATE_LIMIT_REDIS_KEYS[resource]
            assert app_key == RATE_LIMIT_REDIS_KEYS_APP[resource]
            assert pat_key != app_key

    def test_get_status_exposes_the_new_bucket_and_credential(self, client):
        status = client.get_status()
        assert 'rate_limit_app_rest' in status
        assert status['credential'] in (CREDENTIAL_PAT, CREDENTIAL_APP)
        assert status['credential_preference'] in (CREDENTIAL_PAT, CREDENTIAL_APP)


class TestAlarms:
    def test_never_populated_buckets_do_not_alarm(self, client):
        """Unused buckets sit at the 5000/5000 constructor defaults; alarming
        on them would report a healthy budget for a credential that is never
        used, burying real alarms in noise."""
        client._last_alarm_check_at = None
        with patch('services.github_api_client.logger') as log:
            client.alarm_if_needed()
        assert not log.critical.called

    def test_exhausted_app_bucket_alarms_with_its_own_label(self, client):
        client._update_rate_limit_from_http_headers(
            {**HEADERS, 'x-ratelimit-remaining': '5'}, CREDENTIAL_APP
        )
        client._last_alarm_check_at = None
        with patch('services.github_api_client.logger') as log:
            client.alarm_if_needed()
        assert log.critical.called
        assert 'REST/app' in log.critical.call_args[0][0]


# --------------------------------------------------- WI-1/WI-6: token resolution

class TestTokenResolution:
    def test_app_token_is_injected_and_stale_pat_is_cleared(self, client):
        """A PAT left in GITHUB_TOKEN must not win over an injected App token -
        `gh` reads GH_TOKEN first, but clearing the other removes all doubt."""
        app = MagicMock(enabled=True)
        app.get_installation_token.return_value = 'ghs_installation'
        with patch('services.github_app.github_app', app), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'ghp_stale'}):
            env, used = client._auth_env(CREDENTIAL_APP)
        assert used == CREDENTIAL_APP
        assert env['GH_TOKEN'] == 'ghs_installation'
        assert 'GITHUB_TOKEN' not in env

    def test_app_failure_reports_pat_so_accounting_stays_honest(self, client):
        """The fallback must be VISIBLE in the returned credential: attributing
        PAT spend to the App bucket is the exact bug #168 records."""
        app = MagicMock(enabled=True)
        app.get_installation_token.return_value = None
        with patch('services.github_app.github_app', app), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'ghp_real'}):
            env, used = client._auth_env(CREDENTIAL_APP)
        assert used == CREDENTIAL_PAT
        assert env['GH_TOKEN'] == 'ghp_real'

    def test_app_exception_does_not_propagate(self, client):
        app = MagicMock(enabled=True)
        app.get_installation_token.side_effect = RuntimeError("boom")
        with patch('services.github_app.github_app', app), \
             patch.dict(os.environ, {'GITHUB_TOKEN': 'ghp_real'}):
            token, used = client._resolve_token(CREDENTIAL_APP)
        assert used == CREDENTIAL_PAT
        assert token == 'ghp_real'


# ------------------------------------------------------------- WI-5: the guard

class TestProjectsV2Guard:
    """A Projects v2 read made without Projects permission returns an EMPTY,
    SUCCESSFUL result - so board reconciliation reads every existing board as
    absent and duplicates it. The guard therefore has to inspect the grant."""

    def _probe(self, *args):
        from services.github_capabilities import GitHubCapabilities
        return GitHubCapabilities._probe_projects_v2_write(*args)

    def test_app_without_projects_permission_is_denied(self):
        app = MagicMock()
        app.get_installation_permissions.return_value = {
            'contents': 'write', 'issues': 'write', 'metadata': 'read',
        }
        with patch('services.github_app.github_app', app):
            ok, detail = self._probe(CREDENTIAL_APP, True, True)
        assert ok is False
        # the operator needs to know what WAS granted, to see what to add
        assert 'contents' in detail

    def test_app_with_org_projects_write_is_allowed(self):
        app = MagicMock()
        app.get_installation_permissions.return_value = {
            'organization_projects': 'write', 'metadata': 'read',
        }
        with patch('services.github_app.github_app', app):
            ok, _ = self._probe(CREDENTIAL_APP, True, True)
        assert ok is True

    def test_app_with_read_only_projects_is_denied(self):
        """Reconciliation writes columns; read access is not enough."""
        app = MagicMock()
        app.get_installation_permissions.return_value = {
            'organization_projects': 'read',
        }
        with patch('services.github_app.github_app', app):
            ok, _ = self._probe(CREDENTIAL_APP, True, True)
        assert ok is False

    def test_unknown_app_permissions_are_denied_not_assumed(self):
        app = MagicMock()
        app.get_installation_permissions.return_value = None
        with patch('services.github_app.github_app', app):
            ok, _ = self._probe(CREDENTIAL_APP, True, True)
        assert ok is False

    def test_pat_with_project_scope_is_allowed(self):
        result = MagicMock(stdout="HTTP/2 200\nx-oauth-scopes: repo, project\n\n{}")
        with patch('subprocess.run', return_value=result):
            ok, _ = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is True

    def test_pat_without_project_scope_is_denied(self):
        result = MagicMock(stdout="HTTP/2 200\nx-oauth-scopes: repo, read:org\n\n{}")
        with patch('subprocess.run', return_value=result):
            ok, detail = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is False
        assert 'project' in detail

    def test_fine_grained_pat_reporting_no_scopes_is_not_blocked(self):
        """A fine-grained PAT sends no x-oauth-scopes header at all. It may
        well have Projects access; this probe simply cannot prove it, and must
        not block reconciliation on a check that does not apply."""
        result = MagicMock(stdout="HTTP/2 200\ncontent-type: application/json\n\n{}")
        with patch('subprocess.run', return_value=result):
            ok, detail = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is True
        assert 'not verified' in detail

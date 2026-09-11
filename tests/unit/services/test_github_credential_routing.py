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
        with patch('services.github_app.github_app', app):
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
        os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
        status = client.get_status()
        assert 'rate_limit_app_rest' in status
        # Concrete values under a known preference -- `in (PAT, APP)` is true
        # for every value the method can return and asserts nothing.
        assert status['credential'] == CREDENTIAL_PAT
        assert status['credential_preference'] == CREDENTIAL_PAT


class TestAlarms:
    def test_never_populated_buckets_do_not_alarm(self, client):
        """Unused buckets sit at the 5000/5000 constructor defaults; alarming
        on them would report a healthy budget for a credential that is never
        used, burying real alarms in noise.

        Primes an EXHAUSTED-looking bucket while leaving ever_updated False, so
        this actually exercises the guard. Asserting on a pristine bucket would
        pass with the guard deleted -- 5000 remaining alarms at no level."""
        client.rate_limit_app_rest.remaining = 5
        client.rate_limit_app_rest.limit = 6000
        assert client.rate_limit_app_rest.ever_updated is False

        client._last_alarm_check_at = None
        with patch('services.github_api_client.logger') as log:
            client.alarm_if_needed()
        assert not log.critical.called
        assert not log.error.called

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
        result = MagicMock(returncode=0, stderr='',
                           stdout="HTTP/2 200\nx-oauth-scopes: repo, project\n\n{}")
        with patch('subprocess.run', return_value=result):
            ok, _ = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is True

    def test_pat_without_project_scope_is_denied(self):
        result = MagicMock(returncode=0, stderr='',
                           stdout="HTTP/2 200\nx-oauth-scopes: repo, read:org\n\n{}")
        with patch('subprocess.run', return_value=result):
            ok, detail = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is False
        assert 'project' in detail

    def test_fine_grained_pat_reporting_no_scopes_is_not_blocked(self):
        """A fine-grained PAT sends no x-oauth-scopes header at all. It may
        well have Projects access; this probe simply cannot prove it, and must
        not block reconciliation on a check that does not apply.

        Reachable ONLY on a successful call -- see the failure test below."""
        result = MagicMock(returncode=0, stderr='',
                           stdout="HTTP/2 200\ncontent-type: application/json\n\n{}")
        with patch('subprocess.run', return_value=result):
            ok, detail = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is True
        assert 'NOT VERIFIED' in detail

    def test_a_failed_probe_fails_closed_rather_than_open(self):
        """The regression that matters most here.

        A failed `gh api user` -- revoked token, 403, SSO not authorised, no
        network, gh missing -- produces empty stdout and therefore no
        x-oauth-scopes header, which is textually identical to a fine-grained
        PAT. Treating the two the same meant every one of those errors reported
        'this credential can write Projects v2': a guard against a silently
        destructive operation returning success precisely when it had learned
        nothing."""
        result = MagicMock(returncode=1, stdout='',
                           stderr='HTTP 401: Bad credentials')
        with patch('subprocess.run', return_value=result):
            ok, detail = self._probe(CREDENTIAL_PAT, False, True)
        assert ok is False
        assert 'could not verify' in detail.lower()
        assert 'Bad credentials' in detail

    def test_probe_runs_as_the_routed_credential(self):
        """The probe answers for the ACTIVE credential, so it must run as it --
        answering for a different token than reconciliation will use is the
        same class of mistake as discovering boards on one credential and
        creating them with another."""
        result = MagicMock(returncode=0, stderr='',
                           stdout="HTTP/2 200\nx-oauth-scopes: repo, project\n\n{}")
        with patch('subprocess.run', return_value=result) as run, \
             patch('services.github_api_client.routed_gh_env',
                   return_value={'GH_TOKEN': 'routed'}) as routed:
            self._probe(CREDENTIAL_PAT, False, True)
        assert routed.called
        assert run.call_args.kwargs['env'] == {'GH_TOKEN': 'routed'}


# ------------------------------------------- regression: the execution paths
#
# The original tests here covered the HELPERS and not the PATHS, which is
# exactly how the graphql-response bucket bug below reached review: every
# bucket-selection site was tested except the one that was wrong.

class TestGraphqlResponseBodyBucket:
    """`_update_rate_limit_from_graphql_response` must select by credential.

    It accepted a `credential` argument and ignored it, hardcoding the PAT
    bucket and the PAT Redis key. Reachable from graphql()'s `if not headers:`
    fallback, so an App call whose --include headers didn't parse published App
    spend under the key other processes read as the PAT budget -- #168's bug,
    on the one bucket path with no test.
    """

    BODY = {'extensions': {'cost': {'rateLimit': {
        'remaining': 222, 'limit': 6000, 'resetAt': '2026-01-01T00:00:00Z'}}}}

    def test_app_response_body_updates_the_app_bucket(self, client):
        with patch.object(client, '_mirror_rate_limit_to_redis') as mirror:
            client._update_rate_limit_from_graphql_response(self.BODY, CREDENTIAL_APP)

        assert client.rate_limit_app_graphql.remaining == 222
        assert client.rate_limit_app_graphql.resource_type == 'graphql_app'
        assert client.rate_limit_graphql.ever_updated is False, \
            "App reading contaminated the PAT bucket"
        assert mirror.call_args[0][1] == RATE_LIMIT_REDIS_KEYS_APP['graphql'], \
            "App reading published under the PAT's cross-process Redis key"

    def test_pat_response_body_still_updates_the_pat_bucket(self, client):
        with patch.object(client, '_mirror_rate_limit_to_redis') as mirror:
            client._update_rate_limit_from_graphql_response(self.BODY)

        assert client.rate_limit_graphql.remaining == 222
        assert client.rate_limit_app_graphql.ever_updated is False
        assert mirror.call_args[0][1] == RATE_LIMIT_REDIS_KEYS['graphql']


class TestCredentialReachesTheSubprocess:
    """`env=call_env` is threaded independently into three call sites. Testing
    `_auth_env` in isolation leaves all three free to drop it silently."""

    def _app_client(self, client):
        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'app'
        app = MagicMock(enabled=True)
        app.get_installation_token.return_value = 'ghs_installation'
        return app

    def test_graphql_passes_the_routed_env_to_subprocess(self, client):
        app = self._app_client(client)
        done = MagicMock(returncode=0, stdout='{"data":{}}', stderr='')
        with patch('services.github_app.github_app', app),              patch('subprocess.run', return_value=done) as run:
            client.graphql('query{viewer{login}}')
        assert run.call_args.kwargs['env']['GH_TOKEN'] == 'ghs_installation'

    def test_rest_passes_the_routed_env_to_subprocess(self, client):
        app = self._app_client(client)
        done = MagicMock(returncode=0, stdout='{}', stderr='')
        with patch('services.github_app.github_app', app),              patch('subprocess.run', return_value=done) as run:
            client.rest('GET', '/user')
        assert run.call_args.kwargs['env']['GH_TOKEN'] == 'ghs_installation'

    def test_gh_cli_passes_the_routed_env_to_subprocess(self, client):
        """The board path -- every Projects v2 operation goes through here."""
        app = self._app_client(client)
        done = MagicMock(returncode=0, stdout='{}', stderr='')
        with patch('services.github_app.github_app', app),              patch('subprocess.run', return_value=done) as run:
            client.gh_cli(['gh', 'project', 'list', '--owner', 'acme'])
        assert run.call_args.kwargs['env']['GH_TOKEN'] == 'ghs_installation'
        assert 'GITHUB_TOKEN' not in run.call_args.kwargs['env']


class TestHttpAuthorizationScheme:
    """An installation token is a Bearer credential. The PR comment says
    getting this wrong is 'a 401 on every App-routed HTTP call', and nothing
    asserted it."""

    def _response(self):
        r = MagicMock(status_code=200, headers={})
        r.json.return_value = {}
        return r

    def test_app_token_uses_bearer(self, client):
        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'app'
        app = MagicMock(enabled=True)
        app.get_installation_token.return_value = 'ghs_x'
        with patch('services.github_app.github_app', app),              patch('requests.get', return_value=self._response()) as get:
            client.http_request('GET', 'https://api.github.com/user')
        assert get.call_args.kwargs['headers']['Authorization'] == 'Bearer ghs_x'

    def test_pat_uses_the_token_scheme(self, client):
        os.environ.pop(CREDENTIAL_PREFERENCE_ENV, None)
        with patch.dict(os.environ, {'GH_TOKEN': 'ghp_y'}),              patch('requests.get', return_value=self._response()) as get:
            client.http_request('GET', 'https://api.github.com/user')
        assert get.call_args.kwargs['headers']['Authorization'] == 'token ghp_y'

    def test_app_fallback_to_pat_is_attributed_to_the_pat_bucket(self, client):
        """The fallback clause of the accounting invariant, end to end."""
        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'app'
        app = MagicMock(enabled=True)
        app.get_installation_token.return_value = None  # mint fails

        r = MagicMock(status_code=200, headers={
            'x-ratelimit-limit': '5000', 'x-ratelimit-remaining': '4321',
            'x-ratelimit-reset': '1789070000'})
        r.json.return_value = {}

        with patch('services.github_app.github_app', app),              patch.dict(os.environ, {'GH_TOKEN': 'ghp_real'}),              patch.object(client, '_mirror_rate_limit_to_redis'),              patch('requests.get', return_value=r):
            client.http_request('GET', 'https://api.github.com/user')

        assert client.rate_limit_rest.remaining == 4321
        assert client.rate_limit_app_rest.ever_updated is False, \
            "PAT spend was attributed to the App bucket"


class TestRoutedGhEnvHelper:
    """Credential routing that covers only GitHubAPIClient's own four methods
    is not credential routing: board DISCOVERY runs through raw `gh`
    subprocesses elsewhere, and a board this credential cannot see is
    indistinguishable from one that does not exist."""

    def test_returns_the_routed_token(self):
        from services.github_api_client import routed_gh_env

        os.environ[CREDENTIAL_PREFERENCE_ENV] = 'app'
        app = MagicMock(enabled=True)
        app.get_installation_token.return_value = 'ghs_routed'
        with patch('services.github_app.github_app', app):
            env = routed_gh_env()
        assert env['GH_TOKEN'] == 'ghs_routed'

    def test_never_raises(self):
        """Falls back to the ambient environment, i.e. pre-routing behaviour."""
        from services.github_api_client import routed_gh_env

        with patch('services.github_api_client.get_github_client',
                   side_effect=RuntimeError('boom')):
            env = routed_gh_env()
        assert isinstance(env, dict)

    def test_board_discovery_uses_it(self):
        """get_projects_list_for_owner is the read whose empty result triggers
        board creation."""
        import inspect
        import services.github_owner_utils as owner_utils

        src = inspect.getsource(owner_utils)
        assert src.count('env=routed_gh_env()') >= 2, (
            "board discovery queries must run as the routed credential")

    def test_board_verification_uses_it(self):
        import inspect
        import services.github_project_manager as gpm

        src = inspect.getsource(gpm)
        assert 'env=routed_gh_env()' in src

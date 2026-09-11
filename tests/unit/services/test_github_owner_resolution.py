"""The GitHub owner a call is addressed to must come from project config (#188, #131).

`GitHubIntegration` used to carry two owner fields with two different
resolutions: `repo_owner` preferred the caller's value, `github_org` read the
global GITHUB_ORG environment variable and nothing else. Every REST endpoint
the class builds read `github_org`, so the owner every real call site passes --
`GitHubIntegration(repo_owner=project_config.github['org'], ...)` -- was
accepted and then ignored.

Wherever GITHUB_ORG happened to hold the right org this was invisible.
docker-compose sets it on the orchestrator container. services/project_monitor.py
does NOT set it on the repair-cycle container it launches, so every GitHub call
made from inside a repair cycle addressed `/repos/None/<repo>/...`, 404'd, and
was logged to a --rm container's discarded stdout. That is #188: a repair
cycle's inner agent runs post no output, measured as 0 signed comments across
23 agent calls on documentation_robotics#909 and 3 on context-studio#1224.

The tests below pin the two halves of the fix independently, because either
alone would hide a regression in the other.
"""

import os
from unittest.mock import patch

import pytest

pytest.importorskip("requests")

from services.github_integration import GitHubIntegration  # noqa: E402


class _RecordingClient:
    """Captures the endpoint instead of calling GitHub."""

    def __init__(self):
        self.endpoints = []

    def rest(self, method=None, endpoint=None, data=None, **kwargs):
        self.endpoints.append((method, endpoint))
        return True, {'html_url': 'https://example.invalid/c/1', 'id': 1}


@pytest.fixture
def no_github_org(monkeypatch):
    """The repair-cycle container's environment: GITHUB_ORG simply absent."""
    monkeypatch.delenv('GITHUB_ORG', raising=False)


@pytest.fixture
def unconfigured_app(monkeypatch):
    """Keep construction off the GitHub App path; it is irrelevant here."""
    class _NoApp:
        def is_configured(self):
            return False

        def get_installation_token(self):
            return None

    monkeypatch.setattr(
        'services.github_app_auth.get_github_app_auth', lambda: _NoApp()
    )


class TestOwnerResolution:

    def test_passed_owner_wins_over_the_environment(self, unconfigured_app, monkeypatch):
        """The whole point: config beats the global env var, not the reverse.

        This is #131's latent multi-org bug as well -- a project in an org other
        than GITHUB_ORG was querying GITHUB_ORG's copy of it.
        """
        monkeypatch.setenv('GITHUB_ORG', 'wrong-org')
        gh = GitHubIntegration(repo_owner='right-org', repo_name='repo')
        assert gh.repo_owner == 'right-org'
        assert gh.github_org == 'right-org'

    def test_environment_is_the_fallback_when_nothing_is_passed(
        self, unconfigured_app, monkeypatch
    ):
        monkeypatch.setenv('GITHUB_ORG', 'fallback-org')
        gh = GitHubIntegration(repo_name='repo')
        assert gh.repo_owner == 'fallback-org'
        assert gh.github_org == 'fallback-org'

    def test_the_two_owner_fields_cannot_disagree(self, unconfigured_app, no_github_org):
        """They are one resolution under two names now.

        `github_org` is kept because callers outside the class read it
        (services/feature_branch_manager.py among them). If it ever becomes a
        second source of truth again, this fails.
        """
        gh = GitHubIntegration(repo_owner='some-org', repo_name='repo')
        assert gh.github_org == gh.repo_owner


class TestEndpointConstruction:

    def test_comment_is_addressed_to_the_configured_owner_without_github_org(
        self, unconfigured_app, no_github_org
    ):
        """The #188 regression test, in the repair container's exact environment."""
        client = _RecordingClient()
        gh = GitHubIntegration(repo_owner='tinkermonkey', repo_name='context-studio')

        with patch('services.github_integration.get_github_client', return_value=client):
            import asyncio
            result = asyncio.run(gh.post_issue_comment(1224, 'body text'))

        assert result['success'] is True
        assert client.endpoints == [
            ('POST', '/repos/tinkermonkey/context-studio/issues/1224/comments')
        ]

    def test_no_endpoint_is_ever_built_with_a_literal_none_owner(
        self, unconfigured_app, no_github_org
    ):
        """`/repos/None/<repo>` is a well-formed path, which is the problem.

        It reaches GitHub and comes back 404, indistinguishable from a deleted
        repo or a permissions gap. Refusing to build it is what turns a
        misconfiguration into one loud error instead of a silent, plausible
        404 on every call.
        """
        gh = GitHubIntegration(repo_name='some-repo')
        assert gh.repo_owner is None

        with pytest.raises(ValueError, match='no GitHub owner is resolved'):
            gh._repo_path()

    def test_missing_repo_is_refused_too(self, unconfigured_app, no_github_org):
        gh = GitHubIntegration(repo_owner='an-org')
        with pytest.raises(ValueError, match='no repo name'):
            gh._repo_path()

    def test_post_reports_failure_rather_than_addressing_none(
        self, unconfigured_app, no_github_org
    ):
        """A caller that cannot resolve an owner gets a failed result, not a 404.

        post_issue_comment catches its own exceptions and returns
        {'success': False}; what must not happen is a request going out.
        """
        client = _RecordingClient()
        gh = GitHubIntegration(repo_name='some-repo')

        with patch('services.github_integration.get_github_client', return_value=client):
            import asyncio
            result = asyncio.run(gh.post_issue_comment(1, 'body'))

        assert result['success'] is False
        assert client.endpoints == [], "no request may be sent without an owner"


class TestRepairCycleContainerEnvironment:
    """The other half: the container that exposed this must carry GITHUB_ORG.

    Fixing GitHubIntegration is the real fix -- it makes the owner come from
    project config, which is where it belongs. But other code running in that
    container still reads GITHUB_ORG directly (services/project_manager.py's
    project discovery), so the variable is forwarded as well.
    """

    def test_github_org_is_passed_to_the_repair_cycle_container(self):
        import inspect
        import services.project_monitor as pm

        source = inspect.getsource(pm)
        assert "'-e', f'GITHUB_ORG=" in source, (
            "the repair-cycle container's docker run no longer forwards "
            "GITHUB_ORG -- see #188 for what that cost last time"
        )

    def test_github_org_is_forwarded_from_the_environment_not_project_config(self):
        """It is a global by definition; this container is scoped to one project."""
        import inspect
        import services.project_monitor as pm

        source = inspect.getsource(pm)
        assert 'GITHUB_ORG={os.environ.get("GITHUB_ORG", "")}' in source

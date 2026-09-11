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

    def test_an_explicitly_passed_repo_beats_the_instance_default(
        self, unconfigured_app, no_github_org
    ):
        """`repo or self.repo_name`, in that order.

        Four call sites pass a repo that is not the instance's own -- every
        method taking a `repo=` argument does. Inverting the precedence would
        silently address the wrong repository under a resolved owner, which is
        the same class of defect as #188 and would not show up anywhere else in
        this file.
        """
        gh = GitHubIntegration(repo_owner='an-org', repo_name='default-repo')
        assert gh._repo_path('other-repo') == 'an-org/other-repo'
        assert gh._repo_path() == 'an-org/default-repo'

    def test_construction_without_any_owner_is_logged_at_error(
        self, unconfigured_app, no_github_org, caplog
    ):
        """That log IS the documented mitigation for constructing with no owner.

        The constructor deliberately does not raise -- callers that never build
        an endpoint should not be taken down -- so this record is the only
        notice anyone gets before _repo_path() refuses later. Asserting on the
        LEVEL, not the wording.
        """
        import logging

        with caplog.at_level(logging.ERROR, logger='services.github_integration'):
            GitHubIntegration(repo_name='some-repo')

        assert [r.levelname for r in caplog.records
                if r.name == 'services.github_integration'] == ['ERROR']

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


class TestUnresolvedOwnerKeepsEachMethodsContract:
    """_repo_path() refuses loudly; no method may turn that into a raise.

    The refusal is the point of the fix, but it has to arrive the way every
    other failure in this class arrives: an ERROR log and the value the method
    documents. Five methods did not get that for free. Four of them
    (has_agent_processed_issue, add_issue_label, create_issue_from_agent,
    mark_pr_ready) had no handler that catches ValueError at all -- three sit
    under `except subprocess.CalledProcessError` only, and mark_pr_ready
    resolves the path before its retry loop, outside any try. The fifth,
    get_feedback_comments, caught it but reported it at WARNING with the
    traceback parked at INFO.

    Every one of these is reached only when the owner cannot be resolved, i.e.
    exactly the repair-cycle container of #188, where a raise would abort the
    caller's remaining steps and WARNING/INFO go to discarded stdout.
    """

    @staticmethod
    def _ownerless(repo_name='some-repo'):
        return GitHubIntegration(repo_name=repo_name)

    def test_mark_pr_ready_returns_false(self, unconfigured_app, no_github_org, caplog):
        import asyncio
        import logging

        gh = self._ownerless()
        with caplog.at_level(logging.ERROR, logger='services.github_integration'):
            assert asyncio.run(gh.mark_pr_ready(7)) is False
        assert any('mark PR #7 ready' in r.message for r in caplog.records)

    def test_has_agent_processed_issue_returns_false(
        self, unconfigured_app, no_github_org
    ):
        import asyncio

        gh = self._ownerless()
        assert asyncio.run(
            gh.has_agent_processed_issue(1, 'some_agent', repo='a-repo')
        ) is False

    def test_add_issue_label_does_not_raise(self, unconfigured_app, no_github_org):
        import asyncio

        gh = self._ownerless()
        assert asyncio.run(gh.add_issue_label(1, ['bug'], repo='a-repo')) is None

    def test_create_issue_from_agent_reports_failure(
        self, unconfigured_app, no_github_org
    ):
        import asyncio

        gh = self._ownerless()
        result = asyncio.run(
            gh.create_issue_from_agent('t', 'b', repo='a-repo')
        )
        assert result['success'] is False

    def test_get_feedback_comments_reports_at_error_not_warning(
        self, unconfigured_app, no_github_org, caplog
    ):
        """Its generic handler logs at WARNING, which is right for a malformed
        comment body and wrong for "this instance cannot address any repo"."""
        import asyncio
        import logging

        gh = self._ownerless()
        with caplog.at_level(logging.DEBUG, logger='services.github_integration'):
            assert asyncio.run(gh.get_feedback_comments(1)) == []

        records = [r for r in caplog.records
                   if r.name == 'services.github_integration'
                   and 'feedback comments' in r.message]
        assert records and all(r.levelno >= logging.ERROR for r in records), \
            [(r.levelname, r.message) for r in records]

    def test_a_malformed_gh_response_is_not_swallowed_as_not_processed(
        self, unconfigured_app, no_github_org
    ):
        """The narrow placement of those handlers is load-bearing.

        json.JSONDecodeError IS a ValueError. A blanket `except ValueError`
        around has_agent_processed_issue's body would report "no agent has
        processed this issue" for a `gh` response it simply could not read --
        and that answer is what the dispatcher uses to decide whether to run an
        agent again.
        """
        import asyncio
        import json as _json
        from unittest.mock import MagicMock

        gh = GitHubIntegration(repo_owner='an-org', repo_name='a-repo')
        completed = MagicMock(returncode=0, stdout='not json at all', stderr='')

        with patch('services.github_integration.subprocess.run',
                   return_value=completed):
            with pytest.raises(_json.JSONDecodeError):
                asyncio.run(
                    gh.has_agent_processed_issue(1, 'some_agent', repo='a-repo')
                )


class TestRepairCycleContainerEnvironment:
    """The other half: the container that exposed this must carry GITHUB_ORG.

    Fixing GitHubIntegration is the real fix -- it makes the owner come from
    project config, which is where it belongs. But other code running in that
    container still reads GITHUB_ORG directly (services/project_manager.py's
    project discovery), so the variable is forwarded as well.
    """

    @staticmethod
    def _captured_docker_cmd(monkeypatch):
        """Run _launch_repair_cycle_container and return the argv it built.

        Asserted on the actual command rather than on the module's source text.
        A source grep passes against a line that has been commented out -- that
        is not a hypothetical: the earlier version of these two tests both went
        green with `# DISABLED: '-e', f'GITHUB_ORG=...'` in place and the
        container receiving nothing, i.e. #188 fully reintroduced under a clean
        suite.
        """
        import subprocess as _subprocess
        import services.project_monitor as pm

        captured = {}

        class _Runner:
            network_name = 'net'

            @staticmethod
            def _sanitize_container_name(name):
                return name

            @staticmethod
            def _detect_host_home_path():
                return '/host/home'

            def _detect_host_workspace_path(self):
                return '/host/workspace'

        monkeypatch.setattr('claude.docker_runner.DockerAgentRunner', _Runner)

        def _fake_run(cmd, **kwargs):
            captured['cmd'] = cmd
            return _subprocess.CompletedProcess(cmd, 0, stdout='deadbeefcafe\n',
                                                stderr='')

        monkeypatch.setattr(pm.subprocess, 'run', _fake_run)

        pm._launch_repair_cycle_container(
            project_name='context-studio',
            issue_number=1224,
            pipeline_run_id='run-abcdefgh',
            stage_name='Testing',
            context_file='/workspace/ctx.json',
            project_dir='/workspace/context-studio',
        )
        return captured['cmd']

    def test_github_org_is_passed_to_the_repair_cycle_container(self, monkeypatch):
        monkeypatch.setenv('GITHUB_ORG', 'the-org')

        cmd = self._captured_docker_cmd(monkeypatch)

        pairs = [(cmd[i], cmd[i + 1]) for i in range(len(cmd) - 1)
                 if cmd[i] == '-e']
        assert ('-e', 'GITHUB_ORG=the-org') in pairs, (
            "the repair-cycle container's docker run no longer forwards "
            f"GITHUB_ORG -- see #188 for what that cost last time. Got: {pairs}"
        )

    def test_github_org_is_forwarded_from_the_environment_not_project_config(
        self, monkeypatch
    ):
        """It is a global by definition; this container is scoped to one project.

        'context-studio' is the project being repaired and 'the-org' is what the
        orchestrator's own environment holds; the container must get the latter.
        """
        monkeypatch.setenv('GITHUB_ORG', 'the-org')

        cmd = self._captured_docker_cmd(monkeypatch)

        values = [cmd[i + 1] for i in range(len(cmd) - 1)
                  if cmd[i] == '-e' and cmd[i + 1].startswith('GITHUB_ORG=')]
        assert values == ['GITHUB_ORG=the-org']

"""
Tests for ProjectManager.discover_github_projects()'s migration onto
GitHubAPIClient.gh_cli() (GitHub circuit breaker consolidation, phase 1).

Before: a raw `subprocess.run(['gh', 'repo', 'list', ...])` call with no
circuit breaker protection at all. After: routed through gh_cli(), so a
sustained GitHub outage/rate-limit now backs this off like everything else.
"""
import os
from unittest.mock import MagicMock, patch

import pytest

from services.project_manager import ProjectManager
from services.github_api_client import GitHubBreaker, get_github_client


@pytest.fixture
def manager():
    return ProjectManager(projects_config_path="/nonexistent/projects.yaml")


@pytest.fixture(autouse=True)
def reset_breaker():
    client = get_github_client()
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None
    yield
    client.breaker.state = GitHubBreaker.CLOSED
    client.breaker._generic_failure_count = 0
    client.breaker.trip_reason = None


class TestDiscoverGithubProjects:
    def test_no_token_short_circuits_without_calling_gh(self, manager, monkeypatch):
        monkeypatch.delenv("GITHUB_TOKEN", raising=False)
        with patch("subprocess.run") as mock_run:
            result = manager.discover_github_projects()
        assert result == {}
        mock_run.assert_not_called()

    def test_no_org_short_circuits_without_calling_gh(self, manager, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.delenv("GITHUB_ORG", raising=False)
        with patch("subprocess.run") as mock_run:
            result = manager.discover_github_projects()
        assert result == {}
        mock_run.assert_not_called()

    def test_success_parses_repos_via_gh_cli(self, manager, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("GITHUB_ORG", "acme")
        mock_result = MagicMock(
            returncode=0,
            stdout='[{"name": "repo-a", "sshUrl": "git@github.com:acme/repo-a.git"}]',
            stderr="",
        )
        with patch("subprocess.run", return_value=mock_result) as mock_run:
            result = manager.discover_github_projects()
        assert result == {"repo-a": "git@github.com:acme/repo-a.git"}
        assert mock_run.call_args.args[0] == [
            "gh", "repo", "list", "acme", "--limit", "100", "--json", "name,sshUrl",
        ]

    def test_gh_failure_returns_empty_dict(self, manager, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("GITHUB_ORG", "acme")
        mock_result = MagicMock(returncode=1, stdout="", stderr="HTTP 404: Not Found")
        with patch("subprocess.run", return_value=mock_result):
            result = manager.discover_github_projects()
        assert result == {}

    def test_gh_exit_zero_with_non_json_stdout_returns_empty_dict(self, manager, monkeypatch):
        """gh_cli() falls back to raw stdout (not a raised exception) when gh
        exits 0 but stdout isn't valid JSON. Without the isinstance guard,
        `for repo in <str>` would iterate characters and `repo['name']` would
        raise TypeError instead of degrading gracefully like every other
        failure here."""
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("GITHUB_ORG", "acme")
        mock_result = MagicMock(returncode=0, stdout="not json at all", stderr="")
        with patch("subprocess.run", return_value=mock_result):
            result = manager.discover_github_projects()
        assert result == {}

    def test_open_circuit_breaker_short_circuits_without_calling_subprocess(self, manager, monkeypatch):
        """The whole point of the migration: an open breaker now protects
        this call site too, where before it had no protection at all."""
        monkeypatch.setenv("GITHUB_TOKEN", "x")
        monkeypatch.setenv("GITHUB_ORG", "acme")
        get_github_client().breaker.state = GitHubBreaker.OPEN
        get_github_client().breaker.reset_time = None

        with patch("subprocess.run") as mock_run:
            result = manager.discover_github_projects()

        assert result == {}
        mock_run.assert_not_called()

"""
Tests for mcp/server.py's migration onto GitHubAPIClient.gh_cli()
(GitHub circuit breaker consolidation, follow-up sweep).

mcp/server.py was already almost entirely protected -- _gh_graphql() and its
REST equivalent already route through get_github_client().graphql()/.rest().
The one gap was toggle_project_v2_workflow()'s owner-resolution fallback
(`gh api /user --jq .login`, used only when `owner` isn't supplied), a raw
subprocess.run with no breaker protection at all.
"""
import sys
import os
from unittest.mock import MagicMock, patch

import pytest

from services.github_api_client import GitHubBreaker, get_github_client

# Same import dance as tests/unit/test_mcp_server_board_scoped_lookup.py:
# mcp/server.py deliberately has no __init__.py alongside it (see its module
# docstring) so /app/mcp/ isn't treated as a regular package that would
# shadow the installed `mcp` SDK. sys.path is mutated only for the duration
# of this import, then restored, to avoid polluting later tests' import
# resolution (confirmed necessary by that sibling file's own comment).
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
_mcp_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..', 'mcp'))
sys.path.insert(0, _repo_root)
sys.path.insert(0, _mcp_dir)
try:
    import server as mcp_server
finally:
    sys.path.remove(_mcp_dir)
    sys.path.remove(_repo_root)


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


def _mock_result(stdout="", returncode=0, stderr=""):
    return MagicMock(returncode=returncode, stdout=stdout, stderr=stderr)


class TestToggleProjectV2WorkflowOwnerResolution:
    @pytest.mark.asyncio
    async def test_resolved_owner_is_used_in_the_graphql_query(self):
        """Confirms the gh_cli()-resolved login actually flows into the rest
        of the function, not just that gh_cli() was called. _query_project()
        swallows a _gh_graphql RuntimeError internally (tries org, then user,
        before giving up), so the observable outcome is the final "not
        found" ValueError once both lookups are exhausted -- proving
        resolution succeeded and the function proceeded past it."""
        result = _mock_result(stdout="octocat\n")
        with patch('subprocess.run', return_value=result), \
             patch.object(mcp_server, '_gh_graphql', side_effect=RuntimeError("stop here")) as mock_gql:
            with pytest.raises(ValueError, match="not found for owner 'octocat'"):
                await mcp_server.toggle_project_v2_workflow(
                    project_number=1, workflow_id="1", enabled=True, owner=None
                )
        assert 'login: "octocat"' in mock_gql.call_args_list[0].args[0]

    @pytest.mark.asyncio
    async def test_gh_failure_raises_with_stderr_detail(self):
        result = _mock_result(returncode=1, stderr="HTTP 401: Bad credentials")
        with patch('subprocess.run', return_value=result):
            with pytest.raises(RuntimeError, match="Bad credentials"):
                await mcp_server.toggle_project_v2_workflow(
                    project_number=1, workflow_id="1", enabled=True, owner=None
                )

    @pytest.mark.asyncio
    async def test_open_breaker_raises_without_calling_subprocess(self):
        """The whole point of the migration: an open breaker now protects
        this call site too, where before it had no protection at all."""
        client = get_github_client()
        client.breaker.state = GitHubBreaker.OPEN
        client.breaker.reset_time = None
        try:
            with patch('subprocess.run') as mock_run:
                with pytest.raises(RuntimeError):
                    await mcp_server.toggle_project_v2_workflow(
                        project_number=1, workflow_id="1", enabled=True, owner=None
                    )
            mock_run.assert_not_called()
        finally:
            client.breaker.state = GitHubBreaker.CLOSED
            client.breaker._generic_failure_count = 0
            client.breaker.trip_reason = None

    @pytest.mark.asyncio
    async def test_owner_supplied_skips_resolution_entirely(self):
        """No gh_cli()/subprocess call at all when owner is explicitly given."""
        with patch('subprocess.run') as mock_run, \
             patch.object(mcp_server, '_gh_graphql', side_effect=RuntimeError("stop here")):
            with pytest.raises(ValueError, match="not found for owner 'explicit-owner'"):
                await mcp_server.toggle_project_v2_workflow(
                    project_number=1, workflow_id="1", enabled=True, owner="explicit-owner"
                )
        mock_run.assert_not_called()

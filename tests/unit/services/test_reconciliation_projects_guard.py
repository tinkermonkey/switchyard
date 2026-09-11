"""
WI-5 guard: board reconciliation must refuse to run on a credential that
cannot write Projects v2.

Why a guard rather than letting it fail naturally: it does NOT fail naturally.
A Projects v2 query made without Projects permission returns an empty,
successful result (verified live - the same query returned 55 projects on a
PAT and 0 on an App installation token lacking the permission). Reconciliation
reads that empty result as "no board exists yet" and creates a fresh duplicate
of every configured board, on every startup, silently.

The guard is a SKIP, not a raise: a deployment whose Projects permission has
not been granted yet should keep running issues, PRs, discussions and agent
dispatch rather than refusing to boot.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

pytest.importorskip("requests")

from services.github_capabilities import GitHubCapability  # noqa: E402


@pytest.fixture
def manager():
    from services.github_project_manager import GitHubProjectManager
    m = GitHubProjectManager.__new__(GitHubProjectManager)
    m.state_manager = MagicMock()
    m.state_manager.needs_reconciliation.return_value = True
    m.state_manager.is_state_fresh.return_value = False
    m.state_manager.backup_state.return_value = None
    m.config_manager = MagicMock()
    return m


def _client_with_closed_breaker():
    client = MagicMock()
    client.breaker.is_open.return_value = False
    return client


@pytest.mark.asyncio
async def test_reconciliation_is_skipped_without_projects_write(manager):
    caps = MagicMock()
    caps.has_capability.return_value = False
    caps.get_status.return_value = {'projects_v2_detail': 'no projects permission'}

    with patch('services.github_project_manager.get_github_client',
               return_value=_client_with_closed_breaker()), \
         patch('services.github_capabilities.github_capabilities', caps):
        result = await manager.reconcile_project('demo')

    assert result is False
    # The decisive assertion: it must not have gone on to read config and
    # start creating boards.
    manager.config_manager.get_project_config.assert_not_called()
    caps.has_capability.assert_called_with(GitHubCapability.PROJECTS_V2_WRITE)


@pytest.mark.asyncio
async def test_reconciliation_proceeds_when_projects_write_is_available(manager):
    """The guard must not become a blanket refusal - a correctly permissioned
    credential has to reach the normal path."""
    caps = MagicMock()
    caps.has_capability.return_value = True

    project_config = MagicMock()
    project_config.pipelines = []
    manager.config_manager.get_project_config.return_value = project_config

    with patch('services.github_project_manager.get_github_client',
               return_value=_client_with_closed_breaker()), \
         patch('services.github_capabilities.github_capabilities', caps), \
         patch.object(manager, '_reconcile_labels', new=AsyncMock(return_value=True)):
        result = await manager.reconcile_project('demo')

    assert result is True
    manager.config_manager.get_project_config.assert_called_once_with('demo')

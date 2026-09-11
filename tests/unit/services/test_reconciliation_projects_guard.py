"""
Board reconciliation must refuse to run on a credential that cannot write
Projects v2 -- and that refusal must not take the orchestrator down with it.

Why a guard at all: a Projects v2 query made without the permission returns an
empty, SUCCESSFUL result (verified live -- the same query returned 55 projects
on a PAT and 0 on an App installation token lacking it). Reconciliation reads
that as "no board exists yet" and creates a fresh duplicate of every configured
board, on every startup, silently.

Why it raises rather than returning False: the condition is CREDENTIAL-global,
so it is true for every project the instant it is true for one. Returning False
made it indistinguishable from a per-project failure, and main.py counts those
and exits(1) when they cover every project -- turning a missing permission into
a boot crash-loop, which is the exact opposite of the guard's purpose.
"""

import pytest
from unittest.mock import patch, MagicMock, AsyncMock

pytest.importorskip("requests")

from services.github_capabilities import GitHubCapability  # noqa: E402
from services.github_project_manager import (  # noqa: E402
    ProjectsPermissionUnavailable,
)


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


def _caps(can_write, detail='no projects permission'):
    caps = MagicMock()
    caps.has_capability.return_value = can_write
    caps.get_status.return_value = {'projects_v2_detail': detail}
    return caps


@pytest.mark.asyncio
async def test_refusal_raises_rather_than_returning_false(manager):
    caps = _caps(False)
    with patch('services.github_project_manager.get_github_client',
               return_value=_client_with_closed_breaker()), \
         patch('services.github_capabilities.github_capabilities', caps):
        with pytest.raises(ProjectsPermissionUnavailable) as excinfo:
            await manager.reconcile_project('demo')

    # The decisive assertion: it must not have gone on to read config and
    # start creating boards.
    manager.config_manager.get_project_config.assert_not_called()
    caps.has_capability.assert_called_with(GitHubCapability.PROJECTS_V2_WRITE)
    # The operator-facing reason has to survive into the exception, since that
    # is the only thing telling them why board management stopped.
    assert 'no projects permission' in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_broad_handler_does_not_swallow_the_refusal(manager):
    """reconcile_project wraps its body in `except Exception: return False`.
    If that catches the refusal, the crash-loop comes straight back."""
    caps = _caps(False)
    with patch('services.github_project_manager.get_github_client',
               return_value=_client_with_closed_breaker()), \
         patch('services.github_capabilities.github_capabilities', caps):
        with pytest.raises(ProjectsPermissionUnavailable):
            await manager.reconcile_project('demo')


@pytest.mark.asyncio
async def test_reconciliation_proceeds_when_projects_write_is_available(manager):
    """The guard must not become a blanket refusal."""
    caps = _caps(True)
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


@pytest.mark.asyncio
async def test_a_genuine_failure_still_returns_false(manager):
    """The refusal must be distinguishable from a real failure, not replace it."""
    caps = _caps(True)
    manager.config_manager.get_project_config.side_effect = RuntimeError('boom')

    with patch('services.github_project_manager.get_github_client',
               return_value=_client_with_closed_breaker()), \
         patch('services.github_capabilities.github_capabilities', caps):
        result = await manager.reconcile_project('demo')

    assert result is False


class TestStartupHandling:
    """The consequence the original guard test missed entirely: it asserted
    reconcile_project's return value and never exercised main.py, which is
    where the exit(1) lives."""

    def test_main_treats_the_refusal_as_a_skip_not_a_failure(self):
        import inspect
        import main

        src = inspect.getsource(main)
        assert 'except ProjectsPermissionUnavailable' in src, (
            "main.py must handle the refusal explicitly; counting it as a "
            "reconcile failure reinstates the exit(1) crash-loop")
        # The skip counter must not feed the failure tally that gates exit(1).
        skip_block = src.split('except ProjectsPermissionUnavailable', 1)[1].split('if not success')[0]
        assert 'failure_count += 1' not in skip_block
        assert 'projects_permission_skips += 1' in skip_block
        assert 'continue' in skip_block

    def test_skip_counter_is_not_reset_each_iteration(self):
        """A Projects refusal is credential-global, so only a loop-scoped total
        says anything about it. (failure_count's own per-iteration reset is
        issue #192 and deliberately untouched here.)"""
        import inspect
        import main

        src = inspect.getsource(main)
        init = src.index('projects_permission_skips = 0')
        loop = src.index('for project_name in projects:')
        assert init < loop, "skip counter must be initialised outside the loop"

"""
project_checkout_lock_if_shared_async() -- the shared base-clone guard (#140 item 4).

The guard it replaces was copy-pasted near-identically at four call sites
(claude/claude_integration.py x2, services/auto_commit.py x1,
services/feature_branch_manager.py x1). #140 item 4 counted three -- the
feature_branch_manager.py copy was added later, by #151/WI-6, which is the
clearest demonstration of why this belongs in one place:

    if workspace_manager.is_base_clone_dir(project, work_dir):
        async with project_checkout_lock_async(project, issue_number):
            return await do_the_work(...)
    return await do_the_work(...)

Each copy is a place a future call site can forget the guard, or fix one branch
and miss the other. These tests pin the centralized version's contract: it locks
exactly when the directory IS the shared base clone, takes nothing when it is
not, yields which of the two happened (auto_commit branches on that to decide
whether it must re-read the checked-out branch after the wait), and propagates
a lock timeout rather than falling through to the unlocked path.
"""

import asyncio
from contextlib import asynccontextmanager
from unittest.mock import Mock, patch

import pytest

from services.project_checkout_lock import (
    ProjectCheckoutLockTimeoutError,
    project_checkout_lock_if_shared_async,
)


@asynccontextmanager
async def _recording_lock(calls, *args, **kwargs):
    calls.append((args, kwargs))
    yield


@pytest.fixture
def workspace_manager():
    """Patched where the guard imports it -- function-locally from
    services.project_workspace, to avoid the import cycle that a module-level
    import would close."""
    manager = Mock()
    with patch('services.project_workspace.workspace_manager', manager):
        yield manager


@pytest.mark.asyncio
class TestSharedBaseCloneIsLocked:

    async def test_yields_true_and_takes_the_lock(self, workspace_manager):
        workspace_manager.is_base_clone_dir.return_value = True
        calls = []

        with patch('services.project_checkout_lock.project_checkout_lock_async',
                   lambda *a, **k: _recording_lock(calls, *a, **k)):
            async with project_checkout_lock_if_shared_async(
                'proj', '/workspace/proj', 42
            ) as is_shared:
                assert is_shared is True

        assert len(calls) == 1
        args, _ = calls[0]
        assert args[0] == 'proj'
        # Log attribution only -- never the lock's holder identity.
        assert args[1] == 42

    async def test_decision_is_made_on_the_caller_supplied_directory(
        self, workspace_manager
    ):
        workspace_manager.is_base_clone_dir.return_value = True

        with patch('services.project_checkout_lock.project_checkout_lock_async',
                   lambda *a, **k: _recording_lock([], *a, **k)):
            async with project_checkout_lock_if_shared_async('proj', '/workspace/proj'):
                pass

        workspace_manager.is_base_clone_dir.assert_called_once_with(
            'proj', '/workspace/proj'
        )

    async def test_lock_kwargs_are_forwarded(self, workspace_manager):
        """Tests inject a facade/timeout through the guard rather than around it."""
        workspace_manager.is_base_clone_dir.return_value = True
        calls = []
        facade = Mock()

        with patch('services.project_checkout_lock.project_checkout_lock_async',
                   lambda *a, **k: _recording_lock(calls, *a, **k)):
            async with project_checkout_lock_if_shared_async(
                'proj', '/workspace/proj', None, timeout_seconds=1.0, facade=facade
            ):
                pass

        _, kwargs = calls[0]
        assert kwargs['timeout_seconds'] == 1.0
        assert kwargs['facade'] is facade

    async def test_a_lock_timeout_propagates_rather_than_running_unlocked(
        self, workspace_manager
    ):
        """The whole point of the lock: a timeout must reach the caller's
        contention path, never degrade into "run it anyway"."""
        workspace_manager.is_base_clone_dir.return_value = True
        ran = []

        @asynccontextmanager
        async def _timing_out(*args, **kwargs):
            raise ProjectCheckoutLockTimeoutError("busy")
            yield  # pragma: no cover

        with patch('services.project_checkout_lock.project_checkout_lock_async', _timing_out):
            with pytest.raises(ProjectCheckoutLockTimeoutError):
                async with project_checkout_lock_if_shared_async('proj', '/workspace/proj'):
                    ran.append(True)

        assert ran == [], "the guarded body must not run when the lock was not acquired"


@pytest.mark.asyncio
class TestEpicWorktreeIsNotLocked:

    async def test_yields_false_and_takes_nothing(self, workspace_manager):
        """An epic worktree shares its directory with nothing, so locking it
        would serialize sibling epics for no reason."""
        workspace_manager.is_base_clone_dir.return_value = False
        calls = []
        ran = []

        with patch('services.project_checkout_lock.project_checkout_lock_async',
                   lambda *a, **k: _recording_lock(calls, *a, **k)):
            async with project_checkout_lock_if_shared_async(
                'proj', '/workspace/proj-epic-7'
            ) as is_shared:
                assert is_shared is False
                ran.append(True)

        assert calls == []
        assert ran == [True]

    async def test_body_exception_propagates_with_no_lock_taken(self, workspace_manager):
        workspace_manager.is_base_clone_dir.return_value = False

        with pytest.raises(ValueError):
            async with project_checkout_lock_if_shared_async('proj', '/workspace/x'):
                raise ValueError("boom")

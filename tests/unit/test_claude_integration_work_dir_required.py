"""
Unit tests for claude_integration._require_work_dir() (issue #151/WI-6 item 12,
from #140 item 12).

is_base_clone_dir() fails CLOSED for a directory that doesn't exist -- it treats
one as the shared base clone rather than silently skipping the project_checkout
lock -- and its docstring cited claude_integration's own
`Path(context.get('work_dir', '.'))` as the case that motivated it. That default
was the one case it could never catch: '.' resolves to the orchestrator's own
cwd, which always exists, so a context with no work_dir fell straight through to
the real comparison, came back False, and ran the local (non-Docker) branch
UNLOCKED. The same default independently pointed _run_claude_code_locally() at
the orchestrator's own checkout as the agent's working directory.

The fix is at the call site, not in is_base_clone_dir(), which cannot tell a
deliberate '.' from a defaulted one: a missing work_dir is a construction bug
upstream (every production producer sets it) and is refused.
"""

import pytest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from claude.claude_integration import _require_work_dir, run_claude_code


@asynccontextmanager
async def _noop_lock(*args, **kwargs):
    yield


def _local_execution_context(**overrides):
    """A use_docker=False context, i.e. the local-execution branch that reads
    work_dir."""
    context = {
        'agent': 'dev_environment_setup',
        'task_id': 'task-1',
        'project': 'test-project',
        'use_docker': False,
        'context': {'issue_number': 42},
    }
    context.update(overrides)
    return context


class TestRequireWorkDir:

    def test_returns_the_configured_directory(self):
        assert _require_work_dir({'work_dir': '/workspace/test-project'}, 'a') == \
            Path('/workspace/test-project')

    def test_missing_work_dir_is_refused(self):
        """THE regression: this used to silently become Path('.')."""
        with pytest.raises(Exception) as exc_info:
            _require_work_dir({'project': 'test-project'}, 'dev_environment_setup')
        assert 'work_dir' in str(exc_info.value)

    def test_empty_work_dir_is_refused(self):
        """An empty/whitespace value is the same missing-field bug wearing a
        different shape, and resolves to the orchestrator's cwd just as '.' does."""
        for empty in ('', '   ', None):
            with pytest.raises(Exception):
                _require_work_dir({'work_dir': empty}, 'dev_environment_setup')

    def test_the_refusal_names_the_context_for_diagnosis(self):
        """The caller is a bug upstream, so the message has to say enough to find
        it -- matching the existing project='unknown' refusal in this same file."""
        with pytest.raises(Exception) as exc_info:
            _require_work_dir({'project': 'p', 'task_id': 't'}, 'some_agent')
        message = str(exc_info.value)
        assert 'some_agent' in message
        assert 'task_id' in message


@pytest.mark.asyncio
class TestLocalExecutionRefusesAMissingWorkDir:

    async def test_no_lock_is_taken_and_the_agent_never_runs(self):
        """Refused BEFORE the dev_container_build lock, so a malformed context
        never takes a lock it would abandon a line later -- and never reaches the
        unlocked local execution the missing work_dir used to permit."""
        context = _local_execution_context()
        context.pop('work_dir', None)

        # Patched where the lock module imports it (#140 item 4 moved the
        # is_base_clone_dir() decision out of claude_integration and into
        # project_checkout_lock_if_shared_async(), which imports
        # workspace_manager function-locally to avoid an import cycle).
        with patch('services.dev_container_build_lock.dev_container_build_lock_async',
                   _noop_lock) as _build_lock, \
             patch('services.project_workspace.workspace_manager') as mock_wm, \
             patch('claude.claude_integration._run_claude_code_locally',
                   new_callable=AsyncMock) as mock_local:
            with pytest.raises(Exception) as exc_info:
                await run_claude_code('prompt', context)

        assert 'work_dir' in str(exc_info.value)
        mock_local.assert_not_called()
        # The lock decision never even got as far as asking about the directory.
        mock_wm.is_base_clone_dir.assert_not_called()

    async def test_a_real_work_dir_still_reaches_the_lock_decision(self):
        """The guard must not change the behavior of a well-formed context."""
        context = _local_execution_context(work_dir='/workspace/test-project')

        with patch('services.dev_container_build_lock.dev_container_build_lock_async',
                   _noop_lock), \
             patch('services.project_checkout_lock.project_checkout_lock_async', _noop_lock), \
             patch('services.project_workspace.workspace_manager') as mock_wm, \
             patch('claude.claude_integration._run_claude_code_locally',
                   new_callable=AsyncMock, return_value='done') as mock_local:
            mock_wm.is_base_clone_dir.return_value = True

            result = await run_claude_code('prompt', context)

        assert result == 'done'
        mock_local.assert_awaited_once()
        assert mock_wm.is_base_clone_dir.call_args.args[1] == Path('/workspace/test-project')

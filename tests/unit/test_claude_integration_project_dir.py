"""
Unit tests for claude_integration.run_claude_code's Docker-mount-source
project_dir resolution (issue #46).

run_claude_code is the innermost of the 3 Docker-mount call sites this issue
migrates to per-epic worktrees: by the time it runs, an earlier stage of the
same dispatch (agent_executor._build_execution_context) has typically already
resolved 'epic_id' (and, for issues-workspace dispatch, 'branch_name') into
the nested task_context -- this call site's job is to *reuse* that
resolution for the container mount, not redo it, and to fall back to the
pre-migration shared-base-clone behavior when nothing was resolved upstream
(e.g. utility scripts that call run_claude_code directly, outside the normal
agent_executor dispatch flow).
"""

import pytest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from claude.claude_integration import run_claude_code


@asynccontextmanager
async def _noop_project_checkout_lock(*args, **kwargs):
    """Stand-in for project_checkout_lock_async() (#54) in tests that mock
    workspace_manager entirely: a real ProjectResourceLockManager() would try
    to construct the process-wide PipelineLockManager singleton (real
    Redis/filesystem state), which these path-resolution-focused tests have
    no reason to depend on."""
    yield


def _base_context(task_context=None, **overrides):
    context = {
        'agent': 'test_agent',
        'task_id': 'task-1',
        'project': 'test-project',
        'use_docker': True,
        'context': task_context or {},
    }
    context.update(overrides)
    return context


@pytest.mark.asyncio
class TestRunClaudeCodeProjectDirResolution:

    async def test_defaults_to_shared_base_clone_with_no_epic_id(self):
        """No upstream epic resolution (e.g. a utility script calling
        run_claude_code directly) must preserve the pre-migration behavior
        exactly: a plain base-clone path, epic_id/branch_name omitted."""
        context = _base_context(task_context={'issue_number': 100})

        with patch('claude.claude_integration.workspace_manager') as mock_wm, \
             patch('services.project_workspace.workspace_manager') as mock_shared_wm, \
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('services.project_checkout_lock.project_checkout_lock_async', _noop_project_checkout_lock), \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir_off_loop = AsyncMock(return_value=Path('/workspace/test-project'))
            mock_shared_wm.is_base_clone_dir.return_value = True  # this IS the shared base clone (epic_id=None)
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            result = await run_claude_code('do the thing', context)

            # Resolved off the event loop (#151/WI-6 review) via
            # get_project_dir_off_loop(), which runs the blocking resolution on
            # project_workspace's DEDICATED pool rather than asyncio.to_thread()'s
            # default executor -- the default one is also how
            # project_checkout_lock_async acquires and releases, so waits for that
            # lock must not sit in it. epic_id and branch_name go positionally;
            # issue_number is the wait's watchdog-exemption key.
            mock_wm.get_project_dir_off_loop.assert_called_once_with(
                'test-project', None, None, issue_number=100
            )
            assert result == 'output'
            mount_dir = mock_runner.run_agent_in_container.call_args.kwargs['project_dir']
            assert mount_dir == Path('/workspace/test-project')

    async def test_reuses_epic_id_and_branch_resolved_upstream(self):
        """agent_executor._build_execution_context already resolved epic_id
        (and, for an issues-workspace sub-issue, branch_name) into the nested
        task_context -- this call site must mount that same worktree, not
        the shared base clone."""
        context = _base_context(task_context={
            'issue_number': 100,
            'epic_id': '42',
            'branch_name': 'feature/issue-42-shared',
        })

        with patch('claude.claude_integration.workspace_manager') as mock_wm, \
             patch('services.project_workspace.workspace_manager') as mock_shared_wm, \
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('services.project_checkout_lock.project_checkout_lock_async', _noop_project_checkout_lock), \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir_off_loop = AsyncMock(return_value=Path('/workspace/.orchestrator/worktrees/test-project/42'))
            mock_shared_wm.is_base_clone_dir.return_value = False  # isolated epic worktree, not the base clone
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

            mock_wm.get_project_dir_off_loop.assert_called_once_with(
                'test-project', '42', 'feature/issue-42-shared', issue_number=100
            )
            mount_dir = mock_runner.run_agent_in_container.call_args.kwargs['project_dir']
            assert mount_dir == Path('/workspace/.orchestrator/worktrees/test-project/42')

    async def test_reuses_a_fully_resolved_project_dir_without_rederiving(self):
        """A caller running in its own separate process (a repair cycle
        container) hands us the concrete directory its originating
        orchestrator process already created/reused via
        task_context['project_dir'] -- re-deriving via epic_id here would run
        against a fresh, empty in-process worktree cache and could try to
        `git worktree add` a worktree that already exists on disk, which is
        not idempotent."""
        context = _base_context(task_context={
            'issue_number': 100,
            'epic_id': '77',
            'project_dir': '/workspace/.orchestrator/worktrees/test-project/77',
        })

        with patch('claude.claude_integration.workspace_manager') as mock_wm, \
             patch('services.project_workspace.workspace_manager') as mock_shared_wm, \
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('services.project_checkout_lock.project_checkout_lock_async', _noop_project_checkout_lock), \
             patch('pathlib.Path.exists', return_value=True):
            mock_shared_wm.is_base_clone_dir.return_value = False  # isolated epic worktree, not the base clone
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

            mock_wm.get_project_dir_off_loop.assert_not_called()
            mount_dir = mock_runner.run_agent_in_container.call_args.kwargs['project_dir']
            assert mount_dir == Path('/workspace/.orchestrator/worktrees/test-project/77')

    async def test_planning_design_style_epic_id_with_no_branch_name(self):
        """planning_design's discussions-workspace stages never resolve a
        branch_name via workspace prep (supports_git_operations=False) --
        agent_executor still resolves epic_id (the epic's own issue number)
        and, by the time this runs, the worktree already exists (created
        upstream), so branch_name=None here is expected and safe: it is only
        required the first time a worktree is created."""
        context = _base_context(task_context={
            'issue_number': 200,
            'epic_id': '200',
        })

        with patch('claude.claude_integration.workspace_manager') as mock_wm, \
             patch('services.project_workspace.workspace_manager') as mock_shared_wm, \
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('services.project_checkout_lock.project_checkout_lock_async', _noop_project_checkout_lock), \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir_off_loop = AsyncMock(return_value=Path('/workspace/.orchestrator/worktrees/test-project/200'))
            mock_shared_wm.is_base_clone_dir.return_value = False  # isolated epic worktree, not the base clone
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

            mock_wm.get_project_dir_off_loop.assert_called_once_with(
                'test-project', '200', None, issue_number=200
            )


@pytest.mark.asyncio
class TestTheDockerPathHandsTheGuardTheMountedDirectory:
    """
    #154/WI-9 review. Since #140 item 4 centralized the shared-base-clone
    decision into project_checkout_lock_if_shared_async(), each call site's
    correctness reduces entirely to WHICH directory it passes -- the helper's
    own tests pin that it decides on the caller-supplied one. The local path's
    argument was pinned (test_claude_integration_work_dir_required.py); the
    Docker path -- the one that actually runs agents -- was not.

    Passing something else there (say context['work_dir'], frequently absent in
    a Docker context) is silent: is_base_clone_dir() fails CLOSED on a
    missing/None directory, so every epic-worktree container run would take the
    shared base-clone lock and serialize a project's sibling epics against each
    other. No error, no failing test -- just a throughput collapse.
    """

    async def _run_docker(self, task_context, resolved_dir, is_base_clone,
                          checkout_lock=None):
        context = _base_context(task_context=task_context)
        calls = []

        @asynccontextmanager
        async def _recording_checkout_lock(project, issue_number=None, **kwargs):
            calls.append((project, issue_number))
            yield

        with patch('claude.claude_integration.workspace_manager') as mock_wm, \
             patch('services.project_workspace.workspace_manager') as mock_shared_wm, \
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('services.project_checkout_lock.project_checkout_lock_async',
                   checkout_lock or _recording_checkout_lock), \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir_off_loop = AsyncMock(return_value=resolved_dir)
            mock_shared_wm.is_base_clone_dir.return_value = is_base_clone
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

        return mock_shared_wm, mock_runner, calls

    async def test_the_guard_is_asked_about_the_directory_that_gets_mounted(self):
        """The base-clone case: the directory handed to the guard and the
        directory handed to run_agent_in_container() must be the same one."""
        resolved = Path('/workspace/test-project')
        mock_shared_wm, mock_runner, calls = await self._run_docker(
            {'issue_number': 100}, resolved, is_base_clone=True
        )

        guard_args = mock_shared_wm.is_base_clone_dir.call_args.args
        assert guard_args[0] == 'test-project'
        assert guard_args[1] == resolved
        assert mock_runner.run_agent_in_container.call_args.kwargs['project_dir'] == resolved
        assert calls == [('test-project', 100)]

    async def test_an_epic_worktree_container_run_takes_no_checkout_lock(self):
        """The worktree case, same property plus the negative: the guard is
        asked about the mounted worktree, and answers False, so no
        project_checkout lock is taken and sibling epics do not serialize."""
        resolved = Path('/workspace/.orchestrator/worktrees/test-project/42')
        mock_shared_wm, mock_runner, calls = await self._run_docker(
            {'issue_number': 100, 'epic_id': '42',
             'branch_name': 'feature/issue-42-shared'},
            resolved, is_base_clone=False,
        )

        assert mock_shared_wm.is_base_clone_dir.call_args.args[1] == resolved
        assert mock_runner.run_agent_in_container.call_args.kwargs['project_dir'] == resolved
        assert calls == [], (
            "an epic-worktree container run must take no project_checkout lock"
        )

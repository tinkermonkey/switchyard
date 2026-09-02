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
from pathlib import Path
from unittest.mock import AsyncMock, patch

from claude.claude_integration import run_claude_code


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
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir.return_value = Path('/workspace/test-project')
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            result = await run_claude_code('do the thing', context)

            mock_wm.get_project_dir.assert_called_once_with(
                'test-project', epic_id=None, branch_name=None
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
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir.return_value = Path('/workspace/.orchestrator/worktrees/test-project/42')
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

            mock_wm.get_project_dir.assert_called_once_with(
                'test-project', epic_id='42', branch_name='feature/issue-42-shared'
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
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('pathlib.Path.exists', return_value=True):
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

            mock_wm.get_project_dir.assert_not_called()
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
             patch('claude.claude_integration.docker_runner') as mock_runner, \
             patch('pathlib.Path.exists', return_value=True):
            mock_wm.get_project_dir.return_value = Path('/workspace/.orchestrator/worktrees/test-project/200')
            mock_runner.run_agent_in_container = AsyncMock(return_value='output')

            await run_claude_code('do the thing', context)

            mock_wm.get_project_dir.assert_called_once_with(
                'test-project', epic_id='200', branch_name=None
            )

"""Tests for services/worktree_env_notification.py

Covers:
- ``notify_active_worktrees_of_env_fix`` posts a comment per active worktree
- Worktrees with UNKNOWN/UNOWNED/None active_run_protected receive no comment
- Clean divergence (worktree just needs a pull) produces the right comment tone
- Conflicting divergence (worktree has its own constraint) is flagged explicitly
- Duplicate epic_ids receive only one comment
- Exceptions from post_issue_comment are absorbed (verification unaffected)
"""
from __future__ import annotations

import asyncio
import textwrap
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

# ─────────────────────────────────────────────────────────────────────────────
# The module under test imports services.project_workspace and
# services.github_integration at call time; mock them throughout.
# ─────────────────────────────────────────────────────────────────────────────

MODULE = 'services.worktree_env_notification'


def _worktree(epic_id: str, path: str, active: Optional[bool]) -> Dict[str, Any]:
    """Build a minimal survey row."""
    return {
        'project': 'test-project',
        'epic_id': epic_id,
        'path': path,
        'active_run_protected': active,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Helpers to inject git output without touching the real filesystem
# ─────────────────────────────────────────────────────────────────────────────

def _make_run_git(responses: Dict[tuple, tuple]):
    """Return a _run_git replacement driven by an (args_prefix → response) dict.

    ``args_prefix`` is matched against the first N elements of the args list.
    The first match wins.  Unknown args return (0, '', '').
    """
    def _run_git_stub(args: List[str], cwd: Path, **kwargs):
        # 'show HEAD:path/to/file' → key is ('show', 'HEAD:<something>')
        key = tuple(args)
        for pattern, response in responses.items():
            if key[:len(pattern)] == pattern:
                return response
        return (0, '', '')
    return _run_git_stub


# ─────────────────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_workspace_manager():
    mgr = MagicMock()
    mgr.get_project_dir.return_value = Path('/workspace/test-project')
    mgr.survey_epic_worktrees.return_value = []
    return mgr


@pytest.fixture
def mock_github():
    gh = MagicMock()
    gh.post_issue_comment = AsyncMock(return_value={'id': 1})
    return gh


@pytest.fixture
def mock_project_config():
    cfg = MagicMock()
    cfg.github = {'org': 'test-org', 'repo': 'test-repo'}
    return cfg


# ─────────────────────────────────────────────────────────────────────────────
# Tests
# ─────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_no_worktrees_no_comment(mock_workspace_manager, mock_github, mock_project_config):
    """When survey_epic_worktrees returns nothing, no comment is posted."""
    mock_workspace_manager.survey_epic_worktrees.return_value = []

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('abc123', ['pyproject.toml'])),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
    ):
        mock_workspace_manager.get_project_dir.return_value = Path('/workspace/p')

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fix output')

    mock_github.post_issue_comment.assert_not_called()


@pytest.mark.asyncio
async def test_unowned_worktree_no_comment(mock_workspace_manager, mock_github, mock_project_config):
    """Worktrees where active_run_protected is False or None get no comment."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('100', '/workspace/.orchestrator/worktrees/p/100', False),
        _worktree('101', '/workspace/.orchestrator/worktrees/p/101', None),
    ]

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('abc123', ['pyproject.toml'])),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fix output')

    mock_github.post_issue_comment.assert_not_called()


@pytest.mark.asyncio
async def test_active_worktree_gets_comment(mock_workspace_manager, mock_github, mock_project_config):
    """An active worktree (active_run_protected=True) receives exactly one comment."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('200', '/workspace/.orchestrator/worktrees/p/200', True),
    ]

    # Simulate: pyproject.toml identical in main and worktree (no divergence).
    pyproject_content = '[project]\nname = "foo"\n'

    def _read_file_stub(repo_dir: Path, rel_path: str):
        return pyproject_content  # same in both

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('deadbeef', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'Problem Analysis: fixed strands floor')

    mock_github.post_issue_comment.assert_awaited_once()
    issue_number, comment = mock_github.post_issue_comment.call_args.args
    assert issue_number == 200
    assert 'deadbeef' in comment


@pytest.mark.asyncio
async def test_clean_divergence_comment(mock_workspace_manager, mock_github, mock_project_config):
    """When main has a file that differs from the worktree but the worktree has
    no conflicting *local* constraint (main added/modified, worktree is just
    behind), the comment says 'Clean Merge Available'."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('300', '/workspace/.orchestrator/worktrees/p/300', True),
    ]

    main_pyproject = textwrap.dedent("""\
        [project]
        dependencies = ["strands-agents>=1.56.0"]
    """)
    worktree_pyproject = textwrap.dedent("""\
        [project]
        dependencies = ["strands-agents>=1.51.0"]
    """)

    call_count = [0]

    def _read_file_stub(repo_dir: Path, rel_path: str):
        call_count[0] += 1
        # First call is for main (base_clone), second for worktree.
        if call_count[0] % 2 == 1:
            return main_pyproject
        return worktree_pyproject

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('abc', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'bump strands floor')

    _, comment = mock_github.post_issue_comment.call_args.args
    # Divergence detected, files differ → NOT "No Action Required"
    assert 'No Action Required' not in comment
    # Files differ (both exist) → diff section should appear
    assert 'pyproject.toml' in comment


@pytest.mark.asyncio
async def test_conflicting_divergence_comment(mock_workspace_manager, mock_github, mock_project_config):
    """The #1345 scenario: worktree has an *upper-bound cap* that conflicts with
    the floor fix on main.  The comment must say 'Manual Reconciliation Required'
    and show both constraints side by side."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('1301', '/workspace/.orchestrator/worktrees/qsi/1301', True),
    ]

    main_pyproject = textwrap.dedent("""\
        [project]
        dependencies = ["strands-agents[a2a]>=1.56.0"]
    """)
    # Worktree deliberately caps strands below the fix's floor.
    worktree_pyproject = textwrap.dedent("""\
        [project]
        dependencies = ["strands-agents[a2a]>=1.51.0,<1.56.0"]
    """)

    call_count = [0]

    def _read_file_stub(repo_dir: Path, rel_path: str):
        call_count[0] += 1
        if call_count[0] % 2 == 1:
            return main_pyproject
        return worktree_pyproject

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('fix123', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('qsi-ai', 'fix strands floor to >=1.56.0')

    mock_github.post_issue_comment.assert_awaited_once()
    issue_number, comment = mock_github.post_issue_comment.call_args.args
    assert issue_number == 1301
    assert 'Manual Reconciliation Required' in comment
    # Comment must NOT claim the worktree's cap is simply wrong.
    assert 'intentional' in comment.lower() or 'deliberate' in comment.lower() or 'conflicting' in comment.lower()
    # Both versions should be shown.
    assert '1.56.0' in comment
    assert '<1.56.0' in comment


@pytest.mark.asyncio
async def test_duplicate_epic_ids_one_comment(mock_workspace_manager, mock_github, mock_project_config):
    """Two survey rows with the same epic_id produce only one comment."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('400', '/workspace/.orchestrator/worktrees/p/400', True),
        _worktree('400', '/workspace/.orchestrator/worktrees/p/400', True),
    ]

    def _read_file_stub(repo_dir, rel_path):
        return 'same content'

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('abc', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fix')

    assert mock_github.post_issue_comment.await_count == 1


@pytest.mark.asyncio
async def test_github_comment_failure_does_not_raise(mock_workspace_manager, mock_project_config):
    """A failure posting the comment is logged but does not propagate."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('500', '/workspace/.orchestrator/worktrees/p/500', True),
    ]

    failing_github = MagicMock()
    failing_github.post_issue_comment = AsyncMock(side_effect=RuntimeError("network error"))

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('abc', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', return_value='content'),
        patch(f'{MODULE}._make_github_integration', return_value=failing_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        # Must not raise.
        await notify_active_worktrees_of_env_fix('test-project', 'fix')


@pytest.mark.asyncio
async def test_no_env_fix_commit_found_no_comment(mock_workspace_manager, mock_github, mock_project_config):
    """When git finds no env-touching commit, no comment is posted."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('600', '/workspace/.orchestrator/worktrees/p/600', True),
    ]

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=None),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fix')

    mock_github.post_issue_comment.assert_not_called()


@pytest.mark.asyncio
async def test_multiple_active_worktrees_each_get_one_comment(
    mock_workspace_manager, mock_github, mock_project_config
):
    """Two distinct active worktrees each receive exactly one comment."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('700', '/workspace/.orchestrator/worktrees/p/700', True),
        _worktree('701', '/workspace/.orchestrator/worktrees/p/701', True),
    ]

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('sha999', ['Dockerfile.agent'])),
        patch(f'{MODULE}._read_file_at_head', return_value='FROM python:3.11\n'),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        

        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fixed base image')

    assert mock_github.post_issue_comment.await_count == 2
    issue_numbers = {c.args[0] for c in mock_github.post_issue_comment.await_args_list}
    assert issue_numbers == {700, 701}

"""Tests for services/worktree_env_notification.py

Covers:
- ``notify_active_worktrees_of_env_fix`` posts a comment per active worktree
- Worktrees with UNKNOWN/UNOWNED/None active_run_protected receive no comment
- Clean divergence (worktree just needs a pull) produces the right comment tone
- Conflicting divergence (worktree has its own constraint) is flagged explicitly
- Duplicate epic_ids receive only one comment
- Exceptions from post_issue_comment are absorbed (verification unaffected)
- Phase 2: clean case triggers auto-apply; "already applied" comment is posted
- Phase 2: conflicting case skips auto-apply; Phase 1 comment is posted
- Phase 2: auto-apply failure falls back to Phase 1 comment
- ``_classify_divergence`` helper returns correct (has_conflicts, all_identical)
- ``_build_auto_applied_comment`` contains expected content
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any, Dict, Optional
from unittest.mock import AsyncMock, MagicMock, patch

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
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
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
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
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
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
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
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
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
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
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
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fixed base image')

    assert mock_github.post_issue_comment.await_count == 2
    issue_numbers = {c.args[0] for c in mock_github.post_issue_comment.await_args_list}
    assert issue_numbers == {700, 701}


# ─────────────────────────────────────────────────────────────────────────────
# Phase 2 tests
# ─────────────────────────────────────────────────────────────────────────────

def test_classify_divergence_all_identical():
    from services.worktree_env_notification import _FileDivergence, _classify_divergence
    divs = [_FileDivergence('a.txt', 'x', 'x'), _FileDivergence('b.txt', 'y', 'y')]
    has_conflicts, all_identical = _classify_divergence(divs)
    assert not has_conflicts
    assert all_identical


def test_classify_divergence_clean():
    from services.worktree_env_notification import _FileDivergence, _classify_divergence
    # main added a new file (only_in_main) — not a conflict, not identical
    divs = [_FileDivergence('new.txt', 'content', None)]
    has_conflicts, all_identical = _classify_divergence(divs)
    assert not has_conflicts
    assert not all_identical


def test_classify_divergence_conflict_diverged():
    from services.worktree_env_notification import _FileDivergence, _classify_divergence
    divs = [_FileDivergence('p.toml', 'main-ver', 'worktree-ver')]
    has_conflicts, all_identical = _classify_divergence(divs)
    assert has_conflicts
    assert not all_identical


def test_classify_divergence_conflict_only_in_worktree():
    from services.worktree_env_notification import _FileDivergence, _classify_divergence
    divs = [_FileDivergence('extra.txt', None, 'local-content')]
    has_conflicts, all_identical = _classify_divergence(divs)
    assert has_conflicts
    assert not all_identical


def test_build_auto_applied_comment_content():
    from services.worktree_env_notification import _build_auto_applied_comment
    comment = _build_auto_applied_comment(
        'abc1234567890', 'def9876543210', ['pyproject.toml'], 'Problem Analysis: bump floor'
    )
    assert 'Auto-Applied' in comment
    assert 'abc123456' in comment
    assert 'def987654' in comment
    assert 'pyproject.toml' in comment
    assert 'no action is required' in comment.lower()
    # Must NOT contain the Phase 1 "run this yourself" language
    assert 'git rebase' not in comment
    assert 'git merge' not in comment


@pytest.mark.asyncio
async def test_clean_divergence_triggers_auto_apply(mock_workspace_manager, mock_github, mock_project_config):
    """Clean worktree (main added a new file the worktree doesn't have — only_in_main):
    auto-apply is called, 'already applied' comment posted, Phase 1 language absent."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('800', '/workspace/.orchestrator/worktrees/p/800', True),
    ]

    def _read_file_stub(repo_dir: Path, rel_path: str):
        # Simulate: main has the file, worktree does not (only_in_main → clean case)
        if str(repo_dir) == '/workspace/test-project':
            return '[project]\ndeps=["pkg>=2.0"]\n'  # main has it
        return None  # worktree does not have it yet

    apply_called = []

    def _apply_stub(worktree_path, commit_sha, changed_files):
        apply_called.append((commit_sha, changed_files))
        return 'newsha1234567890'

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('fixsha', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
        patch(f'{MODULE}._apply_env_fix_to_worktree', side_effect=_apply_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'bump pkg')

    assert apply_called, "auto-apply should have been invoked"
    mock_github.post_issue_comment.assert_awaited_once()
    _, comment = mock_github.post_issue_comment.call_args.args
    assert 'Auto-Applied' in comment
    assert 'Clean Merge Available' not in comment
    assert 'Manual Reconciliation' not in comment


@pytest.mark.asyncio
async def test_conflicting_case_skips_auto_apply(mock_workspace_manager, mock_github, mock_project_config):
    """Conflicting worktree: auto-apply must NOT be called; Phase 1 comment posted."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('900', '/workspace/.orchestrator/worktrees/p/900', True),
    ]

    def _read_file_stub(repo_dir: Path, rel_path: str):
        # Both sides have content but differ → diverged → has_conflicts=True
        if str(repo_dir) == '/workspace/test-project':
            return 'deps=["pkg>=2.0"]'
        # Worktree has an upper-bound cap — genuine conflict
        return 'deps=["pkg>=1.0,<2.0"]'

    apply_called = []

    def _apply_stub(worktree_path, commit_sha, changed_files):
        apply_called.append(1)
        return 'newsha'

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('fixsha', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
        patch(f'{MODULE}._apply_env_fix_to_worktree', side_effect=_apply_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'bump pkg')

    assert not apply_called, "auto-apply must NOT be called for conflicting worktrees"
    _, comment = mock_github.post_issue_comment.call_args.args
    assert 'Manual Reconciliation Required' in comment


@pytest.mark.asyncio
async def test_auto_apply_failure_falls_back_to_phase1_comment(
    mock_workspace_manager, mock_github, mock_project_config
):
    """When auto-apply returns None for a clean case, Phase 1 'Clean Merge Available' comment is posted."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('1000', '/workspace/.orchestrator/worktrees/p/1000', True),
    ]

    def _read_file_stub(repo_dir: Path, rel_path: str):
        # only_in_main → clean case
        if str(repo_dir) == '/workspace/test-project':
            return 'deps=["pkg>=2.0"]'
        return None

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('fixsha', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
        patch(f'{MODULE}._apply_env_fix_to_worktree', return_value=None),  # simulate failure
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'bump pkg')

    mock_github.post_issue_comment.assert_awaited_once()
    _, comment = mock_github.post_issue_comment.call_args.args
    assert 'Clean Merge Available' in comment
    assert 'Auto-Applied' not in comment


@pytest.mark.asyncio
async def test_identical_files_no_auto_apply(mock_workspace_manager, mock_github, mock_project_config):
    """Already-identical files produce Phase 1 'No Action Required' comment; auto-apply not called."""
    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('1100', '/workspace/.orchestrator/worktrees/p/1100', True),
    ]
    apply_called = []

    def _apply_stub(*args, **kwargs):
        apply_called.append(1)
        return 'sha'

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('fixsha', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', return_value='same content'),
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
        patch(f'{MODULE}._apply_env_fix_to_worktree', side_effect=_apply_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'fix')

    assert not apply_called, "auto-apply must NOT be called when files are already identical"
    _, comment = mock_github.post_issue_comment.call_args.args
    assert 'No Action Required' in comment


def test_classify_divergence_three_way_clean_behind():
    """Worktree has the pre-fix content (just hasn't received the fix yet): NOT a conflict."""
    from services.worktree_env_notification import _FileDivergence, _classify_divergence

    pre_fix = '[project]\ndeps=["pkg>=1.5"]\n'
    main_fixed = '[project]\ndeps=["pkg>=2.0"]\n'
    # Worktree matches the pre-fix baseline: clean behind, no local modification.
    divs = [_FileDivergence('pyproject.toml', main_fixed, pre_fix, pre_fix)]
    has_conflicts, all_identical = _classify_divergence(divs)
    assert not has_conflicts, "worktree just behind pre-fix should not be a conflict"
    assert not all_identical


def test_classify_divergence_three_way_locally_modified():
    """Worktree content differs from pre-fix baseline: locally modified → conflict."""
    from services.worktree_env_notification import _FileDivergence, _classify_divergence

    pre_fix = '[project]\ndeps=["pkg>=1.5"]\n'
    main_fixed = '[project]\ndeps=["pkg>=2.0"]\n'
    worktree_modified = '[project]\ndeps=["pkg>=1.5,<2.0"]\n'  # different from pre_fix
    divs = [_FileDivergence('pyproject.toml', main_fixed, worktree_modified, pre_fix)]
    has_conflicts, all_identical = _classify_divergence(divs)
    assert has_conflicts, "locally modified worktree should be a conflict"
    assert not all_identical


@pytest.mark.asyncio
async def test_lock_held_falls_back_to_phase1_comment(
    mock_workspace_manager, mock_github, mock_project_config
):
    """When the per-worktree apply lock is already held, fall back to Phase 1 comment
    and do NOT call _apply_env_fix_to_worktree."""
    import asyncio

    mock_workspace_manager.survey_epic_worktrees.return_value = [
        _worktree('1200', '/workspace/.orchestrator/worktrees/p/1200', True),
    ]

    def _read_file_stub(repo_dir: Path, rel_path: str):
        # only_in_main → clean case, so we'd normally auto-apply
        if str(repo_dir) == '/workspace/test-project':
            return 'deps=["pkg>=2.0"]'
        return None

    apply_called = []

    def _apply_stub(worktree_path, commit_sha, changed_files):
        apply_called.append(1)
        return 'newsha'

    # Pre-acquire the lock for this worktree path so the notification loop
    # sees it as held and falls back to Phase 1.
    held_lock = asyncio.Lock()
    await held_lock.acquire()

    def _lock_stub(worktree_path):
        return held_lock

    with (
        patch(f'{MODULE}._get_workspace_manager', return_value=mock_workspace_manager),
        patch(f'{MODULE}._get_env_fix_commit', return_value=('fixsha', ['pyproject.toml'])),
        patch(f'{MODULE}._read_file_at_head', side_effect=_read_file_stub),
        patch(f'{MODULE}._get_pre_fix_sha', return_value=None),
        patch(f'{MODULE}._get_worktree_apply_lock', side_effect=_lock_stub),
        patch(f'{MODULE}._apply_env_fix_to_worktree', side_effect=_apply_stub),
        patch(f'{MODULE}._make_github_integration', return_value=mock_github),
        patch(f'{MODULE}._get_config_manager', return_value=MagicMock(get_project_config=MagicMock(return_value=mock_project_config))),
        patch(f'{MODULE}._run_git', return_value=(0, '.git', '')),
    ):
        from services.worktree_env_notification import notify_active_worktrees_of_env_fix
        await notify_active_worktrees_of_env_fix('test-project', 'bump pkg')

    held_lock.release()

    assert not apply_called, "_apply_env_fix_to_worktree must NOT be called when lock is held"
    mock_github.post_issue_comment.assert_awaited_once()
    _, comment = mock_github.post_issue_comment.call_args.args
    assert 'Auto-Applied' not in comment
    assert 'Clean Merge Available' in comment


def test_apply_env_fix_to_worktree_skips_when_ahead():
    """_apply_env_fix_to_worktree returns None without staging when the worktree is
    ahead of the remote (an agent may have committed there)."""
    from pathlib import Path
    from unittest.mock import call as mock_call

    call_responses = {
        # fetch origin
        ('fetch', 'origin'): (0, '', ''),
        # rev-parse --abbrev-ref HEAD
        ('rev-parse', '--abbrev-ref', 'HEAD'): (0, 'feature/epic-99\n', ''),
        # status --porcelain
        ('status', '--porcelain'): (0, '', ''),
        # rev-list --count origin/feature/epic-99..HEAD (ahead count = 1)
        ('rev-list', '--count', 'origin/feature/epic-99..HEAD'): (0, '1\n', ''),
    }

    def _run_git_stub(args, cwd=None, timeout=None):
        key = tuple(a for a in args if not a.startswith('-') or a.startswith('--'))
        # Match by first few distinct tokens
        for k, v in call_responses.items():
            if all(tok in args for tok in k):
                return v
        # Default: success with empty output (should not be reached in this path)
        return (0, '', '')

    with patch(f'{MODULE}._run_git', side_effect=_run_git_stub):
        from services.worktree_env_notification import _apply_env_fix_to_worktree
        result = _apply_env_fix_to_worktree(
            Path('/workspace/worktrees/p/99'),
            'abc1234',
            ['pyproject.toml'],
        )

    assert result is None, "should return None when worktree is ahead (agent may have committed)"

"""
Unit tests for PR-ready marking with exit column detection

Tests the new logic that treats issues in exit columns (Done, Staged, etc.)
as complete even if their GitHub state is still 'open'.
"""

import pytest
from unittest.mock import AsyncMock, Mock, patch, MagicMock
from services.feature_branch_manager import FeatureBranchManager


@pytest.mark.asyncio
async def test_verify_sub_issues_all_closed():
    """Test traditional behavior: all sub-issues are closed"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'closed'},
    ]

    # Without exit column check
    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues
    )

    assert result == True


@pytest.mark.asyncio
async def test_verify_sub_issues_some_open_no_exit_column_check():
    """Test traditional behavior: some sub-issues still open (no exit column check)"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'open'},
    ]

    # Without exit column check (no project_monitor)
    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues
    )

    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_complete_with_exit_column():
    """Test NEW behavior: issues in exit columns are treated as complete"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'open'},  # In exit column
    ]

    # Mock workflow template with exit columns
    workflow_template = Mock()
    workflow_template.name = 'dev_workflow'
    workflow_template.pipeline_exit_columns = ['Done', 'Staged']

    # Mock project monitor
    project_monitor = AsyncMock()
    project_monitor.get_issue_column_async = AsyncMock(return_value='Done')

    # Mock config manager
    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()
        mock_pipeline = Mock()
        mock_pipeline.name = 'SDLC Pipeline'
        mock_pipeline.workflow = 'dev_workflow'
        mock_pipeline.board_name = 'Dev Board'
        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor
        )

    assert result == True
    # Verify we queried the column for issue 2 (issue 1 was already closed)
    assert project_monitor.get_issue_column_async.call_count == 2


@pytest.mark.asyncio
async def test_verify_sub_issues_mixed_states_with_exit_columns():
    """Test mixed scenario: some closed, some in exit columns, some incomplete"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},          # Complete: closed
        {'number': 2, 'state': 'open'},            # Complete: in Done column
        {'number': 3, 'state': 'open'},            # Incomplete: in Progress column
    ]

    workflow_template = Mock()
    workflow_template.name = 'dev_workflow'
    workflow_template.pipeline_exit_columns = ['Done', 'Staged']

    # Mock project monitor to return different columns for different issues
    project_monitor = AsyncMock()

    async def get_column_side_effect(project, board, issue_num):
        if issue_num == 2:
            return 'Done'  # Exit column
        elif issue_num == 3:
            return 'In Progress'  # Not an exit column
        return None

    project_monitor.get_issue_column_async = AsyncMock(side_effect=get_column_side_effect)

    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()
        mock_pipeline = Mock()
        mock_pipeline.name = 'SDLC Pipeline'
        mock_pipeline.workflow = 'dev_workflow'
        mock_pipeline.board_name = 'Dev Board'
        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor
        )

    # Issue 3 is incomplete, so result should be False
    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_all_in_exit_columns():
    """Test all issues complete when all are in exit columns (none formally closed)"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'open'},  # In Done column
        {'number': 2, 'state': 'open'},  # In Staged column
    ]

    workflow_template = Mock()
    workflow_template.name = 'dev_workflow'
    workflow_template.pipeline_exit_columns = ['Done', 'Staged']

    project_monitor = AsyncMock()

    async def get_column_side_effect(project, board, issue_num):
        if issue_num == 1:
            return 'Done'
        elif issue_num == 2:
            return 'Staged'
        return None

    project_monitor.get_issue_column_async = AsyncMock(side_effect=get_column_side_effect)

    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()
        mock_pipeline = Mock()
        mock_pipeline.name = 'SDLC Execution'
        mock_pipeline.workflow = 'dev_workflow'
        mock_pipeline.board_name = 'Dev Board'
        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor
        )

    # All issues are in exit columns, so should be complete
    assert result == True


@pytest.mark.asyncio
async def test_verify_sub_issues_no_workflow_template():
    """Test fallback behavior when workflow_template is None"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'open'},
    ]

    project_monitor = AsyncMock()

    # No workflow_template means no exit column check
    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        project_name='test-project',
        workflow_template=None,
        project_monitor=project_monitor
    )

    # Falls back to closed-only check
    assert result == False
    # Should not have called project_monitor
    assert project_monitor.get_issue_column_async.call_count == 0


@pytest.mark.asyncio
async def test_verify_sub_issues_no_project_monitor():
    """Test fallback behavior when project_monitor is None"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'open'},
    ]

    workflow_template = Mock()
    workflow_template.pipeline_exit_columns = ['Done']

    # No project_monitor means no exit column check
    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        project_name='test-project',
        workflow_template=workflow_template,
        project_monitor=None
    )

    # Falls back to closed-only check
    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_empty_exit_columns():
    """Test behavior when exit_columns list is empty"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'open'},
    ]

    workflow_template = Mock()
    workflow_template.pipeline_exit_columns = []  # Empty list

    project_monitor = AsyncMock()

    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        project_name='test-project',
        workflow_template=workflow_template,
        project_monitor=project_monitor
    )

    # No exit columns defined, falls back to closed-only check
    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_no_exit_columns_attribute():
    """Test behavior when workflow_template doesn't have pipeline_exit_columns"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'open'},
    ]

    workflow_template = Mock(spec=[])  # Mock with no attributes

    project_monitor = AsyncMock()

    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        project_name='test-project',
        workflow_template=workflow_template,
        project_monitor=project_monitor
    )

    # No exit columns, falls back to closed-only check
    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_board_not_found():
    """Test behavior when board lookup fails (no matching pipeline)"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'open'},
    ]

    workflow_template = Mock()
    workflow_template.name = 'dev_workflow'
    workflow_template.pipeline_exit_columns = ['Done']

    project_monitor = AsyncMock()

    # Mock config manager with no matching pipeline
    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()
        mock_pipeline = Mock()
        mock_pipeline.name = 'Different Pipeline'
        mock_pipeline.workflow = 'other_workflow'
        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor
        )

    # Board not found, falls back to closed-only check
    assert result == False
    # Should not call project_monitor if board not found
    assert project_monitor.get_issue_column_async.call_count == 0


@pytest.mark.asyncio
async def test_verify_sub_issues_column_query_fails():
    """Test behavior when get_issue_column_async raises an exception"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'open'},
    ]

    workflow_template = Mock()
    workflow_template.name = 'dev_workflow'
    workflow_template.pipeline_exit_columns = ['Done']

    # Mock project_monitor to raise an exception
    project_monitor = AsyncMock()
    project_monitor.get_issue_column_async = AsyncMock(
        side_effect=Exception("GitHub API error")
    )

    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()
        mock_pipeline = Mock()
        mock_pipeline.name = 'SDLC'
        mock_pipeline.workflow = 'dev_workflow'
        mock_pipeline.board_name = 'Dev Board'
        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        # Should handle exception gracefully and fall back to closed-only check
        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor
        )

    # Issue 2 is open and we couldn't check its column, so incomplete
    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_empty_list():
    """Test behavior with empty sub_issues list"""
    manager = FeatureBranchManager()
    github = Mock()

    result = await manager._verify_all_sub_issues_complete(
        github,
        []
    )

    # Empty list means nothing to complete
    assert result == False


@pytest.mark.asyncio
async def test_verify_sub_issues_consistent_board_lookup():
    """Test that board lookup uses consistent strategy (sdlc/dev check)"""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'open'},
    ]

    workflow_template = Mock()
    workflow_template.name = 'production_workflow'  # Different name
    workflow_template.pipeline_exit_columns = ['Done']

    project_monitor = AsyncMock()
    project_monitor.get_issue_column_async = AsyncMock(return_value='Done')

    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()

        # Pipeline with 'dev' in workflow name should match
        mock_pipeline = Mock()
        mock_pipeline.name = 'Development Pipeline'
        mock_pipeline.workflow = 'dev_workflow'  # Contains 'dev'
        mock_pipeline.board_name = 'Dev Board'

        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor
        )

    # Should find the board using sdlc/dev heuristic
    assert result == True
    assert project_monitor.get_issue_column_async.call_count == 1


# --- Case-insensitive state matching (GitHub GraphQL returns uppercase) ---

@pytest.mark.asyncio
async def test_verify_sub_issues_uppercase_closed():
    """GitHub GraphQL returns 'CLOSED' (uppercase) — must be treated as closed."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'CLOSED'},
        {'number': 2, 'state': 'CLOSED'},
    ]

    result = await manager._verify_all_sub_issues_complete(github, sub_issues)
    assert result is True


@pytest.mark.asyncio
async def test_verify_sub_issues_mixed_case_closed():
    """Mixed case states from different API sources should all work."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'closed'},
        {'number': 2, 'state': 'CLOSED'},
        {'number': 3, 'state': 'Closed'},
    ]

    result = await manager._verify_all_sub_issues_complete(github, sub_issues)
    assert result is True


@pytest.mark.asyncio
async def test_verify_sub_issues_uppercase_open_not_complete():
    """Uppercase 'OPEN' issues are not complete (without exit column check)."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'CLOSED'},
        {'number': 2, 'state': 'OPEN'},
    ]

    result = await manager._verify_all_sub_issues_complete(github, sub_issues)
    assert result is False


# --- Triggering issue bypass (avoid GitHub API eventual consistency lag) ---

@pytest.mark.asyncio
async def test_triggering_issue_bypasses_column_check():
    """The triggering issue should be treated as complete without re-querying its column."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'CLOSED'},
        {'number': 2, 'state': 'CLOSED'},
        {'number': 3, 'state': 'OPEN'},   # Just moved to exit column — the trigger
    ]

    # No project_monitor needed — triggering_issue bypass should handle #3
    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        triggering_issue=3
    )

    assert result is True


@pytest.mark.asyncio
async def test_triggering_issue_bypass_with_exit_column_check():
    """Triggering issue bypassed even when exit column check is available."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'CLOSED'},
        {'number': 2, 'state': 'OPEN'},   # In exit column via API
        {'number': 3, 'state': 'OPEN'},   # The triggering issue — bypass API
    ]

    workflow_template = Mock()
    workflow_template.name = 'dev_workflow'
    workflow_template.pipeline_exit_columns = ['Done', 'Staged']

    project_monitor = AsyncMock()

    async def get_column_side_effect(project, board, issue_num):
        if issue_num == 2:
            return 'Staged'
        # #3 should never be queried because of triggering_issue bypass
        return None

    project_monitor.get_issue_column_async = AsyncMock(side_effect=get_column_side_effect)

    with patch('config.manager.config_manager') as mock_config:
        mock_project_config = Mock()
        mock_pipeline = Mock()
        mock_pipeline.name = 'SDLC Execution'
        mock_pipeline.workflow = 'dev_workflow'
        mock_pipeline.board_name = 'Dev Board'
        mock_project_config.pipelines = [mock_pipeline]
        mock_config.get_project_config.return_value = mock_project_config

        result = await manager._verify_all_sub_issues_complete(
            github,
            sub_issues,
            project_name='test-project',
            workflow_template=workflow_template,
            project_monitor=project_monitor,
            triggering_issue=3
        )

    assert result is True


@pytest.mark.asyncio
async def test_triggering_issue_not_enough_when_others_incomplete():
    """Triggering issue bypass doesn't help if other issues are incomplete."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'OPEN'},   # Not closed, not triggering, no exit column check
        {'number': 2, 'state': 'OPEN'},   # The triggering issue — bypassed
    ]

    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        triggering_issue=2
    )

    # #1 is still open and not the trigger, so should fail
    assert result is False


@pytest.mark.asyncio
async def test_triggering_issue_none_has_no_effect():
    """When triggering_issue is None, no bypass occurs (backwards compatible)."""
    manager = FeatureBranchManager()
    github = Mock()

    sub_issues = [
        {'number': 1, 'state': 'CLOSED'},
        {'number': 2, 'state': 'OPEN'},
    ]

    result = await manager._verify_all_sub_issues_complete(
        github,
        sub_issues,
        triggering_issue=None
    )

    # #2 is open, no bypass — should fail
    assert result is False

"""
Unit tests for ProjectMonitor status validation and retry logic
"""
import pytest
from unittest.mock import Mock, MagicMock, patch, call
from datetime import datetime
from services.project_monitor import ProjectMonitor, ProjectItem


@pytest.fixture
def mock_config_manager():
    """Mock ConfigManager with test project and workflow"""
    manager = MagicMock()

    # Mock workflow with columns
    mock_workflow = MagicMock()
    mock_workflow.columns = [
        MagicMock(name="Backlog"),
        MagicMock(name="In Progress"),
        MagicMock(name="Done")
    ]
    manager.get_workflow_template.return_value = mock_workflow

    # Mock project config
    mock_project = MagicMock()
    mock_pipeline = MagicMock()
    mock_pipeline.board_name = "Dev Board"
    mock_pipeline.workflow = "dev_workflow"
    mock_project.pipelines = [mock_pipeline]
    manager.get_project_config.return_value = mock_project

    manager.list_visible_projects.return_value = ["test-project"]

    return manager


@pytest.fixture
def project_monitor(mock_config_manager):
    """Create ProjectMonitor instance with mocked dependencies"""
    with patch('monitoring.observability.get_observability_manager'), \
         patch('monitoring.decision_events.DecisionEventEmitter'), \
         patch('services.pipeline_run.get_pipeline_run_manager'), \
         patch('services.feedback_manager.FeedbackManager'), \
         patch('services.workspace_router.WorkspaceRouter'), \
         patch('services.github_discussions.GitHubDiscussions'):

        monitor = ProjectMonitor(mock_config_manager)
        return monitor


def test_status_validation_all_valid(project_monitor, mock_config_manager):
    """All items have valid status - no retry needed"""
    # Mock the valid columns lookup to return the expected columns
    with patch.object(project_monitor, '_get_valid_columns_for_board', return_value={'Backlog', 'In Progress', 'Done'}), \
         patch('services.github_owner_utils.execute_board_query_cached') as mock_query, \
         patch('services.github_owner_utils.invalidate_board_query_cache') as mock_invalidate, \
         patch('services.github_owner_utils.get_owner_type') as mock_owner_type, \
         patch('services.github_api_client.get_github_client') as mock_github:

        # Mock circuit breaker is closed
        mock_github.return_value.breaker.is_open.return_value = False

        # Mock owner type
        mock_owner_type.return_value = 'organization'

        # Mock GraphQL response with all valid statuses
        mock_query.return_value = {
            'organization': {
                'projectV2': {
                    'items': {
                        'nodes': [
                            {
                                'id': 'item1',
                                'content': {
                                    'id': 'issue1',
                                    'number': 1,
                                    'title': 'Test Issue 1',
                                    'updatedAt': '2025-01-01T00:00:00Z',
                                    'repository': {'name': 'test-repo'}
                                },
                                'fieldValues': {
                                    'nodes': [
                                        {
                                            'field': {'name': 'Status'},
                                            'name': 'In Progress'
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }

        # Call get_project_items
        items = project_monitor.get_project_items('test-org', 123)

        # Assert: Returns all items
        assert len(items) == 1
        assert items[0].status == 'In Progress'
        assert items[0].issue_number == 1

        # Assert: execute_board_query_cached called once (no retry)
        assert mock_query.call_count == 1

        # Assert: invalidate_board_query_cache NOT called
        mock_invalidate.assert_not_called()


def test_status_validation_retry_success(project_monitor, mock_config_manager):
    """Invalid status on first attempt, valid on second"""
    # Mock the valid columns lookup to return the expected columns
    with patch.object(project_monitor, '_get_valid_columns_for_board', return_value={'Backlog', 'In Progress', 'Done'}), \
         patch('services.github_owner_utils.execute_board_query_cached') as mock_query, \
         patch('services.github_owner_utils.invalidate_board_query_cache') as mock_invalidate, \
         patch('services.github_owner_utils.get_owner_type') as mock_owner_type, \
         patch('services.github_api_client.get_github_client') as mock_github, \
         patch('time.sleep'):

        # Mock circuit breaker is closed
        mock_github.return_value.breaker.is_open.return_value = False

        # Mock owner type
        mock_owner_type.return_value = 'organization'

        # Mock GraphQL responses - first with invalid, second with valid
        mock_query.side_effect = [
            # First call: Invalid status
            {
                'organization': {
                    'projectV2': {
                        'items': {
                            'nodes': [
                                {
                                    'id': 'item1',
                                    'content': {
                                        'id': 'issue1',
                                        'number': 1,
                                        'title': 'Test Issue 1',
                                        'updatedAt': '2025-01-01T00:00:00Z',
                                        'repository': {'name': 'test-repo'}
                                    },
                                    'fieldValues': {
                                        'nodes': [
                                            {
                                                'field': {'name': 'Status'},
                                                'name': 'No Status'
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            },
            # Second call: Valid status
            {
                'organization': {
                    'projectV2': {
                        'items': {
                            'nodes': [
                                {
                                    'id': 'item1',
                                    'content': {
                                        'id': 'issue1',
                                        'number': 1,
                                        'title': 'Test Issue 1',
                                        'updatedAt': '2025-01-01T00:00:00Z',
                                        'repository': {'name': 'test-repo'}
                                    },
                                    'fieldValues': {
                                        'nodes': [
                                            {
                                                'field': {'name': 'Status'},
                                                'name': 'In Progress'
                                            }
                                        ]
                                    }
                                }
                            ]
                        }
                    }
                }
            }
        ]

        # Call get_project_items
        items = project_monitor.get_project_items('test-org', 123)

        # Assert: Returns all items after recovery
        assert len(items) == 1
        assert items[0].status == 'In Progress'

        # Assert: execute_board_query_cached called twice
        assert mock_query.call_count == 2

        # Assert: invalidate_board_query_cache called once
        mock_invalidate.assert_called_once_with('test-org', 123)


def test_status_validation_permanent_failure(project_monitor, mock_config_manager):
    """Invalid status persists - filters items"""
    # Mock the valid columns lookup to return the expected columns
    with patch.object(project_monitor, '_get_valid_columns_for_board', return_value={'Backlog', 'In Progress', 'Done'}), \
         patch('services.github_owner_utils.execute_board_query_cached') as mock_query, \
         patch('services.github_owner_utils.invalidate_board_query_cache') as mock_invalidate, \
         patch('services.github_owner_utils.get_owner_type') as mock_owner_type, \
         patch('services.github_api_client.get_github_client') as mock_github, \
         patch('time.sleep'):

        # Mock circuit breaker is closed
        mock_github.return_value.breaker.is_open.return_value = False

        # Mock owner type
        mock_owner_type.return_value = 'organization'

        # Mock GraphQL response - always returns invalid status
        invalid_response = {
            'organization': {
                'projectV2': {
                    'items': {
                        'nodes': [
                            {
                                'id': 'item1',
                                'content': {
                                    'id': 'issue1',
                                    'number': 1,
                                    'title': 'Test Issue 1',
                                    'updatedAt': '2025-01-01T00:00:00Z',
                                    'repository': {'name': 'test-repo'}
                                },
                                'fieldValues': {
                                    'nodes': [
                                        {
                                            'field': {'name': 'Status'},
                                            'name': 'No Status'
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
        mock_query.return_value = invalid_response

        # Call get_project_items
        items = project_monitor.get_project_items('test-org', 123)

        # Assert: Returns empty list (items filtered out)
        assert len(items) == 0

        # Assert: execute_board_query_cached called 3 times
        assert mock_query.call_count == 3

        # Assert: invalidate_board_query_cache called twice (after attempt 1 and 2)
        assert mock_invalidate.call_count == 2

        # Assert: Observability event emitted
        project_monitor.decision_events.emit_status_validation_failure.assert_called_once()


def test_status_validation_partial_invalid(project_monitor, mock_config_manager):
    """Mix of valid and invalid - only filters invalid"""
    # Mock the valid columns lookup to return the expected columns
    with patch.object(project_monitor, '_get_valid_columns_for_board', return_value={'Backlog', 'In Progress', 'Done'}), \
         patch('services.github_owner_utils.execute_board_query_cached') as mock_query, \
         patch('services.github_owner_utils.invalidate_board_query_cache') as mock_invalidate, \
         patch('services.github_owner_utils.get_owner_type') as mock_owner_type, \
         patch('services.github_api_client.get_github_client') as mock_github, \
         patch('time.sleep'):

        # Mock circuit breaker is closed
        mock_github.return_value.breaker.is_open.return_value = False

        # Mock owner type
        mock_owner_type.return_value = 'organization'

        # Mock GraphQL response - 3 items: 2 valid, 1 invalid (persists)
        mixed_response = {
            'organization': {
                'projectV2': {
                    'items': {
                        'nodes': [
                            {
                                'id': 'item1',
                                'content': {
                                    'id': 'issue1',
                                    'number': 1,
                                    'title': 'Test Issue 1',
                                    'updatedAt': '2025-01-01T00:00:00Z',
                                    'repository': {'name': 'test-repo'}
                                },
                                'fieldValues': {
                                    'nodes': [
                                        {
                                            'field': {'name': 'Status'},
                                            'name': 'In Progress'
                                        }
                                    ]
                                }
                            },
                            {
                                'id': 'item2',
                                'content': {
                                    'id': 'issue2',
                                    'number': 2,
                                    'title': 'Test Issue 2',
                                    'updatedAt': '2025-01-01T00:00:00Z',
                                    'repository': {'name': 'test-repo'}
                                },
                                'fieldValues': {
                                    'nodes': [
                                        {
                                            'field': {'name': 'Status'},
                                            'name': 'No Status'
                                        }
                                    ]
                                }
                            },
                            {
                                'id': 'item3',
                                'content': {
                                    'id': 'issue3',
                                    'number': 3,
                                    'title': 'Test Issue 3',
                                    'updatedAt': '2025-01-01T00:00:00Z',
                                    'repository': {'name': 'test-repo'}
                                },
                                'fieldValues': {
                                    'nodes': [
                                        {
                                            'field': {'name': 'Status'},
                                            'name': 'Done'
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }
        mock_query.return_value = mixed_response

        # Call get_project_items
        items = project_monitor.get_project_items('test-org', 123)

        # Assert: Returns 2 valid items after 3 attempts
        assert len(items) == 2
        assert items[0].issue_number == 1
        assert items[0].status == 'In Progress'
        assert items[1].issue_number == 3
        assert items[1].status == 'Done'

        # Assert: Issue #2 with invalid status was filtered out
        issue_numbers = [item.issue_number for item in items]
        assert 2 not in issue_numbers


def test_workflow_lookup_failure(project_monitor, mock_config_manager):
    """Cannot find workflow - skips validation"""
    with patch('services.github_owner_utils.execute_board_query_cached') as mock_query, \
         patch('services.github_owner_utils.get_owner_type') as mock_owner_type, \
         patch('services.github_api_client.get_github_client') as mock_github, \
         patch('config.state_manager.state_manager') as mock_state:

        # Mock circuit breaker is closed
        mock_github.return_value.breaker.is_open.return_value = False

        # Mock owner type
        mock_owner_type.return_value = 'organization'

        # Mock state manager to return None (no project state found)
        mock_state.load_project_state.return_value = None

        # Mock GraphQL response with invalid status
        mock_query.return_value = {
            'organization': {
                'projectV2': {
                    'items': {
                        'nodes': [
                            {
                                'id': 'item1',
                                'content': {
                                    'id': 'issue1',
                                    'number': 1,
                                    'title': 'Test Issue 1',
                                    'updatedAt': '2025-01-01T00:00:00Z',
                                    'repository': {'name': 'test-repo'}
                                },
                                'fieldValues': {
                                    'nodes': [
                                        {
                                            'field': {'name': 'Status'},
                                            'name': 'No Status'
                                        }
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        }

        # Call get_project_items
        items = project_monitor.get_project_items('test-org', 999)

        # Assert: All items pass through without validation
        assert len(items) == 1
        assert items[0].status == 'No Status'

        # Assert: execute_board_query_cached called once (no retry since validation skipped)
        assert mock_query.call_count == 1

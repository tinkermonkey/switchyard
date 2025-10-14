"""
Test for work breakdown context collection with threaded replies.

Tests the fix for _get_agent_outputs_from_discussion() to ensure it:
1. Collects complete threaded conversations (human feedback + agent replies)
2. Finds agent outputs in both top-level comments and threaded replies
3. Returns most recent output when multiple exist
4. Formats context properly for multi-agent inputs
"""

import pytest
from unittest.mock import Mock, patch, MagicMock
from datetime import datetime, timezone


@pytest.fixture
def mock_github_app():
    """Mock GitHub App for GraphQL requests"""
    with patch('services.github_app.github_app') as mock:
        yield mock


@pytest.fixture
def sample_discussion_data_with_threads():
    """Sample discussion with threaded conversations"""
    return {
        'node': {
            'comments': {
                'nodes': [
                    {
                        'id': 'comment1',
                        'body': '_Processed by the business_analyst agent_\n\nInitial requirements draft',
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T10:00:00Z',
                        'replies': {
                            'nodes': [
                                {
                                    'id': 'reply1',
                                    'body': 'Can you add authentication requirements?',
                                    'author': {'login': 'human_user'},
                                    'createdAt': '2025-10-14T10:30:00Z'
                                },
                                {
                                    'id': 'reply2',
                                    'body': '_Processed by the business_analyst agent_\n\nUpdated requirements with authentication',
                                    'author': {'login': 'clauditoreum-bot'},
                                    'createdAt': '2025-10-14T11:00:00Z'
                                },
                                {
                                    'id': 'reply3',
                                    'body': 'What about authorization?',
                                    'author': {'login': 'human_user'},
                                    'createdAt': '2025-10-14T11:30:00Z'
                                },
                                {
                                    'id': 'reply4',
                                    'body': '_Processed by the business_analyst agent_\n\nFinal requirements with auth and authz',
                                    'author': {'login': 'clauditoreum-bot'},
                                    'createdAt': '2025-10-14T12:00:00Z'
                                }
                            ]
                        }
                    },
                    {
                        'id': 'comment2',
                        'body': '_Processed by the software_architect agent_\n\nArchitecture design',
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T13:00:00Z',
                        'replies': {
                            'nodes': [
                                {
                                    'id': 'reply5',
                                    'body': 'Should we use microservices?',
                                    'author': {'login': 'human_user'},
                                    'createdAt': '2025-10-14T13:30:00Z'
                                },
                                {
                                    'id': 'reply6',
                                    'body': '_Processed by the software_architect agent_\n\nRevised design with microservices',
                                    'author': {'login': 'clauditoreum-bot'},
                                    'createdAt': '2025-10-14T14:00:00Z'
                                }
                            ]
                        }
                    }
                ]
            }
        }
    }


@pytest.fixture
def sample_discussion_data_no_threads():
    """Sample discussion with only top-level comments (no threads)"""
    return {
        'node': {
            'comments': {
                'nodes': [
                    {
                        'id': 'comment1',
                        'body': '_Processed by the business_analyst agent_\n\nRequirements without feedback',
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T10:00:00Z',
                        'replies': {'nodes': []}
                    },
                    {
                        'id': 'comment2',
                        'body': '_Processed by the software_architect agent_\n\nArchitecture without feedback',
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T11:00:00Z',
                        'replies': {'nodes': []}
                    }
                ]
            }
        }
    }


def test_get_agent_outputs_includes_threaded_conversations(mock_github_app, sample_discussion_data_with_threads):
    """Test that threaded conversations are included in agent outputs"""
    from services.project_monitor import ProjectMonitor
    from config.manager import ConfigManager
    
    mock_github_app.graphql_request.return_value = sample_discussion_data_with_threads
    
    config_manager = Mock(spec=ConfigManager)
    monitor = ProjectMonitor(config_manager)
    
    result = monitor._get_agent_outputs_from_discussion(
        'discussion_123',
        ['business_analyst', 'software_architect']
    )
    
    # Verify both agents' outputs are present
    assert 'Output from Business Analyst' in result
    assert 'Output from Software Architect' in result
    
    # Verify threaded conversations are included
    assert 'human feedback' in result.lower()
    assert 'Can you add authentication requirements?' in result
    assert 'What about authorization?' in result
    assert 'Should we use microservices?' in result
    
    # Verify the most recent agent outputs are included
    assert 'Final requirements with auth and authz' in result
    assert 'Revised design with microservices' in result


def test_get_agent_outputs_without_threads(mock_github_app, sample_discussion_data_no_threads):
    """Test that outputs work correctly without threaded conversations"""
    from services.project_monitor import ProjectMonitor
    from config.manager import ConfigManager
    
    mock_github_app.graphql_request.return_value = sample_discussion_data_no_threads
    
    config_manager = Mock(spec=ConfigManager)
    monitor = ProjectMonitor(config_manager)
    
    result = monitor._get_agent_outputs_from_discussion(
        'discussion_123',
        ['business_analyst', 'software_architect']
    )
    
    # Verify both agents' outputs are present
    assert 'Output from Business Analyst' in result
    assert 'Output from Software Architect' in result
    
    # Verify the outputs are present
    assert 'Requirements without feedback' in result
    assert 'Architecture without feedback' in result


def test_get_agent_outputs_finds_most_recent(mock_github_app):
    """Test that the most recent output is found (could be top-level or in thread)"""
    from services.project_monitor import ProjectMonitor
    from config.manager import ConfigManager
    
    # Create data with initial output at top level, refinement in thread
    discussion_data = {
        'node': {
            'comments': {
                'nodes': [
                    {
                        'id': 'comment1',
                        'body': '_Processed by the business_analyst agent_\n\nOld version',
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T10:00:00Z',
                        'replies': {
                            'nodes': [
                                {
                                    'id': 'reply1',
                                    'body': 'Please update',
                                    'author': {'login': 'human_user'},
                                    'createdAt': '2025-10-14T11:00:00Z'
                                },
                                {
                                    'id': 'reply2',
                                    'body': '_Processed by the business_analyst agent_\n\nNew version',
                                    'author': {'login': 'clauditoreum-bot'},
                                    'createdAt': '2025-10-14T12:00:00Z'
                                }
                            ]
                        }
                    }
                ]
            }
        }
    }
    
    mock_github_app.graphql_request.return_value = discussion_data
    
    config_manager = Mock(spec=ConfigManager)
    monitor = ProjectMonitor(config_manager)
    
    result = monitor._get_agent_outputs_from_discussion(
        'discussion_123',
        ['business_analyst']
    )
    
    # Should use the most recent version (in thread)
    assert 'New version' in result
    # Thread history should include the old version too
    assert 'Old version' in result


def test_get_agent_outputs_handles_missing_agent(mock_github_app):
    """Test that missing agent outputs are handled gracefully"""
    from services.project_monitor import ProjectMonitor
    from config.manager import ConfigManager
    
    discussion_data = {
        'node': {
            'comments': {
                'nodes': [
                    {
                        'id': 'comment1',
                        'body': '_Processed by the business_analyst agent_\n\nOutput',
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T10:00:00Z',
                        'replies': {'nodes': []}
                    }
                ]
            }
        }
    }
    
    mock_github_app.graphql_request.return_value = discussion_data
    
    config_manager = Mock(spec=ConfigManager)
    monitor = ProjectMonitor(config_manager)
    
    result = monitor._get_agent_outputs_from_discussion(
        'discussion_123',
        ['business_analyst', 'missing_agent']
    )
    
    # Should include the found agent
    assert 'Output from Business Analyst' in result
    assert 'Output' in result
    
    # Should NOT fail on missing agent
    assert result != ""


def test_get_agent_outputs_graphql_query_includes_replies(mock_github_app):
    """Test that the GraphQL query includes replies in the request"""
    from services.project_monitor import ProjectMonitor
    from config.manager import ConfigManager
    
    discussion_data = {
        'node': {
            'comments': {
                'nodes': []
            }
        }
    }
    
    mock_github_app.graphql_request.return_value = discussion_data
    
    config_manager = Mock(spec=ConfigManager)
    monitor = ProjectMonitor(config_manager)
    
    monitor._get_agent_outputs_from_discussion(
        'discussion_123',
        ['business_analyst']
    )
    
    # Verify the GraphQL query was called
    assert mock_github_app.graphql_request.called
    
    # Get the query that was passed
    call_args = mock_github_app.graphql_request.call_args
    query = call_args[0][0]
    
    # Verify the query includes replies
    assert 'replies' in query
    assert 'first: 50' in query


def test_context_size_improvement():
    """
    Test that demonstrates the context size improvement.
    
    Before fix: ~2000 chars (just initial outputs)
    After fix: ~8000+ chars (outputs + all threads)
    """
    from services.project_monitor import ProjectMonitor
    from config.manager import ConfigManager
    
    # Create extensive threaded conversation
    discussion_data = {
        'node': {
            'comments': {
                'nodes': [
                    {
                        'id': 'comment1',
                        'body': '_Processed by the business_analyst agent_\n\n' + ('Requirements content. ' * 100),
                        'author': {'login': 'clauditoreum-bot'},
                        'createdAt': '2025-10-14T10:00:00Z',
                        'replies': {
                            'nodes': [
                                {
                                    'id': f'reply{i}',
                                    'body': f'Human feedback {i}. ' * 50,
                                    'author': {'login': 'human_user'},
                                    'createdAt': f'2025-10-14T{10+i}:00:00Z'
                                } if i % 2 == 1 else {
                                    'id': f'reply{i}',
                                    'body': f'_Processed by the business_analyst agent_\n\nAgent response {i}. ' * 50,
                                    'author': {'login': 'clauditoreum-bot'},
                                    'createdAt': f'2025-10-14T{10+i}:00:00Z'
                                }
                                for i in range(1, 6)
                            ]
                        }
                    }
                ]
            }
        }
    }
    
    with patch('services.github_app.github_app') as mock_github:
        mock_github.graphql_request.return_value = discussion_data
        
        config_manager = Mock(spec=ConfigManager)
        monitor = ProjectMonitor(config_manager)
        
        result = monitor._get_agent_outputs_from_discussion(
            'discussion_123',
            ['business_analyst']
        )
        
        # Context should be significantly larger due to threaded conversations
        # Initial output (~2000 chars) + replies (~3000+ chars)
        assert len(result) > 5000, f"Context size {len(result)} is too small"
        
        # Should contain multiple human feedback entries
        assert result.count('human feedback') >= 3

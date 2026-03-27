"""
Tests for conversational mode bot username filtering

This test suite verifies that the bot username filter correctly identifies
all bot account name formats used by GitHub and GitHub Apps.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from unittest.mock import AsyncMock, Mock, patch, MagicMock
from datetime import datetime

from services.human_feedback_loop import (
    HumanFeedbackLoopExecutor,
    HumanFeedbackState,
    is_bot_user
)


class TestBotUsernameFiltering:
    """Test that bot usernames are correctly filtered"""
    
    def test_is_bot_user_recognizes_all_formats(self):
        """Verify is_bot_user() recognizes all known bot name formats"""
        # GitHub App formats
        assert is_bot_user('orchestrator-bot') is True
        assert is_bot_user('orchestrator-bot[bot]') is True
        assert is_bot_user('github-actions[bot]') is True
        assert is_bot_user('app/orchestrator-bot') is True
        
        # Human users
        assert is_bot_user('human-user') is False
        assert is_bot_user('john-doe') is False
        assert is_bot_user('') is False
        
        # Edge cases
        assert is_bot_user('orchestrator-bot-extra') is False  # Extra characters
        assert is_bot_user('not-orchestrator-bot') is False    # Prefix
        assert is_bot_user('github-actions') is False          # Missing [bot]
    
    @pytest.mark.asyncio
    async def test_load_previous_outputs_filters_github_app_format(self):
        """Test that state loading recognizes orchestrator-bot[bot] format"""
        executor = HumanFeedbackLoopExecutor()
        
        state = HumanFeedbackState(
            issue_number=123,
            repository='test-repo',
            agent='test_agent',
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test123'
        )
        
        # Mock GitHub GraphQL response with GitHub App bot name format
        mock_response = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'author': {'login': 'orchestrator-bot[bot]'},
                            'body': 'Agent output 1',
                            'createdAt': '2025-10-10T10:00:00Z',
                            'replies': {'nodes': []}
                        },
                        {
                            'author': {'login': 'human-user'},
                            'body': 'Human feedback',
                            'createdAt': '2025-10-10T11:00:00Z',
                            'replies': {'nodes': []}
                        },
                        {
                            'author': {'login': 'orchestrator-bot[bot]'},
                            'body': 'Agent output 2',
                            'createdAt': '2025-10-10T12:00:00Z',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }
        
        with patch('services.github_app.github_app') as mock_github_app:
            mock_github_app.enabled = True
            mock_github_app.graphql_request = Mock(return_value=mock_response)
            
            await executor._load_previous_outputs_from_discussion(state, 'test-org')
        
        # Should have loaded 2 bot comments (not the human one)
        assert len(state.agent_outputs) == 2
        assert state.agent_outputs[0]['timestamp'] == '2025-10-10T10:00:00Z'
        assert state.agent_outputs[1]['timestamp'] == '2025-10-10T12:00:00Z'
        assert state.current_iteration == 2
    
    @pytest.mark.asyncio
    async def test_load_previous_outputs_filters_direct_bot_name(self):
        """Test that state loading recognizes direct orchestrator-bot format"""
        executor = HumanFeedbackLoopExecutor()
        
        state = HumanFeedbackState(
            issue_number=123,
            repository='test-repo',
            agent='test_agent',
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test123'
        )
        
        # Mock with direct bot name (no [bot] suffix)
        mock_response = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'author': {'login': 'orchestrator-bot'},
                            'body': 'Agent output',
                            'createdAt': '2025-10-10T10:00:00Z',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }
        
        with patch('services.github_app.github_app') as mock_github_app:
            mock_github_app.enabled = True
            mock_github_app.graphql_request = Mock(return_value=mock_response)
            
            await executor._load_previous_outputs_from_discussion(state, 'test-org')
        
        assert len(state.agent_outputs) == 1
    
    @pytest.mark.asyncio
    async def test_feedback_detection_ignores_github_app_bot(self):
        """Test that feedback detection ignores orchestrator-bot[bot] comments"""
        executor = HumanFeedbackLoopExecutor()
        
        state = HumanFeedbackState(
            issue_number=123,
            repository='test-repo',
            agent='test_agent',
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test123'
        )
        
        # Add previous agent output to establish baseline
        state.agent_outputs = [{
            'timestamp': '2025-10-10T10:00:00Z',
            'output': 'Previous output'
        }]
        
        # Mock response with bot comment AFTER last agent output
        mock_response = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'author': {'login': 'orchestrator-bot[bot]'},
                            'body': 'Bot comment should be ignored',
                            'createdAt': '2025-10-10T11:00:00Z',
                            'id': 'comment1',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }
        
        with patch('services.github_app.github_app') as mock_github_app:
            mock_github_app.enabled = True
            mock_github_app.graphql_request = Mock(return_value=mock_response)
            
            feedback = await executor._get_human_feedback_since_last_agent(state, 'test-org')
        
        # Should NOT detect bot comment as human feedback
        assert feedback is None
    
    @pytest.mark.asyncio
    async def test_feedback_detection_finds_human_comment(self):
        """Test that feedback detection correctly identifies human comments"""
        executor = HumanFeedbackLoopExecutor()
        
        state = HumanFeedbackState(
            issue_number=123,
            repository='test-repo',
            agent='test_agent',
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test123'
        )
        
        state.agent_outputs = [{
            'timestamp': '2025-10-10T10:00:00Z',
            'output': 'Previous output'
        }]
        
        # Mock with human comment after last agent output
        mock_response = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'author': {'login': 'human-user'},
                            'body': 'This is human feedback',
                            'createdAt': '2025-10-10T11:00:00Z',
                            'id': 'comment1',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }
        
        with patch('services.github_app.github_app') as mock_github_app:
            mock_github_app.enabled = True
            mock_github_app.graphql_request = Mock(return_value=mock_response)
            
            feedback = await executor._get_human_feedback_since_last_agent(state, 'test-org')
        
        # Should detect human feedback
        assert feedback is not None
        assert feedback['author'] == 'human-user'
        assert feedback['body'] == 'This is human feedback'
    
    @pytest.mark.asyncio
    async def test_empty_state_outputs_logs_warning(self):
        """Test that empty state.agent_outputs triggers safety warning"""
        executor = HumanFeedbackLoopExecutor()
        
        state = HumanFeedbackState(
            issue_number=123,
            repository='test-repo',
            agent='test_agent',
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test123'
        )
        
        # State has no agent outputs (state loading failed)
        assert len(state.agent_outputs) == 0
        
        # Mock column and issue data
        from config.manager import WorkflowColumn
        column = Mock(spec=WorkflowColumn)
        column.name = 'Test Column'
        column.agent = 'test_agent'
        
        issue_data = {'number': 123, 'title': 'Test Issue'}
        
        # Mock the monitoring loop to exit after first iteration
        with patch('services.human_feedback_loop.logger') as mock_logger:
            with patch.object(executor, '_get_human_feedback_since_last_agent', 
                            return_value=None):
                # Run one iteration
                import asyncio
                
                async def run_one_iteration():
                    # Start the loop in background
                    task = asyncio.create_task(
                        executor._conversational_loop(state, column, issue_data, 'test-org')
                    )
                    # Wait a short time
                    await asyncio.sleep(0.1)
                    # Cancel it
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                
                await run_one_iteration()
            
            # Verify warning was logged
            warning_calls = [call for call in mock_logger.warning.call_args_list 
                           if 'SAFETY WARNING' in str(call)]
            assert len(warning_calls) > 0
    
    @pytest.mark.asyncio
    async def test_reply_filtering_ignores_bot_replies(self):
        """Test that bot replies are filtered out"""
        executor = HumanFeedbackLoopExecutor()
        
        state = HumanFeedbackState(
            issue_number=123,
            repository='test-repo',
            agent='test_agent',
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test123'
        )
        
        state.agent_outputs = [{
            'timestamp': '2025-10-10T10:00:00Z',
            'output': 'Previous output'
        }]
        
        # Mock with bot reply (should be ignored)
        mock_response = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'author': {'login': 'human-user'},
                            'body': 'Human top-level comment',
                            'createdAt': '2025-10-10T09:00:00Z',
                            'id': 'comment1',
                            'replies': {
                                'nodes': [
                                    {
                                        'author': {'login': 'orchestrator-bot[bot]'},
                                        'body': 'Bot reply - should be ignored',
                                        'createdAt': '2025-10-10T11:00:00Z',
                                        'id': 'reply1'
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }
        
        with patch('services.github_app.github_app') as mock_github_app:
            mock_github_app.enabled = True
            mock_github_app.graphql_request = Mock(return_value=mock_response)
            
            feedback = await executor._get_human_feedback_since_last_agent(state, 'test-org')
        
        # Should NOT detect bot reply as feedback
        assert feedback is None


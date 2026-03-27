"""
Unit tests for human feedback detection

Tests the logic for detecting human comments in discussions,
determining parent comments, and building correct context.

Critical for conversational loops and review cycle resumption.
"""

import os
import pytest
if not os.path.isdir('/app'):
    pytest.skip("Requires Docker container environment", allow_module_level=True)

from datetime import datetime, timezone, timedelta
from services.human_feedback_loop import HumanFeedbackLoopExecutor, HumanFeedbackState


@pytest.fixture
def feedback_executor():
    """Create a HumanFeedbackLoopExecutor for testing"""
    return HumanFeedbackLoopExecutor()


@pytest.fixture
def feedback_state():
    """Create a basic HumanFeedbackState"""
    state = HumanFeedbackState(
        issue_number=96,
        repository='context-studio',
        agent='business_analyst',
        project_name='context-studio',
        board_name='idea-development',
        workspace_type='discussions',
        discussion_id='D_test123'
    )
    # Add an initial agent output
    state.agent_outputs.append({
        'output': 'Initial BA output',
        'timestamp': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    })
    return state


class TestFeedbackDetection:
    """Test detection of human feedback in discussions"""

    @pytest.mark.asyncio
    async def test_detect_human_top_level_comment(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Detect human feedback as top-level comment"""
        # Given: Discussion with BA output and human top-level comment
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'Human question here',
                            'author': {'login': 'tinkermonkey'},
                            'createdAt': datetime.now(timezone.utc).isoformat(),
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should detect human comment
        assert feedback is not None
        assert feedback['author'] == 'tinkermonkey'
        assert feedback['body'] == 'Human question here'
        assert feedback['parent_comment'] is None  # Top-level

    @pytest.mark.asyncio
    async def test_detect_human_reply(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Detect human feedback as reply to bot comment"""
        # Given: Discussion with BA output and human reply
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            'replies': {
                                'nodes': [
                                    {
                                        'id': 'reply_1',
                                        'body': 'Human question here',
                                        'author': {'login': 'tinkermonkey'},
                                        'createdAt': datetime.now(timezone.utc).isoformat()
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should detect human reply with parent
        assert feedback is not None
        assert feedback['author'] == 'tinkermonkey'
        assert feedback['body'] == 'Human question here'
        assert feedback['parent_comment'] is not None
        assert feedback['parent_comment']['id'] == 'comment_1'
        assert '_Processed by the business_analyst agent_' in feedback['parent_comment']['body']

    @pytest.mark.asyncio
    async def test_ignore_old_human_comments(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Ignore human comments older than last agent output"""
        # Given: Discussion with human comment BEFORE last agent output
        old_time = datetime.now(timezone.utc) - timedelta(minutes=20)
        recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)

        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'Old human comment',
                            'author': {'login': 'tinkermonkey'},
                            'createdAt': old_time.isoformat(),
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': recent_time.isoformat(),
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should NOT detect old comment
        assert feedback is None

    @pytest.mark.asyncio
    async def test_ignore_bot_comments(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Ignore bot comments (only detect human feedback)"""
        # Given: Discussion with only bot comments
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'RR output\n\n_Processed by the requirements_reviewer agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': datetime.now(timezone.utc).isoformat(),
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should NOT detect bot comments
        assert feedback is None

    @pytest.mark.asyncio
    async def test_detect_most_recent_feedback(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: If multiple human comments, detect most recent"""
        base_time = datetime.now(timezone.utc)

        # Given: Discussion with multiple human comments
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (base_time - timedelta(minutes=10)).isoformat(),
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'First human comment',
                            'author': {'login': 'tinkermonkey'},
                            'createdAt': (base_time - timedelta(minutes=5)).isoformat(),
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_3',
                            'body': 'Second human comment',
                            'author': {'login': 'tinkermonkey'},
                            'createdAt': base_time.isoformat(),
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should detect FIRST unprocessed human comment
        # (The function finds first match after last agent output)
        assert feedback is not None
        assert feedback['body'] == 'First human comment'


class TestParentCommentDetection:
    """Test parent comment detection for threaded replies"""

    @pytest.mark.asyncio
    async def test_parent_comment_includes_full_body(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Parent comment includes full body text"""
        ba_output = 'This is a long BA output\n\nWith multiple paragraphs\n\n_Processed by the business_analyst agent_'

        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': ba_output,
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            'replies': {
                                'nodes': [
                                    {
                                        'id': 'reply_1',
                                        'body': 'Question',
                                        'author': {'login': 'tinkermonkey'},
                                        'createdAt': datetime.now(timezone.utc).isoformat()
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Parent comment should include full BA output
        assert feedback is not None
        assert feedback['parent_comment'] is not None
        assert feedback['parent_comment']['body'] == ba_output
        assert 'multiple paragraphs' in feedback['parent_comment']['body']

    @pytest.mark.asyncio
    async def test_parent_comment_includes_author(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Parent comment includes author information"""
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            'replies': {
                                'nodes': [
                                    {
                                        'id': 'reply_1',
                                        'body': 'Question',
                                        'author': {'login': 'tinkermonkey'},
                                        'createdAt': datetime.now(timezone.utc).isoformat()
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Parent comment should include author
        assert feedback is not None
        assert feedback['parent_comment']['author'] == 'orchestrator-bot'

    @pytest.mark.asyncio
    async def test_reply_to_human_comment(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Human can reply to another human's comment"""
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'First human comment',
                            'author': {'login': 'user1'},
                            'createdAt': (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(),
                            'replies': {
                                'nodes': [
                                    {
                                        'id': 'reply_1',
                                        'body': 'Reply to first comment',
                                        'author': {'login': 'user2'},
                                        'createdAt': datetime.now(timezone.utc).isoformat()
                                    }
                                ]
                            }
                        }
                    ]
                }
            }
        }

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should detect FIRST human comment (top-level)
        assert feedback is not None
        assert feedback['body'] == 'First human comment'
        assert feedback['author'] == 'user1'


class TestTimezoneHandling:
    """Test timezone handling in feedback detection"""

    @pytest.mark.asyncio
    async def test_handles_timezone_aware_timestamps(
        self,
        feedback_executor,
        feedback_state,
        monkeypatch
    ):
        """Test: Correctly compares timezone-aware timestamps"""
        # Given: Timestamps with explicit timezone
        discussion = {
            'node': {
                'comments': {
                    'nodes': [
                        {
                            'id': 'comment_1',
                            'body': 'BA output\n\n_Processed by the business_analyst agent_',
                            'author': {'login': 'orchestrator-bot'},
                            'createdAt': '2025-10-03T13:00:00+00:00',
                            'replies': {'nodes': []}
                        },
                        {
                            'id': 'comment_2',
                            'body': 'Human comment',
                            'author': {'login': 'tinkermonkey'},
                            'createdAt': '2025-10-03T13:05:00+00:00',
                            'replies': {'nodes': []}
                        }
                    ]
                }
            }
        }

        # Update state with timezone-aware timestamp
        feedback_state.agent_outputs[-1]['timestamp'] = '2025-10-03T13:00:00+00:00'

        def mock_graphql(query, variables):
            return discussion

        from services.github_app import github_app
        monkeypatch.setattr(github_app, 'graphql_request', mock_graphql)

        # When: Check for feedback (should not raise timezone comparison error)
        feedback = await feedback_executor._get_human_feedback_since_last_agent(
            feedback_state,
            'tinkermonkey'
        )

        # Then: Should detect feedback without timezone errors
        assert feedback is not None
        assert feedback['body'] == 'Human comment'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

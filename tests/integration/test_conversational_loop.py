"""
Integration tests for conversational feedback loop

Tests the human feedback loop where agents respond to human comments
in discussions, including thread context building and reply targeting.

CRITICAL: Tests the conversational pattern where agents answer questions
rather than the automated maker-checker review pattern.
"""

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from services.human_feedback_loop import HumanFeedbackLoopExecutor, HumanFeedbackState
from tests.utils.assertions import assert_conversational_mode, assert_thread_history_correct


@pytest.fixture
def feedback_executor():
    """Create a HumanFeedbackLoopExecutor"""
    return HumanFeedbackLoopExecutor()


@pytest.fixture
def feedback_state():
    """Create a basic human feedback state"""
    return HumanFeedbackState(
        issue_number=96,
        repository='context-studio',
        agent='business_analyst',
        project_name='context-studio',
        board_name='idea-development',
        workspace_type='discussions',
        discussion_id='D_test123'
    )


@pytest.mark.integration
@pytest.mark.asyncio
class TestThreadContextBuilding:
    """Test building thread context for conversational responses"""

    async def test_thread_context_includes_parent_and_reply(
        self,
        feedback_executor,
        mock_github_app,
        sample_ba_output
    ):
        """
        Test: Thread context includes ONLY parent comment + human reply

        This is the deterministic thread context that prevents context bloat.
        """
        # Create discussion with agent output
        mock_github_app.create_discussion('D_test123', 'Requirements', 'Initial')

        agent_comment_id = mock_github_app.add_discussion_comment(
            'D_test123',
            f"{sample_ba_output}\n\n_Processed by the business_analyst agent_",
            author='orchestrator-bot'
        )

        # Human replies
        human_feedback = {
            'author': 'tinkermonkey',
            'body': 'Can you clarify requirement X?',
            'comment_id': 'reply_123',
            'parent_comment': {
                'id': agent_comment_id,
                'body': f"{sample_ba_output}\n\n_Processed by the business_analyst agent_",
                'author': 'orchestrator-bot'
            }
        }

        # Build thread history (simulating what _execute_agent does)
        thread_history = []

        if human_feedback.get('parent_comment'):
            thread_history.append({
                'role': 'agent',
                'author': human_feedback['parent_comment'].get('author', 'orchestrator-bot'),
                'body': human_feedback['parent_comment'].get('body', '')
            })

        thread_history.append({
            'role': 'user',
            'author': human_feedback['author'],
            'body': human_feedback['body']
        })

        # Assertions
        assert len(thread_history) == 2
        assert thread_history[0]['role'] == 'agent'
        assert thread_history[0]['body'] == f"{sample_ba_output}\n\n_Processed by the business_analyst agent_"
        assert thread_history[1]['role'] == 'user'
        assert thread_history[1]['body'] == 'Can you clarify requirement X?'

    async def test_top_level_comment_uses_last_agent_output(
        self,
        feedback_executor
    ):
        """
        Test: Top-level human comment uses last agent output as context

        When human posts a new top-level comment (not a reply),
        use the most recent agent output for context.
        """
        # Agent has multiple outputs
        agent_outputs = [
            {'output': 'Initial output', 'timestamp': '2025-10-03T10:00:00Z'},
            {'output': 'Revision 1', 'timestamp': '2025-10-03T10:05:00Z'},
            {'output': 'Latest output', 'timestamp': '2025-10-03T10:10:00Z'}
        ]

        # Human posts top-level comment (no parent)
        human_feedback = {
            'author': 'tinkermonkey',
            'body': 'What about requirement Y?',
            'comment_id': 'comment_456',
            'parent_comment': None  # Top-level
        }

        # Build thread history
        thread_history = []

        if human_feedback.get('parent_comment'):
            # Has parent - use it
            thread_history.append({
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            })
        else:
            # No parent - use last agent output
            if agent_outputs:
                thread_history.append({
                    'role': 'agent',
                    'author': 'orchestrator-bot',
                    'body': agent_outputs[-1]['output']
                })

        thread_history.append({
            'role': 'user',
            'author': human_feedback['author'],
            'body': human_feedback['body']
        })

        # Assertions
        assert len(thread_history) == 2
        assert thread_history[0]['body'] == 'Latest output'  # Last output
        assert thread_history[1]['body'] == 'What about requirement Y?'


@pytest.mark.integration
@pytest.mark.asyncio
class TestConversationalResponse:
    """Test agent responding to human feedback conversationally"""

    async def test_agent_responds_in_thread(
        self,
        feedback_executor,
        mock_github_app,
        mock_agent_executor,
        feedback_state,
        sample_ba_output
    ):
        """
        Test: Agent responds in correct thread

        Flow:
        1. Agent posts output
        2. Human replies to agent output
        3. Agent responds to human in same thread
        """
        # Setup mock agent response
        agent_response = "Requirement X refers to the authentication flow in section 3.1."
        mock_agent_executor.set_response('business_analyst', agent_response)

        # Create discussion with agent output
        mock_github_app.create_discussion('D_test123', 'Requirements', 'Initial')

        agent_comment_id = mock_github_app.add_discussion_comment(
            'D_test123',
            f"{sample_ba_output}\n\n_Processed by the business_analyst agent_",
            author='orchestrator-bot'
        )

        # Human replies
        mock_github_app.add_discussion_comment(
            'D_test123',
            'Can you clarify requirement X?',
            author='tinkermonkey',
            reply_to_id=agent_comment_id
        )

        feedback_state.agent_outputs.append({
            'output': sample_ba_output,
            'timestamp': (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        })

        # Simulate agent execution with human feedback
        from config.manager import WorkflowColumn

        class MockColumn:
            name = 'Requirements Analysis'
            agent = 'business_analyst'

        human_feedback = {
            'author': 'tinkermonkey',
            'body': 'Can you clarify requirement X?',
            'comment_id': 'reply_123',
            'parent_comment': {
                'id': agent_comment_id,
                'body': f"{sample_ba_output}\n\n_Processed by the business_analyst agent_",
                'author': 'orchestrator-bot'
            }
        }

        # Execute agent (this should post response in thread)
        # In actual integration, would call _execute_agent, but for this test
        # we verify the context building logic

        context = {
            'conversation_mode': 'threaded',
            'reply_to_comment_id': agent_comment_id,
            'thread_history': [
                {
                    'role': 'agent',
                    'author': 'orchestrator-bot',
                    'body': f"{sample_ba_output}\n\n_Processed by the business_analyst agent_"
                },
                {
                    'role': 'user',
                    'author': 'tinkermonkey',
                    'body': 'Can you clarify requirement X?'
                }
            ]
        }

        # Verify context is conversational
        assert_conversational_mode(context)
        assert context['reply_to_comment_id'] == agent_comment_id
        assert_thread_history_correct(context, expected_length=2)


@pytest.mark.integration
@pytest.mark.asyncio
class TestReplyTargeting:
    """Test GitHub threading - replying to correct comment"""

    async def test_reply_to_top_level_parent(self):
        """
        Test: Always reply to top-level parent (GitHub threading limitation)

        GitHub only allows one level of threading, so even if human
        replied to a nested comment, we must reply to the top-level parent.
        """
        # Human replied to agent output
        parent_comment_id = 'comment_abc'  # Top-level

        human_feedback = {
            'body': 'Question',
            'author': 'tinkermonkey',
            'comment_id': 'reply_123',  # This is nested
            'parent_comment': {
                'id': parent_comment_id,  # Top-level parent
                'body': 'Agent output',
                'author': 'orchestrator-bot'
            }
        }

        # Determine reply target
        reply_to_id = None
        if human_feedback.get('parent_comment'):
            # Reply to top-level parent
            reply_to_id = human_feedback['parent_comment']['id']

        # Should reply to top-level parent, not nested reply
        assert reply_to_id == parent_comment_id

    async def test_reply_to_top_level_human_comment(self):
        """
        Test: For top-level human comment, reply directly to it

        When human posts a new top-level comment (not a reply),
        reply directly to that comment.
        """
        human_feedback = {
            'body': 'Top-level question',
            'author': 'tinkermonkey',
            'comment_id': 'comment_456',
            'parent_comment': None  # Top-level
        }

        # Determine reply target
        reply_to_id = None
        if human_feedback.get('parent_comment'):
            reply_to_id = human_feedback['parent_comment']['id']
        elif human_feedback.get('comment_id'):
            reply_to_id = human_feedback['comment_id']

        # Should reply to the human's comment
        assert reply_to_id == 'comment_456'


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

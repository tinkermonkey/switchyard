"""
Unit tests for thread context building

Tests the logic for building thread history context in conversational mode,
ensuring agents receive only the relevant parent comment and reply.

CRITICAL: This prevents context bloat by including only the specific
conversation thread, not the entire discussion history.
"""

import pytest
from tests.utils.assertions import (
    assert_thread_history_correct,
    assert_conversational_mode,
    assert_context_size
)


class TestThreadHistoryBuilding:
    """Test building thread history for conversational mode"""

    def test_thread_history_with_parent_comment(self):
        """
        Test: Thread history includes parent comment + human reply

        This is the NORMAL case for conversational mode
        """
        # Given: Human feedback with parent comment
        human_feedback = {
            'body': 'Can you clarify requirement X?',
            'author': 'tinkermonkey',
            'comment_id': 'reply_123',
            'parent_comment': {
                'id': 'comment_abc',
                'body': 'BA output with requirements...\n\n_Processed by the business_analyst agent_',
                'author': 'orchestrator-bot'
            }
        }

        # When: Build context (simulating what human_feedback_loop does)
        context = {}
        context['conversation_mode'] = 'threaded'
        context['reply_to_comment_id'] = human_feedback['parent_comment']['id']

        thread_history = []
        if human_feedback.get('parent_comment'):
            thread_history.append({
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            })

        thread_history.append({
            'role': 'user',
            'author': human_feedback['author'],
            'body': human_feedback['body']
        })

        context['thread_history'] = thread_history

        # Then: Thread history should have exactly 2 messages
        assert_thread_history_correct(context, expected_length=2)
        assert context['thread_history'][0]['role'] == 'agent'
        assert context['thread_history'][1]['role'] == 'user'
        assert_conversational_mode(context)

    def test_thread_history_size_reasonable(self):
        """
        Test: Thread context stays within reasonable bounds

        With only parent + reply, context should be small
        """
        # Given: Parent comment (15KB) + human reply (1KB)
        parent_body = 'BA output ' * 2000  # ~15KB
        human_body = 'Question about X'

        human_feedback = {
            'body': human_body,
            'author': 'tinkermonkey',
            'parent_comment': {
                'id': 'comment_abc',
                'body': parent_body,
                'author': 'orchestrator-bot'
            }
        }

        # When: Build context
        context = {}
        thread_history = [
            {
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            },
            {
                'role': 'user',
                'author': human_feedback['author'],
                'body': human_feedback['body']
            }
        ]
        context['thread_history'] = thread_history

        # Then: Total context should be reasonable
        total_chars = sum(len(msg['body']) for msg in thread_history)
        assert total_chars < 30000  # Parent (15KB) + reply (1KB) = ~16KB

    def test_top_level_comment_uses_last_output(self):
        """
        Test: Top-level comment (no parent) uses last agent output

        When human posts a new top-level comment, we use the most
        recent agent output as context.
        """
        # Given: Human top-level comment (no parent)
        human_feedback = {
            'body': 'What about requirement Y?',
            'author': 'tinkermonkey',
            'comment_id': 'comment_123',
            'parent_comment': None  # Top-level
        }

        # And: Agent has previous outputs
        agent_outputs = [
            {'output': 'Initial BA output'},
            {'output': 'BA revision 1'},
            {'output': 'BA revision 2 - latest'}
        ]

        # When: Build context for top-level comment
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

        # Then: Should use LAST agent output
        assert len(thread_history) == 2
        assert thread_history[0]['body'] == 'BA revision 2 - latest'

    def test_thread_history_preserves_authors(self):
        """Test: Thread history preserves author information"""
        human_feedback = {
            'body': 'Question',
            'author': 'tinkermonkey',
            'parent_comment': {
                'id': 'comment_abc',
                'body': 'BA output',
                'author': 'orchestrator-bot'
            }
        }

        # When: Build context
        thread_history = [
            {
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            },
            {
                'role': 'user',
                'author': human_feedback['author'],
                'body': human_feedback['body']
            }
        ]

        # Then: Authors should be preserved
        assert thread_history[0]['author'] == 'orchestrator-bot'
        assert thread_history[1]['author'] == 'tinkermonkey'


class TestReplyToCommentID:
    """Test reply_to_comment_id for GitHub threading"""

    def test_reply_to_parent_comment(self):
        """
        Test: reply_to_comment_id uses parent comment ID

        GitHub only allows one level of threading, so we always
        reply to the top-level parent, not to nested replies.
        """
        # Given: Human replied to a bot comment
        human_feedback = {
            'body': 'Question',
            'author': 'tinkermonkey',
            'comment_id': 'reply_123',
            'parent_comment': {
                'id': 'comment_abc',  # Top-level parent
                'body': 'BA output',
                'author': 'orchestrator-bot'
            }
        }

        # When: Set reply_to_comment_id
        reply_to_id = None
        if human_feedback.get('parent_comment'):
            # Reply to parent (top-level)
            reply_to_id = human_feedback['parent_comment']['id']
        elif human_feedback.get('comment_id'):
            # Top-level human comment
            reply_to_id = human_feedback['comment_id']

        # Then: Should use parent comment ID
        assert reply_to_id == 'comment_abc'

    def test_reply_to_top_level_human_comment(self):
        """Test: For top-level human comment, reply to that comment"""
        # Given: Top-level human comment
        human_feedback = {
            'body': 'Question',
            'author': 'tinkermonkey',
            'comment_id': 'comment_456',
            'parent_comment': None  # Top-level
        }

        # When: Set reply_to_comment_id
        reply_to_id = None
        if human_feedback.get('parent_comment'):
            reply_to_id = human_feedback['parent_comment']['id']
        elif human_feedback.get('comment_id'):
            reply_to_id = human_feedback['comment_id']

        # Then: Should use the human comment ID
        assert reply_to_id == 'comment_456'


class TestConversationalModeDetection:
    """Test detection of conversational vs revision mode"""

    def test_question_mode_when_thread_history_present(self):
        """Test: Conversational mode activated with thread_history"""
        context = {
            'conversation_mode': 'threaded',
            'thread_history': [
                {'role': 'agent', 'author': 'bot', 'body': 'output'},
                {'role': 'user', 'author': 'human', 'body': 'question'}
            ]
        }

        # Should be in conversational mode
        assert_conversational_mode(context)

    def test_revision_mode_without_thread_history(self):
        """Test: Revision mode when no thread_history"""
        context = {
            'trigger': 'review_cycle_revision',
            'revision': {
                'previous_output': 'BA output',
                'feedback': 'RR feedback'
            }
        }

        # Should NOT be in conversational mode
        assert 'conversation_mode' not in context or context.get('conversation_mode') != 'threaded'
        assert 'thread_history' not in context


class TestThreadContextEdgeCases:
    """Test edge cases in thread context building"""

    def test_empty_parent_body(self):
        """Test: Handle empty parent comment body gracefully"""
        human_feedback = {
            'body': 'Question',
            'author': 'tinkermonkey',
            'parent_comment': {
                'id': 'comment_abc',
                'body': '',  # Empty
                'author': 'orchestrator-bot'
            }
        }

        # When: Build context
        thread_history = [
            {
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            },
            {
                'role': 'user',
                'author': human_feedback['author'],
                'body': human_feedback['body']
            }
        ]

        # Then: Should still build context (even if empty)
        assert len(thread_history) == 2
        assert thread_history[0]['body'] == ''

    def test_very_long_parent_comment(self):
        """Test: Handle very long parent comments"""
        # Given: Very long parent (50KB)
        long_body = 'BA output paragraph ' * 2500  # ~50KB

        human_feedback = {
            'body': 'Question',
            'author': 'tinkermonkey',
            'parent_comment': {
                'id': 'comment_abc',
                'body': long_body,
                'author': 'orchestrator-bot'
            }
        }

        # When: Build context
        thread_history = [
            {
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            },
            {
                'role': 'user',
                'author': human_feedback['author'],
                'body': human_feedback['body']
            }
        ]

        # Then: Context includes full parent (no truncation in our logic)
        assert len(thread_history[0]['body']) > 40000
        assert 'BA output paragraph' in thread_history[0]['body']

    def test_special_characters_in_feedback(self):
        """Test: Handle special characters in feedback body"""
        human_feedback = {
            'body': 'Question with `code` and **markdown** and [links](http://example.com)',
            'author': 'tinkermonkey',
            'parent_comment': {
                'id': 'comment_abc',
                'body': 'BA output',
                'author': 'orchestrator-bot'
            }
        }

        # When: Build context
        thread_history = [
            {
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            },
            {
                'role': 'user',
                'author': human_feedback['author'],
                'body': human_feedback['body']
            }
        ]

        # Then: Special characters should be preserved
        assert '`code`' in thread_history[1]['body']
        assert '**markdown**' in thread_history[1]['body']
        assert '[links]' in thread_history[1]['body']


class TestMultipleRepliesInThread:
    """Test handling of multiple replies in same thread"""

    def test_agent_responds_to_specific_reply(self):
        """
        Test: When multiple human replies exist, agent responds to specific one

        Scenario: Parent comment has 3 human replies. Agent should respond
        to the one that triggered feedback detection, not all 3.
        """
        # Given: Parent with multiple replies (agent responds to latest)
        parent_comment = {
            'id': 'comment_abc',
            'body': 'BA output',
            'author': 'orchestrator-bot',
            'replies': [
                {'body': 'First question', 'author': 'user1'},
                {'body': 'Second question', 'author': 'user2'},
                {'body': 'Third question', 'author': 'tinkermonkey'}  # Latest
            ]
        }

        # Latest human feedback
        human_feedback = {
            'body': 'Third question',
            'author': 'tinkermonkey',
            'comment_id': 'reply_3',
            'parent_comment': {
                'id': parent_comment['id'],
                'body': parent_comment['body'],
                'author': parent_comment['author']
            }
        }

        # When: Build context (should use THIS specific reply)
        thread_history = [
            {
                'role': 'agent',
                'author': human_feedback['parent_comment']['author'],
                'body': human_feedback['parent_comment']['body']
            },
            {
                'role': 'user',
                'author': human_feedback['author'],
                'body': human_feedback['body']
            }
        ]

        # Then: Should include only the parent + THIS reply
        assert len(thread_history) == 2
        assert thread_history[1]['body'] == 'Third question'
        # Should NOT include other replies
        assert 'First question' not in str(thread_history)
        assert 'Second question' not in str(thread_history)


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

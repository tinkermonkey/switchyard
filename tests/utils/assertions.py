"""
Test assertion helpers

Provides domain-specific assertions for orchestrator testing
to make tests more readable and maintainable.
"""

from typing import Dict, List, Any, Optional
from services.review_cycle import ReviewCycleState


def assert_state_transition(
    before_state: ReviewCycleState,
    after_state: ReviewCycleState,
    expected_transition: Optional[str] = None
):
    """
    Verify valid state machine transition

    Args:
        before_state: State before transition
        after_state: State after transition
        expected_transition: Optional specific transition to verify

    Raises:
        AssertionError: If transition is invalid
    """
    valid_transitions = {
        'initialized': ['maker_working', 'reviewer_working'],
        'reviewer_working': ['maker_working', 'awaiting_human_feedback', 'completed'],
        'maker_working': ['reviewer_working', 'awaiting_human_feedback'],
        'awaiting_human_feedback': ['reviewer_working', 'completed'],
        'completed': []  # Terminal state
    }

    before_status = before_state.status
    after_status = after_state.status

    # Check if transition is valid
    assert after_status in valid_transitions[before_status], \
        f"Invalid state transition: {before_status} → {after_status}. " \
        f"Valid transitions from {before_status}: {valid_transitions[before_status]}"

    # Check if it matches expected transition
    if expected_transition:
        assert after_status == expected_transition, \
            f"Expected transition to {expected_transition}, got {after_status}"


def assert_context_size(context: str, max_chars: int = 50000, min_chars: int = 100):
    """
    Verify context is within reasonable size bounds

    Args:
        context: Context string to check
        max_chars: Maximum allowed characters
        min_chars: Minimum expected characters (catch empty context)

    Raises:
        AssertionError: If context size is out of bounds
    """
    actual_size = len(context)

    assert actual_size >= min_chars, \
        f"Context too small: {actual_size} chars (min: {min_chars}). " \
        f"This may indicate context extraction failed."

    assert actual_size <= max_chars, \
        f"Context too large: {actual_size} chars (max: {max_chars}). " \
        f"This may indicate context bloat (including too many previous iterations)."


def assert_single_agent_signature(context: str, agent_name: str):
    """
    Verify context contains exactly ONE agent signature

    This ensures we're not accidentally including multiple iterations
    of the same agent's output.

    Args:
        context: Context string to check
        agent_name: Agent name (e.g., 'business_analyst')

    Raises:
        AssertionError: If signature count is not exactly 1
    """
    signature = f"_Processed by the {agent_name} agent_"
    count = context.count(signature)

    assert count == 1, \
        f"Expected exactly 1 {agent_name} signature, found {count}. " \
        f"This may indicate context includes multiple iterations."


def assert_agent_signature_present(context: str, agent_name: str):
    """
    Verify context contains agent signature

    Args:
        context: Context string to check
        agent_name: Agent name (e.g., 'business_analyst')

    Raises:
        AssertionError: If signature is not present
    """
    signature = f"_Processed by the {agent_name} agent_"
    assert signature in context, \
        f"Expected {agent_name} signature not found in context. " \
        f"Context may be from wrong agent or malformed."


def assert_agent_signature_absent(context: str, agent_name: str):
    """
    Verify context does NOT contain agent signature

    Args:
        context: Context string to check
        agent_name: Agent name (e.g., 'requirements_reviewer')

    Raises:
        AssertionError: If signature is present
    """
    signature = f"_Processed by the {agent_name} agent_"
    assert signature not in context, \
        f"Unexpected {agent_name} signature found in context. " \
        f"Context may include wrong agent's output."


def assert_comment_posted(
    mock_github: Any,
    body_substring: str,
    discussion_id: Optional[str] = None
):
    """
    Assert that a comment containing specific text was posted

    Args:
        mock_github: MockGitHubApp instance
        body_substring: Text to search for in comment bodies
        discussion_id: Optional discussion ID to check

    Raises:
        AssertionError: If no matching comment was posted
    """
    assert mock_github.assert_comment_posted(body_substring), \
        f"Expected comment containing '{body_substring}' was not posted"


def assert_comment_threaded_correctly(
    discussion: Dict[str, Any],
    comment_body_substring: str,
    expected_parent_body_substring: str
):
    """
    Verify a comment is threaded as a reply to the expected parent

    Args:
        discussion: Discussion data structure
        comment_body_substring: Text to identify the comment
        expected_parent_body_substring: Text to identify expected parent

    Raises:
        AssertionError: If comment is not threaded correctly
    """
    # Find the comment
    found_as_reply = False
    parent_matches = False

    for comment in discussion['comments']['nodes']:
        for reply in comment.get('replies', {}).get('nodes', []):
            if comment_body_substring in reply.get('body', ''):
                found_as_reply = True
                if expected_parent_body_substring in comment.get('body', ''):
                    parent_matches = True
                    break

    assert found_as_reply, \
        f"Comment containing '{comment_body_substring}' was not found as a reply. " \
        f"It may have been posted as a top-level comment."

    assert parent_matches, \
        f"Comment was threaded, but not to the expected parent containing '{expected_parent_body_substring}'"


def assert_iteration_incremented(
    before_state: ReviewCycleState,
    after_state: ReviewCycleState
):
    """
    Verify iteration counter was incremented by exactly 1

    Args:
        before_state: State before operation
        after_state: State after operation

    Raises:
        AssertionError: If iteration was not incremented correctly
    """
    expected = before_state.current_iteration + 1
    actual = after_state.current_iteration

    assert actual == expected, \
        f"Expected iteration {expected}, got {actual}"


def assert_maker_output_added(
    before_state: ReviewCycleState,
    after_state: ReviewCycleState,
    expected_output_substring: Optional[str] = None
):
    """
    Verify a new maker output was added to state

    Args:
        before_state: State before maker execution
        after_state: State after maker execution
        expected_output_substring: Optional text to verify in new output

    Raises:
        AssertionError: If maker output was not added
    """
    before_count = len(before_state.maker_outputs)
    after_count = len(after_state.maker_outputs)

    assert after_count == before_count + 1, \
        f"Expected {before_count + 1} maker outputs, got {after_count}"

    if expected_output_substring:
        latest_output = after_state.maker_outputs[-1]['output']
        assert expected_output_substring in latest_output, \
            f"Expected substring '{expected_output_substring}' not found in latest maker output"


def assert_review_output_added(
    before_state: ReviewCycleState,
    after_state: ReviewCycleState,
    expected_output_substring: Optional[str] = None
):
    """
    Verify a new review output was added to state

    Args:
        before_state: State before reviewer execution
        after_state: State after reviewer execution
        expected_output_substring: Optional text to verify in new output

    Raises:
        AssertionError: If review output was not added
    """
    before_count = len(before_state.review_outputs)
    after_count = len(after_state.review_outputs)

    assert after_count == before_count + 1, \
        f"Expected {before_count + 1} review outputs, got {after_count}"

    if expected_output_substring:
        latest_output = after_state.review_outputs[-1]['output']
        assert expected_output_substring in latest_output, \
            f"Expected substring '{expected_output_substring}' not found in latest review output"


def assert_escalation_occurred(state: ReviewCycleState):
    """
    Verify state indicates escalation occurred

    Args:
        state: State to check

    Raises:
        AssertionError: If state does not indicate escalation
    """
    assert state.status == 'awaiting_human_feedback', \
        f"Expected status 'awaiting_human_feedback', got '{state.status}'"

    assert state.escalation_time is not None, \
        "Expected escalation_time to be set, but it is None"


def assert_thread_history_correct(
    context: Dict[str, Any],
    expected_length: int,
    expected_authors: Optional[List[str]] = None
):
    """
    Verify thread history in context is correct

    Args:
        context: Task context dictionary
        expected_length: Expected number of messages in thread
        expected_authors: Optional list of expected author names in order

    Raises:
        AssertionError: If thread history is incorrect
    """
    assert 'thread_history' in context, \
        "Expected 'thread_history' in context, but it is missing"

    history = context['thread_history']
    assert len(history) == expected_length, \
        f"Expected {expected_length} messages in thread history, got {len(history)}"

    if expected_authors:
        actual_authors = [msg['author'] for msg in history]
        assert actual_authors == expected_authors, \
            f"Expected authors {expected_authors}, got {actual_authors}"


def assert_conversational_mode(context: Dict[str, Any]):
    """
    Verify context is in conversational mode

    Args:
        context: Task context dictionary

    Raises:
        AssertionError: If context is not in conversational mode
    """
    assert context.get('conversation_mode') == 'threaded', \
        f"Expected conversation_mode='threaded', got {context.get('conversation_mode')}"

    assert 'thread_history' in context, \
        "Expected 'thread_history' in conversational mode"

    assert len(context['thread_history']) > 0, \
        "Expected non-empty thread_history in conversational mode"

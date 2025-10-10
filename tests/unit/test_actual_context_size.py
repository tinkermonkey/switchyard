"""
Test actual context sizes returned by _get_discussion_context()

This test compares the actual implementation against our expectations
by mocking the GraphQL call to use fixture data.
"""

import json
import logging
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

logger = logging.getLogger(__name__)


@pytest.fixture
def discussion_95_data():
    """Load discussion 95 snapshot"""
    fixture_path = Path(__file__).parent.parent / 'fixtures' / 'discussion_95.json'
    with open(fixture_path) as f:
        return json.load(f)


def test_actual_context_extraction_business_analyst(discussion_95_data):
    """
    Test actual context sizes and compare to total discussion size

    This validates that we're extracting reasonable-sized context
    """
    discussion = discussion_95_data['repository']['discussion']
    comments = discussion['comments']['nodes']

    # Find the last business_analyst comment
    ba_signature = '_Processed by the business_analyst agent_'
    ba_comment = None
    for comment in reversed(comments):
        if ba_signature in comment.get('body', ''):
            ba_comment = comment
            break

    assert ba_comment is not None

    # Check size
    ba_body = ba_comment['body']
    ba_replies = ba_comment.get('replies', {}).get('nodes', [])

    # Filter to human replies only
    human_replies = [
        r for r in ba_replies
        if r.get('author') and 'bot' not in r['author']['login'].lower()
    ]

    context_size = len(ba_body) + sum(len(r['body']) for r in human_replies)

    logger.info(f"\n=== Expected Context (Last BA Comment + Replies) ===")
    logger.info(f"BA comment size: {len(ba_body):,} chars ({len(ba_body)/1024:.1f} KB)")
    logger.info(f"Human replies: {len(human_replies)}")
    logger.info(f"Total expected context: {context_size:,} chars ({context_size/1024:.1f} KB)")

    # This is the expected size for a single comment + replies
    assert context_size < 50000, \
        f"Context size ({context_size}) exceeds 50KB - likely including too much"

    # Now count what happens if we included EVERYTHING (the problem)
    total_discussion = len(discussion['body'])
    for comment in comments:
        total_discussion += len(comment['body'])
        for reply in comment.get('replies', {}).get('nodes', []):
            total_discussion += len(reply['body'])

    logger.info(f"\n=== Total Discussion Size (ALL comments - BAD) ===")
    logger.info(f"Total discussion: {total_discussion:,} chars ({total_discussion/1024:.1f} KB)")
    logger.info(f"Ratio: {context_size/total_discussion*100:.1f}% of total")
    logger.info(f"")
    logger.info(f"If we pass entire discussion: {total_discussion/1024:.1f} KB")
    logger.info(f"If we pass only last comment: {context_size/1024:.1f} KB")
    logger.info(f"Savings: {(total_discussion-context_size)/1024:.1f} KB ({(1-context_size/total_discussion)*100:.0f}% reduction)")

    # The context should be a small fraction of the total discussion
    assert context_size < total_discussion * 0.2, \
        "Context is > 20% of total discussion - likely including too many comments"


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

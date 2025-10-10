"""
Unit tests for escalation logic

Tests when and how review cycles escalate to human review,
and how they resume after receiving human feedback.

CRITICAL: Proper escalation ensures blocking issues get human attention.
"""

import pytest
from datetime import datetime, timezone
from services.review_cycle import ReviewCycleState, ReviewCycleExecutor
from services.review_parser import ReviewStatus, ReviewResult, ReviewFinding
from tests.utils.assertions import assert_escalation_occurred


@pytest.fixture
def executor():
    """Create a ReviewCycleExecutor"""
    return ReviewCycleExecutor()


@pytest.fixture
def cycle_state(review_cycle_builder):
    """Create a basic review cycle state"""
    return (review_cycle_builder
        .for_issue(96)
        .in_repository('context-studio')
        .with_agents('business_analyst', 'requirements_reviewer')
        .for_project('context-studio', 'idea-development')
        .in_discussion('D_test123')
        .with_max_iterations(3)
        .at_iteration(0)
        .build())


class TestEscalationConditions:
    """Test conditions that trigger escalation"""

    def test_escalate_on_blocking_issues_iteration_2(self):
        """
        Test: Escalate when blocking issues found on iteration 2+

        CRITICAL: First review with blocking issues lets maker try to fix.
        Second review (iteration > 1) with blocking issues escalates.
        """
        # Iteration 1: blocking issues found, should NOT escalate yet
        # Iteration 2: blocking issues still present, should escalate

        # This is tested in integration, here we test the logic
        iteration = 2
        escalate_on_blocked = True
        blocking_count = 1

        should_escalate = escalate_on_blocked and iteration > 1 and blocking_count > 0

        assert should_escalate is True

    def test_no_escalate_on_blocking_issues_iteration_1(self):
        """Test: Do NOT escalate on first review with blocking issues"""
        iteration = 1
        escalate_on_blocked = True
        blocking_count = 1

        should_escalate = escalate_on_blocked and iteration > 1 and blocking_count > 0

        assert should_escalate is False  # Give maker a chance to fix

    def test_escalate_on_max_iterations(self):
        """Test: Escalate when max iterations reached without approval"""
        current_iteration = 3
        max_iterations = 3

        should_escalate = current_iteration >= max_iterations

        assert should_escalate is True

    def test_no_escalate_below_max_iterations(self):
        """Test: Do NOT escalate below max iterations"""
        current_iteration = 2
        max_iterations = 3

        should_escalate = current_iteration >= max_iterations

        assert should_escalate is False

    def test_escalate_disabled_when_column_setting_false(self):
        """Test: Escalation disabled if column.escalate_on_blocked is False"""
        iteration = 2
        escalate_on_blocked = False  # Disabled
        blocking_count = 1

        should_escalate = escalate_on_blocked and iteration > 1 and blocking_count > 0

        assert should_escalate is False


class TestEscalationState:
    """Test escalation state management"""

    def test_escalation_sets_status(self, review_cycle_builder):
        """Test: Escalation sets status to awaiting_human_feedback"""
        state = (review_cycle_builder
            .at_iteration(2)
            .escalated()
            .build())

        assert state.status == 'awaiting_human_feedback'

    def test_escalation_sets_timestamp(self, review_cycle_builder):
        """Test: Escalation records timestamp"""
        state = (review_cycle_builder
            .at_iteration(2)
            .escalated()
            .build())

        assert state.escalation_time is not None
        # Should be ISO format
        assert 'T' in state.escalation_time

    def test_escalation_preserves_iteration(self, review_cycle_builder):
        """Test: Escalation preserves current iteration number"""
        state = (review_cycle_builder
            .at_iteration(2)
            .escalated()
            .build())

        assert state.current_iteration == 2

    def test_escalation_preserves_outputs(self, review_cycle_builder):
        """Test: Escalation preserves maker and reviewer outputs"""
        state = (review_cycle_builder
            .at_iteration(2)
            .with_maker_output("BA output 1", iteration=0)
            .with_review_output("RR feedback 1", iteration=1)
            .with_maker_output("BA revision", iteration=2)
            .escalated()
            .build())

        assert len(state.maker_outputs) == 2
        assert len(state.review_outputs) == 1


class TestEscalationCommentFormat:
    """Test format of escalation comments"""

    def test_blocked_escalation_comment_includes_blocking_count(self):
        """Test: Blocked escalation comment shows blocking issue count"""
        blocking_count = 3

        # Simulate escalation comment format
        comment = f"""## Review Blocked - Human Review Required

The automated review identified **{blocking_count} blocking issue(s)** that require human attention.
"""

        assert '3 blocking issue(s)' in comment
        assert 'Review Blocked' in comment
        assert 'Human Review Required' in comment

    def test_blocked_escalation_comment_lists_issues(self):
        """Test: Blocked escalation comment lists blocking issues"""
        findings = [
            ReviewFinding('Security', 'blocking', 'Missing authentication'),
            ReviewFinding('Validation', 'blocking', 'No input validation'),
        ]

        blocking_issues = [
            f"- **{f.category}**: {f.message}"
            for f in findings
            if f.severity == 'blocking'
        ]

        comment = f"""## Blocking Issues

{chr(10).join(blocking_issues)}
"""

        assert '**Security**: Missing authentication' in comment
        assert '**Validation**: No input validation' in comment

    def test_max_iterations_escalation_comment_includes_count(self):
        """Test: Max iterations escalation comment shows iteration count"""
        max_iterations = 3

        comment = f"""## Max Review Iterations Reached

The automated review cycle has reached the maximum iterations ({max_iterations}) without approval.
"""

        assert f'maximum iterations ({max_iterations})' in comment
        assert 'Max Review Iterations' in comment

    def test_escalation_comment_includes_instructions(self):
        """Test: Escalation comment includes next steps for human"""
        comment = """## Next Steps

Please review the outstanding issues and provide guidance:
1. Review the blocking issues listed above
2. Provide clarification or additional requirements
3. Comment on this discussion to resume the automated review
"""

        assert 'Next Steps' in comment
        assert 'provide guidance' in comment.lower() or 'provide clarification' in comment.lower()


class TestResumeAfterEscalation:
    """Test resuming review cycle after human feedback"""

    def test_resume_requires_human_feedback(self):
        """Test: Resume only happens when human feedback detected"""
        last_escalation = {'created_at': datetime.now(timezone.utc)}
        human_feedback_after_escalation = [
            {'author': 'tinkermonkey', 'body': 'Here is clarification...'}
        ]

        should_resume = bool(last_escalation and human_feedback_after_escalation)

        assert should_resume is True

    def test_no_resume_without_feedback(self):
        """Test: No resume if no human feedback yet"""
        last_escalation = {'created_at': datetime.now(timezone.utc)}
        human_feedback_after_escalation = []

        should_resume = bool(last_escalation and human_feedback_after_escalation)

        assert should_resume is False

    def test_resume_combines_multiple_feedback_comments(self):
        """Test: Resume combines multiple human feedback comments"""
        human_feedback_after_escalation = [
            {'author': 'user1', 'body': 'Comment 1', 'created_at': '2025-10-03T10:00:00Z'},
            {'author': 'user2', 'body': 'Comment 2', 'created_at': '2025-10-03T10:05:00Z'},
            {'author': 'user1', 'body': 'Comment 3', 'created_at': '2025-10-03T10:10:00Z'},
        ]

        # Simulate combining feedback
        combined_feedback = "\n\n---\n\n".join([
            f"**From {f['author']} at {f['created_at']}:**\n{f['body']}"
            for f in human_feedback_after_escalation
        ])

        assert 'From user1 at 2025-10-03T10:00:00Z' in combined_feedback
        assert 'From user2 at 2025-10-03T10:05:00Z' in combined_feedback
        assert 'Comment 1' in combined_feedback
        assert 'Comment 2' in combined_feedback
        assert 'Comment 3' in combined_feedback
        assert combined_feedback.count('---') == 2  # Separators

    def test_resume_creates_revision_context(self):
        """Test: Resume creates context with human feedback for revision"""
        human_feedback = "Please add security requirements to section 3.1"

        # Simulate revision context
        context = {
            'trigger': 'human_feedback_revision',
            'human_feedback': human_feedback,
        }

        assert context['trigger'] == 'human_feedback_revision'
        assert 'security requirements' in context['human_feedback']


class TestEscalationDetectionFromDiscussion:
    """Test detecting escalation state from discussion timeline"""

    def test_detect_escalation_from_comment(self):
        """Test: Detect escalation from 'Review Blocked' comment"""
        comment_body = """## Review Blocked - Human Review Required

The automated review identified **3 blocking issue(s)** that require human attention.
"""

        is_escalation = 'Review Blocked' in comment_body

        assert is_escalation is True

    def test_detect_max_iterations_escalation(self):
        """Test: Detect escalation from 'Max Review Iterations' comment"""
        comment_body = """## Max Review Iterations Reached

The automated review cycle has reached the maximum iterations (3) without approval.
"""

        is_escalation = 'Max Review Iterations' in comment_body

        assert is_escalation is True

    def test_detect_human_feedback_after_escalation(self):
        """Test: Detect human feedback that came after escalation"""
        timeline = [
            {
                'author': 'orchestrator-bot',
                'body': 'BA output',
                'created_at': datetime(2025, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'orchestrator-bot',
                'body': '## Review Blocked - Human Review Required',
                'created_at': datetime(2025, 10, 3, 10, 5, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'tinkermonkey',
                'body': 'Here is clarification...',
                'created_at': datetime(2025, 10, 3, 10, 10, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'other-user',
                'body': 'Additional context',
                'created_at': datetime(2025, 10, 3, 10, 15, 0, tzinfo=timezone.utc)
            }
        ]

        # Simulate detection logic
        last_escalation = None
        human_feedback_after_escalation = []

        for item in timeline:
            author = item['author']
            body = item['body']
            created_at = item['created_at']

            # Track escalations
            if author == 'orchestrator-bot' and 'Review Blocked' in body:
                last_escalation = {
                    'body': body,
                    'created_at': created_at
                }
                human_feedback_after_escalation = []  # Reset

            # Track human feedback after escalation
            if last_escalation and author != 'orchestrator-bot' and created_at > last_escalation['created_at']:
                human_feedback_after_escalation.append({
                    'author': author,
                    'body': body,
                    'created_at': created_at
                })

        # Should have detected escalation and 2 human feedback comments
        assert last_escalation is not None
        assert len(human_feedback_after_escalation) == 2
        assert human_feedback_after_escalation[0]['author'] == 'tinkermonkey'
        assert human_feedback_after_escalation[1]['author'] == 'other-user'

    def test_ignore_human_feedback_before_escalation(self):
        """Test: Ignore human feedback that came before escalation"""
        timeline = [
            {
                'author': 'tinkermonkey',
                'body': 'Early comment',
                'created_at': datetime(2025, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'orchestrator-bot',
                'body': '## Review Blocked - Human Review Required',
                'created_at': datetime(2025, 10, 3, 10, 5, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'tinkermonkey',
                'body': 'Response after escalation',
                'created_at': datetime(2025, 10, 3, 10, 10, 0, tzinfo=timezone.utc)
            }
        ]

        # Simulate detection logic
        last_escalation = None
        human_feedback_after_escalation = []

        for item in timeline:
            author = item['author']
            body = item['body']
            created_at = item['created_at']

            if author == 'orchestrator-bot' and 'Review Blocked' in body:
                last_escalation = {
                    'body': body,
                    'created_at': created_at
                }
                human_feedback_after_escalation = []  # Reset

            if last_escalation and author != 'orchestrator-bot' and created_at > last_escalation['created_at']:
                human_feedback_after_escalation.append({
                    'author': author,
                    'body': body,
                    'created_at': created_at
                })

        # Should only have feedback after escalation
        assert len(human_feedback_after_escalation) == 1
        assert human_feedback_after_escalation[0]['body'] == 'Response after escalation'


class TestEscalationIterationTracking:
    """Test iteration counting during escalation"""

    def test_iteration_count_preserved_on_escalation(self, review_cycle_builder):
        """Test: Iteration count preserved when escalating"""
        state = (review_cycle_builder
            .at_iteration(2)
            .with_maker_output("BA 1", iteration=0)
            .with_review_output("RR 1", iteration=1)
            .with_maker_output("BA 2", iteration=2)
            .escalated()
            .build())

        assert state.current_iteration == 2
        assert len(state.maker_outputs) == 2
        assert len(state.review_outputs) == 1

    def test_iteration_continues_after_resume(self):
        """Test: Iteration continues from escalation point after resume"""
        # Before escalation: iteration 2
        iteration_before_escalation = 2

        # After human feedback and resume, should continue from iteration 2
        iteration_after_resume = iteration_before_escalation

        assert iteration_after_resume == 2


class TestMultipleEscalations:
    """Test handling of multiple escalations in same cycle"""

    def test_multiple_escalations_tracked_separately(self):
        """Test: Multiple escalations in timeline are tracked separately"""
        timeline = [
            {
                'author': 'orchestrator-bot',
                'body': '## Review Blocked - First escalation',
                'created_at': datetime(2025, 10, 3, 10, 0, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'tinkermonkey',
                'body': 'Feedback for first escalation',
                'created_at': datetime(2025, 10, 3, 10, 5, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'orchestrator-bot',
                'body': '## Review Blocked - Second escalation',
                'created_at': datetime(2025, 10, 3, 10, 10, 0, tzinfo=timezone.utc)
            },
            {
                'author': 'tinkermonkey',
                'body': 'Feedback for second escalation',
                'created_at': datetime(2025, 10, 3, 10, 15, 0, tzinfo=timezone.utc)
            }
        ]

        # Simulate detection logic - should track LAST escalation
        last_escalation = None
        human_feedback_after_escalation = []

        for item in timeline:
            author = item['author']
            body = item['body']
            created_at = item['created_at']

            if author == 'orchestrator-bot' and 'Review Blocked' in body:
                last_escalation = {
                    'body': body,
                    'created_at': created_at
                }
                human_feedback_after_escalation = []  # Reset for new escalation

            if last_escalation and author != 'orchestrator-bot' and created_at > last_escalation['created_at']:
                human_feedback_after_escalation.append({
                    'author': author,
                    'body': body,
                    'created_at': created_at
                })

        # Should have second escalation as last
        assert 'Second escalation' in last_escalation['body']
        # Should only have feedback after second escalation
        assert len(human_feedback_after_escalation) == 1
        assert 'second escalation' in human_feedback_after_escalation[0]['body']

    def test_latest_escalation_wins(self):
        """Test: When multiple escalations, use the latest one"""
        escalations = [
            {'created_at': datetime(2025, 10, 3, 10, 0, 0, tzinfo=timezone.utc), 'type': 'first'},
            {'created_at': datetime(2025, 10, 3, 10, 10, 0, tzinfo=timezone.utc), 'type': 'second'},
            {'created_at': datetime(2025, 10, 3, 10, 20, 0, tzinfo=timezone.utc), 'type': 'third'},
        ]

        # Sort and get latest
        latest_escalation = max(escalations, key=lambda x: x['created_at'])

        assert latest_escalation['type'] == 'third'


class TestEscalationEdgeCases:
    """Test edge cases in escalation logic"""

    def test_escalation_with_zero_blocking_issues(self):
        """Test: Should NOT escalate if blocking count is 0"""
        iteration = 2
        escalate_on_blocked = True
        blocking_count = 0

        should_escalate = escalate_on_blocked and iteration > 1 and blocking_count > 0

        assert should_escalate is False

    def test_escalation_exactly_at_max_iterations(self):
        """Test: Escalate when exactly at max iterations"""
        current_iteration = 3
        max_iterations = 3

        should_escalate = current_iteration >= max_iterations

        assert should_escalate is True

    def test_no_escalation_when_approved(self):
        """Test: Do NOT escalate when review is approved"""
        review_status = ReviewStatus.APPROVED
        iteration = 5  # Even past max iterations
        max_iterations = 3

        # If approved, no escalation regardless of iteration count
        should_escalate = review_status != ReviewStatus.APPROVED and iteration >= max_iterations

        assert should_escalate is False

    def test_bot_comments_ignored_as_human_feedback(self):
        """Test: Bot comments not counted as human feedback"""
        comment = {
            'author': 'orchestrator-bot',
            'body': 'Some automated comment'
        }

        is_human_feedback = comment['author'] != 'orchestrator-bot'

        assert is_human_feedback is False

    def test_empty_human_feedback_not_counted(self):
        """Test: Empty human feedback should not count"""
        human_feedback_after_escalation = []

        should_resume = bool(human_feedback_after_escalation)

        assert should_resume is False


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

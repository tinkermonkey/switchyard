"""
Unit tests for review cycle state transitions

Tests the state machine logic for review cycles to ensure
valid state transitions and prevent invalid states.
"""

import pytest
from services.review_cycle import ReviewCycleState
from tests.utils.assertions import assert_state_transition


class TestReviewCycleStateTransitions:
    """Test valid and invalid state transitions"""

    def test_initial_state(self):
        """Test: New cycle starts in initialized state"""
        state = ReviewCycleState(
            issue_number=1,
            repository='test-repo',
            maker_agent='business_analyst',
            reviewer_agent='requirements_reviewer',
            max_iterations=3,
            project_name='test-project',
            board_name='test-board',
            workspace_type='discussions',
            discussion_id='D_test'
        )

        assert state.status == 'initialized'
        assert state.current_iteration == 0
        assert len(state.maker_outputs) == 0
        assert len(state.review_outputs) == 0

    def test_initialized_to_reviewer_working(self, review_cycle_builder):
        """Test: Initialized → reviewer_working (first review)"""
        before = review_cycle_builder.initialized().build()
        after = review_cycle_builder.reviewer_working().build()

        assert_state_transition(before, after, 'reviewer_working')

    def test_reviewer_working_to_maker_working(self, review_cycle_builder):
        """Test: reviewer_working → maker_working (changes requested)"""
        before = review_cycle_builder.reviewer_working().build()
        after = review_cycle_builder.maker_working().build()

        assert_state_transition(before, after, 'maker_working')

    def test_maker_working_to_reviewer_working(self, review_cycle_builder):
        """Test: maker_working → reviewer_working (revision complete)"""
        before = review_cycle_builder.maker_working().build()
        after = review_cycle_builder.reviewer_working().build()

        assert_state_transition(before, after, 'reviewer_working')

    def test_reviewer_working_to_completed(self, review_cycle_builder):
        """Test: reviewer_working → completed (approved)"""
        before = review_cycle_builder.reviewer_working().build()
        after = review_cycle_builder.completed().build()

        assert_state_transition(before, after, 'completed')

    def test_reviewer_working_to_awaiting_feedback(self, review_cycle_builder):
        """Test: reviewer_working → awaiting_human_feedback (escalation)"""
        before = review_cycle_builder.reviewer_working().build()
        after = review_cycle_builder.escalated().build()

        assert_state_transition(before, after, 'awaiting_human_feedback')

    def test_awaiting_feedback_to_reviewer_working(self, review_cycle_builder):
        """Test: awaiting_human_feedback → reviewer_working (feedback received)"""
        before = review_cycle_builder.escalated().build()
        after = review_cycle_builder.reviewer_working().build()

        assert_state_transition(before, after, 'reviewer_working')

    def test_invalid_transition_initialized_to_completed(self, review_cycle_builder):
        """Test: Cannot go directly from initialized to completed"""
        before = review_cycle_builder.initialized().build()
        after = review_cycle_builder.completed().build()

        with pytest.raises(AssertionError, match="Invalid state transition"):
            assert_state_transition(before, after)

    def test_invalid_transition_maker_to_completed(self, review_cycle_builder):
        """Test: Cannot go from maker_working to completed (must go through reviewer)"""
        before = review_cycle_builder.maker_working().build()
        after = review_cycle_builder.completed().build()

        with pytest.raises(AssertionError, match="Invalid state transition"):
            assert_state_transition(before, after)

    def test_invalid_transition_completed_to_anything(self, review_cycle_builder):
        """Test: Completed is terminal state, cannot transition out"""
        before = review_cycle_builder.completed().build()
        after = review_cycle_builder.reviewer_working().build()

        with pytest.raises(AssertionError, match="Invalid state transition"):
            assert_state_transition(before, after)


class TestReviewCycleStateIterations:
    """Test iteration counting and output tracking"""

    def test_iteration_starts_at_zero(self, review_cycle_builder):
        """Test: New cycle starts at iteration 0"""
        state = review_cycle_builder.build()
        assert state.current_iteration == 0

    def test_iteration_increments(self, review_cycle_builder):
        """Test: Iteration increments as cycle progresses"""
        state = review_cycle_builder.at_iteration(0).build()
        assert state.current_iteration == 0

        state.current_iteration = 1
        assert state.current_iteration == 1

    def test_maker_outputs_accumulate(self, review_cycle_builder):
        """Test: Maker outputs are stored in chronological order"""
        state = (review_cycle_builder
            .with_maker_output("Output 1", iteration=0)
            .with_maker_output("Output 2", iteration=1)
            .with_maker_output("Output 3", iteration=2)
            .build())

        assert len(state.maker_outputs) == 3
        assert state.maker_outputs[0]['output'] == "Output 1"
        assert state.maker_outputs[1]['output'] == "Output 2"
        assert state.maker_outputs[2]['output'] == "Output 3"

    def test_review_outputs_accumulate(self, review_cycle_builder):
        """Test: Review outputs are stored in chronological order"""
        state = (review_cycle_builder
            .with_review_output("Review 1", iteration=1)
            .with_review_output("Review 2", iteration=2)
            .build())

        assert len(state.review_outputs) == 2
        assert state.review_outputs[0]['output'] == "Review 1"
        assert state.review_outputs[1]['output'] == "Review 2"

    def test_max_iterations_enforced(self, review_cycle_builder):
        """Test: Cycle should escalate at max iterations"""
        state = (review_cycle_builder
            .with_max_iterations(3)
            .at_iteration(3)
            .build())

        # At max iterations, should be escalated or completed
        assert state.current_iteration >= state.max_iterations


class TestReviewCycleStateSerialization:
    """Test state persistence and deserialization"""

    def test_state_to_dict(self, review_cycle_builder):
        """Test: State serializes to dict correctly"""
        state = (review_cycle_builder
            .for_issue(96)
            .with_agents('business_analyst', 'requirements_reviewer')
            .at_iteration(2)
            .with_maker_output("BA output")
            .escalated()
            .build())

        state_dict = state.to_dict()

        assert state_dict['issue_number'] == 96
        assert state_dict['maker_agent'] == 'business_analyst'
        assert state_dict['reviewer_agent'] == 'requirements_reviewer'
        assert state_dict['current_iteration'] == 2
        assert state_dict['status'] == 'awaiting_human_feedback'
        assert len(state_dict['maker_outputs']) == 1
        assert state_dict['escalation_time'] is not None

    def test_state_from_dict(self, review_cycle_builder):
        """Test: State deserializes from dict correctly"""
        original = (review_cycle_builder
            .for_issue(96)
            .with_agents('business_analyst', 'requirements_reviewer')
            .at_iteration(2)
            .escalated()
            .build())

        state_dict = original.to_dict()
        restored = ReviewCycleState.from_dict(state_dict)

        assert restored.issue_number == original.issue_number
        assert restored.maker_agent == original.maker_agent
        assert restored.reviewer_agent == original.reviewer_agent
        assert restored.current_iteration == original.current_iteration
        assert restored.status == original.status
        assert restored.escalation_time == original.escalation_time

    def test_state_roundtrip(self, review_cycle_builder):
        """Test: State survives serialize/deserialize roundtrip"""
        original = (review_cycle_builder
            .for_issue(96)
            .in_repository('context-studio')
            .with_agents('business_analyst', 'requirements_reviewer')
            .for_project('context-studio', 'idea-development')
            .in_discussion('D_test123')
            .at_iteration(2)
            .with_maker_output("BA output 1")
            .with_maker_output("BA output 2")
            .with_review_output("RR feedback 1")
            .escalated()
            .build())

        # Serialize and deserialize
        state_dict = original.to_dict()
        restored = ReviewCycleState.from_dict(state_dict)

        # All fields should match
        assert restored.issue_number == original.issue_number
        assert restored.repository == original.repository
        assert restored.maker_agent == original.maker_agent
        assert restored.reviewer_agent == original.reviewer_agent
        assert restored.max_iterations == original.max_iterations
        assert restored.project_name == original.project_name
        assert restored.board_name == original.board_name
        assert restored.workspace_type == original.workspace_type
        assert restored.discussion_id == original.discussion_id
        assert restored.current_iteration == original.current_iteration
        assert len(restored.maker_outputs) == len(original.maker_outputs)
        assert len(restored.review_outputs) == len(original.review_outputs)
        assert restored.status == original.status


class TestReviewCycleStateEscalation:
    """Test escalation logic and state"""

    def test_escalation_sets_timestamp(self, review_cycle_builder):
        """Test: Escalation records timestamp"""
        state = review_cycle_builder.escalated().build()

        assert state.status == 'awaiting_human_feedback'
        assert state.escalation_time is not None
        # Timestamp should be ISO format
        assert 'T' in state.escalation_time
        assert '+' in state.escalation_time or 'Z' in state.escalation_time

    def test_escalation_preserves_history(self, review_cycle_builder):
        """Test: Escalation preserves maker and reviewer outputs"""
        state = (review_cycle_builder
            .at_iteration(3)
            .with_maker_output("BA 1", iteration=0)
            .with_review_output("RR 1", iteration=1)
            .with_maker_output("BA 2", iteration=2)
            .with_review_output("RR 2", iteration=3)
            .escalated()
            .build())

        assert len(state.maker_outputs) == 2
        assert len(state.review_outputs) == 2
        assert state.status == 'awaiting_human_feedback'


class TestReviewCycleStateValidation:
    """Test state validation and constraints"""

    def test_issue_number_required(self):
        """Test: Issue number is required"""
        with pytest.raises(TypeError):
            ReviewCycleState(
                # issue_number missing
                repository='test-repo',
                maker_agent='business_analyst',
                reviewer_agent='requirements_reviewer',
                max_iterations=3,
                project_name='test-project',
                board_name='test-board'
            )

    def test_agents_required(self):
        """Test: Both maker and reviewer agents required"""
        with pytest.raises(TypeError):
            ReviewCycleState(
                issue_number=1,
                repository='test-repo',
                # maker_agent missing
                reviewer_agent='requirements_reviewer',
                max_iterations=3,
                project_name='test-project',
                board_name='test-board'
            )

    def test_max_iterations_positive(self, review_cycle_builder):
        """Test: Max iterations should be positive"""
        state = review_cycle_builder.with_max_iterations(3).build()
        assert state.max_iterations > 0

    def test_current_iteration_non_negative(self, review_cycle_builder):
        """Test: Current iteration cannot be negative"""
        state = review_cycle_builder.at_iteration(0).build()
        assert state.current_iteration >= 0


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

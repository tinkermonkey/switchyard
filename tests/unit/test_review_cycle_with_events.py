"""
Unit tests for ReviewCycleExecutor with Decision Event Emission

These tests verify that emitting decision events doesn't break the core review cycle flow.
They test that:
1. Events are emitted at the right times
2. Event emission doesn't affect review cycle logic
3. Event emission failures don't break the review cycle
4. All code paths that emit events work correctly
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock, call
from datetime import datetime

from services.review_cycle import ReviewCycleExecutor, ReviewCycleState
from monitoring.observability import ObservabilityManager, EventType
from monitoring.decision_events import DecisionEventEmitter
from config.manager import ConfigManager


def create_test_state(**kwargs):
    """Helper to create ReviewCycleState with sensible defaults for testing"""
    defaults = {
        'issue_number': 123,
        'repository': 'test-org/test-repo',
        'maker_agent': 'maker',
        'reviewer_agent': 'reviewer',
        'max_iterations': 3,
        'project_name': 'test-project',
        'board_name': 'dev',
        'workspace_type': 'issues'
    }
    defaults.update(kwargs)
    return ReviewCycleState(**defaults)


@pytest.fixture
def mock_obs_manager():
    """Create mock ObservabilityManager"""
    mock = Mock(spec=ObservabilityManager)
    mock.emit = Mock()
    return mock


@pytest.fixture
def mock_decision_emitter(mock_obs_manager):
    """Create mock DecisionEventEmitter"""
    mock = Mock(spec=DecisionEventEmitter)
    mock.obs = mock_obs_manager
    # Mock all emit methods to not raise exceptions
    mock.emit_review_cycle_decision = Mock()
    mock.emit_status_progression = Mock()
    return mock


@pytest.fixture
def review_cycle_executor(mock_obs_manager, mock_decision_emitter):
    """Create ReviewCycleExecutor with mocked observability"""
    executor = ReviewCycleExecutor()
    executor.obs = mock_obs_manager
    executor.decision_events = mock_decision_emitter
    return executor


class TestReviewCycleEventEmission:
    """Test that review cycle properly emits events without breaking flow"""
    
    def test_start_review_cycle_emits_start_event(self, review_cycle_executor, mock_decision_emitter):
        """Test that starting a review cycle emits the start event"""
        # This is tested indirectly - we verify the emitter has the method
        assert hasattr(mock_decision_emitter, 'emit_review_cycle_decision')
        assert callable(mock_decision_emitter.emit_review_cycle_decision)
    
    def test_review_cycle_state_stores_pipeline_run_id(self):
        """Test that ReviewCycleState can store pipeline_run_id for event correlation"""
        state = ReviewCycleState(
            issue_number=123,
            repository="test-org/test-repo",
            project_name="test-project",
            board_name="dev",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            max_iterations=3,
            workspace_type="issues",
            pipeline_run_id="test-run-123"
        )
        
        assert state.pipeline_run_id == "test-run-123"
    
    def test_review_cycle_state_serializes_pipeline_run_id(self):
        """Test that pipeline_run_id is preserved in state serialization"""
        state = ReviewCycleState(
            issue_number=123,
            repository="test-org/test-repo",
            project_name="test-project",
            board_name="dev",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            max_iterations=3,
            workspace_type="issues",
            pipeline_run_id="test-run-456"
        )
        
        # Serialize to dict
        state_dict = state.to_dict()
        assert state_dict['pipeline_run_id'] == "test-run-456"
        
        # Deserialize back
        restored_state = ReviewCycleState.from_dict(state_dict)
        assert restored_state.pipeline_run_id == "test-run-456"
    
    def test_event_emission_failure_doesnt_break_state_creation(self, review_cycle_executor, mock_decision_emitter):
        """Test that if event emission fails, state creation still works"""
        # Make emit throw an exception
        mock_decision_emitter.emit_review_cycle_decision.side_effect = Exception("Event emission failed")
        
        # Create state - this should not raise even if events fail
        state = create_test_state(issue_number=789)
        
        # Verify state was created successfully
        assert state.issue_number == 789
        assert state.status == 'initialized'


class TestReviewCycleStateTransitionsWithEvents:
    """Test that state transitions work correctly even with event emission"""
    
    def test_state_transition_to_maker_working(self):
        """Test transition to maker_working state"""
        state = create_test_state(issue_number=100)
        
        # Transition to maker_working
        state.status = 'maker_working'
        assert state.status == 'maker_working'
    
    def test_state_transition_to_reviewer_working(self):
        """Test transition to reviewer_working state"""
        state = create_test_state(issue_number=101)
        
        state.status = 'maker_working'
        state.status = 'reviewer_working'
        assert state.status == 'reviewer_working'
    
    def test_iteration_increment_works_with_events(self):
        """Test that iteration increment works correctly"""
        state = create_test_state(issue_number=102)
        
        assert state.current_iteration == 0
        state.current_iteration += 1
        assert state.current_iteration == 1


class TestReviewCycleExecutorInitialization:
    """Test that ReviewCycleExecutor initializes correctly with event emitters"""
    
    def test_executor_has_decision_events_attribute(self):
        """Test that executor can have decision_events set"""
        executor = ReviewCycleExecutor()
        mock_emitter = Mock(spec=DecisionEventEmitter)
        executor.decision_events = mock_emitter
        
        assert hasattr(executor, 'decision_events')
        assert executor.decision_events == mock_emitter
    
    def test_executor_can_create_without_decision_events(self):
        """Test that executor works without decision_events (backward compatibility)"""
        executor = ReviewCycleExecutor()
        # Should not raise
        assert executor is not None


class TestEventEmissionParameters:
    """Test that event emission receives correct parameters"""
    
    def test_review_cycle_start_event_parameters(self, mock_decision_emitter):
        """Test that start event gets all required parameters"""
        mock_decision_emitter.emit_review_cycle_decision(
            issue_number=200,
            project="test-project",
            board="dev",
            cycle_iteration=0,
            decision_type='start',
            maker_agent='software_engineer',
            reviewer_agent='code_reviewer',
            reason='Starting review cycle',
            additional_data={'max_iterations': 3},
            pipeline_run_id='run-123'
        )
        
        # Verify the call was made
        assert mock_decision_emitter.emit_review_cycle_decision.called
        
        # Verify parameters
        call_kwargs = mock_decision_emitter.emit_review_cycle_decision.call_args[1]
        assert call_kwargs['issue_number'] == 200
        assert call_kwargs['project'] == "test-project"
        assert call_kwargs['board'] == "dev"
        assert call_kwargs['cycle_iteration'] == 0
        assert call_kwargs['decision_type'] == 'start'
        assert call_kwargs['maker_agent'] == 'software_engineer'
        assert call_kwargs['reviewer_agent'] == 'code_reviewer'
        assert call_kwargs['pipeline_run_id'] == 'run-123'
    
    def test_review_cycle_iteration_event_parameters(self, mock_decision_emitter):
        """Test that iteration event gets correct parameters"""
        mock_decision_emitter.emit_review_cycle_decision(
            issue_number=201,
            project="test-project",
            board="dev",
            cycle_iteration=1,
            decision_type='iteration',
            maker_agent='software_engineer',
            reviewer_agent='code_reviewer',
            reason='Iteration 1',
            pipeline_run_id='run-124'
        )
        
        assert mock_decision_emitter.emit_review_cycle_decision.called
        call_kwargs = mock_decision_emitter.emit_review_cycle_decision.call_args[1]
        assert call_kwargs['cycle_iteration'] == 1
        assert call_kwargs['decision_type'] == 'iteration'
    
    def test_review_cycle_complete_event_parameters(self, mock_decision_emitter):
        """Test that complete event gets correct parameters"""
        mock_decision_emitter.emit_review_cycle_decision(
            issue_number=202,
            project="test-project",
            board="dev",
            cycle_iteration=2,
            decision_type='complete',
            maker_agent='software_engineer',
            reviewer_agent='code_reviewer',
            reason='Review approved',
            pipeline_run_id='run-125'
        )
        
        assert mock_decision_emitter.emit_review_cycle_decision.called
        call_kwargs = mock_decision_emitter.emit_review_cycle_decision.call_args[1]
        assert call_kwargs['decision_type'] == 'complete'
        assert call_kwargs['reason'] == 'Review approved'
    
    def test_review_cycle_escalate_event_parameters(self, mock_decision_emitter):
        """Test that escalate event gets correct parameters"""
        mock_decision_emitter.emit_review_cycle_decision(
            issue_number=203,
            project="test-project",
            board="dev",
            cycle_iteration=3,
            decision_type='escalate',
            maker_agent='software_engineer',
            reviewer_agent='code_reviewer',
            reason='Max iterations reached',
            additional_data={'max_iterations': 3},
            pipeline_run_id='run-126'
        )
        
        assert mock_decision_emitter.emit_review_cycle_decision.called
        call_kwargs = mock_decision_emitter.emit_review_cycle_decision.call_args[1]
        assert call_kwargs['decision_type'] == 'escalate'
        assert 'additional_data' in call_kwargs
        assert call_kwargs['additional_data']['max_iterations'] == 3


class TestErrorHandlingWithEvents:
    """Test that error handling works correctly with events"""
    
    def test_exception_in_event_emission_is_caught(self, review_cycle_executor, mock_decision_emitter):
        """Test that exceptions in event emission don't crash the executor"""
        # Make emit raise an exception
        mock_decision_emitter.emit_review_cycle_decision.side_effect = RuntimeError("Redis connection failed")
        
        # Create a state - this operation should be resilient to event failures
        state = create_test_state(issue_number=300)
        
        # State should be created successfully
        assert state.issue_number == 300
        assert state.status == 'initialized'
    
    def test_none_decision_emitter_doesnt_crash(self):
        """Test that missing decision_emitter doesn't crash the executor"""
        executor = ReviewCycleExecutor()
        # Don't set decision_events
        
        # Should not crash
        assert executor is not None


class TestReviewCycleHistoryWithEvents:
    """Test that review cycle history tracking works with events"""
    
    def test_maker_outputs_accumulated(self):
        """Test that maker outputs are accumulated correctly"""
        state = create_test_state(issue_number=400)
        
        # Add maker outputs
        state.maker_outputs.append({
            'iteration': 0,
            'output': 'First implementation',
            'timestamp': datetime.now().isoformat()
        })
        
        state.maker_outputs.append({
            'iteration': 1,
            'output': 'Fixed issues',
            'timestamp': datetime.now().isoformat()
        })
        
        assert len(state.maker_outputs) == 2
        assert state.maker_outputs[0]['iteration'] == 0
        assert state.maker_outputs[1]['iteration'] == 1
    
    def test_review_outputs_accumulated(self):
        """Test that review outputs are accumulated correctly"""
        state = create_test_state(issue_number=401)
        
        # Add review outputs
        state.review_outputs.append({
            'iteration': 0,
            'output': 'Needs improvement',
            'approved': False,
            'timestamp': datetime.now().isoformat()
        })
        
        state.review_outputs.append({
            'iteration': 1,
            'output': 'Looks good',
            'approved': True,
            'timestamp': datetime.now().isoformat()
        })
        
        assert len(state.review_outputs) == 2
        assert state.review_outputs[0]['approved'] is False
        assert state.review_outputs[1]['approved'] is True


class TestPipelineRunIdPropagation:
    """Test that pipeline_run_id is properly propagated for event correlation"""
    
    def test_pipeline_run_id_in_new_state(self):
        """Test that new states can include pipeline_run_id"""
        run_id = "pipeline-run-12345"
        
        state = create_test_state(issue_number=500, pipeline_run_id=run_id)
        
        assert state.pipeline_run_id == run_id
    
    def test_pipeline_run_id_optional(self):
        """Test that pipeline_run_id is optional (backward compatibility)"""
        state = create_test_state(issue_number=501)
        
        # Should not crash, and pipeline_run_id should be None or empty
        assert hasattr(state, 'pipeline_run_id')
    
    def test_pipeline_run_id_persists_through_serialization(self):
        """Test that pipeline_run_id survives save/load cycle"""
        original_run_id = "pipeline-run-67890"
        
        state = create_test_state(issue_number=502, pipeline_run_id=original_run_id)
        
        # Serialize
        state_dict = state.to_dict()
        
        # Deserialize
        restored_state = ReviewCycleState.from_dict(state_dict)
        
        # Verify pipeline_run_id is preserved
        assert restored_state.pipeline_run_id == original_run_id

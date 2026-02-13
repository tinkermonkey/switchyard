"""
Unit tests for DecisionEventEmitter

Tests all convenience methods and ensures consistent event structure.
"""

import pytest
from unittest.mock import Mock, MagicMock, call
from datetime import datetime

from monitoring.observability import ObservabilityManager, EventType
from monitoring.decision_events import DecisionEventEmitter


class TestDecisionEventEmitter:
    """Test suite for DecisionEventEmitter"""
    
    @pytest.fixture
    def mock_obs(self):
        """Create a mock ObservabilityManager"""
        mock = Mock(spec=ObservabilityManager)
        mock.emit = Mock()
        return mock
    
    @pytest.fixture
    def emitter(self, mock_obs):
        """Create a DecisionEventEmitter with mock ObservabilityManager"""
        return DecisionEventEmitter(mock_obs)
    
    # ========== AGENT ROUTING TESTS ==========
    
    def test_emit_agent_routing_decision(self, emitter, mock_obs):
        """Test agent routing decision emission"""
        emitter.emit_agent_routing_decision(
            issue_number=123,
            project="test-project",
            board="dev",
            current_status="Ready",
            selected_agent="software_architect",
            reason="Status mapping",
            alternatives=["business_analyst", "product_manager"],
            workspace_type="issues"
        )
        
        # Verify emit was called
        assert mock_obs.emit.called
        
        # Get call arguments
        call_args = mock_obs.emit.call_args
        event_type, agent, task_id, project, data = (
            call_args[0][0],
            call_args[1]['agent'],
            call_args[1]['task_id'],
            call_args[1]['project'],
            call_args[1]['data']
        )
        
        # Verify event type
        assert event_type == EventType.AGENT_ROUTING_DECISION
        
        # Verify agent and project
        assert agent == "orchestrator"
        assert project == "test-project"
        
        # Verify data structure
        assert data['decision_category'] == 'routing'
        assert data['issue_number'] == 123
        assert data['board'] == "dev"
        assert data['workspace_type'] == "issues"
        assert data['inputs']['current_status'] == "Ready"
        assert data['decision']['selected_agent'] == "software_architect"
        assert data['reason'] == "Status mapping"
        assert "business_analyst" in data['reasoning_data']['alternatives_considered']
        assert "product_manager" in data['reasoning_data']['alternatives_considered']
    
    def test_emit_agent_selected(self, emitter, mock_obs):
        """Test simplified agent selection emission"""
        emitter.emit_agent_selected(
            issue_number=456,
            project="my-project",
            board="qa",
            selected_agent="qa_reviewer",
            reason="QA stage selected"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.AGENT_SELECTED
        assert call_args[1]['data']['issue_number'] == 456
        assert call_args[1]['data']['decision']['agent'] == "qa_reviewer"
    
    def test_emit_workspace_routing(self, emitter, mock_obs):
        """Test workspace routing decision emission"""
        emitter.emit_workspace_routing(
            issue_number=789,
            project="test-project",
            board="dev",
            stage="design",
            selected_workspace="discussions",
            category_id="cat123",
            reason="Design stage uses discussions"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.WORKSPACE_ROUTING_DECISION
        data = call_args[1]['data']
        assert data['decision']['workspace'] == "discussions"
        assert data['decision']['category_id'] == "cat123"
        assert data['inputs']['stage'] == "design"
    
    # ========== FEEDBACK TESTS ==========
    
    def test_emit_feedback_detected(self, emitter, mock_obs):
        """Test feedback detection emission"""
        emitter.emit_feedback_detected(
            issue_number=100,
            project="feedback-test",
            board="main",
            feedback_source="comment",
            feedback_content="This is a test comment with feedback",
            target_agent="software_architect",
            action_taken="queue_agent_task",
            workspace_type="issues"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.FEEDBACK_DETECTED
        data = call_args[1]['data']
        assert data['decision_category'] == 'feedback'
        assert data['issue_number'] == 100
        assert data['inputs']['feedback_source'] == "comment"
        assert data['decision']['target_agent'] == "software_architect"
        assert data['decision']['action_taken'] == "queue_agent_task"
    
    def test_emit_feedback_detected_truncates_long_content(self, emitter, mock_obs):
        """Test that long feedback content is truncated"""
        long_feedback = "x" * 1000  # 1000 characters
        
        emitter.emit_feedback_detected(
            issue_number=101,
            project="test",
            board="main",
            feedback_source="comment",
            feedback_content=long_feedback,
            target_agent="test_agent",
            action_taken="queue_agent_task"
        )
        
        assert mock_obs.emit.called
        data = mock_obs.emit.call_args[1]['data']
        
        # Verify truncation
        assert len(data['inputs']['feedback_content']) <= 503  # 500 + "..."
        assert data['inputs']['feedback_length'] == 1000
    
    def test_emit_feedback_listening_started(self, emitter, mock_obs):
        """Test feedback listening started emission"""
        emitter.emit_feedback_listening_started(
            issue_number=102,
            project="test",
            board="main",
            agent="software_engineer",
            monitoring_for=["comment", "label_change"]
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.FEEDBACK_LISTENING_STARTED
        data = call_args[1]['data']
        assert data['monitoring_agent'] == "software_engineer"
        assert "comment" in data['monitoring_for']
    
    def test_emit_feedback_listening_stopped(self, emitter, mock_obs):
        """Test feedback listening stopped emission"""
        emitter.emit_feedback_listening_stopped(
            issue_number=103,
            project="test",
            board="main",
            agent="software_engineer",
            reason="task_completed",
            feedback_received=True
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.FEEDBACK_LISTENING_STOPPED
        data = call_args[1]['data']
        assert data['monitoring_agent'] == "software_engineer"
        assert data['feedback_received'] is True
        assert data['reason'] == "task_completed"
    
    def test_emit_feedback_ignored(self, emitter, mock_obs):
        """Test feedback ignored emission"""
        emitter.emit_feedback_ignored(
            issue_number=104,
            project="test",
            board="main",
            feedback_source="comment",
            reason="Not actionable feedback"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.FEEDBACK_IGNORED
        data = call_args[1]['data']
        assert data['feedback_source'] == "comment"
        assert data['reason'] == "Not actionable feedback"
    
    # ========== STATUS PROGRESSION TESTS ==========
    
    def test_emit_status_progression_started(self, emitter, mock_obs):
        """Test status progression started emission"""
        emitter.emit_status_progression(
            issue_number=200,
            project="progression-test",
            board="dev",
            from_status="Ready",
            to_status="In Progress",
            trigger="agent_completion",
            success=None  # Not yet executed
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.STATUS_PROGRESSION_STARTED
        data = call_args[1]['data']
        assert data['decision_category'] == 'progression'
        assert data['inputs']['from_status'] == "Ready"
        assert data['decision']['to_status'] == "In Progress"
        assert data['inputs']['trigger'] == "agent_completion"
    
    def test_emit_status_progression_completed(self, emitter, mock_obs):
        """Test status progression completed emission"""
        emitter.emit_status_progression(
            issue_number=201,
            project="progression-test",
            board="dev",
            from_status="Ready",
            to_status="In Progress",
            trigger="manual",
            success=True
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.STATUS_PROGRESSION_COMPLETED
        data = call_args[1]['data']
        assert data['success'] is True
    
    def test_emit_status_progression_failed(self, emitter, mock_obs):
        """Test status progression failed emission"""
        emitter.emit_status_progression(
            issue_number=202,
            project="progression-test",
            board="dev",
            from_status="Ready",
            to_status="In Progress",
            trigger="automation",
            success=False,
            error="GitHub API error"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.STATUS_PROGRESSION_FAILED
        data = call_args[1]['data']
        assert data['success'] is False
        assert data['error'] == "GitHub API error"
    
    def test_emit_pipeline_stage_transition(self, emitter, mock_obs):
        """Test pipeline stage transition emission"""
        emitter.emit_pipeline_stage_transition(
            issue_number=203,
            project="test",
            board="dev",
            from_stage="development",
            to_stage="review",
            reason="Development completed"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.PIPELINE_STAGE_TRANSITION
        data = call_args[1]['data']
        assert data['from_stage'] == "development"
        assert data['to_stage'] == "review"
    
    # ========== REVIEW CYCLE TESTS ==========
    
    def test_emit_review_cycle_started(self, emitter, mock_obs):
        """Test review cycle started emission"""
        emitter.emit_review_cycle_decision(
            issue_number=300,
            project="review-test",
            board="dev",
            cycle_iteration=0,
            decision_type="start",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            reason="Starting review cycle"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.REVIEW_CYCLE_STARTED
        data = call_args[1]['data']
        assert data['decision_category'] == 'review_cycle'
        assert data['inputs']['maker_agent'] == "software_engineer"
        assert data['inputs']['reviewer_agent'] == "code_reviewer"
    
    def test_emit_review_cycle_iteration(self, emitter, mock_obs):
        """Test review cycle iteration emission"""
        emitter.emit_review_cycle_decision(
            issue_number=301,
            project="review-test",
            board="dev",
            cycle_iteration=1,
            decision_type="iteration",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            reason="Iteration 1"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.REVIEW_CYCLE_ITERATION
        data = call_args[1]['data']
        assert data['inputs']['cycle_iteration'] == 1
    
    def test_emit_review_cycle_maker_selected(self, emitter, mock_obs):
        """Test review cycle maker selected emission"""
        emitter.emit_review_cycle_decision(
            issue_number=302,
            project="review-test",
            board="dev",
            cycle_iteration=1,
            decision_type="maker_selected",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            reason="Maker executing"
        )
        
        assert mock_obs.emit.called
        assert mock_obs.emit.call_args[0][0] == EventType.REVIEW_CYCLE_MAKER_SELECTED
    
    def test_emit_review_cycle_reviewer_selected(self, emitter, mock_obs):
        """Test review cycle reviewer selected emission"""
        emitter.emit_review_cycle_decision(
            issue_number=303,
            project="review-test",
            board="dev",
            cycle_iteration=1,
            decision_type="reviewer_selected",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            reason="Reviewer executing"
        )
        
        assert mock_obs.emit.called
        assert mock_obs.emit.call_args[0][0] == EventType.REVIEW_CYCLE_REVIEWER_SELECTED
    
    def test_emit_review_cycle_escalated(self, emitter, mock_obs):
        """Test review cycle escalated emission"""
        emitter.emit_review_cycle_decision(
            issue_number=304,
            project="review-test",
            board="dev",
            cycle_iteration=3,
            decision_type="escalate",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            reason="Max iterations reached",
            additional_data={'max_iterations': 3}
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.REVIEW_CYCLE_ESCALATED
        data = call_args[1]['data']
        assert data['max_iterations'] == 3
    
    def test_emit_review_cycle_completed(self, emitter, mock_obs):
        """Test review cycle completed emission"""
        emitter.emit_review_cycle_decision(
            issue_number=305,
            project="review-test",
            board="dev",
            cycle_iteration=2,
            decision_type="complete",
            maker_agent="software_engineer",
            reviewer_agent="code_reviewer",
            reason="Review approved"
        )
        
        assert mock_obs.emit.called
        assert mock_obs.emit.call_args[0][0] == EventType.REVIEW_CYCLE_COMPLETED
    
    # ========== CONVERSATIONAL LOOP TESTS ==========
    
    def test_emit_conversational_loop_started(self, emitter, mock_obs):
        """Test conversational loop started emission"""
        emitter.emit_conversational_loop_started(
            issue_number=400,
            project="conv-test",
            board="main",
            agent="business_analyst",
            workspace_type="discussions",
            discussion_id="disc123"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.CONVERSATIONAL_LOOP_STARTED
        data = call_args[1]['data']
        assert data['decision_category'] == 'conversational_loop'
        assert data['workspace_type'] == "discussions"
        assert data['discussion_id'] == "disc123"
    
    def test_emit_conversational_question_routed(self, emitter, mock_obs):
        """Test conversational question routed emission"""
        emitter.emit_conversational_question_routed(
            issue_number=401,
            project="conv-test",
            board="main",
            question="What is the architecture approach?",
            target_agent="software_architect",
            reason="Architecture question detected"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.CONVERSATIONAL_QUESTION_ROUTED
        data = call_args[1]['data']
        assert data['target_agent'] == "software_architect"
        assert "architecture" in data['question'].lower()
    
    def test_emit_conversational_question_routed_truncates_long_question(self, emitter, mock_obs):
        """Test that long questions are truncated"""
        long_question = "x" * 500
        
        emitter.emit_conversational_question_routed(
            issue_number=402,
            project="conv-test",
            board="main",
            question=long_question,
            target_agent="test_agent",
            reason="Test"
        )
        
        assert mock_obs.emit.called
        data = mock_obs.emit.call_args[1]['data']
        
        # Verify truncation
        assert len(data['question']) <= 203  # 200 + "..."
    
    def test_emit_conversational_loop_paused(self, emitter, mock_obs):
        """Test conversational loop paused emission"""
        emitter.emit_conversational_loop_paused(
            issue_number=403,
            project="conv-test",
            board="main",
            reason="Waiting for human input"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.CONVERSATIONAL_LOOP_PAUSED
        data = call_args[1]['data']
        assert data['reason'] == "Waiting for human input"
    
    def test_emit_conversational_loop_resumed(self, emitter, mock_obs):
        """Test conversational loop resumed emission"""
        emitter.emit_conversational_loop_resumed(
            issue_number=404,
            project="conv-test",
            board="main",
            reason="Human input received"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.CONVERSATIONAL_LOOP_RESUMED
        data = call_args[1]['data']
        assert data['reason'] == "Human input received"
    
    # ========== ERROR HANDLING TESTS ==========
    
    def test_emit_error_decision_encountered(self, emitter, mock_obs):
        """Test error encountered emission"""
        emitter.emit_error_decision(
            error_type="DockerImageNotFoundError",
            error_message="Image not found",
            context={'agent': 'test_agent', 'task_id': 'task123'},
            recovery_action="queue_dev_setup",
            success=False,  # Changed to False to test ERROR_ENCOUNTERED
            project="error-test"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.ERROR_ENCOUNTERED
        data = call_args[1]['data']
        assert data['decision_category'] == 'error_handling'
        assert data['error_type'] == "DockerImageNotFoundError"
        assert data['decision']['recovery_action'] == "queue_dev_setup"
        assert data['success'] is False
    
    def test_emit_error_decision_recovered(self, emitter, mock_obs):
        """Test error recovered emission"""
        emitter.emit_error_decision(
            error_type="APIRateLimitError",
            error_message="Rate limit exceeded",
            context={'api': 'github'},
            recovery_action="retry_with_backoff",
            success=True,
            project="error-test"
        )
        
        # Should use ERROR_RECOVERED when success=True
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        # The implementation actually uses ERROR_RECOVERED when success is True
        assert call_args[0][0] == EventType.ERROR_RECOVERED
    
    def test_emit_circuit_breaker_opened(self, emitter, mock_obs):
        """Test circuit breaker opened emission"""
        emitter.emit_circuit_breaker_opened(
            circuit_name="github_api",
            failure_count=5,
            threshold=5,
            last_error="Connection timeout"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.CIRCUIT_BREAKER_OPENED
        data = call_args[1]['data']
        assert data['circuit_name'] == "github_api"
        assert data['failure_count'] == 5
        assert data['threshold'] == 5
    
    def test_emit_circuit_breaker_closed(self, emitter, mock_obs):
        """Test circuit breaker closed emission"""
        emitter.emit_circuit_breaker_closed(
            circuit_name="github_api",
            reason="Service recovered"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.CIRCUIT_BREAKER_CLOSED
        data = call_args[1]['data']
        assert data['circuit_name'] == "github_api"
    
    def test_emit_retry_attempted(self, emitter, mock_obs):
        """Test retry attempted emission"""
        emitter.emit_retry_attempted(
            operation_name="github_api_call",
            attempt_number=2,
            max_attempts=3,
            project="retry-test",
            issue_number=500,
            last_error="Connection timeout"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.RETRY_ATTEMPTED
        data = call_args[1]['data']
        assert data['operation_name'] == "github_api_call"
        assert data['attempt_number'] == 2
        assert data['max_attempts'] == 3
    
    # ========== TASK QUEUE TESTS ==========
    
    def test_emit_task_queued(self, emitter, mock_obs):
        """Test task queued emission"""
        emitter.emit_task_queued(
            agent="software_engineer",
            project="queue-test",
            issue_number=600,
            board="dev",
            priority="NORMAL",
            reason="Agent task queued"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.TASK_QUEUED
        data = call_args[1]['data']
        assert data['decision_category'] == 'task_management'
        assert data['agent'] == "software_engineer"
        assert data['priority'] == "NORMAL"
    
    def test_emit_task_dequeued(self, emitter, mock_obs):
        """Test task dequeued emission"""
        emitter.emit_task_dequeued(
            agent="software_engineer",
            project="queue-test",
            issue_number=601,
            board="dev",
            reason="Ready for execution"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.TASK_DEQUEUED
        data = call_args[1]['data']
        assert data['agent'] == "software_engineer"
    
    def test_emit_task_priority_changed(self, emitter, mock_obs):
        """Test task priority changed emission"""
        emitter.emit_task_priority_changed(
            project="queue-test",
            issue_number=602,
            board="dev",
            old_priority="NORMAL",
            new_priority="HIGH",
            reason="Critical bug detected"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.TASK_PRIORITY_CHANGED
        data = call_args[1]['data']
        assert data['old_priority'] == "NORMAL"
        assert data['new_priority'] == "HIGH"
        assert data['decision']['new_priority'] == "HIGH"
    
    def test_emit_task_cancelled(self, emitter, mock_obs):
        """Test task cancelled emission"""
        emitter.emit_task_cancelled(
            project="queue-test",
            issue_number=603,
            board="dev",
            agent="software_engineer",
            reason="Issue closed"
        )
        
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        
        assert call_args[0][0] == EventType.TASK_CANCELLED
        data = call_args[1]['data']
        assert data['agent'] == "software_engineer"
        assert data['reason'] == "Issue closed"

    # ========== SUB-ISSUE CREATION TESTS ==========

    def test_emit_sub_issue_created_pr_review(self, emitter, mock_obs):
        """Test sub-issue created event for PR review workflow"""
        emitter.emit_sub_issue_created(
            project="test-project",
            parent_issue=456,
            issue_number=789,
            title="[PR Review] Missing error handling",
            board="SDLC Execution",
            reason="PR review finding: high severity issue",
            source="pr_review",
            context_data={
                'severity': 'high',
                'source_phase': 'code_review',
                'review_cycle': 2,
                'pr_url': 'https://github.com/org/repo/pull/123'
            },
            pipeline_run_id="pipeline_123"
        )

        # Verify structure
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        assert call_args[0][0] == EventType.SUB_ISSUE_CREATED
        assert call_args[1]['data']['decision_category'] == 'issue_creation'
        assert call_args[1]['data']['parent_issue'] == 456
        assert call_args[1]['data']['issue_number'] == 789
        assert call_args[1]['data']['inputs']['source'] == 'pr_review'
        assert call_args[1]['data']['inputs']['severity'] == 'high'
        assert call_args[1]['data']['inputs']['review_cycle'] == 2
        assert call_args[1]['data']['decision']['linked_to_parent'] is True
        assert call_args[1]['pipeline_run_id'] == "pipeline_123"

    def test_emit_sub_issue_created_work_breakdown(self, emitter, mock_obs):
        """Test sub-issue created event for work breakdown workflow"""
        emitter.emit_sub_issue_created(
            project="test-project",
            parent_issue=123,
            issue_number=456,
            title="Phase 1: Infrastructure setup",
            board="SDLC Execution",
            reason="Work breakdown phase: Phase 1: Infrastructure setup",
            source="work_breakdown",
            context_data={
                'phase': 'Phase 1: Infrastructure setup',
                'order_in_phase': 1
            },
            pipeline_run_id=None
        )

        # Verify work breakdown context
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        assert call_args[0][0] == EventType.SUB_ISSUE_CREATED
        assert call_args[1]['data']['inputs']['source'] == 'work_breakdown'
        assert call_args[1]['data']['inputs']['phase'] == 'Phase 1: Infrastructure setup'
        assert call_args[1]['data']['inputs']['order_in_phase'] == 1
        assert call_args[1]['pipeline_run_id'] is None

    def test_emit_sub_issue_creation_failed_pr_review(self, emitter, mock_obs):
        """Test sub-issue creation failed event for PR review workflow"""
        test_error = Exception("GitHub API rate limit exceeded")

        emitter.emit_sub_issue_creation_failed(
            project="test-project",
            parent_issue=456,
            title="[PR Review] Missing error handling",
            board="SDLC Execution",
            error=test_error,
            source="pr_review",
            context_data={
                'severity': 'high',
                'source_phase': 'code_review',
                'review_cycle': 2,
                'pr_url': 'https://github.com/org/repo/pull/123'
            },
            pipeline_run_id="pipeline_123"
        )

        # Verify structure
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        assert call_args[0][0] == EventType.SUB_ISSUE_CREATION_FAILED

        data = call_args[1]['data']
        assert data['decision_category'] == 'issue_creation'
        assert data['parent_issue'] == 456
        assert data['decision']['success'] is False
        assert data['decision']['error_type'] == 'Exception'
        assert 'rate limit' in data['decision']['error_message']
        assert data['inputs']['source'] == 'pr_review'
        assert data['inputs']['severity'] == 'high'

    def test_emit_sub_issue_creation_failed_work_breakdown(self, emitter, mock_obs):
        """Test sub-issue creation failed event for work breakdown workflow"""
        import subprocess
        test_error = subprocess.CalledProcessError(1, 'gh', stderr='Issue creation failed')

        emitter.emit_sub_issue_creation_failed(
            project="test-project",
            parent_issue=123,
            title="Phase 1: Infrastructure setup",
            board="SDLC Execution",
            error=test_error,
            source="work_breakdown",
            context_data={
                'phase': 'Phase 1: Infrastructure setup',
                'order_in_phase': 1
            },
            pipeline_run_id=None
        )

        # Verify structure
        assert mock_obs.emit.called
        call_args = mock_obs.emit.call_args
        assert call_args[0][0] == EventType.SUB_ISSUE_CREATION_FAILED

        data = call_args[1]['data']
        assert data['decision']['error_type'] == 'CalledProcessError'
        assert data['inputs']['source'] == 'work_breakdown'
        assert data['inputs']['phase'] == 'Phase 1: Infrastructure setup'

    # ========== CONSISTENCY TESTS ==========
    
    def test_all_decision_events_have_decision_category(self, emitter, mock_obs):
        """Test that all decision events include decision_category"""
        # Test a sample of different event types
        test_cases = [
            ('emit_agent_routing_decision', {
                'issue_number': 1, 'project': 'test', 'board': 'dev',
                'current_status': 'Ready', 'selected_agent': 'test',
                'reason': 'test'
            }),
            ('emit_feedback_detected', {
                'issue_number': 2, 'project': 'test', 'board': 'dev',
                'feedback_source': 'comment', 'feedback_content': 'test',
                'target_agent': 'test', 'action_taken': 'queue'
            }),
            ('emit_status_progression', {
                'issue_number': 3, 'project': 'test', 'board': 'dev',
                'from_status': 'A', 'to_status': 'B', 'trigger': 'test'
            }),
        ]
        
        for method_name, kwargs in test_cases:
            mock_obs.emit.reset_mock()
            method = getattr(emitter, method_name)
            method(**kwargs)
            
            data = mock_obs.emit.call_args[1]['data']
            assert 'decision_category' in data, f"{method_name} missing decision_category"
    
    def test_all_events_use_orchestrator_agent(self, emitter, mock_obs):
        """Test that all decision events use 'orchestrator' as agent"""
        # Sample different event types
        emitter.emit_agent_routing_decision(
            issue_number=1, project='test', board='dev',
            current_status='Ready', selected_agent='test', reason='test'
        )
        assert mock_obs.emit.call_args[1]['agent'] == 'orchestrator'
        
        mock_obs.emit.reset_mock()
        emitter.emit_feedback_detected(
            issue_number=2, project='test', board='dev',
            feedback_source='comment', feedback_content='test',
            target_agent='test', action_taken='queue'
        )
        assert mock_obs.emit.call_args[1]['agent'] == 'orchestrator'
    
    def test_task_id_format_consistency(self, emitter, mock_obs):
        """Test that task_id follows consistent format"""
        emitter.emit_agent_routing_decision(
            issue_number=123, project='my-project', board='dev',
            current_status='Ready', selected_agent='test', reason='test'
        )
        
        task_id = mock_obs.emit.call_args[1]['task_id']
        assert 'routing_my-project_123' in task_id


class TestGetDecisionEventEmitter:
    """Test the singleton getter function"""
    
    def test_get_decision_event_emitter_returns_instance(self):
        """Test that getter returns DecisionEventEmitter instance"""
        from monitoring.decision_events import get_decision_event_emitter
        
        emitter = get_decision_event_emitter()
        assert isinstance(emitter, DecisionEventEmitter)
    
    def test_get_decision_event_emitter_returns_same_instance(self):
        """Test that getter returns the same instance (singleton)"""
        from monitoring.decision_events import get_decision_event_emitter
        
        emitter1 = get_decision_event_emitter()
        emitter2 = get_decision_event_emitter()
        
        assert emitter1 is emitter2

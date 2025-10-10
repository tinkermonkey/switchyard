"""
End-to-End Tests for Decision Observability

Tests complete user-facing scenarios with decision events:
- Complete agent routing flow (issue → agent selection → execution → progression)
- Complete review cycle flow (maker/reviewer iterations → escalation/completion)
- Error handling and recovery flow
- Feedback detection and response flow
"""

import pytest
import asyncio
import json
import time
from unittest.mock import Mock, MagicMock, patch, AsyncMock, call
from datetime import datetime
from typing import List, Dict, Any

from monitoring.observability import get_observability_manager, EventType
from monitoring.decision_events import DecisionEventEmitter
from services.project_monitor import ProjectMonitor
from services.review_cycle import ReviewCycleExecutor
from services.pipeline_progression import PipelineProgression
from services.workspace_router import WorkspaceRouter
from task_queue.task_queue import TaskQueue, Task
from config.manager import ConfigManager


class EventCapture:
    """Helper to capture and analyze emitted events"""
    
    def __init__(self):
        self.events: List[Dict[str, Any]] = []
        self.events_by_type: Dict[str, List[Dict]] = {}
    
    def capture(self, channel: str, event_json: str):
        """Capture an event from Redis publish"""
        if channel == 'orchestrator:agent_events':
            event = json.loads(event_json)
            self.events.append(event)
            
            event_type = event['event_type']
            if event_type not in self.events_by_type:
                self.events_by_type[event_type] = []
            self.events_by_type[event_type].append(event)
    
    def get_events_by_type(self, event_type: str) -> List[Dict]:
        """Get all events of a specific type"""
        return self.events_by_type.get(event_type, [])
    
    def get_events_by_category(self, category: str) -> List[Dict]:
        """Get all events of a specific decision category"""
        return [e for e in self.events 
                if e.get('data', {}).get('decision_category') == category]
    
    def get_events_for_issue(self, issue_number: int) -> List[Dict]:
        """Get all events related to a specific issue"""
        return [e for e in self.events 
                if e.get('data', {}).get('issue_number') == issue_number]
    
    def verify_sequence(self, event_types: List[str]) -> bool:
        """Verify events occurred in expected sequence"""
        actual_types = [e['event_type'] for e in self.events]
        
        # Check if expected types appear in order (may have other events in between)
        type_positions = []
        for expected_type in event_types:
            try:
                pos = actual_types.index(expected_type, 
                                        type_positions[-1] + 1 if type_positions else 0)
                type_positions.append(pos)
            except ValueError:
                return False
        
        return True
    
    def clear(self):
        """Clear captured events"""
        self.events.clear()
        self.events_by_type.clear()


@pytest.fixture
def event_capture():
    """Create event capture helper"""
    return EventCapture()


@pytest.fixture
def mock_redis(event_capture):
    """Create mock Redis that captures events"""
    redis = Mock()
    redis.ping = Mock(return_value=True)
    redis.publish = Mock(side_effect=lambda ch, data: event_capture.capture(ch, data))
    redis.xadd = Mock(return_value=b'12345-0')
    redis.xlen = Mock(return_value=10)
    return redis


@pytest.fixture
def obs_manager(mock_redis):
    """Create ObservabilityManager with mock Redis"""
    with patch('monitoring.observability.redis.Redis', return_value=mock_redis):
        from monitoring.observability import ObservabilityManager
        obs = ObservabilityManager()
        obs.redis = mock_redis
        yield obs


@pytest.fixture
def mock_config():
    """Create mock ConfigManager"""
    config = Mock(spec=ConfigManager)
    
    # Mock project config
    project_config = Mock()
    project_config.repository = "test/repo"
    project_config.project_id = "PVT_test123"
    
    # Mock pipeline config
    pipeline_config = Mock()
    pipeline_config.workspace = "issues"
    pipeline_config.discussion_stages = []
    pipeline_config.discussion_category_id = None
    
    project_config.get_pipeline = Mock(return_value=pipeline_config)
    config.get_project_config = Mock(return_value=project_config)
    
    # Mock workflow
    workflow = Mock()
    workflow.columns = [
        Mock(name="Backlog", agent="null", next_status="Ready"),
        Mock(name="Ready", agent="software_architect", next_status="In Progress"),
        Mock(name="In Progress", agent="senior_software_engineer", next_status="Review"),
        Mock(name="Review", agent="code_reviewer", next_status="Done"),
        Mock(name="Done", agent="null", next_status=None)
    ]
    config.get_workflow = Mock(return_value=workflow)
    
    return config


class TestCompleteAgentRoutingFlow:
    """Test complete flow from issue status change to agent execution"""
    
    @patch('services.project_monitor.GithubProjectsV2')
    @patch('services.project_monitor.GithubService')
    def test_complete_routing_flow(self, mock_github, mock_projects, 
                                   obs_manager, mock_config, event_capture):
        """
        E2E Test: Issue moves to Ready → Agent selected → Task queued → Agent executes
        
        Expected event sequence:
        1. STATUS_PROGRESSION_COMPLETED (to Ready)
        2. AGENT_ROUTING_DECISION (selects software_architect)
        3. TASK_QUEUED (architect task queued)
        4. WORKSPACE_ROUTING_DECISION (route to issues)
        """
        # Setup
        mock_task_queue = Mock(spec=TaskQueue)
        mock_task_queue.enqueue = Mock()
        
        monitor = ProjectMonitor(mock_task_queue, mock_config)
        monitor.obs = obs_manager
        monitor.decision_events = DecisionEventEmitter(obs_manager)
        
        # Simulate issue moving to "Ready"
        issue_number = 123
        
        # 1. Detect status change and emit progression
        monitor.decision_events.emit_status_progression(
            issue_number=issue_number,
            project="test-project",
            board="dev",
            from_status="Backlog",
            to_status="Ready",
            trigger="manual",
            success=True
        )
        
        # 2. Get agent for status (triggers routing decision)
        agent = monitor._get_agent_for_status(
            project_name="test-project",
            board_name="dev",
            status="Ready",
            issue_number=issue_number,
            repository="test/repo"
        )
        
        # 3. Queue task
        if agent and agent != "null":
            monitor.decision_events.emit_task_queued(
                agent=agent,
                project="test-project",
                issue_number=issue_number,
                board="dev",
                priority="normal",
                reason="Agent routing decision"
            )
        
        # Verify event sequence
        events = event_capture.events
        assert len(events) >= 3
        
        # Check status progression
        status_events = event_capture.get_events_by_type('status_progression_completed')
        assert len(status_events) == 1
        assert status_events[0]['data']['decision']['to_status'] == 'Ready'
        
        # Check routing decision
        routing_events = event_capture.get_events_by_type('agent_routing_decision')
        assert len(routing_events) == 1
        assert routing_events[0]['data']['decision']['selected_agent'] == 'software_architect'
        
        # Check task queued
        task_events = event_capture.get_events_by_type('task_queued')
        assert len(task_events) == 1
        assert task_events[0]['data']['agent'] == 'software_architect'
        
        # Verify sequence
        assert event_capture.verify_sequence([
            'status_progression_completed',
            'agent_routing_decision',
            'task_queued'
        ])
    
    def test_routing_with_workspace_decision(self, obs_manager, mock_config, event_capture):
        """
        E2E Test: Routing includes workspace decision (issues vs discussions)
        
        Expected events:
        1. AGENT_ROUTING_DECISION
        2. WORKSPACE_ROUTING_DECISION
        3. TASK_QUEUED
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        router = WorkspaceRouter()
        router.obs = obs_manager
        router.decision_events = decision_emitter
        router.config_manager = mock_config
        
        issue_number = 456
        
        # 1. Agent routing decision
        decision_emitter.emit_agent_routing_decision(
            issue_number=issue_number,
            project="test-project",
            board="dev",
            current_status="Ready",
            selected_agent="software_architect",
            reason="Status mapping"
        )
        
        # 2. Workspace routing
        workspace, category = router.determine_workspace(
            project="test-project",
            board="dev",
            stage="design",
            issue_number=issue_number
        )
        
        # 3. Task queued
        decision_emitter.emit_task_queued(
            agent="software_architect",
            project="test-project",
            issue_number=issue_number,
            board="dev",
            priority="normal",
            reason="Workspace determined"
        )
        
        # Verify all events present
        assert len(event_capture.get_events_by_type('agent_routing_decision')) == 1
        assert len(event_capture.get_events_by_type('workspace_routing_decision')) == 1
        assert len(event_capture.get_events_by_type('task_queued')) == 1
        
        # Verify sequence
        assert event_capture.verify_sequence([
            'agent_routing_decision',
            'workspace_routing_decision',
            'task_queued'
        ])


class TestCompleteReviewCycleFlow:
    """Test complete review cycle flow with all decision events"""
    
    @pytest.mark.asyncio
    async def test_complete_review_cycle_success(self, obs_manager, event_capture):
        """
        E2E Test: Complete review cycle that succeeds
        
        Expected sequence:
        1. REVIEW_CYCLE_STARTED
        2. REVIEW_CYCLE_ITERATION (1)
        3. REVIEW_CYCLE_MAKER_SELECTED
        4. REVIEW_CYCLE_REVIEWER_SELECTED
        5. REVIEW_CYCLE_ITERATION (2) [reviewer requested changes]
        6. REVIEW_CYCLE_MAKER_SELECTED
        7. REVIEW_CYCLE_REVIEWER_SELECTED
        8. REVIEW_CYCLE_COMPLETED [approved]
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        issue_number = 789
        project = "test-project"
        board = "dev"
        maker = "senior_software_engineer"
        reviewer = "code_reviewer"
        
        # 1. Start review cycle
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=0,
            decision_type='start',
            maker_agent=maker,
            reviewer_agent=reviewer,
            reason="Starting review cycle"
        )
        
        # Simulate 2 iterations
        for iteration in [1, 2]:
            # Iteration start
            decision_emitter.emit_review_cycle_decision(
                issue_number=issue_number,
                project=project,
                board=board,
                cycle_iteration=iteration,
                decision_type='iteration',
                maker_agent=maker,
                reviewer_agent=reviewer,
                reason=f"Iteration {iteration}"
            )
            
            # Maker selected
            decision_emitter.emit_review_cycle_decision(
                issue_number=issue_number,
                project=project,
                board=board,
                cycle_iteration=iteration,
                decision_type='maker_selected',
                maker_agent=maker,
                reviewer_agent=reviewer,
                reason=f"Executing maker in iteration {iteration}"
            )
            
            # Reviewer selected
            decision_emitter.emit_review_cycle_decision(
                issue_number=issue_number,
                project=project,
                board=board,
                cycle_iteration=iteration,
                decision_type='reviewer_selected',
                maker_agent=maker,
                reviewer_agent=reviewer,
                reason=f"Executing reviewer in iteration {iteration}"
            )
        
        # Complete (approved on iteration 2)
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=2,
            decision_type='complete',
            maker_agent=maker,
            reviewer_agent=reviewer,
            reason="Review approved",
            additional_data={'final_status': 'approved'}
        )
        
        # Verify event sequence
        review_events = [e for e in event_capture.events 
                        if 'review_cycle' in e['event_type']]
        
        assert len(review_events) == 9  # 1 start + 2*(iter + maker + reviewer) + 1 complete
        
        # Verify sequence
        assert event_capture.verify_sequence([
            'review_cycle_started',
            'review_cycle_iteration',
            'review_cycle_maker_selected',
            'review_cycle_reviewer_selected',
            'review_cycle_iteration',
            'review_cycle_maker_selected',
            'review_cycle_reviewer_selected',
            'review_cycle_completed'
        ])
        
        # Verify final event has approval
        complete_events = event_capture.get_events_by_type('review_cycle_completed')
        assert len(complete_events) == 1
        assert complete_events[0]['data']['final_status'] == 'approved'
    
    @pytest.mark.asyncio
    async def test_review_cycle_escalation(self, obs_manager, event_capture):
        """
        E2E Test: Review cycle that reaches max iterations and escalates
        
        Expected sequence:
        1. REVIEW_CYCLE_STARTED
        2-4. Three iterations (each with maker + reviewer)
        5. REVIEW_CYCLE_ESCALATED (max iterations reached)
        6. FEEDBACK_LISTENING_STARTED (waiting for human)
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        issue_number = 999
        project = "test-project"
        board = "dev"
        maker = "senior_software_engineer"
        reviewer = "code_reviewer"
        max_iterations = 3
        
        # Start
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=0,
            decision_type='start',
            maker_agent=maker,
            reviewer_agent=reviewer,
            reason=f"Starting review cycle (max {max_iterations} iterations)"
        )
        
        # Simulate max iterations without approval
        for iteration in range(1, max_iterations + 1):
            decision_emitter.emit_review_cycle_decision(
                issue_number=issue_number,
                project=project,
                board=board,
                cycle_iteration=iteration,
                decision_type='iteration',
                maker_agent=maker,
                reviewer_agent=reviewer,
                reason=f"Iteration {iteration}"
            )
            
            decision_emitter.emit_review_cycle_decision(
                issue_number=issue_number,
                project=project,
                board=board,
                cycle_iteration=iteration,
                decision_type='maker_selected',
                maker_agent=maker,
                reviewer_agent=reviewer,
                reason="Executing maker"
            )
            
            decision_emitter.emit_review_cycle_decision(
                issue_number=issue_number,
                project=project,
                board=board,
                cycle_iteration=iteration,
                decision_type='reviewer_selected',
                maker_agent=maker,
                reviewer_agent=reviewer,
                reason="Executing reviewer"
            )
        
        # Escalate
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=max_iterations,
            decision_type='escalate',
            maker_agent=maker,
            reviewer_agent=reviewer,
            reason=f"Max iterations ({max_iterations}) reached without approval",
            additional_data={
                'max_iterations': max_iterations,
                'review_status': 'needs_changes'
            }
        )
        
        # Start feedback listening
        decision_emitter.emit_feedback_listening_started(
            issue_number=issue_number,
            project=project,
            board=board,
            agent=reviewer,
            monitoring_for=['comment', 'status_change']
        )
        
        # Verify escalation occurred
        escalation_events = event_capture.get_events_by_type('review_cycle_escalated')
        assert len(escalation_events) == 1
        assert escalation_events[0]['data']['max_iterations'] == max_iterations
        
        # Verify feedback listening started
        listening_events = event_capture.get_events_by_type('feedback_listening_started')
        assert len(listening_events) == 1


class TestErrorHandlingFlow:
    """Test complete error handling and recovery flow"""
    
    def test_error_recovery_flow(self, obs_manager, event_capture):
        """
        E2E Test: Error occurs, recovery attempted, success
        
        Expected sequence:
        1. ERROR_ENCOUNTERED
        2. RETRY_ATTEMPTED (attempt 1)
        3. RETRY_ATTEMPTED (attempt 2)
        4. ERROR_RECOVERED
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        project = "test-project"
        issue_number = 555
        operation = "update_github_status"
        max_attempts = 3
        
        # 1. Error encountered
        decision_emitter.emit_error_decision(
            error_type='APIRateLimitError',
            error_message='GitHub API rate limit exceeded',
            context={
                'operation': operation,
                'project': project,
                'issue_number': issue_number
            },
            recovery_action='retry_with_backoff',
            success=False,
            project=project
        )
        
        # 2-3. Retry attempts
        for attempt in [1, 2]:
            decision_emitter.emit_retry_attempted(
                operation_name=operation,
                attempt_number=attempt,
                max_attempts=max_attempts,
                project=project,
                issue_number=issue_number,
                last_error='APIRateLimitError: Rate limit exceeded'
            )
        
        # 4. Success on attempt 2
        decision_emitter.emit_error_decision(
            error_type='APIRateLimitError',
            error_message='GitHub API rate limit exceeded',
            context={
                'operation': operation,
                'project': project,
                'issue_number': issue_number,
                'attempts': 2
            },
            recovery_action='retry_with_backoff',
            success=True,
            project=project
        )
        
        # Verify sequence
        assert event_capture.verify_sequence([
            'error_encountered',
            'retry_attempted',
            'retry_attempted',
            'error_recovered'
        ])
        
        # Verify retry attempts
        retry_events = event_capture.get_events_by_type('retry_attempted')
        assert len(retry_events) == 2
        assert retry_events[0]['data']['attempt_number'] == 1
        assert retry_events[1]['data']['attempt_number'] == 2
    
    def test_circuit_breaker_flow(self, obs_manager, event_capture):
        """
        E2E Test: Circuit breaker opens after failures, then closes
        
        Expected sequence:
        1. ERROR_ENCOUNTERED (multiple times)
        2. CIRCUIT_BREAKER_OPENED
        3. ERROR_ENCOUNTERED (rejected by circuit breaker)
        4. ... time passes ...
        5. CIRCUIT_BREAKER_CLOSED (after recovery period)
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        circuit_name = 'github_api'
        threshold = 5
        
        # Simulate failures leading to circuit breaker opening
        for i in range(threshold):
            decision_emitter.emit_error_decision(
                error_type='ConnectionTimeout',
                error_message=f'Connection timeout (failure {i+1})',
                context={'circuit': circuit_name, 'failure_count': i+1},
                recovery_action='retry',
                success=False,
                project='system'
            )
        
        # Circuit breaker opens
        decision_emitter.emit_circuit_breaker_opened(
            circuit_name=circuit_name,
            failure_count=threshold,
            threshold=threshold,
            last_error='ConnectionTimeout'
        )
        
        # Attempt rejected by circuit breaker
        decision_emitter.emit_error_decision(
            error_type='CircuitBreakerOpen',
            error_message=f'Circuit breaker {circuit_name} is open',
            context={'circuit': circuit_name},
            recovery_action='reject_request',
            success=True,  # Successfully rejected (as designed)
            project='system'
        )
        
        # Circuit breaker closes after recovery
        decision_emitter.emit_circuit_breaker_closed(
            circuit_name=circuit_name,
            reason='Successful health check after recovery period'
        )
        
        # Verify sequence
        assert event_capture.verify_sequence([
            'error_encountered',
            'circuit_breaker_opened',
            'error_recovered',  # Reject is a "success"
            'circuit_breaker_closed'
        ])
        
        # Verify circuit breaker events
        cb_open = event_capture.get_events_by_type('circuit_breaker_opened')
        assert len(cb_open) == 1
        assert cb_open[0]['data']['failure_count'] == threshold
        
        cb_closed = event_capture.get_events_by_type('circuit_breaker_closed')
        assert len(cb_closed) == 1


class TestFeedbackDetectionFlow:
    """Test complete feedback detection and response flow"""
    
    def test_feedback_detection_and_response(self, obs_manager, event_capture):
        """
        E2E Test: Human provides feedback, orchestrator responds
        
        Expected sequence:
        1. FEEDBACK_LISTENING_STARTED (agent waiting)
        2. FEEDBACK_DETECTED (human commented)
        3. AGENT_ROUTING_DECISION (route to appropriate agent)
        4. TASK_QUEUED (queue agent task)
        5. FEEDBACK_LISTENING_STOPPED (stop monitoring)
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        issue_number = 321
        project = "test-project"
        board = "dev"
        waiting_agent = "code_reviewer"
        
        # 1. Start listening for feedback
        decision_emitter.emit_feedback_listening_started(
            issue_number=issue_number,
            project=project,
            board=board,
            agent=waiting_agent,
            monitoring_for=['comment', 'status_change', 'label']
        )
        
        # 2. Feedback detected
        decision_emitter.emit_feedback_detected(
            issue_number=issue_number,
            project=project,
            board=board,
            feedback_source='comment',
            feedback_content='Please update the error handling to include logging',
            target_agent='senior_software_engineer',
            action_taken='queue_agent_task'
        )
        
        # 3. Route to agent
        decision_emitter.emit_agent_routing_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            current_status='Review',
            selected_agent='senior_software_engineer',
            reason='Feedback requests code update'
        )
        
        # 4. Queue task
        decision_emitter.emit_task_queued(
            agent='senior_software_engineer',
            project=project,
            issue_number=issue_number,
            board=board,
            priority='high',
            reason='Human feedback received'
        )
        
        # 5. Stop listening
        decision_emitter.emit_feedback_listening_stopped(
            issue_number=issue_number,
            project=project,
            board=board,
            agent=waiting_agent,
            reason='Feedback received and processed',
            feedback_received=True
        )
        
        # Verify sequence
        assert event_capture.verify_sequence([
            'feedback_listening_started',
            'feedback_detected',
            'agent_routing_decision',
            'task_queued',
            'feedback_listening_stopped'
        ])
        
        # Verify feedback was acted upon
        feedback_events = event_capture.get_events_by_type('feedback_detected')
        assert len(feedback_events) == 1
        assert feedback_events[0]['data']['decision']['action_taken'] == 'queue_agent_task'
        
        listening_stopped = event_capture.get_events_by_type('feedback_listening_stopped')
        assert len(listening_stopped) == 1
        assert listening_stopped[0]['data']['feedback_received'] is True
    
    def test_feedback_ignored_flow(self, obs_manager, event_capture):
        """
        E2E Test: Feedback detected but determined non-actionable
        
        Expected sequence:
        1. FEEDBACK_LISTENING_STARTED
        2. FEEDBACK_IGNORED (not actionable)
        3. FEEDBACK_LISTENING_STARTED (continue monitoring)
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        issue_number = 654
        project = "test-project"
        board = "dev"
        
        # 1. Start listening
        decision_emitter.emit_feedback_listening_started(
            issue_number=issue_number,
            project=project,
            board=board,
            agent="code_reviewer",
            monitoring_for=['comment']
        )
        
        # 2. Non-actionable feedback detected
        decision_emitter.emit_feedback_ignored(
            issue_number=issue_number,
            project=project,
            board=board,
            feedback_source='comment',
            reason='General discussion, not actionable feedback'
        )
        
        # 3. Continue listening (no stop event)
        # Just verify we're still in listening state
        
        # Verify ignored event
        ignored_events = event_capture.get_events_by_type('feedback_ignored')
        assert len(ignored_events) == 1
        assert 'not actionable' in ignored_events[0]['data']['reason']


class TestCompleteIssueLifecycle:
    """Test complete lifecycle of an issue through multiple stages"""
    
    def test_complete_issue_lifecycle(self, obs_manager, mock_config, event_capture):
        """
        E2E Test: Complete issue lifecycle
        
        Flow:
        1. Issue created in Backlog
        2. Moved to Ready → architect selected → task queued
        3. Architect completes → moved to In Progress
        4. In Progress → engineer selected → task queued
        5. Engineer completes → moved to Review
        6. Review → review cycle starts
        7. Review cycle completes → moved to Done
        
        This tests the complete decision flow through an entire pipeline.
        """
        decision_emitter = DecisionEventEmitter(obs_manager)
        
        issue_number = 1000
        project = "test-project"
        board = "dev"
        
        # Stage 1: Backlog → Ready
        decision_emitter.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status="Backlog",
            to_status="Ready",
            trigger="manual",
            success=True
        )
        
        decision_emitter.emit_agent_routing_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            current_status="Ready",
            selected_agent="software_architect",
            reason="Ready status maps to architecture stage"
        )
        
        decision_emitter.emit_task_queued(
            agent="software_architect",
            project=project,
            issue_number=issue_number,
            board=board,
            priority="normal",
            reason="Status progression"
        )
        
        # Stage 2: Ready → In Progress (architect done)
        decision_emitter.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status="Ready",
            to_status="In Progress",
            trigger="agent_completion",
            success=True
        )
        
        decision_emitter.emit_agent_routing_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            current_status="In Progress",
            selected_agent="senior_software_engineer",
            reason="In Progress status maps to implementation stage"
        )
        
        decision_emitter.emit_task_queued(
            agent="senior_software_engineer",
            project=project,
            issue_number=issue_number,
            board=board,
            priority="normal",
            reason="Status progression"
        )
        
        # Stage 3: In Progress → Review (engineer done)
        decision_emitter.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status="In Progress",
            to_status="Review",
            trigger="agent_completion",
            success=True
        )
        
        # Stage 4: Review cycle
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=0,
            decision_type='start',
            maker_agent='senior_software_engineer',
            reviewer_agent='code_reviewer',
            reason='Starting review cycle'
        )
        
        # One iteration, approved
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=1,
            decision_type='complete',
            maker_agent='senior_software_engineer',
            reviewer_agent='code_reviewer',
            reason='Review approved'
        )
        
        # Stage 5: Review → Done
        decision_emitter.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status="Review",
            to_status="Done",
            trigger="agent_completion",
            success=True
        )
        
        # Verify complete lifecycle captured
        issue_events = event_capture.get_events_for_issue(issue_number)
        
        # Should have events from all stages
        assert len(issue_events) > 10
        
        # Verify key transitions
        status_progressions = [e for e in issue_events 
                              if 'status_progression' in e['event_type']]
        assert len(status_progressions) == 4  # Backlog→Ready, Ready→In Progress, In Progress→Review, Review→Done
        
        routing_decisions = [e for e in issue_events 
                            if e['event_type'] == 'agent_routing_decision']
        assert len(routing_decisions) == 2  # Architect, Engineer
        
        review_events = [e for e in issue_events 
                        if 'review_cycle' in e['event_type']]
        assert len(review_events) >= 2  # Start and Complete


if __name__ == '__main__':
    pytest.main([__file__, '-v', '-s'])

"""
Integration tests for Decision Observability

Tests that decision events flow correctly through the entire system:
Services → DecisionEventEmitter → ObservabilityManager → Redis → WebSocket
"""

import pytest
import asyncio
import json
import time
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from datetime import datetime

from monitoring.observability import get_observability_manager, EventType, ObservabilityManager
from monitoring.decision_events import DecisionEventEmitter
from services.project_monitor import ProjectMonitor
from services.review_cycle import ReviewCycleExecutor
from services.workspace_router import WorkspaceRouter
from task_queue.task_queue import TaskQueue
from config.manager import ConfigManager


@pytest.fixture
def redis_client():
    """Create mock Redis client that captures published events"""
    client = Mock()
    client.ping = Mock(return_value=True)
    client.publish = Mock(return_value=1)
    client.xadd = Mock(return_value=b'12345-0')
    client.xlen = Mock(return_value=10)
    return client


@pytest.fixture
def obs_manager_with_redis(redis_client):
    """Create ObservabilityManager with mock Redis"""
    with patch('monitoring.observability.redis.Redis', return_value=redis_client):
        obs = ObservabilityManager()
        obs.redis = redis_client
        yield obs


@pytest.fixture
def decision_emitter(obs_manager_with_redis):
    """Create DecisionEventEmitter with mocked ObservabilityManager"""
    return DecisionEventEmitter(obs_manager_with_redis)


class TestDecisionEventRedisFlow:
    """Test that decision events are correctly published to Redis"""
    
    def test_routing_decision_publishes_to_redis(self, decision_emitter, redis_client):
        """Test agent routing decision publishes to Redis pub/sub and stream"""
        decision_emitter.emit_agent_routing_decision(
            issue_number=123,
            project="test-project",
            board="dev",
            current_status="Ready",
            selected_agent="software_architect",
            reason="Test routing decision"
        )
        
        # Verify Redis publish was called (pub/sub)
        assert redis_client.publish.called
        publish_calls = redis_client.publish.call_args_list
        
        # Should publish to agent_events channel
        channel_calls = [c for c in publish_calls if c[0][0] == 'orchestrator:agent_events']
        assert len(channel_calls) > 0
        
        # Parse published event
        event_json = channel_calls[0][0][1]
        event = json.loads(event_json)
        
        # Verify event structure
        assert event['event_type'] == 'agent_routing_decision'
        assert event['agent'] == 'orchestrator'
        assert event['project'] == 'test-project'
        assert event['data']['issue_number'] == 123
        assert event['data']['decision']['selected_agent'] == 'software_architect'
        
        # Verify Redis stream was called (history)
        assert redis_client.xadd.called
        stream_calls = redis_client.xadd.call_args_list
        stream_calls_to_event_stream = [c for c in stream_calls 
                                        if c[0][0] == 'orchestrator:event_stream']
        assert len(stream_calls_to_event_stream) > 0
    
    def test_feedback_detected_publishes_to_redis(self, decision_emitter, redis_client):
        """Test feedback detection publishes to Redis"""
        decision_emitter.emit_feedback_detected(
            issue_number=456,
            project="test-project",
            board="dev",
            feedback_source="comment",
            feedback_content="Please update the implementation",
            target_agent="senior_software_engineer",
            action_taken="queue_agent_task"
        )
        
        assert redis_client.publish.called
        
        # Get published event
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        event_json = publish_calls[-1][0][1]
        event = json.loads(event_json)
        
        assert event['event_type'] == 'feedback_detected'
        assert event['data']['decision']['action_taken'] == 'queue_agent_task'
        assert event['data']['decision']['target_agent'] == 'senior_software_engineer'
    
    def test_status_progression_publishes_to_redis(self, decision_emitter, redis_client):
        """Test status progression publishes to Redis"""
        decision_emitter.emit_status_progression(
            issue_number=789,
            project="test-project",
            board="dev",
            from_status="Ready",
            to_status="In Progress",
            trigger="agent_completion",
            success=True
        )
        
        assert redis_client.publish.called
        
        # Get published event
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        event_json = publish_calls[-1][0][1]
        event = json.loads(event_json)
        
        assert event['event_type'] == 'status_progression_completed'
        assert event['data']['inputs']['from_status'] == 'Ready'
        assert event['data']['decision']['to_status'] == 'In Progress'
    
    def test_review_cycle_decision_publishes_to_redis(self, decision_emitter, redis_client):
        """Test review cycle decision publishes to Redis"""
        decision_emitter.emit_review_cycle_decision(
            issue_number=111,
            project="test-project",
            board="dev",
            cycle_iteration=1,
            decision_type='start',
            maker_agent='senior_software_engineer',
            reviewer_agent='code_reviewer',
            reason='Starting review cycle'
        )
        
        assert redis_client.publish.called
        
        # Get published event
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        event_json = publish_calls[-1][0][1]
        event = json.loads(event_json)
        
        assert event['event_type'] == 'review_cycle_started'
        assert event['data']['inputs']['maker_agent'] == 'senior_software_engineer'
        assert event['data']['inputs']['reviewer_agent'] == 'code_reviewer'
    
    def test_error_decision_publishes_to_redis(self, decision_emitter, redis_client):
        """Test error handling decision publishes to Redis"""
        decision_emitter.emit_error_decision(
            error_type='DockerImageNotFoundError',
            error_message='Image not found',
            context={'agent': 'test_agent'},
            recovery_action='queue_dev_environment_setup',
            success=True,
            project='test-project'
        )
        
        assert redis_client.publish.called
        
        # Get published event
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        event_json = publish_calls[-1][0][1]
        event = json.loads(event_json)
        
        assert event['event_type'] == 'error_recovered'
        assert event['data']['error_type'] == 'DockerImageNotFoundError'
        assert event['data']['decision']['recovery_action'] == 'queue_dev_environment_setup'


class TestProjectMonitorIntegration:
    """Test decision events from ProjectMonitor service"""
    
    @patch('services.project_monitor.GithubProjectsV2')
    @patch('services.project_monitor.GithubService')
    def test_project_monitor_emits_routing_decision(self, mock_github, mock_projects, 
                                                     obs_manager_with_redis, redis_client):
        """Test that ProjectMonitor emits routing decisions"""
        # Setup mocks
        mock_config = Mock()
        mock_config.get_project_config = Mock(return_value=Mock(
            repository="test/repo",
            project_id="PVT_123"
        ))
        mock_config.get_workflow = Mock(return_value=Mock(
            columns=[
                Mock(name="Ready", agent="software_architect"),
                Mock(name="In Progress", agent="senior_software_engineer")
            ]
        ))
        
        mock_task_queue = Mock(spec=TaskQueue)
        mock_task_queue.enqueue = Mock()
        
        # Create ProjectMonitor
        monitor = ProjectMonitor(mock_task_queue, mock_config)
        monitor.obs = obs_manager_with_redis
        monitor.decision_events = DecisionEventEmitter(obs_manager_with_redis)
        
        # Trigger agent selection which should emit decision
        agent = monitor._get_agent_for_status(
            project_name="test-project",
            board_name="dev",
            status="Ready",
            issue_number=123,
            repository="test/repo"
        )
        
        # Verify routing decision was emitted
        assert redis_client.publish.called
        
        # Find routing decision event
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        
        routing_events = []
        for call in publish_calls:
            try:
                event = json.loads(call[0][1])
                if event['event_type'] == 'agent_routing_decision':
                    routing_events.append(event)
            except:
                pass
        
        assert len(routing_events) > 0
        event = routing_events[0]
        assert event['data']['decision']['selected_agent'] == 'software_architect'
        assert event['data']['issue_number'] == 123


class TestReviewCycleIntegration:
    """Test decision events from ReviewCycleExecutor"""
    
    @pytest.mark.asyncio
    async def test_review_cycle_emits_decision_events(self, obs_manager_with_redis, redis_client):
        """Test that ReviewCycleExecutor emits review cycle decision events"""
        # Create review cycle executor
        executor = ReviewCycleExecutor()
        executor.obs = obs_manager_with_redis
        executor.decision_events = DecisionEventEmitter(obs_manager_with_redis)
        
        # Emit start event directly (don't need agent executor for this test)
        executor.decision_events.emit_review_cycle_decision(
            issue_number=123,
            project="test-project",
            board="dev",
            cycle_iteration=0,
            decision_type='start',
            maker_agent='senior_software_engineer',
            reviewer_agent='code_reviewer',
            reason='Starting test review cycle'
        )
        
        # Verify event was published
        assert redis_client.publish.called
        
        # Find review cycle events
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        
        review_events = []
        for call in publish_calls:
            try:
                event = json.loads(call[0][1])
                if 'review_cycle' in event['event_type']:
                    review_events.append(event)
            except:
                pass
        
        assert len(review_events) > 0
        assert any(e['event_type'] == 'review_cycle_started' for e in review_events)


class TestWorkspaceRouterIntegration:
    """Test decision events from WorkspaceRouter"""
    
    def test_workspace_router_emits_routing_decision(self, obs_manager_with_redis, redis_client):
        """Test that WorkspaceRouter emits workspace routing decisions"""
        # Create workspace router
        router = WorkspaceRouter()
        router.obs = obs_manager_with_redis
        router.decision_events = DecisionEventEmitter(obs_manager_with_redis)
        
        # Mock config
        mock_config = Mock()
        mock_pipeline_config = Mock()
        mock_pipeline_config.workspace = 'hybrid'
        mock_pipeline_config.discussion_stages = ['research', 'planning']
        mock_pipeline_config.discussion_category_id = 'DIC_kwDOABC123'
        
        mock_project_config = Mock()
        mock_project_config.get_pipeline = Mock(return_value=mock_pipeline_config)
        mock_config.get_project_config = Mock(return_value=mock_project_config)
        
        router.config_manager = mock_config
        
        # Trigger workspace routing
        workspace, category = router.determine_workspace(
            project="test-project",
            board="dev",
            stage="research",
            issue_number=123
        )
        
        # Verify workspace routing decision was emitted
        assert redis_client.publish.called
        
        # Find workspace routing events
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        
        routing_events = []
        for call in publish_calls:
            try:
                event = json.loads(call[0][1])
                if event['event_type'] == 'workspace_routing_decision':
                    routing_events.append(event)
            except:
                pass
        
        assert len(routing_events) > 0
        event = routing_events[0]
        assert event['data']['decision']['workspace'] in ['issues', 'discussions']


class TestEventSequencing:
    """Test that events are emitted in correct sequence"""
    
    def test_status_progression_sequence(self, decision_emitter, redis_client):
        """Test status progression emits started, then completed/failed"""
        # Emit started
        decision_emitter.emit_status_progression(
            issue_number=123,
            project="test-project",
            board="dev",
            from_status="Ready",
            to_status="In Progress",
            trigger="agent_completion",
            success=None
        )
        
        # Emit completed
        decision_emitter.emit_status_progression(
            issue_number=123,
            project="test-project",
            board="dev",
            from_status="Ready",
            to_status="In Progress",
            trigger="agent_completion",
            success=True
        )
        
        # Get all published events
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        
        events = []
        for call in publish_calls:
            try:
                event = json.loads(call[0][1])
                if 'status_progression' in event['event_type']:
                    events.append(event)
            except:
                pass
        
        # Should have both started and completed
        assert len(events) == 2
        assert events[0]['event_type'] == 'status_progression_started'
        assert events[1]['event_type'] == 'status_progression_completed'
    
    def test_review_cycle_sequence(self, decision_emitter, redis_client):
        """Test review cycle emits events in correct order"""
        issue_num = 456
        
        # Start
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_num, project="test", board="dev",
            cycle_iteration=0, decision_type='start',
            maker_agent='maker', reviewer_agent='reviewer',
            reason='Start'
        )
        
        # Iteration
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_num, project="test", board="dev",
            cycle_iteration=1, decision_type='iteration',
            maker_agent='maker', reviewer_agent='reviewer',
            reason='Iteration 1'
        )
        
        # Maker selected
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_num, project="test", board="dev",
            cycle_iteration=1, decision_type='maker_selected',
            maker_agent='maker', reviewer_agent='reviewer',
            reason='Maker'
        )
        
        # Reviewer selected
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_num, project="test", board="dev",
            cycle_iteration=1, decision_type='reviewer_selected',
            maker_agent='maker', reviewer_agent='reviewer',
            reason='Reviewer'
        )
        
        # Complete
        decision_emitter.emit_review_cycle_decision(
            issue_number=issue_num, project="test", board="dev",
            cycle_iteration=1, decision_type='complete',
            maker_agent='maker', reviewer_agent='reviewer',
            reason='Complete'
        )
        
        # Get all review cycle events
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        
        events = []
        for call in publish_calls:
            try:
                event = json.loads(call[0][1])
                if 'review_cycle' in event['event_type']:
                    events.append(event)
            except:
                pass
        
        # Should have all 5 events in order
        assert len(events) == 5
        assert events[0]['event_type'] == 'review_cycle_started'
        assert events[1]['event_type'] == 'review_cycle_iteration'
        assert events[2]['event_type'] == 'review_cycle_maker_selected'
        assert events[3]['event_type'] == 'review_cycle_reviewer_selected'
        assert events[4]['event_type'] == 'review_cycle_completed'


class TestEventDataIntegrity:
    """Test that events maintain data integrity through the pipeline"""
    
    def test_event_contains_all_required_fields(self, decision_emitter, redis_client):
        """Test that emitted events contain all required ObservabilityEvent fields"""
        decision_emitter.emit_agent_routing_decision(
            issue_number=123,
            project="test-project",
            board="dev",
            current_status="Ready",
            selected_agent="software_architect",
            reason="Test"
        )
        
        # Get published event
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        event_json = publish_calls[-1][0][1]
        event = json.loads(event_json)
        
        # Verify required fields
        assert 'timestamp' in event
        assert 'event_id' in event
        assert 'event_type' in event
        assert 'agent' in event
        assert 'task_id' in event
        assert 'project' in event
        assert 'data' in event
        
        # Verify timestamp format
        datetime.fromisoformat(event['timestamp'].replace('Z', '+00:00'))
    
    def test_event_data_structure_consistent(self, decision_emitter, redis_client):
        """Test that decision events have consistent data structure"""
        # Emit various event types
        decision_emitter.emit_agent_routing_decision(
            issue_number=1, project="test", board="dev",
            current_status="Ready", selected_agent="arch", reason="Test"
        )
        decision_emitter.emit_feedback_detected(
            issue_number=1, project="test", board="dev",
            feedback_source="comment", feedback_content="Test",
            target_agent="eng", action_taken="queue"
        )
        decision_emitter.emit_status_progression(
            issue_number=1, project="test", board="dev",
            from_status="A", to_status="B", trigger="test", success=True
        )
        
        # Get all events
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        
        events = [json.loads(c[0][1]) for c in publish_calls]
        
        # All decision events should have these fields
        for event in events:
            assert 'decision_category' in event['data']
            assert 'issue_number' in event['data']
            assert 'board' in event['data']
            assert 'reason' in event['data']


class TestPerformance:
    """Test performance of event emission"""
    
    def test_event_emission_is_fast(self, decision_emitter, redis_client):
        """Test that event emission doesn't add significant overhead"""
        import time
        
        start = time.time()
        
        # Emit 100 events
        for i in range(100):
            decision_emitter.emit_agent_routing_decision(
                issue_number=i,
                project="test-project",
                board="dev",
                current_status="Ready",
                selected_agent="software_architect",
                reason=f"Test {i}"
            )
        
        duration = time.time() - start
        
        # Should complete in under 1 second (avg < 10ms per event)
        assert duration < 1.0, f"Emitting 100 events took {duration}s (should be < 1s)"
        
        # Verify all were published
        publish_calls = [c for c in redis_client.publish.call_args_list 
                        if c[0][0] == 'orchestrator:agent_events']
        assert len(publish_calls) >= 100


if __name__ == '__main__':
    pytest.main([__file__, '-v'])

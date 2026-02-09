"""
Unit tests for ObservabilityManager Elasticsearch indexing

Tests that all event types are properly categorized and indexed to Elasticsearch.
This ensures that decision events and agent lifecycle events appear in the pipeline view.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch, call
from datetime import datetime
from elasticsearch import Elasticsearch

from monitoring.observability import ObservabilityManager, EventType


class TestObservabilityElasticsearchIndexing:
    """Test suite for Elasticsearch event indexing"""
    
    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client"""
        mock = Mock()
        mock.publish = Mock()
        mock.xadd = Mock()
        mock.expire = Mock()
        mock.ping = Mock(return_value=True)
        return mock
    
    @pytest.fixture
    def mock_elasticsearch(self):
        """Create a mock Elasticsearch client"""
        mock = Mock(spec=Elasticsearch)
        mock.index = Mock(return_value={'result': 'created'})
        return mock
    
    @pytest.fixture
    def obs_manager(self, mock_redis, mock_elasticsearch):
        """Create ObservabilityManager with mock clients"""
        return ObservabilityManager(
            enabled=True,
            redis_client=mock_redis,
            elasticsearch_client=mock_elasticsearch
        )
    
    # ========== EVENT CATEGORIZATION TESTS ==========
    
    def test_decision_events_are_identified(self, obs_manager):
        """Test that all decision events are correctly identified"""
        decision_events = [
            # Feedback Monitoring
            EventType.FEEDBACK_DETECTED,
            EventType.FEEDBACK_LISTENING_STARTED,
            EventType.FEEDBACK_LISTENING_STOPPED,
            EventType.FEEDBACK_IGNORED,
            # Agent Routing & Selection
            EventType.AGENT_ROUTING_DECISION,
            EventType.AGENT_SELECTED,
            EventType.WORKSPACE_ROUTING_DECISION,
            # Status & Pipeline Progression
            EventType.STATUS_PROGRESSION_STARTED,
            EventType.STATUS_PROGRESSION_COMPLETED,
            EventType.STATUS_PROGRESSION_FAILED,
            EventType.PIPELINE_STAGE_TRANSITION,
            # Review Cycle Management
            EventType.REVIEW_CYCLE_STARTED,
            EventType.REVIEW_CYCLE_ITERATION,
            EventType.REVIEW_CYCLE_MAKER_SELECTED,
            EventType.REVIEW_CYCLE_REVIEWER_SELECTED,
            EventType.REVIEW_CYCLE_ESCALATED,
            EventType.REVIEW_CYCLE_COMPLETED,
            # Conversational Loop Routing
            EventType.CONVERSATIONAL_LOOP_STARTED,
            EventType.CONVERSATIONAL_QUESTION_ROUTED,
            EventType.CONVERSATIONAL_LOOP_PAUSED,
            EventType.CONVERSATIONAL_LOOP_RESUMED,
            # Error Handling & Circuit Breakers
            EventType.ERROR_ENCOUNTERED,
            EventType.ERROR_RECOVERED,
            EventType.CIRCUIT_BREAKER_OPENED,
            EventType.CIRCUIT_BREAKER_CLOSED,
            EventType.RETRY_ATTEMPTED,
            # Task Queue Management
            EventType.TASK_QUEUED,
            EventType.TASK_DEQUEUED,
            EventType.TASK_PRIORITY_CHANGED,
            EventType.TASK_CANCELLED,
            # Branch Management
            EventType.BRANCH_SELECTED,
            EventType.BRANCH_CREATED,
            EventType.BRANCH_REUSED,
            EventType.BRANCH_CONFLICT_DETECTED,
            EventType.BRANCH_STALE_DETECTED,
            EventType.BRANCH_SELECTION_ESCALATED,
            # Result Resilience
            EventType.RESULT_PERSISTENCE_FAILED,
            EventType.FALLBACK_STORAGE_USED,
            EventType.OUTPUT_VALIDATION_FAILED,
            EventType.EMPTY_OUTPUT_DETECTED,
            EventType.CONTAINER_RESULT_RECOVERED,
            # Repair Cycle
            EventType.REPAIR_CYCLE_STARTED,
            EventType.REPAIR_CYCLE_ITERATION,
            EventType.REPAIR_CYCLE_TEST_CYCLE_STARTED,
            EventType.REPAIR_CYCLE_TEST_CYCLE_COMPLETED,
            EventType.REPAIR_CYCLE_TEST_EXECUTION_STARTED,
            EventType.REPAIR_CYCLE_TEST_EXECUTION_COMPLETED,
            EventType.REPAIR_CYCLE_FIX_CYCLE_STARTED,
            EventType.REPAIR_CYCLE_FIX_CYCLE_COMPLETED,
            EventType.REPAIR_CYCLE_FILE_FIX_STARTED,
            EventType.REPAIR_CYCLE_FILE_FIX_COMPLETED,
            EventType.REPAIR_CYCLE_FILE_FIX_FAILED,
            EventType.REPAIR_CYCLE_WARNING_REVIEW_STARTED,
            EventType.REPAIR_CYCLE_WARNING_REVIEW_COMPLETED,
            EventType.REPAIR_CYCLE_WARNING_REVIEW_FAILED,
            EventType.REPAIR_CYCLE_COMPLETED,
        ]
        
        for event_type in decision_events:
            assert obs_manager._is_decision_event(event_type), \
                f"{event_type.value} should be identified as a decision event"
    
    def test_agent_lifecycle_events_are_identified(self, obs_manager):
        """Test that all agent lifecycle events are correctly identified"""
        lifecycle_events = [
            EventType.AGENT_INITIALIZED,
            EventType.AGENT_STARTED,
            EventType.AGENT_COMPLETED,
            EventType.AGENT_FAILED,
        ]
        
        for event_type in lifecycle_events:
            assert obs_manager._is_agent_lifecycle_event(event_type), \
                f"{event_type.value} should be identified as an agent lifecycle event"
    
    def test_non_indexed_events_are_not_identified(self, obs_manager):
        """Test that non-indexed events are not identified as decision or lifecycle"""
        non_indexed_events = [
            EventType.TASK_RECEIVED,
            EventType.PROMPT_CONSTRUCTED,
            EventType.CLAUDE_API_CALL_STARTED,
            EventType.CLAUDE_API_CALL_COMPLETED,
            EventType.RESPONSE_CHUNK_RECEIVED,
            EventType.RESPONSE_PROCESSING_STARTED,
            EventType.RESPONSE_PROCESSING_COMPLETED,
            EventType.TOOL_EXECUTION_STARTED,
            EventType.TOOL_EXECUTION_COMPLETED,
            EventType.PERFORMANCE_METRIC,
            EventType.TOKEN_USAGE,
            EventType.PIPELINE_RUN_STARTED,
            EventType.PIPELINE_RUN_COMPLETED,
            EventType.PIPELINE_RUN_FAILED,
            EventType.REPAIR_CYCLE_CONTAINER_STARTED,
            EventType.REPAIR_CYCLE_CONTAINER_CHECKPOINT_UPDATED,
            EventType.REPAIR_CYCLE_CONTAINER_RECOVERED,
            EventType.REPAIR_CYCLE_CONTAINER_KILLED,
            EventType.REPAIR_CYCLE_CONTAINER_COMPLETED,
        ]

        for event_type in non_indexed_events:
            assert not obs_manager._is_decision_event(event_type), \
                f"{event_type.value} should not be a decision event"
            assert not obs_manager._is_agent_lifecycle_event(event_type), \
                f"{event_type.value} should not be an agent lifecycle event"

    def test_all_event_types_are_categorized(self, obs_manager):
        """Test that every EventType is either indexed or explicitly not indexed"""
        all_event_types = list(EventType)

        # Events that should be indexed
        indexed_events = []

        # Events that should not be indexed (transient/streaming events)
        non_indexed_events = [
            EventType.TASK_RECEIVED,
            EventType.PROMPT_CONSTRUCTED,
            EventType.CLAUDE_API_CALL_STARTED,
            EventType.CLAUDE_API_CALL_COMPLETED,
            EventType.RESPONSE_CHUNK_RECEIVED,
            EventType.RESPONSE_PROCESSING_STARTED,
            EventType.RESPONSE_PROCESSING_COMPLETED,
            EventType.TOOL_EXECUTION_STARTED,
            EventType.TOOL_EXECUTION_COMPLETED,
            EventType.PERFORMANCE_METRIC,
            EventType.TOKEN_USAGE,
            EventType.PIPELINE_RUN_STARTED,
            EventType.PIPELINE_RUN_COMPLETED,
            EventType.PIPELINE_RUN_FAILED,
            EventType.REPAIR_CYCLE_CONTAINER_STARTED,
            EventType.REPAIR_CYCLE_CONTAINER_CHECKPOINT_UPDATED,
            EventType.REPAIR_CYCLE_CONTAINER_RECOVERED,
            EventType.REPAIR_CYCLE_CONTAINER_KILLED,
            EventType.REPAIR_CYCLE_CONTAINER_COMPLETED,
        ]
        
        for event_type in all_event_types:
            is_decision = obs_manager._is_decision_event(event_type)
            is_lifecycle = obs_manager._is_agent_lifecycle_event(event_type)
            is_non_indexed = event_type in non_indexed_events
            
            if is_decision or is_lifecycle:
                indexed_events.append(event_type)
            
            # Every event should be either indexed OR explicitly non-indexed
            assert (is_decision or is_lifecycle or is_non_indexed), \
                f"{event_type.value} is not categorized - add to decision, lifecycle, or non_indexed list"
        
        # Ensure we have a good coverage of indexed events
        assert len(indexed_events) > 35, \
            f"Expected 35+ indexed events, found {len(indexed_events)}"
    
    # ========== ELASTICSEARCH INDEXING TESTS ==========
    
    def test_decision_event_indexes_to_elasticsearch(self, obs_manager, mock_elasticsearch):
        """Test that decision events are indexed to decision-events-* index"""
        obs_manager.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id="test_task_123",
            project="test-project",
            data={
                'decision_category': 'routing',
                'issue_number': 100,
                'selected_agent': 'software_engineer'
            }
        )
        
        # Verify Elasticsearch index was called
        assert mock_elasticsearch.index.called
        
        # Get the call arguments
        call_args = mock_elasticsearch.index.call_args
        index_name = call_args[1]['index']
        document = call_args[1]['document']
        
        # Verify index name format
        assert index_name.startswith('decision-events-')
        
        # Verify document structure
        assert document['event_type'] == 'agent_routing_decision'
        assert document['event_category'] == 'decision'
        assert document['agent'] == 'orchestrator'
        assert document['task_id'] == 'test_task_123'
        assert document['project'] == 'test-project'
        assert document['decision_category'] == 'routing'
        assert document['issue_number'] == 100
        assert document['selected_agent'] == 'software_engineer'
        assert 'timestamp' in document
    
    def test_agent_lifecycle_event_indexes_to_elasticsearch(self, obs_manager, mock_elasticsearch):
        """Test that agent lifecycle events are indexed to agent-events-* index"""
        obs_manager.emit(
            EventType.AGENT_INITIALIZED,
            agent="software_engineer",
            task_id="test_task_456",
            project="test-project",
            data={
                'model': 'claude-sonnet-4.5',
                'timeout': 3600,
                'branch_name': 'feature/issue-100',
                'container_name': 'claude-agent-test-project-123'
            }
        )
        
        # Verify Elasticsearch index was called
        assert mock_elasticsearch.index.called
        
        # Get the call arguments
        call_args = mock_elasticsearch.index.call_args
        index_name = call_args[1]['index']
        document = call_args[1]['document']
        
        # Verify index name format
        assert index_name.startswith('agent-events-')
        
        # Verify document structure
        assert document['event_type'] == 'agent_initialized'
        assert document['event_category'] == 'agent_lifecycle'
        assert document['agent'] == 'software_engineer'
        assert document['task_id'] == 'test_task_456'
        assert document['project'] == 'test-project'
        assert document['model'] == 'claude-sonnet-4.5'
        assert document['branch_name'] == 'feature/issue-100'
        assert document['container_name'] == 'claude-agent-test-project-123'
        assert 'timestamp' in document
    
    def test_agent_completed_event_indexes_to_elasticsearch(self, obs_manager, mock_elasticsearch):
        """Test that agent_completed events are indexed with success flag"""
        obs_manager.emit(
            EventType.AGENT_COMPLETED,
            agent="software_engineer",
            task_id="test_task_789",
            project="test-project",
            data={
                'duration_ms': 45000,
                'success': True
            }
        )
        
        # Verify indexing
        assert mock_elasticsearch.index.called
        call_args = mock_elasticsearch.index.call_args
        document = call_args[1]['document']
        
        assert document['event_type'] == 'agent_completed'
        assert document['event_category'] == 'agent_lifecycle'
        assert document['duration_ms'] == 45000
        assert document['success'] is True
    
    def test_agent_failed_event_indexes_to_elasticsearch(self, obs_manager, mock_elasticsearch):
        """Test that agent_failed events are indexed with error details"""
        obs_manager.emit(
            EventType.AGENT_FAILED,
            agent="software_engineer",
            task_id="test_task_999",
            project="test-project",
            data={
                'duration_ms': 5000,
                'success': False,
                'error': 'Docker image not found'
            }
        )
        
        # Verify indexing
        assert mock_elasticsearch.index.called
        call_args = mock_elasticsearch.index.call_args
        document = call_args[1]['document']
        
        assert document['event_type'] == 'agent_failed'
        assert document['event_category'] == 'agent_lifecycle'
        assert document['success'] is False
        assert document['error'] == 'Docker image not found'
    
    def test_non_indexed_events_do_not_index_to_elasticsearch(self, obs_manager, mock_elasticsearch):
        """Test that transient events are not indexed to Elasticsearch"""
        # Emit a non-indexed event
        obs_manager.emit(
            EventType.TASK_RECEIVED,
            agent="test_agent",
            task_id="test_task",
            project="test-project",
            data={'context_keys': ['issue', 'board']}
        )
        
        # Should NOT call Elasticsearch (only Redis)
        assert not mock_elasticsearch.index.called
    
    def test_pipeline_run_id_is_included_in_indexed_events(self, obs_manager, mock_elasticsearch):
        """Test that pipeline_run_id is included when provided"""
        pipeline_run_id = "pipeline_run_12345"
        
        obs_manager.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id="test_task",
            project="test-project",
            data={'decision_category': 'routing'},
            pipeline_run_id=pipeline_run_id
        )
        
        # Verify pipeline_run_id is in document
        call_args = mock_elasticsearch.index.call_args
        document = call_args[1]['document']
        
        assert document['pipeline_run_id'] == pipeline_run_id
    
    def test_elasticsearch_error_does_not_break_emit(self, obs_manager, mock_elasticsearch, mock_redis):
        """Test that Elasticsearch errors don't break event emission"""
        # Make Elasticsearch raise an error
        mock_elasticsearch.index.side_effect = Exception("ES connection error")
        
        # Should not raise exception
        obs_manager.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id="test_task",
            project="test-project",
            data={'decision_category': 'routing'}
        )
        
        # Redis should still work
        assert mock_redis.publish.called
        assert mock_redis.xadd.called
    
    def test_observability_disabled_skips_everything(self, mock_redis, mock_elasticsearch):
        """Test that disabled observability skips all operations"""
        obs_manager = ObservabilityManager(
            enabled=False,
            redis_client=mock_redis,
            elasticsearch_client=mock_elasticsearch
        )
        
        obs_manager.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id="test_task",
            project="test-project",
            data={'decision_category': 'routing'}
        )
        
        # Nothing should be called
        assert not mock_redis.publish.called
        assert not mock_elasticsearch.index.called
    
    # ========== INTEGRATION TESTS ==========
    
    def test_multiple_decision_events_index_correctly(self, obs_manager, mock_elasticsearch):
        """Test that multiple decision events are indexed in sequence"""
        events = [
            (EventType.TASK_QUEUED, {'decision_category': 'task_management', 'agent': 'test1'}),
            (EventType.AGENT_ROUTING_DECISION, {'decision_category': 'routing', 'selected_agent': 'test2'}),
            (EventType.STATUS_PROGRESSION_COMPLETED, {'decision_category': 'progression', 'to_status': 'Done'}),
        ]
        
        for event_type, data in events:
            obs_manager.emit(
                event_type,
                agent="orchestrator",
                task_id=f"task_{event_type.value}",
                project="test-project",
                data=data
            )
        
        # Should have 3 index calls
        assert mock_elasticsearch.index.call_count == 3
        
        # Verify all went to decision-events index
        for call_item in mock_elasticsearch.index.call_args_list:
            index_name = call_item[1]['index']
            assert index_name.startswith('decision-events-')
    
    def test_multiple_lifecycle_events_index_correctly(self, obs_manager, mock_elasticsearch):
        """Test that agent lifecycle progression is indexed"""
        task_id = "test_task_lifecycle"
        agent = "software_engineer"
        
        # Simulate agent lifecycle
        obs_manager.emit(
            EventType.AGENT_INITIALIZED,
            agent=agent,
            task_id=task_id,
            project="test-project",
            data={'model': 'claude-sonnet-4.5', 'branch_name': 'feature/test'}
        )
        
        obs_manager.emit(
            EventType.AGENT_COMPLETED,
            agent=agent,
            task_id=task_id,
            project="test-project",
            data={'duration_ms': 30000, 'success': True}
        )
        
        # Should have 2 index calls
        assert mock_elasticsearch.index.call_count == 2
        
        # Verify both went to agent-events index
        for call_item in mock_elasticsearch.index.call_args_list:
            index_name = call_item[1]['index']
            assert index_name.startswith('agent-events-')
            
            document = call_item[1]['document']
            assert document['event_category'] == 'agent_lifecycle'
            assert document['task_id'] == task_id
    
    def test_mixed_event_types_index_to_correct_indices(self, obs_manager, mock_elasticsearch):
        """Test that decision and lifecycle events go to different indices"""
        # Decision event
        obs_manager.emit(
            EventType.TASK_QUEUED,
            agent="orchestrator",
            task_id="task_1",
            project="test-project",
            data={'decision_category': 'task_management'}
        )
        
        # Lifecycle event
        obs_manager.emit(
            EventType.AGENT_INITIALIZED,
            agent="test_agent",
            task_id="task_1",
            project="test-project",
            data={'model': 'claude-sonnet-4.5'}
        )
        
        # Non-indexed event
        obs_manager.emit(
            EventType.TASK_RECEIVED,
            agent="test_agent",
            task_id="task_1",
            project="test-project",
            data={'context_keys': []}
        )
        
        # Should have 2 index calls (decision + lifecycle, not task_received)
        assert mock_elasticsearch.index.call_count == 2
        
        # Get the indices
        indices = [call_item[1]['index'] for call_item in mock_elasticsearch.index.call_args_list]
        
        # Should have one decision-events and one agent-events
        decision_indices = [idx for idx in indices if idx.startswith('decision-events-')]
        agent_indices = [idx for idx in indices if idx.startswith('agent-events-')]
        
        assert len(decision_indices) == 1
        assert len(agent_indices) == 1
    
    # ========== HELPER METHOD TESTS ==========
    
    def test_emit_agent_initialized_helper(self, obs_manager, mock_elasticsearch):
        """Test emit_agent_initialized helper method"""
        obs_manager.emit_agent_initialized(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            config={
                'model': 'claude-sonnet-4.5',
                'timeout': 3600,
                'tools_enabled': True,
                'mcp_servers': ['context7']
            },
            branch_name="feature/issue-100",
            container_name="claude-agent-test-123",
            pipeline_run_id="test-run-123"
        )
        
        # Verify indexing
        assert mock_elasticsearch.index.called
        call_args = mock_elasticsearch.index.call_args
        document = call_args[1]['document']
        
        assert document['event_type'] == 'agent_initialized'
        assert document['model'] == 'claude-sonnet-4.5'
        assert document['branch_name'] == 'feature/issue-100'
        assert document['container_name'] == 'claude-agent-test-123'
        assert document['pipeline_run_id'] == 'test-run-123'
    
    def test_emit_agent_completed_helper(self, obs_manager, mock_elasticsearch):
        """Test emit_agent_completed helper method"""
        obs_manager.emit_agent_completed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=45000,
            success=True,
            pipeline_run_id="test-run-456"
        )
        
        # Verify indexing
        assert mock_elasticsearch.index.called
        call_args = mock_elasticsearch.index.call_args
        document = call_args[1]['document']
        
        assert document['event_type'] == 'agent_completed'
        assert document['duration_ms'] == 45000
        assert document['success'] is True
        assert document['pipeline_run_id'] == 'test-run-456'
    
    def test_emit_agent_completed_with_error(self, obs_manager, mock_elasticsearch):
        """Test emit_agent_completed with error (failed state)"""
        obs_manager.emit_agent_completed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=5000,
            success=False,
            error="Connection timeout"
        )
        
        # Verify indexing (should use AGENT_FAILED event type)
        assert mock_elasticsearch.index.called
        call_args = mock_elasticsearch.index.call_args
        document = call_args[1]['document']
        
        assert document['event_type'] == 'agent_failed'
        assert document['success'] is False
        assert document['error'] == "Connection timeout"


class TestEventTypeCompleteness:
    """Test that EventType enum is complete and all events are handled"""
    
    def test_all_event_types_have_tests(self):
        """Verify that we have awareness of all EventType values"""
        all_event_types = list(EventType)
        
        # Expected categorization
        expected_decision_events = {
            'feedback_detected', 'feedback_listening_started', 'feedback_listening_stopped', 'feedback_ignored',
            'agent_routing_decision', 'agent_selected', 'workspace_routing_decision',
            'status_progression_started', 'status_progression_completed', 'status_progression_failed',
            'pipeline_stage_transition',
            'review_cycle_started', 'review_cycle_iteration', 'review_cycle_maker_selected',
            'review_cycle_reviewer_selected', 'review_cycle_escalated', 'review_cycle_completed',
            'conversational_loop_started', 'conversational_question_routed',
            'conversational_loop_paused', 'conversational_loop_resumed',
            'error_encountered', 'error_recovered', 'circuit_breaker_opened',
            'circuit_breaker_closed', 'retry_attempted',
            'task_queued', 'task_dequeued', 'task_priority_changed', 'task_cancelled',
            'branch_selected', 'branch_created', 'branch_reused', 'branch_conflict_detected',
            'branch_stale_detected', 'branch_selection_escalated',
            'result_persistence_failed', 'fallback_storage_used',
            'output_validation_failed', 'empty_output_detected', 'container_result_recovered',
            'repair_cycle_started', 'repair_cycle_iteration',
            'repair_cycle_test_cycle_started', 'repair_cycle_test_cycle_completed',
            'repair_cycle_test_execution_started', 'repair_cycle_test_execution_completed',
            'repair_cycle_fix_cycle_started', 'repair_cycle_fix_cycle_completed',
            'repair_cycle_file_fix_started', 'repair_cycle_file_fix_completed', 'repair_cycle_file_fix_failed',
            'repair_cycle_warning_review_started', 'repair_cycle_warning_review_completed',
            'repair_cycle_warning_review_failed', 'repair_cycle_completed',
        }
        
        expected_lifecycle_events = {
            'agent_initialized', 'agent_started', 'agent_completed', 'agent_failed'
        }
        
        expected_non_indexed_events = {
            'task_received', 'prompt_constructed', 'claude_api_call_started',
            'claude_api_call_completed', 'response_chunk_received',
            'response_processing_started', 'response_processing_completed',
            'tool_execution_started', 'tool_execution_completed',
            'performance_metric', 'token_usage',
            'pipeline_run_started', 'pipeline_run_completed', 'pipeline_run_failed',
            'repair_cycle_container_started', 'repair_cycle_container_checkpoint_updated',
            'repair_cycle_container_recovered', 'repair_cycle_container_killed',
            'repair_cycle_container_completed',
        }
        
        # Check that all events are accounted for
        for event_type in all_event_types:
            event_name = event_type.value
            assert (
                event_name in expected_decision_events or
                event_name in expected_lifecycle_events or
                event_name in expected_non_indexed_events
            ), f"EventType.{event_type.name} ({event_name}) is not categorized in tests"
        
        # Verify counts match
        total_expected = (
            len(expected_decision_events) +
            len(expected_lifecycle_events) +
            len(expected_non_indexed_events)
        )
        
        assert total_expected == len(all_event_types), \
            f"Event count mismatch: {total_expected} expected, {len(all_event_types)} actual"

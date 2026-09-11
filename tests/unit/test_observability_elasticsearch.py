"""
Unit tests for ObservabilityManager Elasticsearch indexing

Tests that all event types are properly categorized and indexed to Elasticsearch.
This ensures that decision events and agent lifecycle events appear in the pipeline view.
"""

import json
import logging

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
        # llen must return a real int: _es_write()'s backup-buffer path does
        # `if self.redis.llen(...) >= self._ES_BACKUP_MAX`, and on a bare Mock
        # that comparison is a TypeError which the enclosing `except Exception`
        # swallows -- so the buffer silently never receives anything and any
        # test asserting on it is testing the fixture, not the code.
        mock.llen = Mock(return_value=0)
        mock.lpush = Mock()
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
            EventType.REPAIR_CYCLE_FAILED,
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
        # Events that are neither decision events nor agent lifecycle events
        non_indexed_events = [
            EventType.RESPONSE_CHUNK_RECEIVED,
            EventType.RESPONSE_PROCESSING_STARTED,
            EventType.RESPONSE_PROCESSING_COMPLETED,
            EventType.TOOL_EXECUTION_STARTED,
            EventType.TOOL_EXECUTION_COMPLETED,
        ]

        for event_type in non_indexed_events:
            assert not obs_manager._is_decision_event(event_type), \
                f"{event_type.value} should not be a decision event"
            assert not obs_manager._is_agent_lifecycle_event(event_type), \
                f"{event_type.value} should not be an agent lifecycle event"

    def test_task_received_and_claude_api_are_lifecycle_events(self, obs_manager):
        """Test that task_received and claude_api events are indexed as agent lifecycle"""
        lifecycle_events = [
            EventType.TASK_RECEIVED,
            EventType.CLAUDE_API_CALL_STARTED,
            EventType.CLAUDE_API_CALL_COMPLETED,
            EventType.CLAUDE_API_CALL_FAILED,
        ]

        for event_type in lifecycle_events:
            assert obs_manager._is_agent_lifecycle_event(event_type), \
                f"{event_type.value} should be an agent lifecycle event"

    # test_all_event_types_are_categorized used to live here: a SECOND
    # hand-maintained mirror of the same categorization, 750 lines above
    # TestEventTypeCompleteness.test_all_event_types_have_tests. Deleted rather
    # than repaired, because it was strictly subsumed by that one and was
    # actively teaching the wrong answer.
    #
    # Its non_indexed_events list still contained every error that test's
    # docstring describes as drift -- TASK_RECEIVED, PROMPT_CONSTRUCTED, the
    # CLAUDE_API_CALL_*/CONTAINER_* eleven, PERFORMANCE_METRIC, TOKEN_USAGE,
    # PIPELINE_RUN_* and REPAIR_CYCLE_CONTAINER_* -- all of which the code
    # actually indexes. It never failed because its only assertion was
    # `is_decision or is_lifecycle or is_non_indexed`, and everything on that
    # wrong list satisfies one of the first two disjuncts. So the list was
    # unfalsifiable, and it directly contradicted
    # test_task_received_and_claude_api_are_lifecycle_events twelve lines above.
    # It was also the copy a contributor adding an EventType was most likely to
    # find first. (Its other assertion, `len(indexed_events) > 35`, was slack by
    # a factor of three against the real 105.)

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
            EventType.RESPONSE_CHUNK_RECEIVED,
            agent="test_agent",
            task_id="test_task",
            project="test-project",
            data={'chunk_size': 100}
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

        # es_index_with_retry() really sleeps 2+4+8+16s between its five
        # attempts, so without this the test spends 30 REAL seconds proving
        # something that has nothing to do with the wait -- it was the single
        # slowest test in the unit suite. The retry count is still exercised;
        # only the waiting is skipped.
        with patch('monitoring.observability.time.sleep') as mock_sleep:
            # Should not raise exception
            obs_manager.emit(
                EventType.AGENT_ROUTING_DECISION,
                agent="orchestrator",
                task_id="test_task",
                project="test-project",
                data={'decision_category': 'routing'}
            )

        # All five attempts were made, with a backoff between each.
        assert mock_elasticsearch.index.call_count == 5
        assert [c.args[0] for c in mock_sleep.call_args_list] == [2, 4, 8, 16]

        # Redis should still work
        assert mock_redis.publish.called
        assert mock_redis.xadd.called

        # ...and the document is not lost. _es_write() falls through to the
        # Redis backup buffer that drain_es_backup() replays once ES is healthy
        # again -- the whole durability story for an ES outage, and until this
        # line nothing in the suite touched it (no test referenced
        # _ES_BACKUP_KEY, es:failed_events or drain_es_backup anywhere). This
        # test was already driving the retry to exhaustion, so asserting it is
        # free.
        mock_redis.lpush.assert_called_once()
        backup_key, backup_payload = mock_redis.lpush.call_args.args
        assert backup_key == ObservabilityManager._ES_BACKUP_KEY
        buffered = json.loads(backup_payload)
        assert buffered['index'].startswith('decision-events-')
        assert buffered['document']['event_type'] == EventType.AGENT_ROUTING_DECISION.value
    
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

    # ========== SERIALISATION OF THE OPEN `data` DICT ==========

    def test_a_non_json_native_value_in_data_is_stringified_not_raised(
        self, obs_manager, mock_redis, mock_elasticsearch
    ):
        """`data` is an open dict filled by ~90 emit sites out of task context,
        agent config and pipeline state, and emit() serialises it INLINE on the
        dispatch path. Before `default=str`, a Path/datetime/Enum/dataclass in
        there raised TypeError straight out of execute_agent() -- a telemetry
        payload killing the agent run it was describing.

        Pinned per type rather than as one blob, because `default=str` is only
        consulted for values json does not already handle: a regression that
        narrowed it (say, to a `str` subclass check) would still pass a
        single-case test.
        """
        from pathlib import Path
        from dataclasses import dataclass

        @dataclass
        class _SomeConfig:
            name: str

        obs_manager.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id="test_task",
            project="test-project",
            data={
                'workspace': Path('/workspace/proj'),
                'when': datetime(2026, 1, 2, 3, 4, 5),
                'which': EventType.AGENT_SELECTED,
                'config': _SomeConfig(name='x'),
                'mock': MagicMock(),
            },
        )

        # Emitted rather than dropped, and the values survive as strings.
        assert mock_redis.publish.called
        published = json.loads(mock_redis.publish.call_args.args[1])
        assert published['data']['workspace'] == '/workspace/proj'
        assert published['data']['when'] == '2026-01-02 03:04:05'
        assert published['data']['which'] == str(EventType.AGENT_SELECTED)
        assert isinstance(published['data']['mock'], str)
        # A nested dataclass is handled by asdict() before json.dumps ever sees
        # it, so it survives as structure rather than being stringified.
        assert published['data']['config'] == {'name': 'x'}

        # ...and the event still reaches Elasticsearch, carrying the SAME
        # normalized values, on the FIRST attempt.
        #
        # The second half of that is the assertion that matters. The ES
        # document used to be re-assembled from the raw `data` dict rather than
        # from the normalized envelope, so a Path here reached es.index()
        # untouched, the client raised SerializationError, and
        # es_index_with_retry -- which cannot tell permanent from transient --
        # burned five attempts and 30 real seconds of time.sleep() on the
        # dispatch path before dropping it. call_count == 1 is what pins that
        # shut; a plain `.called` would pass with all five.
        assert mock_elasticsearch.index.call_count == 1
        indexed = mock_elasticsearch.index.call_args.kwargs['document']
        assert indexed['workspace'] == '/workspace/proj'
        assert indexed['when'] == '2026-01-02 03:04:05'
        assert indexed['config'] == {'name': 'x'}
        assert isinstance(indexed['mock'], str)
        # Redis and Elasticsearch cannot disagree about what the event was.
        for key, value in published['data'].items():
            assert indexed[key] == value

    @pytest.mark.parametrize('label', [
        'circular_dict',      # RecursionError
        'unpicklable_object', # TypeError  ("cannot pickle '_thread.lock' object")
        'str_raises',         # that object's own exception (RuntimeError here)
    ])
    def test_an_unserialisable_event_is_dropped_and_logged_not_raised(
        self, obs_manager, mock_redis, mock_elasticsearch, caplog, label
    ):
        """`default=str` does NOT make to_json() total, and the exceptions that
        get past it are not one family.

        to_json() is json.dumps(asdict(self), default=str), and it is asdict()
        -- which runs first and deep-copies every value in `data` -- that
        raises, so `default=str` never gets a say. Measured against this
        dataclass: a cycle is a RecursionError, a lock/socket/generator is a
        TypeError from the pickle machinery, and an object whose __str__ or
        __deepcopy__ raises propagates its own exception type. Only the middle
        one is a TypeError, which is why the guard catches Exception rather than
        a tuple -- the set of types reachable from ~90 emit sites' open `data`
        dicts is not enumerable in advance.

        The deliberate trade being pinned here is that the event is DROPPED --
        losing one telemetry row beats aborting the dispatch it describes -- and
        that the drop is loud (ERROR, naming the event type) rather than silent.
        """
        import threading

        if label == 'circular_dict':
            value = {}
            value['self'] = value
        elif label == 'unpicklable_object':
            value = threading.Lock()
        else:
            class _StrRaises:
                def __str__(self): raise RuntimeError('boom')
                def __repr__(self): raise RuntimeError('boom')
            value = _StrRaises()

        with caplog.at_level(logging.ERROR, logger='monitoring.observability'):
            obs_manager.emit(
                EventType.AGENT_ROUTING_DECISION,
                agent="orchestrator",
                task_id="test_task",
                project="test-project",
                data={'bad': value},
            )

        # Dropped: neither transport saw it.
        assert not mock_redis.publish.called
        assert not mock_redis.xadd.called
        assert not mock_elasticsearch.index.called

        # ...but loudly, and identifiably.
        assert any(
            record.levelno == logging.ERROR
            and EventType.AGENT_ROUTING_DECISION.value in record.getMessage()
            for record in caplog.records
        ), f"expected an ERROR naming the event type, got {caplog.records}"

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
        
        # Another lifecycle event (task_received is now indexed as lifecycle)
        obs_manager.emit(
            EventType.TASK_RECEIVED,
            agent="test_agent",
            task_id="task_1",
            project="test-project",
            data={'context_keys': []}
        )

        # Should have 3 index calls (1 decision + 2 lifecycle)
        assert mock_elasticsearch.index.call_count == 3

        # Get the indices
        indices = [call_item[1]['index'] for call_item in mock_elasticsearch.index.call_args_list]

        # Should have one decision-events and two agent-events
        decision_indices = [idx for idx in indices if idx.startswith('decision-events-')]
        agent_indices = [idx for idx in indices if idx.startswith('agent-events-')]

        assert len(decision_indices) == 1
        assert len(agent_indices) == 2
    
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

    # ========== NEW CONTAINER LIFECYCLE EVENT TESTS ==========

    def test_emit_container_launch_started(self, obs_manager, mock_redis):
        """Test emit_container_launch_started helper method"""
        obs_manager.emit_container_launch_started(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            container_name="claude-agent-test-123",
            image="test-project-agent:latest"
        )

        # Verify Redis publish (container events not indexed to ES)
        assert mock_redis.publish.called

        # Verify event data in Redis
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'container_launch_started'
        assert event['agent'] == 'software_engineer'
        assert event['data']['container_name'] == 'claude-agent-test-123'
        assert event['data']['image'] == 'test-project-agent:latest'

    def test_emit_container_launch_succeeded(self, obs_manager, mock_redis):
        """Test emit_container_launch_succeeded helper method"""
        obs_manager.emit_container_launch_succeeded(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            container_name="claude-agent-test-123",
            container_id="abc123def456"
        )

        # Verify Redis publish
        assert mock_redis.publish.called

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'container_launch_succeeded'
        assert event['data']['container_name'] == 'claude-agent-test-123'
        assert event['data']['container_id'] == 'abc123def456'

    def test_emit_container_launch_failed(self, obs_manager, mock_redis):
        """Test emit_container_launch_failed helper method"""
        obs_manager.emit_container_launch_failed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            container_name="claude-agent-test-123",
            error="Image not found: test-project-agent:latest"
        )

        # Verify Redis publish
        assert mock_redis.publish.called

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'container_launch_failed'
        assert event['data']['container_name'] == 'claude-agent-test-123'
        assert event['data']['error'] == 'Image not found: test-project-agent:latest'

    def test_emit_container_execution_completed(self, obs_manager, mock_redis):
        """Test emit_container_execution_completed helper method"""
        obs_manager.emit_container_execution_completed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            container_name="claude-agent-test-123",
            exit_code=0,
            duration_ms=45000.5
        )

        # Verify Redis publish
        assert mock_redis.publish.called

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'container_execution_completed'
        assert event['data']['container_name'] == 'claude-agent-test-123'
        assert event['data']['exit_code'] == 0
        assert event['data']['duration_ms'] == 45000.5
        assert event['data']['success'] is True  # exit_code 0 = success

    def test_emit_container_execution_failed(self, obs_manager, mock_redis):
        """Test emit_container_execution_failed helper method"""
        obs_manager.emit_container_execution_failed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            container_name="claude-agent-test-123",
            exit_code=1,
            error="Command failed: npm test",
            duration_ms=5000.0
        )

        # Verify Redis publish
        assert mock_redis.publish.called

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'container_execution_failed'
        assert event['data']['container_name'] == 'claude-agent-test-123'
        assert event['data']['exit_code'] == 1
        assert event['data']['error'] == 'Command failed: npm test'
        assert event['data']['duration_ms'] == 5000.0

    def test_emit_claude_call_completed_with_success_parameter(self, obs_manager, mock_redis):
        """Test emit_claude_call_completed with success parameter"""
        # Test successful call
        obs_manager.emit_claude_call_completed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=30000,
            input_tokens=1000,
            output_tokens=2000,
            success=True
        )

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'claude_api_call_completed'
        assert event['data']['duration_ms'] == 30000
        assert event['data']['input_tokens'] == 1000
        assert event['data']['output_tokens'] == 2000
        assert event['data']['total_tokens'] == 3000
        assert event['data']['success'] is True

    def test_emit_claude_call_completed_with_failure(self, obs_manager, mock_redis):
        """Test emit_claude_call_completed with success=False"""
        obs_manager.emit_claude_call_completed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=5000,
            input_tokens=500,
            output_tokens=0,
            success=False
        )

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'claude_api_call_completed'
        assert event['data']['success'] is False
        assert event['data']['output_tokens'] == 0

    def test_emit_claude_call_completed_defaults_to_success(self, obs_manager, mock_redis):
        """Test emit_claude_call_completed defaults success to True"""
        # Call without success parameter
        obs_manager.emit_claude_call_completed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=30000,
            input_tokens=1000,
            output_tokens=2000
        )

        # Verify success defaults to True
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['data']['success'] is True

    def test_emit_claude_call_failed(self, obs_manager, mock_redis):
        """Test emit_claude_call_failed helper method"""
        obs_manager.emit_claude_call_failed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=2000,
            error="Rate limit exceeded",
            exit_code=1
        )

        # Verify Redis publish
        assert mock_redis.publish.called

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'claude_api_call_failed'
        assert event['data']['duration_ms'] == 2000
        assert event['data']['error'] == 'Rate limit exceeded'
        assert event['data']['exit_code'] == 1

    def test_emit_claude_call_failed_without_exit_code(self, obs_manager, mock_redis):
        """Test emit_claude_call_failed without exit_code (optional parameter)"""
        obs_manager.emit_claude_call_failed(
            agent="software_engineer",
            task_id="test_task",
            project="test-project",
            duration_ms=1500,
            error="Connection timeout"
        )

        # Verify event data
        call_args = mock_redis.publish.call_args
        event_json = call_args[0][1]

        import json
        event = json.loads(event_json)
        assert event['event_type'] == 'claude_api_call_failed'
        assert event['data']['error'] == 'Connection timeout'
        assert event['data']['exit_code'] is None


class TestEventTypeCompleteness:
    """Test that EventType enum is complete and all events are handled"""
    
    def test_all_event_types_have_tests(self):
        """Every EventType must be consciously categorized, and the
        categorization here must match what the code actually does.

        The three sets below are a mirror of monitoring/observability.py's
        _is_decision_event() and _is_agent_lifecycle_event(). That is the
        point: adding an EventType fails this test until someone states which
        bucket it belongs in, which is the speed bump this file exists to
        provide. What it did NOT do before was check the mirror against the
        original, and the mirror had drifted badly -- it was missing
        pipeline_run_active_no_container_detected entirely (the failure that
        prompted this rewrite), had performance_metric and token_usage filed
        as non-indexed when the code indexes both (decision-events-* on the
        live cluster carries rows of each), and listed all eleven of
        task_received/prompt_constructed/claude_api_call_*/container_* as
        non-indexed when the code treats them as agent-lifecycle events --
        contradicting test_task_received_and_claude_api_are_lifecycle_events
        in this same file.
        """
        all_event_types = list(EventType)

        expected_decision_events = {
            'agent_output_format_unexpected', 'agent_routing_decision',
            'agent_selected', 'branch_conflict_detected',
            'branch_created', 'branch_reused', 'branch_selected',
            'branch_selection_escalated', 'branch_stale_detected',
            'circuit_breaker_closed', 'circuit_breaker_opened',
            'container_result_recovered', 'conversational_loop_paused',
            'conversational_loop_resumed',
            'conversational_loop_started',
            'conversational_question_routed', 'empty_output_detected',
            'error_encountered', 'error_recovered',
            'execution_state_reconciled', 'fallback_storage_used',
            'feedback_detected', 'feedback_ignored',
            'feedback_listening_started', 'feedback_listening_stopped',
            'github_comment_posted', 'output_validation_failed',
            'performance_metric',
            'pipeline_run_active_no_container_detected',
            'pipeline_run_completed', 'pipeline_run_failed',
            'pipeline_run_started', 'pipeline_stage_transition',
            'pr_review_outcome_tracking', 'pr_review_phase_completed',
            'pr_review_phase_failed', 'pr_review_phase_started',
            'pr_review_stage_completed', 'pr_review_stage_started',
            'prompt_size_warning', 'repair_cycle_completed',
            'repair_cycle_container_checkpoint_updated',
            'repair_cycle_container_completed',
            'repair_cycle_container_killed',
            'repair_cycle_container_recovered',
            'repair_cycle_container_started',
            'repair_cycle_env_rebuild_completed',
            'repair_cycle_env_rebuild_started', 'repair_cycle_failed',
            'repair_cycle_file_fix_completed',
            'repair_cycle_file_fix_failed',
            'repair_cycle_file_fix_started',
            'repair_cycle_fix_cycle_completed',
            'repair_cycle_fix_cycle_started', 'repair_cycle_iteration',
            'repair_cycle_started',
            'repair_cycle_systemic_analysis_completed',
            'repair_cycle_systemic_analysis_started',
            'repair_cycle_systemic_fix_completed',
            'repair_cycle_systemic_fix_started',
            'repair_cycle_test_cycle_completed',
            'repair_cycle_test_cycle_started',
            'repair_cycle_test_execution_completed',
            'repair_cycle_test_execution_started',
            'repair_cycle_warning_review_completed',
            'repair_cycle_warning_review_failed',
            'repair_cycle_warning_review_started',
            'result_persistence_failed', 'retry_attempted',
            'review_cycle_completed', 'review_cycle_escalated',
            'review_cycle_iteration', 'review_cycle_maker_selected',
            'review_cycle_reviewer_selected', 'review_cycle_started',
            'status_progression_completed', 'status_progression_failed',
            'status_progression_started', 'status_validation_failure',
            'sub_issue_created', 'sub_issue_creation_failed',
            'task_cancelled', 'task_dequeued', 'task_priority_changed',
            'task_queued', 'token_usage', 'workspace_routing_decision',
            'worktree_branch_drift_detected',
            'worktree_branch_drift_repaired',
            'worktree_branch_drift_unchecked'
        }

        expected_lifecycle_events = {
            'agent_completed', 'agent_failed', 'agent_initialized',
            'agent_started', 'claude_api_call_completed',
            'claude_api_call_failed', 'claude_api_call_started',
            'container_execution_completed',
            'container_execution_failed', 'container_execution_started',
            'container_launch_failed', 'container_launch_started',
            'container_launch_succeeded', 'prompt_constructed',
            'task_received'
        }

        expected_non_indexed_events = {
            'response_chunk_received', 'response_processing_completed',
            'response_processing_started', 'tool_execution_completed',
            'tool_execution_started'
        }

        # 1. Nothing falls through the three buckets.
        for event_type in all_event_types:
            event_name = event_type.value
            assert (
                event_name in expected_decision_events or
                event_name in expected_lifecycle_events or
                event_name in expected_non_indexed_events
            ), f"EventType.{event_type.name} ({event_name}) is not categorized in tests"

        # 2. Nothing is listed twice, and nothing is listed that no longer exists.
        total_expected = (
            len(expected_decision_events) +
            len(expected_lifecycle_events) +
            len(expected_non_indexed_events)
        )
        assert total_expected == len(all_event_types), \
            f"Event count mismatch: {total_expected} expected, {len(all_event_types)} actual"

        # 3. The mirror agrees with the original. Without this the sets above
        #    are unfalsifiable: an event can sit in the wrong one forever and
        #    the two assertions above still pass.
        #
        #    Both predicates ignore self, so they are called unbound rather
        #    than standing up an ObservabilityManager (which connects to Redis
        #    and Elasticsearch).
        for event_type in all_event_types:
            name = event_type.value
            assert ObservabilityManager._is_decision_event(None, event_type) == (
                name in expected_decision_events
            ), (f"EventType.{event_type.name} ({name}): _is_decision_event() and this "
                f"test's categorization disagree. Decide which is right, don't just move it.")
            assert ObservabilityManager._is_agent_lifecycle_event(None, event_type) == (
                name in expected_lifecycle_events
            ), (f"EventType.{event_type.name} ({name}): _is_agent_lifecycle_event() and "
                f"this test's categorization disagree.")

"""
Observability Manager for real-time agent monitoring
Provides event streaming for web UI observation of agent execution
"""

import json
import redis
import logging
import uuid
from typing import Dict, Any, Optional
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, asdict
from elasticsearch import Elasticsearch

logger = logging.getLogger(__name__)

class EventType(Enum):
    """Types of observability events"""
    # Lifecycle events
    TASK_RECEIVED = "task_received"
    AGENT_INITIALIZED = "agent_initialized"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"

    # Prompt events
    PROMPT_CONSTRUCTED = "prompt_constructed"
    CLAUDE_API_CALL_STARTED = "claude_api_call_started"
    CLAUDE_API_CALL_COMPLETED = "claude_api_call_completed"

    # Response events
    RESPONSE_CHUNK_RECEIVED = "response_chunk_received"
    RESPONSE_PROCESSING_STARTED = "response_processing_started"
    RESPONSE_PROCESSING_COMPLETED = "response_processing_completed"

    # Tool events
    TOOL_EXECUTION_STARTED = "tool_execution_started"
    TOOL_EXECUTION_COMPLETED = "tool_execution_completed"

    # Performance events
    PERFORMANCE_METRIC = "performance_metric"
    TOKEN_USAGE = "token_usage"

    # ========== ORCHESTRATOR DECISION EVENTS ==========
    
    # Feedback Monitoring
    FEEDBACK_DETECTED = "feedback_detected"
    FEEDBACK_LISTENING_STARTED = "feedback_listening_started"
    FEEDBACK_LISTENING_STOPPED = "feedback_listening_stopped"
    FEEDBACK_IGNORED = "feedback_ignored"
    
    # Agent Routing & Selection
    AGENT_ROUTING_DECISION = "agent_routing_decision"
    AGENT_SELECTED = "agent_selected"
    WORKSPACE_ROUTING_DECISION = "workspace_routing_decision"
    
    # Status & Pipeline Progression
    STATUS_PROGRESSION_STARTED = "status_progression_started"
    STATUS_PROGRESSION_COMPLETED = "status_progression_completed"
    STATUS_PROGRESSION_FAILED = "status_progression_failed"
    PIPELINE_STAGE_TRANSITION = "pipeline_stage_transition"
    
    # Review Cycle Management
    REVIEW_CYCLE_STARTED = "review_cycle_started"
    REVIEW_CYCLE_ITERATION = "review_cycle_iteration"
    REVIEW_CYCLE_MAKER_SELECTED = "review_cycle_maker_selected"
    REVIEW_CYCLE_REVIEWER_SELECTED = "review_cycle_reviewer_selected"
    REVIEW_CYCLE_ESCALATED = "review_cycle_escalated"
    REVIEW_CYCLE_COMPLETED = "review_cycle_completed"
    
    # Conversational Loop Routing
    CONVERSATIONAL_LOOP_STARTED = "conversational_loop_started"
    CONVERSATIONAL_QUESTION_ROUTED = "conversational_question_routed"
    CONVERSATIONAL_LOOP_PAUSED = "conversational_loop_paused"
    CONVERSATIONAL_LOOP_RESUMED = "conversational_loop_resumed"
    
    # Error Handling & Circuit Breakers
    ERROR_ENCOUNTERED = "error_encountered"
    ERROR_RECOVERED = "error_recovered"
    CIRCUIT_BREAKER_OPENED = "circuit_breaker_opened"
    CIRCUIT_BREAKER_CLOSED = "circuit_breaker_closed"
    RETRY_ATTEMPTED = "retry_attempted"
    
    # Task Queue Management
    TASK_QUEUED = "task_queued"
    TASK_DEQUEUED = "task_dequeued"
    TASK_PRIORITY_CHANGED = "task_priority_changed"
    TASK_CANCELLED = "task_cancelled"

@dataclass
class ObservabilityEvent:
    """Structured event for agent observability"""
    timestamp: str
    event_id: str
    event_type: str
    agent: str
    task_id: str
    project: str
    data: Dict[str, Any]

    def to_json(self) -> str:
        """Serialize to JSON for Redis pub/sub"""
        return json.dumps(asdict(self))

    @classmethod
    def from_json(cls, json_str: str) -> 'ObservabilityEvent':
        """Deserialize from JSON"""
        return cls(**json.loads(json_str))

class ObservabilityManager:
    """Manages event emission for agent observability"""

    def __init__(self, redis_client: redis.Redis = None, elasticsearch_client: Elasticsearch = None, enabled: bool = True):
        """Initialize observability manager"""
        self.enabled = enabled
        self.es = elasticsearch_client

        if not enabled:
            logger.info("Observability disabled")
            return

        if redis_client:
            self.redis = redis_client
        else:
            # Create default Redis connection
            try:
                self.redis = redis.Redis(
                    host='redis',
                    port=6379,
                    decode_responses=True,
                    socket_connect_timeout=5
                )
                # Test connection
                self.redis.ping()
                logger.info("Observability manager connected to Redis")
            except Exception as e:
                logger.error(f"Failed to connect to Redis for observability: {e}")
                self.enabled = False
        
        # Connect to Elasticsearch if not provided
        if not self.es:
            try:
                self.es = Elasticsearch(['http://elasticsearch:9200'])
                logger.info("Observability manager connected to Elasticsearch")
            except Exception as e:
                logger.warning(f"Failed to connect to Elasticsearch for observability: {e}")
                self.es = None

        # Channel for all agent events (pub/sub)
        self.channel = "orchestrator:agent_events"

        # Stream for event history (with TTL)
        self.stream_key = "orchestrator:event_stream"
        self.stream_maxlen = 1000  # Keep last 1000 events
        self.stream_ttl = 7200  # 2 hours in seconds
    
    def _is_decision_event(self, event_type: EventType) -> bool:
        """Check if an event type is a decision event that should be indexed in Elasticsearch"""
        decision_events = {
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
        }
        return event_type in decision_events

    def emit(self, event_type: EventType, agent: str, task_id: str,
             project: str, data: Dict[str, Any], pipeline_run_id: Optional[str] = None):
        """Emit an observability event"""
        if not self.enabled:
            return

        try:
            # Add pipeline_run_id to data if provided
            if pipeline_run_id:
                data['pipeline_run_id'] = pipeline_run_id

            event = ObservabilityEvent(
                timestamp=datetime.utcnow().isoformat() + 'Z',
                event_id=str(uuid.uuid4()),
                event_type=event_type.value,
                agent=agent,
                task_id=task_id,
                project=project,
                data=data
            )

            event_json = event.to_json()

            # Publish to Redis pub/sub for real-time delivery
            self.redis.publish(self.channel, event_json)

            # Also add to Redis Stream for history (with automatic trimming)
            self.redis.xadd(
                self.stream_key,
                {'event': event_json},
                maxlen=self.stream_maxlen,
                approximate=True  # More efficient trimming
            )

            # Set TTL on the stream key (refreshes on each write)
            self.redis.expire(self.stream_key, self.stream_ttl)
            
            # Index decision events in Elasticsearch
            if self.es and self._is_decision_event(event_type):
                try:
                    # Create index name based on current date
                    index_name = f"decision-events-{datetime.utcnow().strftime('%Y-%m-%d')}"
                    
                    # Prepare document for Elasticsearch
                    doc = {
                        'timestamp': event.timestamp,
                        'event_type': event.event_type,
                        'event_category': 'decision',  # Category for filtering
                        'agent': agent,
                        'task_id': task_id,
                        'project': project,
                        'pipeline_run_id': pipeline_run_id,
                        **data  # Flatten data into document
                    }
                    
                    # Index the document
                    self.es.index(index=index_name, document=doc)
                    logger.info(f"Indexed decision event {event_type.value} to {index_name}")
                except Exception as e:
                    logger.error(f"Failed to index decision event to Elasticsearch: {e}")
            elif self._is_decision_event(event_type):
                logger.warning(f"Decision event {event_type.value} not indexed - ES client is None")

            logger.debug(f"Emitted {event_type.value} event for {agent}/{task_id}")

        except Exception as e:
            logger.error(f"Failed to emit observability event: {e}")

    def emit_task_received(self, agent: str, task_id: str, project: str,
                          context: Dict[str, Any], pipeline_run_id: Optional[str] = None):
        """Emit task received event"""
        self.emit(EventType.TASK_RECEIVED, agent, task_id, project, {
            'context_keys': list(context.keys()),
            'issue_number': context.get('issue_number'),
            'board': context.get('board'),
            'trigger': context.get('trigger')
        }, pipeline_run_id=pipeline_run_id)

    def emit_agent_initialized(self, agent: str, task_id: str, project: str,
                              config: Dict[str, Any], branch_name: Optional[str] = None,
                              container_name: Optional[str] = None):
        """Emit agent initialized event"""
        data = {
            'model': config.get('model'),
            'timeout': config.get('timeout'),
            'tools_enabled': config.get('tools_enabled'),
            'mcp_servers': config.get('mcp_servers')
        }
        if branch_name:
            data['branch_name'] = branch_name
        if container_name:
            data['container_name'] = container_name

        self.emit(EventType.AGENT_INITIALIZED, agent, task_id, project, data)

    def emit_prompt_constructed(self, agent: str, task_id: str, project: str,
                               prompt: str, estimated_tokens: Optional[int] = None):
        """Emit prompt constructed event"""
        # Truncate very long prompts for the event
        prompt_preview = prompt[:1000] + "..." if len(prompt) > 1000 else prompt

        self.emit(EventType.PROMPT_CONSTRUCTED, agent, task_id, project, {
            'prompt': prompt,
            'prompt_preview': prompt_preview,
            'prompt_length': len(prompt),
            'estimated_tokens': estimated_tokens
        })

    def emit_claude_call_started(self, agent: str, task_id: str, project: str,
                                 model: str, input_tokens: Optional[int] = None):
        """Emit Claude API call started event"""
        self.emit(EventType.CLAUDE_API_CALL_STARTED, agent, task_id, project, {
            'model': model,
            'input_tokens': input_tokens,
            'start_time': datetime.utcnow().isoformat() + 'Z'
        })

    def emit_claude_call_completed(self, agent: str, task_id: str, project: str,
                                   duration_ms: float, input_tokens: int,
                                   output_tokens: int):
        """Emit Claude API call completed event"""
        self.emit(EventType.CLAUDE_API_CALL_COMPLETED, agent, task_id, project, {
            'duration_ms': duration_ms,
            'input_tokens': input_tokens,
            'output_tokens': output_tokens,
            'total_tokens': input_tokens + output_tokens
        })

    def emit_response_chunk(self, agent: str, task_id: str, project: str,
                           chunk: str, chunk_index: int):
        """Emit response chunk received event (for streaming)"""
        self.emit(EventType.RESPONSE_CHUNK_RECEIVED, agent, task_id, project, {
            'chunk': chunk,
            'chunk_index': chunk_index,
            'chunk_length': len(chunk)
        })

    def emit_tool_execution(self, agent: str, task_id: str, project: str,
                           tool_name: str, started: bool, duration_ms: Optional[float] = None,
                           result_summary: Optional[str] = None):
        """Emit tool execution event"""
        event_type = EventType.TOOL_EXECUTION_STARTED if started else EventType.TOOL_EXECUTION_COMPLETED

        data = {
            'tool_name': tool_name,
            'timestamp': datetime.utcnow().isoformat() + 'Z'
        }

        if not started:
            data['duration_ms'] = duration_ms
            data['result_summary'] = result_summary

        self.emit(event_type, agent, task_id, project, data)

    def emit_performance_metric(self, agent: str, task_id: str, project: str,
                               metric_name: str, value: float, unit: str):
        """Emit performance metric"""
        self.emit(EventType.PERFORMANCE_METRIC, agent, task_id, project, {
            'metric_name': metric_name,
            'value': value,
            'unit': unit
        })

    def emit_agent_completed(self, agent: str, task_id: str, project: str,
                            duration_ms: float, success: bool,
                            error: Optional[str] = None):
        """Emit agent completion event"""
        event_type = EventType.AGENT_COMPLETED if success else EventType.AGENT_FAILED

        self.emit(event_type, agent, task_id, project, {
            'duration_ms': duration_ms,
            'success': success,
            'error': error
        })

# Global observability manager instance
_observability_manager: Optional[ObservabilityManager] = None

def get_observability_manager() -> ObservabilityManager:
    """Get or create global observability manager"""
    global _observability_manager

    if _observability_manager is None:
        _observability_manager = ObservabilityManager(enabled=True)

    return _observability_manager

def disable_observability():
    """Disable observability globally"""
    global _observability_manager
    if _observability_manager:
        _observability_manager.enabled = False
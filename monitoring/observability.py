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
from monitoring.timestamp_utils import utc_now, utc_isoformat

logger = logging.getLogger(__name__)

# ILM Policy for decision events (7-day retention)
DECISION_EVENTS_ILM_POLICY = {
    "policy": {
        "phases": {
            "hot": {
                "min_age": "0ms",
                "actions": {
                    "set_priority": {
                        "priority": 100
                    }
                }
            },
            "warm": {
                "min_age": "3d",
                "actions": {
                    "set_priority": {
                        "priority": 50
                    }
                }
            },
            "delete": {
                "min_age": "7d",
                "actions": {
                    "delete": {
                        "delete_searchable_snapshot": True
                    }
                }
            }
        }
    }
}

# Index template for decision events
DECISION_EVENTS_TEMPLATE = {
    "index_patterns": ["decision-events-*"],
    "template": {
        "settings": {
            "number_of_shards": 1,
            "number_of_replicas": 1,
            "index": {
                "lifecycle": {
                    "name": "decision-events-ilm-policy"
                }
            }
        },
        "mappings": {
            "properties": {
                "timestamp": {"type": "date"},
                "event_type": {"type": "keyword"},
                "event_category": {"type": "keyword"},
                "agent": {"type": "keyword"},
                "task_id": {"type": "keyword"},
                "project": {"type": "keyword"},
                "pipeline_run_id": {"type": "keyword"},
                "decision_category": {"type": "keyword"},
                "selected_agent": {"type": "keyword"},
                "from_status": {"type": "keyword"},
                "to_status": {"type": "keyword"},
                "iteration": {"type": "integer"},
                "feedback_source": {"type": "keyword"}
            }
        }
    },
    "priority": 200
}

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
    PIPELINE_RUN_STARTED = "pipeline_run_started"
    PIPELINE_RUN_COMPLETED = "pipeline_run_completed"
    PIPELINE_RUN_FAILED = "pipeline_run_failed"

    # Review Cycle Management
    REVIEW_CYCLE_STARTED = "review_cycle_started"
    REVIEW_CYCLE_ITERATION = "review_cycle_iteration"
    REVIEW_CYCLE_MAKER_SELECTED = "review_cycle_maker_selected"
    REVIEW_CYCLE_REVIEWER_SELECTED = "review_cycle_reviewer_selected"
    REVIEW_CYCLE_ESCALATED = "review_cycle_escalated"
    REVIEW_CYCLE_COMPLETED = "review_cycle_completed"
    
    # Repair Cycle Management (Test-Fix Cycles)
    REPAIR_CYCLE_STARTED = "repair_cycle_started"
    REPAIR_CYCLE_ITERATION = "repair_cycle_iteration"
    REPAIR_CYCLE_TEST_CYCLE_STARTED = "repair_cycle_test_cycle_started"
    REPAIR_CYCLE_TEST_CYCLE_COMPLETED = "repair_cycle_test_cycle_completed"
    REPAIR_CYCLE_TEST_EXECUTION_STARTED = "repair_cycle_test_execution_started"
    REPAIR_CYCLE_TEST_EXECUTION_COMPLETED = "repair_cycle_test_execution_completed"
    REPAIR_CYCLE_FIX_CYCLE_STARTED = "repair_cycle_fix_cycle_started"
    REPAIR_CYCLE_FIX_CYCLE_COMPLETED = "repair_cycle_fix_cycle_completed"
    REPAIR_CYCLE_FILE_FIX_STARTED = "repair_cycle_file_fix_started"
    REPAIR_CYCLE_FILE_FIX_COMPLETED = "repair_cycle_file_fix_completed"
    REPAIR_CYCLE_FILE_FIX_FAILED = "repair_cycle_file_fix_failed"
    REPAIR_CYCLE_WARNING_REVIEW_STARTED = "repair_cycle_warning_review_started"
    REPAIR_CYCLE_WARNING_REVIEW_COMPLETED = "repair_cycle_warning_review_completed"
    REPAIR_CYCLE_WARNING_REVIEW_FAILED = "repair_cycle_warning_review_failed"
    REPAIR_CYCLE_COMPLETED = "repair_cycle_completed"
    
    # Repair Cycle Container Lifecycle
    REPAIR_CYCLE_CONTAINER_STARTED = "repair_cycle_container_started"
    REPAIR_CYCLE_CONTAINER_CHECKPOINT_UPDATED = "repair_cycle_container_checkpoint_updated"
    REPAIR_CYCLE_CONTAINER_RECOVERED = "repair_cycle_container_recovered"
    REPAIR_CYCLE_CONTAINER_KILLED = "repair_cycle_container_killed"
    REPAIR_CYCLE_CONTAINER_COMPLETED = "repair_cycle_container_completed"
    
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

    # Container Result Persistence & Recovery
    RESULT_PERSISTENCE_FAILED = "result_persistence_failed"
    FALLBACK_STORAGE_USED = "fallback_storage_used"
    OUTPUT_VALIDATION_FAILED = "output_validation_failed"
    EMPTY_OUTPUT_DETECTED = "empty_output_detected"
    CONTAINER_RESULT_RECOVERED = "container_result_recovered"

    # Task Queue Management
    TASK_QUEUED = "task_queued"
    TASK_DEQUEUED = "task_dequeued"
    TASK_PRIORITY_CHANGED = "task_priority_changed"
    TASK_CANCELLED = "task_cancelled"
    
    # Branch Management
    BRANCH_SELECTED = "branch_selected"
    BRANCH_CREATED = "branch_created"
    BRANCH_REUSED = "branch_reused"
    BRANCH_CONFLICT_DETECTED = "branch_conflict_detected"
    BRANCH_STALE_DETECTED = "branch_stale_detected"
    BRANCH_SELECTION_ESCALATED = "branch_selection_escalated"

    # Medic Events (Failure Detection & Investigation)
    MEDIC_SIGNATURE_CREATED = "medic_signature_created"
    MEDIC_SIGNATURE_UPDATED = "medic_signature_updated"
    MEDIC_SIGNATURE_TRENDING = "medic_signature_trending"
    MEDIC_SIGNATURE_RESOLVED = "medic_signature_resolved"
    MEDIC_INVESTIGATION_QUEUED = "medic_investigation_queued"
    MEDIC_INVESTIGATION_STARTED = "medic_investigation_started"
    MEDIC_INVESTIGATION_COMPLETED = "medic_investigation_completed"
    MEDIC_INVESTIGATION_FAILED = "medic_investigation_failed"

    # Claude Medic events
    MEDIC_CLAUDE_SIGNATURE_CREATED = "medic_claude_signature_created"
    MEDIC_CLAUDE_SIGNATURE_UPDATED = "medic_claude_signature_updated"
    MEDIC_CLAUDE_SIGNATURE_TRENDING = "medic_claude_signature_trending"
    MEDIC_CLAUDE_CLUSTER_DETECTED = "medic_claude_cluster_detected"
    MEDIC_CLAUDE_INVESTIGATION_STARTED = "medic_claude_investigation_started"
    MEDIC_CLAUDE_INVESTIGATION_COMPLETED = "medic_claude_investigation_completed"
    MEDIC_CLAUDE_INVESTIGATION_STALLED = "medic_claude_investigation_stalled"

    # Medic Fix events
    MEDIC_FIX_STARTED = "medic_fix_started"
    MEDIC_FIX_COMPLETED = "medic_fix_completed"
    MEDIC_FIX_FAILED = "medic_fix_failed"

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
            # Create default Redis connection from environment
            try:
                import os
                redis_host = os.getenv('REDIS_HOST', 'redis')
                redis_port = int(os.getenv('REDIS_PORT', '6379'))
                
                self.redis = redis.Redis(
                    host=redis_host,
                    port=redis_port,
                    decode_responses=True,
                    socket_connect_timeout=5
                )
                # Test connection
                self.redis.ping()
                logger.info(f"Observability manager connected to Redis at {redis_host}:{redis_port}")
            except Exception as e:
                logger.error(f"Failed to connect to Redis for observability: {e}")
                self.enabled = False
        
        # Connect to Elasticsearch if not provided
        if not self.es:
            try:
                import os
                es_host = os.getenv('ELASTICSEARCH_HOST', 'elasticsearch')
                es_port = os.getenv('ELASTICSEARCH_PORT', '9200')
                es_url = f'http://{es_host}:{es_port}'
                
                self.es = Elasticsearch([es_url])
                logger.info(f"Observability manager connected to Elasticsearch at {es_url}")
            except Exception as e:
                logger.warning(f"Failed to connect to Elasticsearch for observability: {e}")
                self.es = None

        # Channel for all agent events (pub/sub)
        self.channel = "orchestrator:agent_events"

        # Stream for event history (with TTL)
        self.stream_key = "orchestrator:event_stream"
        self.stream_maxlen = 1000  # Keep last 1000 events
        self.stream_ttl = 7200  # 2 hours in seconds

        # Setup Elasticsearch indices on initialization if ES is available
        if self.es:
            self.setup_elasticsearch()

    def setup_elasticsearch(self):
        """Setup Elasticsearch ILM policies and index templates for decision events"""
        if not self.es:
            logger.warning("Elasticsearch not available, skipping setup")
            return False

        try:
            # Create ILM policy for decision events (7-day retention)
            self.es.ilm.put_lifecycle(
                name="decision-events-ilm-policy",
                body=DECISION_EVENTS_ILM_POLICY
            )
            logger.info("Created/updated ILM policy: decision-events-ilm-policy (7-day retention)")

            # Create index template for decision events
            self.es.indices.put_index_template(
                name="decision-events-template",
                body=DECISION_EVENTS_TEMPLATE
            )
            logger.info("Created/updated index template: decision-events-template")

            return True

        except Exception as e:
            logger.error(f"Failed to setup Elasticsearch for decision events: {e}")
            return False

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
            # Repair Cycle Management
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
            # Container Result Persistence & Recovery
            EventType.RESULT_PERSISTENCE_FAILED,
            EventType.FALLBACK_STORAGE_USED,
            EventType.OUTPUT_VALIDATION_FAILED,
            EventType.EMPTY_OUTPUT_DETECTED,
            EventType.CONTAINER_RESULT_RECOVERED,
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
        }
        return event_type in decision_events

    def _is_agent_lifecycle_event(self, event_type: EventType) -> bool:
        """Check if an event type is an agent lifecycle event that should be indexed in Elasticsearch"""
        lifecycle_events = {
            EventType.AGENT_INITIALIZED,
            EventType.AGENT_STARTED,
            EventType.AGENT_COMPLETED,
            EventType.AGENT_FAILED
        }
        return event_type in lifecycle_events

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
                timestamp=utc_isoformat(),
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
                    index_name = f"decision-events-{utc_now().strftime('%Y-%m-%d')}"
                    
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
            
            # Index agent lifecycle events in Elasticsearch
            if self.es and self._is_agent_lifecycle_event(event_type):
                try:
                    # Create index name based on current date
                    index_name = f"agent-events-{utc_now().strftime('%Y-%m-%d')}"
                    
                    # Prepare document for Elasticsearch
                    doc = {
                        'timestamp': event.timestamp,
                        'event_type': event.event_type,
                        'event_category': 'agent_lifecycle',  # Category for filtering
                        'agent': agent,
                        'task_id': task_id,
                        'project': project,
                        'pipeline_run_id': pipeline_run_id,
                        **data  # Flatten data into document
                    }
                    
                    # Index the document
                    self.es.index(index=index_name, document=doc)
                    logger.info(f"Indexed agent lifecycle event {event_type.value} to {index_name}")
                except Exception as e:
                    logger.error(f"Failed to index agent lifecycle event to Elasticsearch: {e}")
            elif self._is_agent_lifecycle_event(event_type):
                logger.warning(f"Agent lifecycle event {event_type.value} not indexed - ES client is None")

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
                              container_name: Optional[str] = None,
                              pipeline_run_id: Optional[str] = None):
        """Emit agent initialized event"""
        # Generate unique agent execution ID for tracking this specific execution
        agent_execution_id = str(uuid.uuid4())
        
        data = {
            'agent_execution_id': agent_execution_id,
            'model': config.get('model'),
            'timeout': config.get('timeout'),
            'tools_enabled': config.get('tools_enabled'),
            'mcp_servers': config.get('mcp_servers')
        }
        if branch_name:
            data['branch_name'] = branch_name
        if container_name:
            data['container_name'] = container_name

        self.emit(EventType.AGENT_INITIALIZED, agent, task_id, project, data, pipeline_run_id)
        
        # Return the execution ID so caller can use it for subsequent events
        return agent_execution_id

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

    def emit_claude_stream_event(self, agent: str, task_id: str, project: str,
                                  stream_event: Dict[str, Any],
                                  pipeline_run_id: Optional[str] = None):
        """
        Emit Claude Code stream event to Elasticsearch for agent execution tracking.

        This stores raw Claude Code output events (assistant messages, tool calls, etc.)
        in the claude-streams-* index for display in the agent execution UI.

        Args:
            agent: Agent name
            task_id: Task ID
            project: Project name
            stream_event: Raw Claude Code event dict (e.g., {"type": "assistant", "message": {...}})
            pipeline_run_id: Optional pipeline run ID for tracking
        """
        if not self.enabled or not self.es:
            return

        try:
            # Create index name based on current date
            index_name = f"claude-streams-{utc_now().strftime('%Y-%m-%d')}"

            # Prepare document for Elasticsearch
            doc = {
                'timestamp': stream_event.get('timestamp', utc_isoformat()),
                'event_type': 'claude_stream',  # Top-level event type for querying
                'event_category': 'claude_stream',  # Category for filtering
                'agent': agent,
                'task_id': task_id,
                'project': project,
                'pipeline_run_id': pipeline_run_id,
                'raw_event': stream_event  # Store the complete Claude Code event
            }

            # Index the document
            self.es.index(index=index_name, document=doc)
            logger.debug(f"Indexed Claude stream event to {index_name} for {agent}/{task_id}")
        except Exception as e:
            logger.error(f"Failed to index Claude stream event to Elasticsearch: {e}")

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
                            error: Optional[str] = None,
                            pipeline_run_id: Optional[str] = None,
                            output: Optional[str] = None,
                            agent_execution_id: Optional[str] = None):
        """Emit agent completion event"""
        event_type = EventType.AGENT_COMPLETED if success else EventType.AGENT_FAILED

        data = {
            'duration_ms': duration_ms,
            'success': success,
            'error': error
        }
        
        # Include agent_execution_id for tracking
        if agent_execution_id:
            data['agent_execution_id'] = agent_execution_id
        
        # Include output if provided (truncate if too long)
        if output:
            # Store full output (for display in UI)
            data['output'] = output
            # Also store a preview for events list
            data['output_preview'] = output[:1000] + "..." if len(output) > 1000 else output
        
        self.emit(event_type, agent, task_id, project, data, pipeline_run_id)
    
    def emit_branch_selected(self, agent: str, task_id: str, project: str,
                            branch_name: str, reason: str, 
                            issue_number: Optional[int] = None,
                            parent_issue: Optional[int] = None,
                            is_new: bool = False,
                            confidence: Optional[float] = None,
                            pipeline_run_id: Optional[str] = None):
        """Emit branch selection event"""
        data = {
            'branch_name': branch_name,
            'reason': reason,
            'is_new': is_new
        }
        
        if issue_number:
            data['issue_number'] = issue_number
        if parent_issue:
            data['parent_issue'] = parent_issue
        if confidence is not None:
            data['confidence'] = confidence
        
        self.emit(EventType.BRANCH_SELECTED, agent, task_id, project, data, pipeline_run_id)
    
    def cleanup_stale_agent_events_on_startup(self):
        """
        Clean up stale agent_initialized events from Redis stream on startup.
        
        This fixes the issue where agents that crashed or were interrupted
        without emitting completion events remain in the event stream as 'active'.
        
        Strategy:
        1. Read all events from Redis stream
        2. Track agents that have initialized but not completed
        3. For agents without completion that are older than threshold, emit synthetic completion events
        4. This allows the UI to correctly show no active agents after restart
        """
        if not self.enabled or not self.redis:
            return
        
        try:
            from datetime import datetime, timedelta
            
            # Threshold for considering an agent stale (2 hours)
            STALE_THRESHOLD_HOURS = 2
            stale_threshold = datetime.utcnow() - timedelta(hours=STALE_THRESHOLD_HOURS)
            
            # Read all events from the stream
            try:
                stream_data = self.redis.xrange(self.stream_key, '-', '+')
            except Exception as e:
                logger.warning(f"Could not read Redis stream for cleanup: {e}")
                return
            
            if not stream_data:
                logger.debug("No events in Redis stream to clean up")
                return
            
            # Track agent states
            agent_states = {}
            
            for stream_id, event_data in stream_data:
                try:
                    event_json = event_data.get('event')
                    if not event_json:
                        continue
                    
                    event = json.loads(event_json)
                    event_type = event.get('event_type')
                    agent = event.get('agent')
                    task_id = event.get('task_id')
                    timestamp_str = event.get('timestamp')
                    
                    if not agent or not event_type:
                        continue
                    
                    # Parse timestamp
                    try:
                        if timestamp_str.endswith('Z'):
                            event_time = datetime.fromisoformat(timestamp_str[:-1])
                        else:
                            event_time = datetime.fromisoformat(timestamp_str)
                    except:
                        continue
                    
                    # Track agent state
                    if event_type == 'agent_initialized':
                        agent_states[task_id] = {
                            'agent': agent,
                            'task_id': task_id,
                            'project': event.get('project'),
                            'initialized_at': event_time,
                            'completed': False
                        }
                    elif event_type in ['agent_completed', 'agent_failed']:
                        if task_id in agent_states:
                            agent_states[task_id]['completed'] = True
                
                except Exception as e:
                    logger.debug(f"Error processing event for cleanup: {e}")
                    continue
            
            # Find stale agents (initialized but not completed, older than threshold)
            stale_count = 0
            for task_id, state in agent_states.items():
                if not state['completed'] and state['initialized_at'] < stale_threshold:
                    agent = state['agent']
                    project = state['project']
                    
                    logger.info(
                        f"Found stale agent event: {agent} (task: {task_id}, "
                        f"initialized: {state['initialized_at']}) - emitting synthetic completion"
                    )
                    
                    # Emit synthetic agent_failed event to mark it as complete
                    # This will be added to the stream and picked up by the UI
                    self.emit_agent_completed(
                        agent=agent,
                        task_id=task_id,
                        project=project,
                        duration_ms=0,
                        success=False,
                        error="Agent interrupted (orchestrator restarted)",
                        output=None
                    )
                    
                    stale_count += 1
            
            if stale_count > 0:
                logger.info(f"Cleaned up {stale_count} stale agent events")
            else:
                logger.debug("No stale agent events found")
                
        except Exception as e:
            logger.error(f"Error during stale agent event cleanup: {e}")
    
    # ========== REPAIR CYCLE CONTAINER EVENT HELPERS ==========
    
    def emit_repair_cycle_container_started(self, project: str, issue_number: int, 
                                           container_name: str, run_id: str,
                                           pipeline_run_id: Optional[str] = None):
        """Emit repair cycle container started event"""
        self.emit(EventType.REPAIR_CYCLE_CONTAINER_STARTED, 'repair_cycle', 
                 f'repair-{project}-{issue_number}', project, {
            'issue_number': issue_number,
            'container_name': container_name,
            'run_id': run_id
        }, pipeline_run_id)
    
    def emit_repair_cycle_container_checkpoint_updated(self, project: str, issue_number: int,
                                                      container_name: str, checkpoint: Dict[str, Any],
                                                      pipeline_run_id: Optional[str] = None):
        """Emit repair cycle container checkpoint updated event"""
        self.emit(EventType.REPAIR_CYCLE_CONTAINER_CHECKPOINT_UPDATED, 'repair_cycle',
                 f'repair-{project}-{issue_number}', project, {
            'issue_number': issue_number,
            'container_name': container_name,
            'iteration': checkpoint.get('iteration'),
            'test_type': checkpoint.get('test_type'),
            'agent_call_count': checkpoint.get('agent_call_count'),
            'files_fixed': checkpoint.get('files_fixed', [])
        }, pipeline_run_id)
    
    def emit_repair_cycle_container_recovered(self, project: str, issue_number: int,
                                             container_name: str, checkpoint: Dict[str, Any],
                                             pipeline_run_id: Optional[str] = None):
        """Emit repair cycle container recovered event"""
        self.emit(EventType.REPAIR_CYCLE_CONTAINER_RECOVERED, 'repair_cycle',
                 f'repair-{project}-{issue_number}', project, {
            'issue_number': issue_number,
            'container_name': container_name,
            'iteration': checkpoint.get('iteration') if checkpoint else None,
            'test_type': checkpoint.get('test_type') if checkpoint else None,
            'checkpoint_age_seconds': checkpoint.get('checkpoint_age_seconds') if checkpoint else None
        }, pipeline_run_id)
    
    def emit_repair_cycle_container_killed(self, project: str, issue_number: int,
                                          container_name: str, reason: str,
                                          pipeline_run_id: Optional[str] = None):
        """Emit repair cycle container killed event"""
        self.emit(EventType.REPAIR_CYCLE_CONTAINER_KILLED, 'repair_cycle',
                 f'repair-{project}-{issue_number}', project, {
            'issue_number': issue_number,
            'container_name': container_name,
            'reason': reason
        }, pipeline_run_id)
    
    def emit_repair_cycle_container_completed(self, project: str, issue_number: int,
                                             container_name: str, success: bool,
                                             total_agent_calls: int, duration_seconds: float,
                                             pipeline_run_id: Optional[str] = None):
        """Emit repair cycle container completed event"""
        self.emit(EventType.REPAIR_CYCLE_CONTAINER_COMPLETED, 'repair_cycle',
                 f'repair-{project}-{issue_number}', project, {
            'issue_number': issue_number,
            'container_name': container_name,
            'success': success,
            'total_agent_calls': total_agent_calls,
            'duration_seconds': duration_seconds
        }, pipeline_run_id)

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
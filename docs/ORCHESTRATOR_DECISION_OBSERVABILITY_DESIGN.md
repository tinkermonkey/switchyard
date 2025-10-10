# Orchestrator Decision Observability - Design Document

## Executive Summary

This document proposes a comprehensive enhancement to our observability system to capture not just agent lifecycle events, but all orchestrator decisions. This will provide complete visibility into how the orchestrator routes work, manages feedback loops, handles errors, and progresses issues through pipelines.

## Current State Analysis

### Existing Event System

**Strong Foundation:**
- ✅ `ObservabilityManager` in `monitoring/observability.py` with Redis pub/sub
- ✅ `AgentExecutor` guarantees observability for all agent executions
- ✅ Redis Stream with TTL for event history
- ✅ WebSocket streaming to web UI (`web_ui/observability.html`)
- ✅ Structured event data with timestamps and context

**Current Event Categories:**
```python
# Lifecycle events
TASK_RECEIVED, AGENT_INITIALIZED, AGENT_STARTED, AGENT_COMPLETED, AGENT_FAILED

# Prompt events
PROMPT_CONSTRUCTED, CLAUDE_API_CALL_STARTED, CLAUDE_API_CALL_COMPLETED

# Response events
RESPONSE_CHUNK_RECEIVED, RESPONSE_PROCESSING_STARTED, RESPONSE_PROCESSING_COMPLETED

# Tool events
TOOL_EXECUTION_STARTED, TOOL_EXECUTION_COMPLETED

# Performance events
PERFORMANCE_METRIC, TOKEN_USAGE
```

### Gap Analysis

**Missing Decision Events:**
1. ❌ Feedback monitoring and detection
2. ❌ Agent selection/routing decisions
3. ❌ Issue status progression decisions
4. ❌ Maker/reviewer cycle routing
5. ❌ Workspace routing (issues vs discussions)
6. ❌ Error handling decisions and circuit breaker activations
7. ❌ Review escalation to human feedback
8. ❌ Conversational loop question routing

## Design Principles

### 1. Build on Existing Infrastructure
- Extend `ObservabilityManager` rather than create parallel system
- Use same Redis pub/sub + stream pattern
- Maintain backward compatibility with existing UI

### 2. Easy to Maintain
- Clear, consistent patterns for emitting decision events
- Decorator-based automatic capture for common patterns
- Self-documenting event data structures
- Centralized decision event emitter

### 3. Reliability First
- Events emitted even in error paths
- No exceptions thrown from event emission
- Graceful degradation if observability is disabled
- Events captured before and after decision execution

### 4. Structured and Queryable
- Consistent event schema across all decision types
- Rich context for understanding "why" decisions were made
- Support for correlation between decisions and outcomes
- Enable pattern analysis and alerting

## Proposed Architecture

### 1. Extended Event Type Enum

```python
class EventType(Enum):
    """Types of observability events"""
    
    # ========== EXISTING EVENTS (unchanged) ==========
    # Lifecycle events
    TASK_RECEIVED = "task_received"
    AGENT_INITIALIZED = "agent_initialized"
    AGENT_STARTED = "agent_started"
    AGENT_COMPLETED = "agent_completed"
    AGENT_FAILED = "agent_failed"
    
    # ... (all existing events remain)
    
    # ========== NEW: ORCHESTRATOR DECISION EVENTS ==========
    
    # Feedback Monitoring
    FEEDBACK_DETECTED = "feedback_detected"
    FEEDBACK_LISTENING_STARTED = "feedback_listening_started"
    FEEDBACK_LISTENING_STOPPED = "feedback_listening_stopped"
    FEEDBACK_IGNORED = "feedback_ignored"  # Why feedback was not acted upon
    
    # Agent Routing & Selection
    AGENT_ROUTING_DECISION = "agent_routing_decision"
    AGENT_SELECTED = "agent_selected"
    WORKSPACE_ROUTING_DECISION = "workspace_routing_decision"  # Issues vs Discussions
    
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
```

### 2. Decision Event Data Structure

All decision events follow a consistent schema:

```python
@dataclass
class DecisionEvent:
    """Extended event data for orchestrator decisions"""
    
    # Standard fields (inherited from ObservabilityEvent)
    timestamp: str
    event_type: str
    
    # Decision context
    decision_category: str  # "routing", "progression", "error_handling", "feedback"
    decision_point: str     # Where in code this decision was made
    
    # Inputs that led to decision
    inputs: Dict[str, Any]  # State/data used to make decision
    
    # The decision itself
    decision: Dict[str, Any]  # What was decided
    
    # Reasoning
    reason: str             # Human-readable explanation
    reasoning_data: Dict[str, Any]  # Structured reasoning (scores, rules matched, etc.)
    
    # Correlation
    parent_event_id: Optional[str]  # Link to related events (e.g., task that triggered this)
    issue_number: Optional[int]
    discussion_id: Optional[str]
    project: str
    board: Optional[str]
    
    # Outcome tracking
    success: Optional[bool]  # Was decision executed successfully?
    error: Optional[str]     # Error if decision failed to execute
    
    # Metadata
    metadata: Dict[str, Any]  # Additional context
```

### 3. DecisionEventEmitter Helper Class

```python
class DecisionEventEmitter:
    """
    Helper for emitting orchestrator decision events with consistent structure.
    
    Wraps ObservabilityManager to provide decision-specific convenience methods.
    """
    
    def __init__(self, obs_manager: ObservabilityManager):
        self.obs = obs_manager
    
    def emit_agent_routing_decision(
        self,
        issue_number: int,
        project: str,
        board: str,
        current_status: str,
        selected_agent: str,
        reason: str,
        alternatives: List[str] = None,
        workspace_type: str = "issues",
        discussion_id: str = None
    ):
        """Emit event when orchestrator selects which agent to run"""
        self.obs.emit(
            EventType.AGENT_ROUTING_DECISION,
            agent="orchestrator",
            task_id=f"routing_{project}_{issue_number}",
            project=project,
            data={
                'decision_category': 'routing',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'discussion_id': discussion_id,
                'inputs': {
                    'current_status': current_status,
                    'available_agents': alternatives or []
                },
                'decision': {
                    'selected_agent': selected_agent
                },
                'reason': reason,
                'reasoning_data': {
                    'selection_method': 'workflow_mapping',
                    'alternatives_considered': alternatives or []
                }
            }
        )
    
    def emit_feedback_detected(
        self,
        issue_number: int,
        project: str,
        board: str,
        feedback_source: str,  # "comment", "discussion_reply", "label", "status_change"
        feedback_content: str,
        target_agent: Optional[str],
        action_taken: str,
        workspace_type: str = "issues"
    ):
        """Emit event when orchestrator detects feedback on an issue"""
        self.obs.emit(
            EventType.FEEDBACK_DETECTED,
            agent="orchestrator",
            task_id=f"feedback_{project}_{issue_number}",
            project=project,
            data={
                'decision_category': 'feedback',
                'issue_number': issue_number,
                'board': board,
                'workspace_type': workspace_type,
                'inputs': {
                    'feedback_source': feedback_source,
                    'feedback_content': feedback_content[:500]  # Truncate for event
                },
                'decision': {
                    'action_taken': action_taken,
                    'target_agent': target_agent
                },
                'reason': f"Detected feedback from {feedback_source}, routing to {target_agent or 'no agent'}"
            }
        )
    
    def emit_status_progression(
        self,
        issue_number: int,
        project: str,
        board: str,
        from_status: str,
        to_status: str,
        trigger: str,  # "agent_completion", "manual", "auto_progression"
        success: bool,
        error: Optional[str] = None
    ):
        """Emit event when orchestrator moves an issue to a new status"""
        event_type = (EventType.STATUS_PROGRESSION_COMPLETED if success 
                     else EventType.STATUS_PROGRESSION_FAILED)
        
        self.obs.emit(
            event_type,
            agent="orchestrator",
            task_id=f"progression_{project}_{issue_number}",
            project=project,
            data={
                'decision_category': 'progression',
                'issue_number': issue_number,
                'board': board,
                'inputs': {
                    'from_status': from_status,
                    'trigger': trigger
                },
                'decision': {
                    'to_status': to_status
                },
                'success': success,
                'error': error,
                'reason': f"Progressing issue from {from_status} to {to_status} (trigger: {trigger})"
            }
        )
    
    def emit_review_cycle_decision(
        self,
        issue_number: int,
        project: str,
        board: str,
        cycle_iteration: int,
        decision_type: str,  # "start", "maker_selected", "reviewer_selected", "escalate", "complete"
        maker_agent: str,
        reviewer_agent: str,
        reason: str,
        additional_data: Dict[str, Any] = None
    ):
        """Emit event for review cycle routing decisions"""
        event_map = {
            'start': EventType.REVIEW_CYCLE_STARTED,
            'iteration': EventType.REVIEW_CYCLE_ITERATION,
            'maker_selected': EventType.REVIEW_CYCLE_MAKER_SELECTED,
            'reviewer_selected': EventType.REVIEW_CYCLE_REVIEWER_SELECTED,
            'escalate': EventType.REVIEW_CYCLE_ESCALATED,
            'complete': EventType.REVIEW_CYCLE_COMPLETED
        }
        
        data = {
            'decision_category': 'review_cycle',
            'issue_number': issue_number,
            'board': board,
            'inputs': {
                'cycle_iteration': cycle_iteration,
                'maker_agent': maker_agent,
                'reviewer_agent': reviewer_agent
            },
            'reason': reason
        }
        
        if additional_data:
            data.update(additional_data)
        
        self.obs.emit(
            event_map.get(decision_type, EventType.REVIEW_CYCLE_ITERATION),
            agent="orchestrator",
            task_id=f"review_cycle_{project}_{issue_number}_{cycle_iteration}",
            project=project,
            data=data
        )
    
    def emit_error_decision(
        self,
        error_type: str,
        error_message: str,
        context: Dict[str, Any],
        recovery_action: str,
        success: bool,
        project: str = "unknown"
    ):
        """Emit event when orchestrator handles an error"""
        event_type = EventType.ERROR_RECOVERED if success else EventType.ERROR_ENCOUNTERED
        
        self.obs.emit(
            event_type,
            agent="orchestrator",
            task_id=f"error_{datetime.now().timestamp()}",
            project=project,
            data={
                'decision_category': 'error_handling',
                'error_type': error_type,
                'error_message': error_message,
                'context': context,
                'decision': {
                    'recovery_action': recovery_action
                },
                'success': success,
                'reason': f"Attempting recovery: {recovery_action}"
            }
        )
    
    def emit_workspace_routing(
        self,
        issue_number: int,
        project: str,
        board: str,
        stage: str,
        selected_workspace: str,  # "issues" or "discussions"
        category_id: Optional[str],
        reason: str
    ):
        """Emit event when orchestrator routes work to issues vs discussions"""
        self.obs.emit(
            EventType.WORKSPACE_ROUTING_DECISION,
            agent="orchestrator",
            task_id=f"workspace_routing_{project}_{issue_number}",
            project=project,
            data={
                'decision_category': 'routing',
                'issue_number': issue_number,
                'board': board,
                'inputs': {
                    'stage': stage
                },
                'decision': {
                    'workspace': selected_workspace,
                    'category_id': category_id
                },
                'reason': reason
            }
        )
```

### 4. Integration Points

#### 4.1 ProjectMonitor - Feedback Detection
```python
# In services/project_monitor.py

class ProjectMonitor:
    def __init__(self, task_queue, config_manager):
        # ... existing code ...
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
    
    def detect_changes(self, project_name, current_items):
        """Detect changes with observability"""
        changes = []
        
        for item in current_items:
            # ... detection logic ...
            
            if status_changed:
                # EMIT DECISION EVENT
                self.decision_events.emit_status_progression(
                    issue_number=item.issue_number,
                    project=project_name,
                    board=board_name,
                    from_status=old_status,
                    to_status=item.status,
                    trigger="manual",  # User moved it
                    success=True
                )
            
            # ... rest of logic ...
```

#### 4.2 ProjectMonitor - Agent Routing
```python
# In services/project_monitor.py

def _get_agent_for_status(self, project_name, board_name, status, issue_number, repository):
    """Get agent with routing decision observability"""
    
    # ... existing logic to determine agent ...
    
    # EMIT DECISION EVENT
    if agent and agent != 'null':
        alternatives = [col.agent for col in workflow_template.columns 
                       if col.agent and col.agent != 'null']
        
        self.decision_events.emit_agent_routing_decision(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            current_status=status,
            selected_agent=agent,
            reason=f"Status '{status}' maps to agent '{agent}' in workflow",
            alternatives=alternatives
        )
    
    return agent
```

#### 4.3 ReviewCycle - Maker/Reviewer Routing
```python
# In services/review_cycle.py

class ReviewCycleManager:
    def __init__(self):
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
    
    async def start_review_cycle(self, ...):
        """Start review cycle with observability"""
        
        # EMIT: Cycle started
        self.decision_events.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            cycle_iteration=0,
            decision_type='start',
            maker_agent=maker_agent,
            reviewer_agent=reviewer_agent,
            reason=f"Starting review cycle: {maker_agent} → {reviewer_agent}"
        )
        
        # ... existing cycle logic ...
        
        # EMIT: Maker selected
        self.decision_events.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            cycle_iteration=cycle_state.current_iteration,
            decision_type='maker_selected',
            maker_agent=maker_agent,
            reviewer_agent=reviewer_agent,
            reason=f"Executing maker agent iteration {cycle_state.current_iteration}"
        )
```

#### 4.4 WorkspaceRouter - Issues vs Discussions
```python
# In services/workspace_router.py

class WorkspaceRouter:
    def __init__(self):
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
    
    def determine_workspace(self, project, board, stage):
        """Determine workspace with observability"""
        
        # ... existing logic ...
        
        # EMIT DECISION EVENT
        if hasattr(self, 'decision_events') and issue_number:
            self.decision_events.emit_workspace_routing(
                issue_number=issue_number,  # Need to pass this in
                project=project,
                board=board,
                stage=stage,
                selected_workspace=workspace_type,
                category_id=category_id,
                reason=f"Pipeline config specifies '{workspace}' workspace for stage '{stage}'"
            )
        
        return (workspace_type, category_id)
```

#### 4.5 Error Handling - Circuit Breakers
```python
# In services/circuit_breaker.py (or wherever circuit breakers are)

class CircuitBreaker:
    def __init__(self, name):
        self.name = name
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
    
    def record_failure(self):
        """Record failure and check if circuit should open"""
        self.failure_count += 1
        
        if self.failure_count >= self.threshold and self.state == 'closed':
            self.state = 'open'
            
            # EMIT DECISION EVENT
            self.decision_events.emit_error_decision(
                error_type='circuit_breaker_opened',
                error_message=f"Circuit breaker '{self.name}' opened after {self.failure_count} failures",
                context={'threshold': self.threshold, 'failures': self.failure_count},
                recovery_action='stop_processing_until_timeout',
                success=True,
                project='system'
            )
```

### 5. Decorator Pattern for Automatic Capture

For common patterns, provide decorators:

```python
# In monitoring/decision_decorators.py

from functools import wraps
from typing import Callable
from monitoring.observability import get_observability_manager
from monitoring.decision_events import DecisionEventEmitter

def observe_routing_decision(
    get_project: Callable = None,
    get_issue_number: Callable = None,
    get_selected_agent: Callable = None
):
    """
    Decorator to automatically capture agent routing decisions
    
    Usage:
        @observe_routing_decision(
            get_project=lambda args, kwargs: kwargs['project'],
            get_issue_number=lambda args, kwargs: kwargs['issue_number'],
            get_selected_agent=lambda result: result
        )
        def select_agent_for_issue(project, issue_number, status):
            # ... decision logic ...
            return selected_agent
    """
    def decorator(func):
        @wraps(func)
        def wrapper(*args, **kwargs):
            obs = get_observability_manager()
            decision_events = DecisionEventEmitter(obs)
            
            try:
                result = func(*args, **kwargs)
                
                # Extract decision data using provided functions
                if get_project and get_issue_number and get_selected_agent:
                    project = get_project(args, kwargs)
                    issue_number = get_issue_number(args, kwargs)
                    selected_agent = get_selected_agent(result)
                    
                    decision_events.emit_agent_routing_decision(
                        issue_number=issue_number,
                        project=project,
                        board=kwargs.get('board', 'unknown'),
                        current_status=kwargs.get('status', 'unknown'),
                        selected_agent=selected_agent,
                        reason=f"Routed by {func.__name__}"
                    )
                
                return result
            except Exception as e:
                # Emit error event
                decision_events.emit_error_decision(
                    error_type=type(e).__name__,
                    error_message=str(e),
                    context={'function': func.__name__},
                    recovery_action='propagate_exception',
                    success=False
                )
                raise
        
        return wrapper
    return decorator
```

## Implementation Plan

### Phase 1: Core Infrastructure (Week 1)
1. ✅ Extend `EventType` enum with decision events
2. ✅ Create `DecisionEventEmitter` helper class
3. ✅ Add decision event emitters to key services:
   - `ProjectMonitor`
   - `ReviewCycleManager`
   - `WorkspaceRouter`
   - `PipelineProgression`
4. ✅ Update Redis event schema documentation
5. ✅ Add decision events to observability tests

### Phase 2: Integration (Week 2)
1. ✅ Add decision events to all routing points:
   - Agent selection (`project_monitor.py`)
   - Workspace routing (`workspace_router.py`)
   - Review cycle routing (`review_cycle.py`)
2. ✅ Add decision events to feedback detection:
   - Comment monitoring
   - Status changes
   - Label changes
3. ✅ Add decision events to status progression:
   - Auto-progression logic
   - Manual moves
   - Pipeline stage transitions

### Phase 3: Error Handling (Week 3)
1. ✅ Add error decision events:
   - Circuit breaker activation
   - Retry logic
   - Health check failures
   - Task execution errors
2. ✅ Ensure error events emitted in all error paths
3. ✅ Add error recovery tracking

### Phase 4: UI Enhancement (Week 4)
1. ✅ Update `observability.html` to display decision events
2. ✅ Add filtering by decision category
3. ✅ Add decision event timeline view
4. ✅ Add correlation between decisions and outcomes
5. ✅ Add search/filter capabilities

### Phase 5: Testing & Documentation (Week 5)
1. ✅ Comprehensive test coverage for decision events
2. ✅ Integration tests for all decision points
3. ✅ Performance testing (ensure minimal overhead)
4. ✅ Update documentation
5. ✅ Create decision event runbook for operations

## Testing Strategy

### Unit Tests
```python
# tests/monitoring/test_decision_events.py

def test_agent_routing_decision_emitted():
    """Test that agent routing decisions are captured"""
    obs = get_observability_manager()
    decision_events = DecisionEventEmitter(obs)
    
    # Mock Redis to capture events
    with patch.object(obs.redis, 'publish') as mock_publish:
        decision_events.emit_agent_routing_decision(
            issue_number=123,
            project="test-project",
            board="dev",
            current_status="Ready",
            selected_agent="software_architect",
            reason="Status maps to architecture stage"
        )
        
        # Assert event was emitted
        assert mock_publish.called
        event_data = json.loads(mock_publish.call_args[0][1])
        assert event_data['event_type'] == 'agent_routing_decision'
        assert event_data['data']['decision']['selected_agent'] == 'software_architect'
```

### Integration Tests
```python
# tests/integration/test_decision_observability.py

async def test_end_to_end_decision_tracking():
    """Test that decisions flow through to observability UI"""
    
    # Simulate issue status change
    project_monitor = ProjectMonitor(task_queue, config_manager)
    
    # Track emitted events
    events_emitted = []
    
    def capture_event(channel, event_json):
        events_emitted.append(json.loads(event_json))
    
    with patch.object(project_monitor.obs.redis, 'publish', side_effect=capture_event):
        # Trigger agent routing
        agent = project_monitor._get_agent_for_status(
            "test-project", "dev", "In Progress", 123, "test-repo"
        )
        
        # Assert decision events captured
        routing_events = [e for e in events_emitted 
                         if e['event_type'] == 'agent_routing_decision']
        assert len(routing_events) == 1
        assert routing_events[0]['data']['decision']['selected_agent'] == agent
```

## Maintenance Considerations

### 1. Clear Event Emission Patterns

**When to emit decision events:**
- ✅ Before executing a decision (with planned action)
- ✅ After executing a decision (with outcome)
- ✅ When a decision is skipped (with reason)
- ✅ When an error prevents decision execution

**Example pattern:**
```python
def route_to_agent(issue_number, status):
    # EMIT: Decision being made
    decision_events.emit_agent_routing_decision(
        issue_number=issue_number,
        current_status=status,
        selected_agent=agent,  # Decided agent
        reason="Status mapping"
    )
    
    try:
        # Execute decision
        result = task_queue.enqueue(Task(...))
        
        # EMIT: Success (if significant)
        if needs_confirmation_event:
            decision_events.emit_task_queued(...)
        
        return result
    except Exception as e:
        # EMIT: Failure
        decision_events.emit_error_decision(
            error_type=type(e).__name__,
            error_message=str(e),
            recovery_action="log_and_skip",
            success=False
        )
        raise
```

### 2. Event Data Guidelines

**DO:**
- Include enough context to understand the decision independently
- Use consistent field names across similar decision types
- Include reasoning data (what was considered)
- Truncate large strings (prompts, content) to reasonable size
- Link related events via `parent_event_id` or correlation IDs

**DON'T:**
- Include sensitive data (API keys, passwords)
- Duplicate entire agent outputs in events
- Create deeply nested data structures
- Emit events at too granular a level (every function call)

### 3. Performance Considerations

**Overhead Management:**
- Event emission is non-blocking (Redis pub/sub)
- Failed event emission doesn't block main logic
- Events auto-trimmed by Redis Stream (maxlen=1000)
- 2-hour TTL prevents unbounded growth

**Monitoring:**
- Track event emission rate
- Alert on excessive event volume
- Monitor Redis memory usage
- Track event processing lag in UI

### 4. Backward Compatibility

All changes are additive:
- ✅ Existing event types unchanged
- ✅ Existing UI continues to work
- ✅ New events use same infrastructure
- ✅ No breaking changes to event schema

## Success Metrics

### Observability Completeness
- [ ] 100% of routing decisions captured
- [ ] 100% of status progressions captured
- [ ] 100% of review cycle decisions captured
- [ ] 100% of error decisions captured
- [ ] 100% of feedback detection events captured

### Developer Experience
- [ ] Clear documentation of when/how to emit events
- [ ] <5 lines of code to emit any decision event
- [ ] Decorator pattern available for common cases
- [ ] Examples in codebase for each event type

### Operations
- [ ] Decision events queryable in UI
- [ ] Decision-to-outcome correlation visible
- [ ] Pattern detection identifies decision bottlenecks
- [ ] Alerting configured for decision anomalies

### Performance
- [ ] Event emission adds <1ms overhead per decision
- [ ] Redis memory usage <100MB for event history
- [ ] UI renders decision events <500ms
- [ ] No impact on agent execution performance

## Appendix: Example Event Flows

### Flow 1: Issue Status Change → Agent Routing → Execution

```
1. FEEDBACK_DETECTED
   - Source: status_change
   - Issue: #123
   - From: "Backlog" → "In Progress"

2. AGENT_ROUTING_DECISION
   - Selected: software_architect
   - Reason: "Status 'In Progress' maps to architecture stage"
   - Alternatives: [business_analyst, requirements_reviewer]

3. WORKSPACE_ROUTING_DECISION
   - Workspace: issues
   - Reason: "Pipeline config uses issues workspace"

4. TASK_QUEUED
   - Agent: software_architect
   - Priority: NORMAL

5. TASK_RECEIVED (existing)
   - Agent: software_architect

6. AGENT_INITIALIZED (existing)
   - Agent: software_architect

... (agent execution events)

7. AGENT_COMPLETED (existing)
   - Agent: software_architect
   - Success: true

8. STATUS_PROGRESSION_COMPLETED
   - From: "In Progress" → "Architecture Review"
   - Trigger: agent_completion
```

### Flow 2: Review Cycle with Human Escalation

```
1. REVIEW_CYCLE_STARTED
   - Maker: senior_software_engineer
   - Reviewer: code_reviewer
   - Max iterations: 3

2. REVIEW_CYCLE_MAKER_SELECTED
   - Iteration: 1
   - Agent: senior_software_engineer

... (maker execution)

3. REVIEW_CYCLE_REVIEWER_SELECTED
   - Iteration: 1
   - Agent: code_reviewer

... (reviewer execution)

4. REVIEW_CYCLE_ITERATION
   - Iteration: 2 (needs revision)
   - Reason: "Reviewer requested changes"

... (repeated maker/reviewer)

5. REVIEW_CYCLE_ESCALATED
   - Iteration: 3 (max reached)
   - Reason: "Max iterations reached, needs human input"
   - Status: awaiting_human_feedback

6. FEEDBACK_LISTENING_STARTED
   - Issue: #123
   - Monitoring for: human comments

7. FEEDBACK_DETECTED
   - Source: comment
   - Content: "Please proceed, this is acceptable"

8. REVIEW_CYCLE_RESUMED
   - Iteration: 3
   - Action: Accept with human override

9. REVIEW_CYCLE_COMPLETED
   - Total iterations: 3
   - Outcome: approved
```

## Conclusion

This design enhances observability by capturing orchestrator decisions while:
1. ✅ Building on existing infrastructure (no parallel systems)
2. ✅ Maintaining backward compatibility
3. ✅ Providing clear, maintainable patterns
4. ✅ Ensuring reliability (events emitted even in errors)
5. ✅ Enabling powerful analysis and debugging

The implementation is phased to deliver value incrementally while maintaining system stability.

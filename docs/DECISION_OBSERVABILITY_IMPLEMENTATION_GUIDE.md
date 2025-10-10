# Orchestrator Decision Observability - Implementation Guide

## Quick Start

This guide shows you exactly how to add decision observability to your code.

## Table of Contents
1. [Basic Setup](#basic-setup)
2. [Common Patterns](#common-patterns)
3. [Integration Examples](#integration-examples)
4. [Testing](#testing)
5. [Troubleshooting](#troubleshooting)

## Basic Setup

### Step 1: Import the Decision Emitter

```python
from monitoring.observability import get_observability_manager
from monitoring.decision_events import DecisionEventEmitter

class YourService:
    def __init__(self):
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
```

### Step 2: Emit Decision Events

```python
def your_decision_function(self, issue_number, project):
    # Your decision logic
    selected_agent = self._determine_agent(issue_number)
    
    # EMIT DECISION EVENT
    self.decision_events.emit_agent_routing_decision(
        issue_number=issue_number,
        project=project,
        board="dev",
        current_status="In Progress",
        selected_agent=selected_agent,
        reason="Agent selected based on issue status"
    )
    
    return selected_agent
```

## Common Patterns

### Pattern 1: Routing Decision

**When to use:** Anytime you decide which agent should handle work.

```python
def route_issue_to_agent(self, issue_number, status, project, board):
    """Route issue to appropriate agent"""
    
    # Determine agent based on workflow
    agent = self._get_agent_from_workflow(status)
    
    # Get alternatives for context
    all_agents = self._get_all_workflow_agents()
    
    # EMIT DECISION
    self.decision_events.emit_agent_routing_decision(
        issue_number=issue_number,
        project=project,
        board=board,
        current_status=status,
        selected_agent=agent,
        reason=f"Workflow maps status '{status}' to agent '{agent}'",
        alternatives=[a for a in all_agents if a != agent]
    )
    
    # Execute decision
    self.task_queue.enqueue(Task(agent=agent, ...))
    
    return agent
```

### Pattern 2: Feedback Detection

**When to use:** When monitoring for and detecting human feedback.

```python
def check_for_feedback(self, issue_number, project, board):
    """Check if there's new feedback on an issue"""
    
    # Get latest comments
    comments = self.github.get_comments_since(issue_number, last_check_time)
    
    for comment in comments:
        # Determine if this is actionable feedback
        is_feedback, target_agent = self._parse_feedback(comment)
        
        if is_feedback:
            # EMIT FEEDBACK DETECTED
            self.decision_events.emit_feedback_detected(
                issue_number=issue_number,
                project=project,
                board=board,
                feedback_source="comment",
                feedback_content=comment.body,
                target_agent=target_agent,
                action_taken="queue_agent_task"
            )
            
            # Queue task for agent
            self.queue_feedback_task(issue_number, target_agent, comment)
        else:
            # EMIT FEEDBACK IGNORED (optional but helpful)
            self.decision_events.emit_feedback_ignored(
                issue_number=issue_number,
                project=project,
                feedback_source="comment",
                reason="Not actionable feedback (general discussion)"
            )
```

### Pattern 3: Status Progression

**When to use:** When moving an issue to a new status/column.

```python
def progress_issue(self, issue_number, project, board, to_status, trigger="agent_completion"):
    """Progress issue to next status"""
    
    # Get current status
    current_status = self.get_current_status(issue_number)
    
    # EMIT: Starting progression
    self.decision_events.emit_status_progression(
        issue_number=issue_number,
        project=project,
        board=board,
        from_status=current_status,
        to_status=to_status,
        trigger=trigger,
        success=None  # Not yet executed
    )
    
    try:
        # Execute move
        self.github_projects.move_issue(issue_number, to_status)
        
        # EMIT: Success
        self.decision_events.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status=current_status,
            to_status=to_status,
            trigger=trigger,
            success=True
        )
        
        return True
        
    except Exception as e:
        # EMIT: Failure
        self.decision_events.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status=current_status,
            to_status=to_status,
            trigger=trigger,
            success=False,
            error=str(e)
        )
        raise
```

### Pattern 4: Review Cycle Decisions

**When to use:** In maker/reviewer loops.

```python
async def execute_review_iteration(self, cycle_state):
    """Execute one iteration of maker/reviewer cycle"""
    
    iteration = cycle_state.current_iteration
    
    # EMIT: Starting iteration
    self.decision_events.emit_review_cycle_decision(
        issue_number=cycle_state.issue_number,
        project=cycle_state.project_name,
        board=cycle_state.board_name,
        cycle_iteration=iteration,
        decision_type='iteration',
        maker_agent=cycle_state.maker_agent,
        reviewer_agent=cycle_state.reviewer_agent,
        reason=f"Starting iteration {iteration}"
    )
    
    # EMIT: Maker selected
    self.decision_events.emit_review_cycle_decision(
        issue_number=cycle_state.issue_number,
        project=cycle_state.project_name,
        board=cycle_state.board_name,
        cycle_iteration=iteration,
        decision_type='maker_selected',
        maker_agent=cycle_state.maker_agent,
        reviewer_agent=cycle_state.reviewer_agent,
        reason=f"Executing maker agent: {cycle_state.maker_agent}"
    )
    
    # Execute maker
    maker_result = await self.execute_agent(cycle_state.maker_agent, ...)
    
    # EMIT: Reviewer selected
    self.decision_events.emit_review_cycle_decision(
        issue_number=cycle_state.issue_number,
        project=cycle_state.project_name,
        board=cycle_state.board_name,
        cycle_iteration=iteration,
        decision_type='reviewer_selected',
        maker_agent=cycle_state.maker_agent,
        reviewer_agent=cycle_state.reviewer_agent,
        reason=f"Executing reviewer agent: {cycle_state.reviewer_agent}"
    )
    
    # Execute reviewer
    review_result = await self.execute_agent(cycle_state.reviewer_agent, ...)
    
    # Check if needs escalation
    if iteration >= cycle_state.max_iterations and not review_result.approved:
        # EMIT: Escalation
        self.decision_events.emit_review_cycle_decision(
            issue_number=cycle_state.issue_number,
            project=cycle_state.project_name,
            board=cycle_state.board_name,
            cycle_iteration=iteration,
            decision_type='escalate',
            maker_agent=cycle_state.maker_agent,
            reviewer_agent=cycle_state.reviewer_agent,
            reason=f"Max iterations ({iteration}) reached without approval",
            additional_data={
                'max_iterations': cycle_state.max_iterations,
                'review_status': 'needs_changes'
            }
        )
    
    return review_result
```

### Pattern 5: Error Handling

**When to use:** Whenever catching and handling errors.

```python
def execute_with_circuit_breaker(self, operation_name, func, *args, **kwargs):
    """Execute operation with circuit breaker pattern"""
    
    circuit_breaker = self.circuit_breakers.get(operation_name)
    
    # Check circuit state
    if circuit_breaker.is_open():
        # Circuit is open - reject immediately
        self.decision_events.emit_error_decision(
            error_type='circuit_breaker_open',
            error_message=f"Circuit breaker '{operation_name}' is open",
            context={
                'operation': operation_name,
                'failure_count': circuit_breaker.failure_count
            },
            recovery_action='reject_request',
            success=True,  # Successfully rejected (as designed)
            project='system'
        )
        raise CircuitBreakerOpenError(operation_name)
    
    try:
        # Execute operation
        result = func(*args, **kwargs)
        
        # Record success
        circuit_breaker.record_success()
        
        return result
        
    except Exception as e:
        # Record failure
        circuit_breaker.record_failure()
        
        # Check if we should open circuit
        if circuit_breaker.should_open():
            # EMIT: Circuit breaker opened
            self.decision_events.emit_error_decision(
                error_type='circuit_breaker_opened',
                error_message=f"Opening circuit breaker '{operation_name}' after {circuit_breaker.failure_count} failures",
                context={
                    'operation': operation_name,
                    'failure_count': circuit_breaker.failure_count,
                    'threshold': circuit_breaker.threshold,
                    'last_error': str(e)
                },
                recovery_action='open_circuit',
                success=True,
                project='system'
            )
            
            circuit_breaker.open()
        else:
            # EMIT: Error but circuit stays closed
            self.decision_events.emit_error_decision(
                error_type=type(e).__name__,
                error_message=str(e),
                context={
                    'operation': operation_name,
                    'failure_count': circuit_breaker.failure_count
                },
                recovery_action='retry',
                success=False,
                project='system'
            )
        
        raise
```

### Pattern 6: Workspace Routing

**When to use:** Deciding between issues and discussions workspace.

```python
def route_to_workspace(self, project, board, stage, issue_number):
    """Determine which workspace (issues/discussions) to use"""
    
    # Determine workspace
    workspace_type, category_id = self._determine_workspace_internal(
        project, board, stage
    )
    
    # EMIT DECISION
    reason = self._get_routing_reason(project, board, stage, workspace_type)
    
    self.decision_events.emit_workspace_routing(
        issue_number=issue_number,
        project=project,
        board=board,
        stage=stage,
        selected_workspace=workspace_type,
        category_id=category_id,
        reason=reason
    )
    
    return workspace_type, category_id

def _get_routing_reason(self, project, board, stage, workspace_type):
    """Build human-readable reason for workspace routing"""
    pipeline_config = config_manager.get_project_config(project).get_pipeline(board)
    
    if pipeline_config.workspace == 'hybrid':
        if stage in pipeline_config.discussion_stages:
            return f"Stage '{stage}' configured for discussions in hybrid pipeline"
        else:
            return f"Stage '{stage}' defaults to issues in hybrid pipeline"
    else:
        return f"Pipeline configured for '{workspace_type}' workspace"
```

## Integration Examples

### Example 1: Adding to ProjectMonitor

```python
# In services/project_monitor.py

class ProjectMonitor:
    def __init__(self, task_queue: TaskQueue, config_manager: ConfigManager):
        self.task_queue = task_queue
        self.config_manager = config_manager
        
        # ADD DECISION OBSERVABILITY
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
    
    def detect_changes(self, project_name: str, current_items: List[ProjectItem]):
        """Detect changes with decision observability"""
        changes = []
        
        # ... existing change detection ...
        
        for change in detected_changes:
            if change['type'] == 'status_changed':
                # EMIT STATUS PROGRESSION EVENT
                self.decision_events.emit_status_progression(
                    issue_number=change['issue_number'],
                    project=project_name,
                    board=change['board'],
                    from_status=change['old_status'],
                    to_status=change['new_status'],
                    trigger='manual',
                    success=True
                )
        
        return changes
    
    def _get_agent_for_status(self, project_name, board_name, status, 
                              issue_number, repository):
        """Get agent with routing decision observability"""
        
        # ... existing agent lookup logic ...
        
        if agent and agent != 'null':
            # Get alternatives for context
            workflow = config_manager.get_workflow(project_name, board_name)
            all_agents = [col.agent for col in workflow.columns 
                         if col.agent and col.agent != 'null']
            
            # EMIT ROUTING DECISION
            self.decision_events.emit_agent_routing_decision(
                issue_number=issue_number,
                project=project_name,
                board=board_name,
                current_status=status,
                selected_agent=agent,
                reason=f"Status '{status}' maps to agent '{agent}' in workflow template",
                alternatives=[a for a in all_agents if a != agent]
            )
        
        return agent
```

### Example 2: Adding to ReviewCycle

```python
# In services/review_cycle.py

class ReviewCycleManager:
    def __init__(self):
        # ADD DECISION OBSERVABILITY
        from monitoring.observability import get_observability_manager
        from monitoring.decision_events import DecisionEventEmitter
        
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
    
    async def start_review_cycle(self, project_name, board_name, issue_number,
                                maker_agent, reviewer_agent, max_iterations=3):
        """Start review cycle with observability"""
        
        # Create cycle state
        cycle_state = ReviewCycleState(
            issue_number=issue_number,
            maker_agent=maker_agent,
            reviewer_agent=reviewer_agent,
            max_iterations=max_iterations,
            project_name=project_name,
            board_name=board_name
        )
        
        # EMIT: Cycle started
        self.decision_events.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            cycle_iteration=0,
            decision_type='start',
            maker_agent=maker_agent,
            reviewer_agent=reviewer_agent,
            reason=f"Starting review cycle: {maker_agent} ↔ {reviewer_agent} (max {max_iterations} iterations)"
        )
        
        # Execute cycle
        result = await self._execute_review_loop(cycle_state)
        
        # EMIT: Cycle completed
        self.decision_events.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project_name,
            board=board_name,
            cycle_iteration=cycle_state.current_iteration,
            decision_type='complete',
            maker_agent=maker_agent,
            reviewer_agent=reviewer_agent,
            reason=f"Review cycle completed after {cycle_state.current_iteration} iterations",
            additional_data={
                'total_iterations': cycle_state.current_iteration,
                'outcome': 'approved' if result.approved else 'escalated'
            }
        )
        
        return result
```

### Example 3: Adding to Error Handling

```python
# In agents/orchestrator_integration.py

async def process_task_integrated(task, state_manager, logger):
    """Process task with error observability"""
    
    from monitoring.observability import get_observability_manager
    from monitoring.decision_events import DecisionEventEmitter
    
    obs = get_observability_manager()
    decision_events = DecisionEventEmitter(obs)
    
    try:
        # Execute agent
        result = await executor.execute_agent(
            agent_name=task.agent,
            project_name=task.project,
            task_context=task.context,
            task_id_prefix="task"
        )
        
        return result
        
    except DockerImageNotFoundError as e:
        # EMIT: Specific error decision
        decision_events.emit_error_decision(
            error_type='docker_image_not_found',
            error_message=str(e),
            context={
                'agent': task.agent,
                'project': task.project,
                'task_id': task.id
            },
            recovery_action='queue_dev_environment_setup',
            success=True,  # Successfully handled
            project=task.project
        )
        
        # Queue dev environment setup
        await queue_dev_environment_setup(task.project, logger)
        
        # Re-raise to mark task as failed
        raise
        
    except Exception as e:
        # EMIT: Generic error
        decision_events.emit_error_decision(
            error_type=type(e).__name__,
            error_message=str(e),
            context={
                'agent': task.agent,
                'project': task.project,
                'task_id': task.id
            },
            recovery_action='fail_task',
            success=False,
            project=task.project
        )
        
        raise
```

## Testing

### Testing Decision Events

```python
# tests/monitoring/test_decision_events.py

import pytest
from unittest.mock import Mock, patch
from monitoring.observability import ObservabilityManager
from monitoring.decision_events import DecisionEventEmitter

def test_routing_decision_emitted():
    """Test that routing decisions are captured correctly"""
    
    # Create mock observability manager
    mock_obs = Mock(spec=ObservabilityManager)
    mock_obs.emit = Mock()
    
    # Create decision emitter
    decision_events = DecisionEventEmitter(mock_obs)
    
    # Emit routing decision
    decision_events.emit_agent_routing_decision(
        issue_number=123,
        project="test-project",
        board="dev",
        current_status="Ready",
        selected_agent="software_architect",
        reason="Test routing",
        alternatives=["business_analyst", "product_manager"]
    )
    
    # Assert emit was called
    assert mock_obs.emit.called
    
    # Verify event data
    call_args = mock_obs.emit.call_args
    event_type = call_args[0][0]
    data = call_args[1]['data']
    
    assert event_type.value == 'agent_routing_decision'
    assert data['decision']['selected_agent'] == 'software_architect'
    assert 'business_analyst' in data['reasoning_data']['alternatives_considered']
```

### Integration Test

```python
# tests/integration/test_decision_observability_integration.py

@pytest.mark.asyncio
async def test_full_routing_decision_flow():
    """Test that routing decisions flow through entire system"""
    
    # Setup
    task_queue = TaskQueue(use_redis=True)
    config_manager = ConfigManager()
    project_monitor = ProjectMonitor(task_queue, config_manager)
    
    # Capture events
    events_captured = []
    
    def capture_event(channel, event_json):
        events_captured.append(json.loads(event_json))
    
    with patch.object(project_monitor.obs.redis, 'publish', side_effect=capture_event):
        # Trigger routing
        agent = project_monitor._get_agent_for_status(
            "test-project",
            "dev",
            "In Progress",
            123,
            "test-repo"
        )
        
        # Verify routing decision event was emitted
        routing_events = [e for e in events_captured 
                         if e['event_type'] == 'agent_routing_decision']
        
        assert len(routing_events) == 1
        assert routing_events[0]['data']['decision']['selected_agent'] == agent
        assert routing_events[0]['data']['issue_number'] == 123
```

## Troubleshooting

### Events Not Appearing in UI

**Problem:** Decision events are emitted but don't show in observability dashboard.

**Solutions:**
1. Check Redis connection:
   ```python
   from monitoring.observability import get_observability_manager
   obs = get_observability_manager()
   obs.redis.ping()  # Should return True
   ```

2. Verify event channel:
   ```bash
   # In Redis CLI
   redis-cli
   > SUBSCRIBE orchestrator:agent_events
   ```

3. Check UI is listening to correct event types:
   - Update `web_ui/observability.html` to handle new event types
   - Add rendering functions for decision events

### Events Missing Context

**Problem:** Events emitted but missing key fields.

**Solution:** Ensure all required fields passed to emitter:
```python
# BAD - Missing required fields
self.decision_events.emit_agent_routing_decision(
    issue_number=123,
    selected_agent="architect"
    # Missing: project, board, current_status, reason
)

# GOOD - All required fields
self.decision_events.emit_agent_routing_decision(
    issue_number=123,
    project="my-project",
    board="dev",
    current_status="Ready",
    selected_agent="architect",
    reason="Status mapping"
)
```

### Performance Impact

**Problem:** Event emission slowing down system.

**Solutions:**
1. Event emission is async - ensure not blocking:
   ```python
   # Events use Redis pub/sub (non-blocking)
   # No await needed
   self.decision_events.emit_routing_decision(...)
   ```

2. Reduce event data size:
   ```python
   # Truncate large strings
   feedback_content=comment[:500]  # First 500 chars only
   ```

3. Monitor Redis memory:
   ```bash
   redis-cli INFO memory
   ```

### Events Not Persisting

**Problem:** Events visible in real-time but lost after refresh.

**Solution:** Check Redis Stream is writing:
```bash
# Check stream exists and has data
redis-cli
> XLEN orchestrator:event_stream
> XRANGE orchestrator:event_stream - + COUNT 10
```

If stream is empty, check:
1. ObservabilityManager stream configuration
2. Redis permissions
3. Maxlen/TTL settings

## Best Practices

### 1. Emit Before and After Critical Decisions

```python
# EMIT: Decision about to be made
self.decision_events.emit_status_progression(
    ...,
    success=None  # Not yet executed
)

try:
    # Execute
    result = self.move_issue(...)
    
    # EMIT: Success
    self.decision_events.emit_status_progression(
        ...,
        success=True
    )
except Exception as e:
    # EMIT: Failure
    self.decision_events.emit_status_progression(
        ...,
        success=False,
        error=str(e)
    )
    raise
```

### 2. Include Rich Context

```python
# GOOD - Rich context for debugging
self.decision_events.emit_agent_routing_decision(
    issue_number=123,
    project="my-project",
    board="dev",
    current_status="Ready",
    selected_agent="architect",
    reason="Status 'Ready' maps to 'Design' stage which uses architect agent",
    alternatives=["business_analyst", "product_manager"],  # Show what wasn't selected
    workspace_type="issues"
)
```

### 3. Use Consistent Terminology

```python
# Use standard terms for common concepts
trigger="agent_completion"  # Not "finished", "done", "completed"
workspace_type="issues"     # Not "github_issues", "issue_tracker"
feedback_source="comment"   # Not "github_comment", "user_comment"
```

### 4. Link Related Events

```python
# Use parent_event_id to correlate events
parent_id = f"cycle_{issue_number}_{iteration}"

self.decision_events.emit_review_cycle_decision(
    ...,
    additional_data={'parent_event_id': parent_id}
)
```

## Next Steps

1. Read the [Design Document](./ORCHESTRATOR_DECISION_OBSERVABILITY_DESIGN.md)
2. Review the [reference implementation](../monitoring/decision_events.py)
3. Add decision events to your service following patterns above
4. Test using provided test examples
5. Verify events appear in observability UI

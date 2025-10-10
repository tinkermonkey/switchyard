# Decision Observability - Quick Reference

## Setup (One-time per service)

```python
from monitoring.observability import get_observability_manager
from monitoring.decision_events import DecisionEventEmitter

class YourService:
    def __init__(self):
        self.obs = get_observability_manager()
        self.decision_events = DecisionEventEmitter(self.obs)
```

## When to Emit Events

| Situation | Event Method | Example |
|-----------|-------------|---------|
| **Selecting which agent to run** | `emit_agent_routing_decision()` | Status change → agent selection |
| **Detecting feedback** | `emit_feedback_detected()` | User comment detected |
| **Moving issue to new status** | `emit_status_progression()` | "Ready" → "In Progress" |
| **Starting review cycle** | `emit_review_cycle_decision(type='start')` | Maker-reviewer loop begins |
| **Selecting maker/reviewer** | `emit_review_cycle_decision(type='maker_selected')` | Choosing which agent in cycle |
| **Escalating to human** | `emit_review_cycle_decision(type='escalate')` | Max iterations reached |
| **Routing question** | `emit_conversational_question_routed()` | Question → specific agent |
| **Handling error** | `emit_error_decision()` | Error caught, recovery attempted |
| **Opening circuit breaker** | `emit_circuit_breaker_opened()` | Too many failures |
| **Routing workspace** | `emit_workspace_routing()` | Issues vs discussions choice |

## Quick Examples

### 1. Agent Routing
```python
def select_agent(self, issue_number, status, project, board):
    agent = self._get_agent_from_workflow(status)
    
    self.decision_events.emit_agent_routing_decision(
        issue_number=issue_number,
        project=project,
        board=board,
        current_status=status,
        selected_agent=agent,
        reason=f"Status '{status}' maps to {agent}"
    )
    return agent
```

### 2. Feedback Detection
```python
def check_feedback(self, issue_number, project, board):
    comment = self.get_latest_comment(issue_number)
    
    if self.is_actionable(comment):
        self.decision_events.emit_feedback_detected(
            issue_number=issue_number,
            project=project,
            board=board,
            feedback_source="comment",
            feedback_content=comment.body,
            target_agent="software_architect",
            action_taken="queue_agent_task"
        )
```

### 3. Status Progression
```python
def move_issue(self, issue_number, project, board, to_status):
    from_status = self.get_current_status(issue_number)
    
    try:
        # BEFORE
        self.decision_events.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status=from_status,
            to_status=to_status,
            trigger="agent_completion",
            success=None  # Not yet executed
        )
        
        # EXECUTE
        self.github.move_issue(issue_number, to_status)
        
        # AFTER - Success
        self.decision_events.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status=from_status,
            to_status=to_status,
            trigger="agent_completion",
            success=True
        )
    except Exception as e:
        # AFTER - Failure
        self.decision_events.emit_status_progression(
            issue_number=issue_number,
            project=project,
            board=board,
            from_status=from_status,
            to_status=to_status,
            trigger="agent_completion",
            success=False,
            error=str(e)
        )
        raise
```

### 4. Review Cycle
```python
async def start_review_cycle(self, issue_number, project, board):
    # START
    self.decision_events.emit_review_cycle_decision(
        issue_number=issue_number,
        project=project,
        board=board,
        cycle_iteration=0,
        decision_type='start',
        maker_agent="senior_software_engineer",
        reviewer_agent="code_reviewer",
        reason="Starting maker-checker review cycle"
    )
    
    # MAKER
    self.decision_events.emit_review_cycle_decision(
        issue_number=issue_number,
        project=project,
        board=board,
        cycle_iteration=1,
        decision_type='maker_selected',
        maker_agent="senior_software_engineer",
        reviewer_agent="code_reviewer",
        reason="Executing maker: senior_software_engineer"
    )
    
    # ... execute maker ...
    
    # REVIEWER
    self.decision_events.emit_review_cycle_decision(
        issue_number=issue_number,
        project=project,
        board=board,
        cycle_iteration=1,
        decision_type='reviewer_selected',
        maker_agent="senior_software_engineer",
        reviewer_agent="code_reviewer",
        reason="Executing reviewer: code_reviewer"
    )
    
    # ... execute reviewer ...
    
    # ESCALATE (if needed)
    if max_iterations_reached:
        self.decision_events.emit_review_cycle_decision(
            issue_number=issue_number,
            project=project,
            board=board,
            cycle_iteration=3,
            decision_type='escalate',
            maker_agent="senior_software_engineer",
            reviewer_agent="code_reviewer",
            reason="Max iterations (3) reached without approval",
            additional_data={'max_iterations': 3}
        )
```

### 5. Error Handling
```python
def execute_with_error_handling(self, operation):
    try:
        return operation()
    except DockerImageNotFoundError as e:
        # EMIT ERROR DECISION
        self.decision_events.emit_error_decision(
            error_type='docker_image_not_found',
            error_message=str(e),
            context={'operation': 'agent_execution'},
            recovery_action='queue_dev_environment_setup',
            success=True,  # Recovery action worked
            project=self.project
        )
        
        # Queue setup task
        self.queue_dev_env_setup()
        raise
        
    except Exception as e:
        # EMIT GENERIC ERROR
        self.decision_events.emit_error_decision(
            error_type=type(e).__name__,
            error_message=str(e),
            context={'operation': 'agent_execution'},
            recovery_action='fail_task',
            success=False,
            project=self.project
        )
        raise
```

### 6. Circuit Breaker
```python
def check_circuit_breaker(self, operation_name):
    circuit = self.circuit_breakers[operation_name]
    
    if circuit.should_open():
        self.decision_events.emit_circuit_breaker_opened(
            circuit_name=operation_name,
            failure_count=circuit.failure_count,
            threshold=circuit.threshold,
            last_error=str(circuit.last_error)
        )
        circuit.open()
```

## Event Schema Template

```python
{
    "timestamp": "2025-10-09T12:34:56Z",
    "event_type": "<event_type>",
    "agent": "orchestrator",
    "task_id": "<unique_task_id>",
    "project": "<project_name>",
    "data": {
        "decision_category": "<category>",  # routing, progression, error_handling, etc.
        "issue_number": 123,
        "board": "<board_name>",
        "inputs": {
            # What was considered in making decision
        },
        "decision": {
            # What was decided
        },
        "reason": "Human-readable explanation",
        "reasoning_data": {
            # Structured details about reasoning
        }
    }
}
```

## Decision Categories

- `routing` - Agent selection, workspace routing
- `progression` - Status changes, stage transitions
- `review_cycle` - Maker/reviewer routing
- `conversational_loop` - Question routing
- `feedback` - Feedback detection and handling
- `error_handling` - Errors, circuit breakers, retries
- `task_management` - Task queue operations

## Testing Your Events

```python
# Unit test
def test_routing_decision():
    mock_obs = Mock()
    decision_events = DecisionEventEmitter(mock_obs)
    
    decision_events.emit_agent_routing_decision(
        issue_number=123,
        project="test",
        board="dev",
        current_status="Ready",
        selected_agent="architect",
        reason="Test"
    )
    
    assert mock_obs.emit.called
    event_type = mock_obs.emit.call_args[0][0]
    assert event_type == EventType.AGENT_ROUTING_DECISION
```

## Common Mistakes

### ❌ DON'T
```python
# Missing required fields
self.decision_events.emit_agent_routing_decision(
    issue_number=123,
    selected_agent="architect"
    # Missing: project, board, current_status, reason
)

# Including sensitive data
self.decision_events.emit_error_decision(
    error_message=f"API key {api_key} is invalid"  # ❌ Don't include secrets
)

# Not truncating large content
self.decision_events.emit_feedback_detected(
    feedback_content=entire_issue_body  # ❌ Could be megabytes
)
```

### ✅ DO
```python
# Include all required fields
self.decision_events.emit_agent_routing_decision(
    issue_number=123,
    project="my-project",
    board="dev",
    current_status="Ready",
    selected_agent="architect",
    reason="Status maps to architecture stage"
)

# Sanitize error messages
self.decision_events.emit_error_decision(
    error_message="API authentication failed"  # ✅ Generic
)

# Truncate large content
self.decision_events.emit_feedback_detected(
    feedback_content=comment[:500]  # ✅ First 500 chars
)
```

## Performance Tips

1. **Non-blocking**: Event emission is async via Redis pub/sub
2. **No await needed**: Just call the emit method
3. **Fail gracefully**: Emission errors don't crash your code
4. **Auto-trimmed**: Events auto-expire after 2 hours
5. **Efficient**: <1ms overhead per event

## Viewing Events

Events appear in:
- `web_ui/observability.html` - Real-time dashboard
- Redis CLI: `redis-cli XRANGE orchestrator:event_stream - +`
- WebSocket: `ws://localhost:5001/`

## Get Help

- **Design**: `docs/ORCHESTRATOR_DECISION_OBSERVABILITY_DESIGN.md`
- **Guide**: `docs/DECISION_OBSERVABILITY_IMPLEMENTATION_GUIDE.md`
- **Code**: `monitoring/decision_events.py`
- **Summary**: `docs/DECISION_OBSERVABILITY_SUMMARY.md`

---

**Remember**: Emit events at every decision point. Future you will thank you when debugging!

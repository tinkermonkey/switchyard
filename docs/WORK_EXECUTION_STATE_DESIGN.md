# Work Execution State Tracker Design

## Problem Statement

The current orchestrator uses simple comment-based deduplication: once an agent posts a signature comment, it refuses to reprocess that issue. This causes problems when:

1. **Status changes back to a previous column** (manual rework needed)
2. **Previous work failed or was blocked** (should retry)
3. **Environment was fixed** (testing should restart)
4. **Automatic progression triggers work** (should skip to prevent double-triggering)

### Issue 101 Scenario
- Testing agent ran but failed due to environment issues
- Dev environment setup agent fixed the environment
- User moved ticket back to Testing status
- System refuses to restart testing (signature comment exists)

## Current Logic Issues

```python
# services/github_integration.py:139-163
async def has_agent_processed_issue(self, issue_number: int, agent_name: str, ...):
    # Only checks if signature comment exists
    signature = f"_Processed by the {agent_name} agent_"
    for comment in comments:
        if signature in comment['body']:
            return True  # PROBLEM: Always returns true, no context
    return False
```

**Missing:**
- Work outcome tracking (success/failure/blocked)
- Status change detection (manual move vs auto-progression)
- Execution history per column
- Trigger source tracking

## Proposed Solution: Work Execution State Tracker

### Core Concepts

1. **Execution History**: Track all work attempts per issue/column
2. **Outcome Tracking**: Record success, failure, blocked, in_progress
3. **Status Changes**: Track when status changes with timestamps
4. **Trigger Sources**: Distinguish manual_move, auto_progression, webhook

### Data Model

```yaml
# state/execution_history/context-studio_issue_101.yaml
issue_number: 101
project_name: context-studio
board_name: SDLC
execution_history:
  - column: Testing
    agent: senior_qa_engineer
    timestamp: "2025-10-05T10:30:00Z"
    outcome: failure
    trigger_source: manual_move
    error: "Environment not configured"

  - column: Dev Environment Setup
    agent: dev_environment_setup_agent
    timestamp: "2025-10-05T11:00:00Z"
    outcome: success
    trigger_source: manual_move

  - column: Testing
    agent: senior_qa_engineer
    timestamp: "2025-10-05T11:30:00Z"
    outcome: success
    trigger_source: manual_move  # User moved back to Testing

status_changes:
  - from: Testing
    to: Dev Environment Setup
    timestamp: "2025-10-05T10:45:00Z"
    trigger: manual

  - from: Dev Environment Setup
    to: Testing
    timestamp: "2025-10-05T11:15:00Z"
    trigger: manual

current_status: Testing
last_updated: "2025-10-05T11:30:00Z"
```

### Decision Logic

```python
def should_execute_work(issue_number, column, agent, trigger_source):
    """Determine if agent should execute work in this column"""

    history = load_execution_history(issue_number)
    column_executions = [e for e in history if e.column == column and e.agent == agent]
    last_execution = column_executions[-1] if column_executions else None
    status_changes = get_status_changes_to_column(history, column)
    last_status_change = status_changes[-1] if status_changes else None

    # Case 1: First time in this column
    if not last_execution:
        return True, "first_execution"

    # Case 2: Status changed back to this column after previous execution
    # (indicates manual rework needed)
    if last_status_change and last_status_change.timestamp > last_execution.timestamp:
        return True, "manual_rework_detected"

    # Case 3: Previous execution failed or was blocked
    if last_execution.outcome in ['failure', 'blocked']:
        return True, "retry_after_failure"

    # Case 4: Automatic progression triggering after successful execution
    # (prevent double-triggering)
    if (trigger_source == 'pipeline_progression' and
        last_execution.outcome == 'success' and
        not (last_status_change and last_status_change.timestamp > last_execution.timestamp)):
        return False, "skip_auto_progression_after_success"

    # Case 5: Work is already in progress
    if last_execution.outcome == 'in_progress':
        return False, "work_already_in_progress"

    # Case 6: Successful execution, no status change, manual trigger
    # (allow explicit retry)
    if trigger_source in ['manual_move', 'webhook']:
        return True, "explicit_manual_trigger"

    # Default: skip
    return False, "already_processed_successfully"
```

### Trigger Source Detection

```python
def detect_trigger_source(change_event):
    """Detect what caused this status change"""

    # Check if this is from pipeline progression
    if change_event.get('trigger') == 'pipeline_progression':
        return 'pipeline_progression'

    # Check if this is a webhook event
    if change_event.get('source') == 'webhook':
        return 'webhook'

    # Check if status changed (implies manual move)
    if has_status_changed_since_last_execution():
        return 'manual_move'

    # Default to manual
    return 'manual'
```

## Integration Points

### 1. Update `has_agent_processed_issue()`

```python
# services/github_integration.py
async def should_process_issue(self, issue_number, agent, column, trigger_source):
    """Enhanced logic using execution state"""
    from services.work_execution_state import work_execution_tracker

    should_execute, reason = work_execution_tracker.should_execute_work(
        issue_number=issue_number,
        column=column,
        agent=agent,
        trigger_source=trigger_source
    )

    logger.info(f"Should execute {agent} on issue #{issue_number}: {should_execute} ({reason})")
    return should_execute
```

### 2. Record Execution Start

```python
# services/project_monitor.py - when starting work
work_execution_tracker.record_execution_start(
    issue_number=issue_number,
    column=status,
    agent=agent,
    trigger_source=trigger_source,
    project_name=project_name
)
```

### 3. Record Execution Outcome

```python
# services/agent_executor.py - after agent completes
work_execution_tracker.record_execution_outcome(
    issue_number=issue_number,
    column=column,
    agent=agent,
    outcome='success',  # or 'failure', 'blocked'
    error=error_message if failed else None
)
```

### 4. Track Status Changes

```python
# services/project_monitor.py - in detect_changes()
if change['type'] == 'status_changed':
    work_execution_tracker.record_status_change(
        issue_number=change['issue_number'],
        from_status=change['old_status'],
        to_status=change['new_status'],
        trigger='manual',  # or 'auto' if from pipeline_progression
        project_name=project_name
    )
```

## Benefits

1. **Enables Rework**: Manual status changes trigger re-execution
2. **Prevents Double-Triggering**: Auto-progression skips if already successful
3. **Retry on Failure**: Failed work automatically retries
4. **Audit Trail**: Complete execution history for debugging
5. **Elegant Logic**: Clear decision tree, easy to understand and maintain

## Migration Strategy

1. **Phase 1**: Implement WorkExecutionStateTracker service
2. **Phase 2**: Update project_monitor.py to track status changes
3. **Phase 3**: Update deduplication logic to use new tracker
4. **Phase 4**: Update agent_executor.py to record outcomes
5. **Phase 5**: Backfill existing issues (optional)

## Testing Scenarios

1. **Issue 101 Scenario**:
   - Testing fails → move to Dev Setup → fix environment → move back to Testing
   - Should execute testing again ✓

2. **Auto-Progression Prevention**:
   - Testing succeeds → auto-moves to Done → Done column has no agent
   - Should not trigger anything ✓

3. **Explicit Retry**:
   - Testing succeeds → user manually moves back to Testing
   - Should execute testing again ✓

4. **In-Progress Protection**:
   - Testing in progress → user tries to move again
   - Should skip (already running) ✓

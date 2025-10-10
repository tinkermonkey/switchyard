

# Decision Observability Operator Guide

> **Complete guide for operators monitoring and troubleshooting the orchestrator using the decision observability dashboard**

## Table of Contents

1. [Getting Started](#getting-started)
2. [Dashboard Overview](#dashboard-overview)
3. [Event Categories](#event-categories)
4. [Navigation and Filtering](#navigation-and-filtering)
5. [Understanding Decision Events](#understanding-decision-events)
6. [Pattern Recognition](#pattern-recognition)
7. [Troubleshooting Guide](#troubleshooting-guide)
8. [Common Scenarios](#common-scenarios)
9. [Best Practices](#best-practices)
10. [Advanced Features](#advanced-features)

---

## Getting Started

### Accessing the Dashboard

1. **Start the Observability Server**:
   ```bash
   cd /path/to/orchestrator
   python -m services.observability_server
   ```
   Server will start on `http://localhost:5001`

2. **Open Dashboard**:
   - Navigate to `http://localhost:5001/observability.html`
   - Dashboard loads automatically and connects via WebSocket

3. **Verify Connection**:
   - Look for "Connected" status indicator (green)
   - You should see events appearing in real-time

### Dashboard Components

```
┌─────────────────────────────────────────────────────────────────┐
│ Decision Observability Dashboard                    [Connected] │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  Filters: [All Categories ▼] [All Event Types ▼] [Clear]       │
│                                                                   │
│  Timeline: [━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━]               │
│                                                                   │
│  Events (Live):                                                   │
│  ┌─────────────────────────────────────────────────────────┐   │
│  │ ⚡ AGENT_ROUTING_DECISION          12:34:56            │   │
│  │ Issue #123 → software_architect                         │   │
│  │ Reason: Status 'Ready' maps to architecture stage      │   │
│  │ [View Details]                                          │   │
│  ├─────────────────────────────────────────────────────────┤   │
│  │ 💬 FEEDBACK_DETECTED               12:35:12            │   │
│  │ Issue #123 → senior_software_engineer                  │   │
│  │ Action: queue_agent_task                               │   │
│  │ [View Details]                                          │   │
│  └─────────────────────────────────────────────────────────┘   │
│                                                                   │
│  Statistics: [Show/Hide]                                         │
│  - Events: 1,234 | Routing: 456 | Feedback: 89 | Errors: 12    │
└─────────────────────────────────────────────────────────────────┘
```

---

## Dashboard Overview

### Real-Time Event Stream

The main area shows **live events** as they occur:
- Most recent events at the **top**
- Color-coded by event category
- Timestamp shows when event occurred
- Click any event to see full details

### Event Colors

| Color | Category | Example Events |
|-------|----------|---------------|
| 🟦 Blue | Routing | Agent routing, workspace routing |
| 🟩 Green | Progression | Status changes, stage transitions |
| 🟨 Yellow | Feedback | Feedback detected, listening started/stopped |
| 🟪 Purple | Review Cycle | Cycle started, iterations, escalations |
| 🟥 Red | Error | Errors encountered, circuit breaker |
| 🟧 Orange | Task Management | Task queued, dequeued, cancelled |

### Timeline View

Visual representation of event frequency over time:
- **Spikes** indicate high activity periods
- **Gaps** may indicate issues or quiet periods
- Hover to see event count at specific time

---

## Event Categories

### 1. Routing Events

**What they show**: Which agent the orchestrator selects for a task

**Key event types**:
- `AGENT_ROUTING_DECISION` - Main routing decision with reasoning
- `AGENT_SELECTED` - Simplified selection event
- `WORKSPACE_ROUTING_DECISION` - Issues vs discussions choice

**Example event**:
```json
{
  "event_type": "agent_routing_decision",
  "issue_number": 123,
  "selected_agent": "software_architect",
  "reason": "Status 'Ready' maps to 'Design' stage",
  "alternatives": ["business_analyst", "product_manager"]
}
```

**What to look for**:
- ✅ **Good**: Clear reason, agent matches status
- ⚠️ **Warning**: `selected_agent: null` (no agent configured)
- 🚨 **Problem**: Wrong agent repeatedly selected

### 2. Feedback Events

**What they show**: How orchestrator detects and responds to human feedback

**Key event types**:
- `FEEDBACK_DETECTED` - Feedback found and action taken
- `FEEDBACK_LISTENING_STARTED` - Start monitoring for feedback
- `FEEDBACK_LISTENING_STOPPED` - Stop monitoring
- `FEEDBACK_IGNORED` - Feedback not actionable

**Example event**:
```json
{
  "event_type": "feedback_detected",
  "issue_number": 456,
  "feedback_source": "comment",
  "target_agent": "senior_software_engineer",
  "action_taken": "queue_agent_task"
}
```

**What to look for**:
- ✅ **Good**: Feedback detected and acted upon
- ⚠️ **Warning**: Many `FEEDBACK_IGNORED` (may need tuning)
- 🚨 **Problem**: Feedback detected but no action taken

### 3. Status Progression Events

**What they show**: How issues move through pipeline stages

**Key event types**:
- `STATUS_PROGRESSION_STARTED` - Before attempting move
- `STATUS_PROGRESSION_COMPLETED` - Successfully moved
- `STATUS_PROGRESSION_FAILED` - Move failed
- `PIPELINE_STAGE_TRANSITION` - Stage change

**Example event**:
```json
{
  "event_type": "status_progression_completed",
  "issue_number": 789,
  "from_status": "Ready",
  "to_status": "In Progress",
  "trigger": "agent_completion",
  "success": true
}
```

**What to look for**:
- ✅ **Good**: Smooth progression, success=true
- ⚠️ **Warning**: Multiple retries before success
- 🚨 **Problem**: Repeated `STATUS_PROGRESSION_FAILED`

### 4. Review Cycle Events

**What they show**: Maker/reviewer iteration cycles

**Key event types**:
- `REVIEW_CYCLE_STARTED` - Cycle begins
- `REVIEW_CYCLE_ITERATION` - New iteration
- `REVIEW_CYCLE_MAKER_SELECTED` - Maker agent chosen
- `REVIEW_CYCLE_REVIEWER_SELECTED` - Reviewer agent chosen
- `REVIEW_CYCLE_ESCALATED` - Escalate to human
- `REVIEW_CYCLE_COMPLETED` - Cycle done

**Example event**:
```json
{
  "event_type": "review_cycle_started",
  "issue_number": 111,
  "maker_agent": "senior_software_engineer",
  "reviewer_agent": "code_reviewer",
  "cycle_iteration": 0
}
```

**What to look for**:
- ✅ **Good**: 1-2 iterations then complete
- ⚠️ **Warning**: 3+ iterations (refinement needed)
- 🚨 **Problem**: Escalation every time

### 5. Error Handling Events

**What they show**: How orchestrator handles and recovers from errors

**Key event types**:
- `ERROR_ENCOUNTERED` - Error occurred
- `ERROR_RECOVERED` - Successfully recovered
- `CIRCUIT_BREAKER_OPENED` - Too many failures
- `CIRCUIT_BREAKER_CLOSED` - Recovery complete
- `RETRY_ATTEMPTED` - Retry attempt

**Example event**:
```json
{
  "event_type": "error_recovered",
  "error_type": "APIRateLimitError",
  "recovery_action": "wait_and_retry",
  "success": true
}
```

**What to look for**:
- ✅ **Good**: Errors recovered automatically
- ⚠️ **Warning**: Multiple retries needed
- 🚨 **Problem**: Circuit breaker open, errors not recovering

### 6. Task Management Events

**What they show**: Task queue operations

**Key event types**:
- `TASK_QUEUED` - Task added to queue
- `TASK_DEQUEUED` - Task taken for execution
- `TASK_PRIORITY_CHANGED` - Priority updated
- `TASK_CANCELLED` - Task cancelled

**What to look for**:
- ✅ **Good**: Tasks queued and dequeued promptly
- ⚠️ **Warning**: Long gaps between queue and dequeue
- 🚨 **Problem**: Tasks queued but never dequeued

---

## Navigation and Filtering

### Filter by Category

Use the category dropdown to focus on specific decision types:

```
[All Categories ▼]
├─ All Categories
├─ Routing Decisions
├─ Feedback Detection
├─ Status Progression
├─ Review Cycles
├─ Error Handling
├─ Task Management
├─ Conversational Loops
└─ Workspace Routing
```

**Example use cases**:
- **Debugging routing**: Filter by "Routing Decisions"
- **Monitoring errors**: Filter by "Error Handling"
- **Review cycle analysis**: Filter by "Review Cycles"

### Filter by Event Type

For more granular filtering, select specific event types:

```
[All Event Types ▼]
├─ All Event Types
├─ AGENT_ROUTING_DECISION
├─ FEEDBACK_DETECTED
├─ STATUS_PROGRESSION_COMPLETED
├─ REVIEW_CYCLE_ESCALATED
└─ ... (all 32 event types)
```

### Filter by Issue

To track a specific issue:
1. Click on any event for that issue
2. Click "Show all events for issue #123"
3. See complete timeline for that issue

### Search

Use the search box to find:
- Issue numbers: `#123`
- Agent names: `software_architect`
- Error types: `DockerImageNotFoundError`
- Keywords: `escalate`, `retry`, `failed`

---

## Understanding Decision Events

### Anatomy of a Decision Event

Every decision event has this structure:

```json
{
  "timestamp": "2025-10-09T12:34:56.789Z",
  "event_id": "evt_1234567890",
  "event_type": "agent_routing_decision",
  "agent": "orchestrator",
  "task_id": "routing_test-project_123",
  "project": "test-project",
  "data": {
    "decision_category": "routing",
    "issue_number": 123,
    "board": "dev",
    "inputs": {
      "current_status": "Ready",
      "available_agents": ["architect", "analyst"]
    },
    "decision": {
      "selected_agent": "software_architect"
    },
    "reason": "Status 'Ready' maps to architecture stage",
    "reasoning_data": {
      "selection_method": "workflow_mapping",
      "alternatives_considered": ["business_analyst"]
    }
  }
}
```

**Key fields to understand**:

- **inputs**: What information was used to make the decision
- **decision**: What was decided
- **reason**: Human-readable explanation
- **reasoning_data**: Additional context (alternatives, methods, etc.)

### Reading Event Chains

Events often form chains showing complete flows:

**Example: Issue Routing Chain**
```
1. STATUS_PROGRESSION_COMPLETED
   └─> Issue moved to "Ready"
       
2. AGENT_ROUTING_DECISION
   └─> Selected: software_architect
   
3. WORKSPACE_ROUTING_DECISION
   └─> Workspace: issues
   
4. TASK_QUEUED
   └─> Task queued for architect
   
5. TASK_DEQUEUED
   └─> Task taken for execution
```

**How to read**: Follow timestamps to see complete flow from status change to execution.

---

## Pattern Recognition

### Healthy Patterns

#### 1. Normal Routing Flow
```
STATUS_PROGRESSION_COMPLETED
  → AGENT_ROUTING_DECISION (agent selected)
  → TASK_QUEUED
  → TASK_DEQUEUED
  → [Agent executes]
  → STATUS_PROGRESSION_COMPLETED (next stage)
```

**What it means**: Issue flowing smoothly through pipeline

#### 2. Successful Review Cycle
```
REVIEW_CYCLE_STARTED
  → REVIEW_CYCLE_ITERATION (1)
  → REVIEW_CYCLE_MAKER_SELECTED
  → REVIEW_CYCLE_REVIEWER_SELECTED
  → REVIEW_CYCLE_COMPLETED (approved)
```

**What it means**: Review approved on first iteration

#### 3. Successful Error Recovery
```
ERROR_ENCOUNTERED
  → RETRY_ATTEMPTED (1)
  → ERROR_RECOVERED (success)
```

**What it means**: Transient error recovered quickly

### Warning Patterns

#### 1. Multiple Routing Attempts
```
AGENT_ROUTING_DECISION (agent: null)
  → AGENT_ROUTING_DECISION (agent: null)
  → AGENT_ROUTING_DECISION (agent: null)
```

**What it means**: No agent configured for status
**Action**: Check workflow configuration

#### 2. Extended Review Cycles
```
REVIEW_CYCLE_STARTED
  → REVIEW_CYCLE_ITERATION (1)
  → REVIEW_CYCLE_ITERATION (2)
  → REVIEW_CYCLE_ITERATION (3)
  → REVIEW_CYCLE_ESCALATED
```

**What it means**: Agents not reaching consensus
**Action**: Review agent prompts or requirements

#### 3. Frequent Retries
```
ERROR_ENCOUNTERED
  → RETRY_ATTEMPTED (1)
  → RETRY_ATTEMPTED (2)
  → RETRY_ATTEMPTED (3)
  → ERROR_RECOVERED
```

**What it means**: Persistent but recoverable issue
**Action**: Investigate root cause (API limits, network issues)

### Problem Patterns

#### 1. Routing Failures
```
STATUS_PROGRESSION_COMPLETED
  → AGENT_ROUTING_DECISION (agent: null)
  → STATUS_PROGRESSION_FAILED (no agent)
```

**What it means**: Issue stuck - no agent to handle it
**Action**: Configure agent for this status immediately

#### 2. Circuit Breaker Tripped
```
ERROR_ENCOUNTERED
ERROR_ENCOUNTERED
ERROR_ENCOUNTERED
  → CIRCUIT_BREAKER_OPENED
  → ERROR_ENCOUNTERED (rejected)
  → ERROR_ENCOUNTERED (rejected)
```

**What it means**: System protection engaged due to failures
**Action**: Fix underlying issue before circuit closes

#### 3. Feedback Not Acted Upon
```
FEEDBACK_DETECTED
  → [No routing decision]
  → [No task queued]
```

**What it means**: Feedback detected but not processed
**Action**: Check feedback handling configuration

---

## Troubleshooting Guide

### Problem: Agent Not Running

**Symptoms**:
- Issue stuck in status
- No `AGENT_ROUTING_DECISION` event
- Or: `AGENT_ROUTING_DECISION` shows `agent: null`

**Diagnosis**:
1. Filter by "Routing Decisions"
2. Find event for your issue
3. Check `selected_agent` field

**Solutions**:
- **If `agent: null`**: No agent configured for this status
  - Action: Update workflow config to add agent
- **If agent selected but not running**: Task queue issue
  - Action: Check task queue logs

### Problem: Review Cycle Not Completing

**Symptoms**:
- Multiple `REVIEW_CYCLE_ITERATION` events
- Eventually `REVIEW_CYCLE_ESCALATED`

**Diagnosis**:
1. Filter by "Review Cycles"
2. Find your issue's cycle events
3. Count iterations
4. Check escalation reason

**Solutions**:
- **If escalated every time**: Agents too strict
  - Action: Adjust reviewer acceptance criteria
- **If specific issues escalate**: Requirements unclear
  - Action: Improve issue description
- **If random escalations**: Intermittent failures
  - Action: Check agent execution logs

### Problem: High Error Rate

**Symptoms**:
- Many `ERROR_ENCOUNTERED` events
- `CIRCUIT_BREAKER_OPENED` events
- Tasks failing

**Diagnosis**:
1. Filter by "Error Handling"
2. Group by `error_type`
3. Identify most common error

**Solutions**:

**Common Error Types**:

| Error Type | Likely Cause | Solution |
|------------|--------------|----------|
| `APIRateLimitError` | GitHub API limits | Reduce polling frequency |
| `DockerImageNotFoundError` | Dev env not built | Run dev environment setup |
| `ConnectionTimeout` | Network issues | Check network connectivity |
| `AuthenticationError` | Token expired | Refresh GitHub token |

### Problem: Feedback Not Being Detected

**Symptoms**:
- Human comments not triggering agents
- No `FEEDBACK_DETECTED` events

**Diagnosis**:
1. Filter by "Feedback Detection"
2. Check if `FEEDBACK_LISTENING_STARTED` present
3. Check if `FEEDBACK_IGNORED` events

**Solutions**:
- **If no listening events**: Feedback monitoring not started
  - Action: Check ProjectMonitor configuration
- **If many ignored events**: Feedback not matching patterns
  - Action: Review feedback detection patterns
- **If detected but no action**: Routing configuration issue
  - Action: Check agent routing for feedback

### Problem: Status Progression Failures

**Symptoms**:
- `STATUS_PROGRESSION_FAILED` events
- Issues not moving through pipeline

**Diagnosis**:
1. Filter by "Status Progression"
2. Find failed events
3. Check `error` field for reason

**Solutions**:

| Error Message | Solution |
|---------------|----------|
| "GitHub API rate limit" | Wait for rate limit reset |
| "Issue not found" | Check issue number and repository |
| "Invalid status" | Check project board configuration |
| "Permission denied" | Check GitHub App permissions |

---

## Common Scenarios

### Scenario 1: Monitoring a Specific Issue

**Goal**: Watch an issue flow through the complete pipeline

**Steps**:
1. Open dashboard
2. In search box, type issue number: `#123`
3. Press Enter
4. Watch events appear in real-time

**What to watch for**:
- Status progressions (issue moving forward)
- Agent routing decisions (right agents selected)
- Task queue events (tasks being processed)
- Any error events (problems occurring)

**Example healthy flow**:
```
12:00:00 STATUS_PROGRESSION_COMPLETED (→ Ready)
12:00:01 AGENT_ROUTING_DECISION (→ software_architect)
12:00:02 TASK_QUEUED (software_architect)
12:00:03 TASK_DEQUEUED (software_architect)
12:15:30 STATUS_PROGRESSION_COMPLETED (→ In Progress)
12:15:31 AGENT_ROUTING_DECISION (→ senior_software_engineer)
...
```

### Scenario 2: Debugging Stuck Issue

**Problem**: Issue hasn't progressed in hours

**Steps**:
1. Search for issue: `#456`
2. Check last event timestamp
3. Identify last event type

**If last event is**:
- `STATUS_PROGRESSION_COMPLETED`: Should see routing decision next
  - Missing? Check workflow config
- `AGENT_ROUTING_DECISION (agent: null)`: No agent configured
  - Action: Add agent to workflow
- `TASK_QUEUED`: Task waiting in queue
  - Action: Check if task queue is processing
- `REVIEW_CYCLE_ESCALATED`: Waiting for human
  - Action: Provide feedback on issue

### Scenario 3: Investigating Errors

**Problem**: Dashboard showing red error events

**Steps**:
1. Filter by "Error Handling"
2. Group by `error_type` (mentally or in notes)
3. Identify most frequent error
4. Click an error event to see full details
5. Check `recovery_action` and `success` fields

**Analysis**:
- **If `success: true`**: Error recovered automatically ✅
- **If `success: false`**: Error not recovered ⚠️
- **If circuit breaker opened**: Too many failures 🚨

**Action based on error type**:
- Rate limits → Reduce frequency
- Docker errors → Check Docker service
- Auth errors → Refresh tokens
- Network errors → Check connectivity

### Scenario 4: Analyzing Review Cycles

**Goal**: Understand why review cycles take multiple iterations

**Steps**:
1. Filter by "Review Cycles"
2. Find a completed cycle
3. Expand event details
4. Count iterations
5. Look for escalation events

**Healthy cycle**:
- 1-2 iterations
- Clean completion
- No escalation

**Needs attention**:
- 3+ iterations regularly
- Frequent escalations
- Same issue patterns

**Action**: Review agent prompts, improve requirements clarity

---

## Best Practices

### Daily Operations

1. **Morning Check**:
   - Open dashboard
   - Review last 24 hours
   - Check for any circuit breaker events
   - Verify no stuck issues

2. **During Active Development**:
   - Keep dashboard open
   - Watch for error patterns
   - Monitor review cycle iterations
   - Check feedback detection

3. **End of Day Review**:
   - Filter by errors
   - Document any recurring issues
   - Check if any issues need attention

### Using Filters Effectively

**Best practices**:
- Use **category filters** for high-level overview
- Use **event type filters** for specific debugging
- Use **search** for issue-specific investigation
- **Clear filters** between investigations to avoid confusion

### Event Interpretation

**Always check**:
1. **Timestamp** - When did it happen?
2. **Reason** - Why did it happen?
3. **Inputs** - What data was used?
4. **Decision** - What was decided?
5. **Success/Status** - Did it work?

**Context matters**:
- One error event → Likely transient
- Pattern of errors → Systematic issue
- Isolated failure → Investigate specific case
- Repeated failures → Configuration problem

### Documentation

**Keep notes on**:
- Recurring error patterns
- Issues that required manual intervention
- Configuration changes made
- Unusual event sequences

This helps identify trends and improve the system.

---

## Advanced Features

### Event History

Access historical events:
1. Click "Show History"
2. Select date range
3. Review past events

**Use cases**:
- Post-mortem analysis
- Pattern detection over time
- Performance trending

### Event Export

Export events for analysis:
1. Filter to desired events
2. Click "Export"
3. Choose format (JSON, CSV)
4. Save for external analysis

**Use cases**:
- Sharing with team
- Detailed analysis in spreadsheet
- Integration with other tools

### Metrics Dashboard

View aggregated metrics:
- Events per hour/day
- Success rates by type
- Average review cycle iterations
- Error rates by type
- Top issues by event count

### Alerts (Future Feature)

Set up alerts for:
- Circuit breaker opened
- High error rate
- Review cycle escalations
- Tasks stuck in queue

---

## Quick Reference

### Event Type Cheat Sheet

| Category | Event | Meaning |
|----------|-------|---------|
| Routing | `AGENT_ROUTING_DECISION` | Agent selected for issue |
| Routing | `WORKSPACE_ROUTING_DECISION` | Issues vs discussions |
| Feedback | `FEEDBACK_DETECTED` | Feedback found and acted on |
| Feedback | `FEEDBACK_IGNORED` | Feedback not actionable |
| Progression | `STATUS_PROGRESSION_COMPLETED` | Issue moved successfully |
| Progression | `STATUS_PROGRESSION_FAILED` | Issue move failed |
| Review | `REVIEW_CYCLE_STARTED` | Review cycle begins |
| Review | `REVIEW_CYCLE_ESCALATED` | Max iterations reached |
| Error | `ERROR_ENCOUNTERED` | Error occurred |
| Error | `ERROR_RECOVERED` | Error resolved |
| Error | `CIRCUIT_BREAKER_OPENED` | Protection engaged |
| Task | `TASK_QUEUED` | Task added to queue |
| Task | `TASK_DEQUEUED` | Task taken for execution |

### Common Filters

```
// Show only errors
Category: Error Handling

// Show routing decisions
Category: Routing Decisions

// Show specific issue
Search: #123

// Show failures only
Event Type: STATUS_PROGRESSION_FAILED
Event Type: ERROR_ENCOUNTERED

// Show review cycles
Category: Review Cycles
```

### Keyboard Shortcuts

- `Ctrl/Cmd + F` - Focus search
- `Ctrl/Cmd + R` - Refresh
- `Esc` - Clear filters
- `Space` - Pause/resume auto-scroll

---

## Getting Help

### Common Questions

**Q: Events not showing up**
A: Check WebSocket connection (green "Connected" indicator)

**Q: Too many events to follow**
A: Use category filters to focus on what matters

**Q: Don't understand an event**
A: Click "View Details" for full event data and context

**Q: Need to share an event**
A: Click "Copy Event Link" to share specific event

### Resources

- **Design Doc**: `/docs/ORCHESTRATOR_DECISION_OBSERVABILITY_DESIGN.md`
- **Implementation Guide**: `/docs/DECISION_OBSERVABILITY_IMPLEMENTATION_GUIDE.md`
- **Architecture**: `/docs/DECISION_OBSERVABILITY_ARCHITECTURE_DIAGRAM.md`

### Support

For issues or questions:
1. Check troubleshooting guide above
2. Review event details for error messages
3. Check orchestrator logs for more context
4. Contact development team with event IDs

---

## Summary

The Decision Observability Dashboard gives you **complete visibility** into every decision the orchestrator makes:

✅ **See**: What decisions are being made
✅ **Understand**: Why decisions were made
✅ **Debug**: Problems quickly with event trails
✅ **Monitor**: System health in real-time
✅ **Improve**: Identify patterns and optimize

**Remember**: Every decision is captured. If you see unexpected behavior, the events will show you exactly what happened and why.

**Happy monitoring! 🎯**

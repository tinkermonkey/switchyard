# Escalation and Human Feedback Flow

## Key Finding

**The two services do NOT interact during escalation.** Review escalation now uses project_monitor for non-blocking periodic polling, while conversational loops have their own built-in polling.

## Review Cycle Escalation (Internal Handling)

### When Escalation Happens

The `ReviewCycleManager` escalates in two scenarios:

1. **Blocking Issues** - Reviewer marks review as BLOCKED (critical issues found)
2. **Max Iterations** - Cycle reaches max iterations without approval (typically 3)

### Escalation Flow (Non-Blocking)

```
┌─────────────────────────────────────────────────────────────┐
│  ReviewCycleManager (services/review_cycle.py)              │
│                                                              │
│  1. Reviewer finds BLOCKING issues                          │
│     ↓                                                        │
│  2. _escalate_blocked() posts comment to discussion:        │
│     "🚫 Review Blocked - Human Review Required"             │
│     "Please post a comment with your guidance..."           │
│     "The orchestrator will detect your response..."         │
│     ↓                                                        │
│  3. Sets cycle_state.status = 'awaiting_human_feedback'     │
│     Saves state to disk for crash recovery                  │
│     ↓                                                        │
│  4. RETURNS immediately (non-blocking!)                     │
│     Issue stays in current column with BLOCKED status       │
│                                                              │
└─────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────┐
│  ProjectMonitor (services/project_monitor.py)               │
│                                                              │
│  1. Every poll cycle (default 30s):                         │
│     - check_escalated_review_cycles()                       │
│     - Load active cycles from disk                          │
│     ↓                                                        │
│  2. For each cycle with status='awaiting_human_feedback':   │
│     - Query discussion for new comments                     │
│     - Check if any comment is from human (not bot)          │
│     - Check if created after escalation_time                │
│     ↓                                                        │
│  3. When human feedback detected:                           │
│     - Call resume_review_cycle_with_feedback()              │
│     ↓                                                        │
│  4. Resume review cycle:                                    │
│     - Re-invoke REVIEWER agent with:                        │
│       - review_cycle.post_human_feedback = True             │
│       - review_cycle.human_feedback = <feedback text>       │
│     - Reviewer updates review based on human input          │
│     - Continue maker-checker cycle if changes needed        │
│     - Or complete if approved/still blocked                 │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Key Methods

**ReviewCycleManager** (`services/review_cycle.py`):
- `_escalate_blocked()` - Post escalation comment, set state, return (non-blocking)
- `resume_review_cycle_with_feedback()` - Resume cycle when feedback is detected
- `resume_active_cycles()` - On startup, note cycles awaiting feedback

**ProjectMonitor** (`services/project_monitor.py`):
- `check_escalated_review_cycles()` - Poll for feedback every 30s, never times out
- Called in main monitoring loop alongside other checks

### Code Locations

**Escalation**: `services/review_cycle.py:748-764` (`_escalate_blocked` + return)
**Resume**: `services/review_cycle.py:408-575` (`resume_review_cycle_with_feedback`)
**Polling**: `services/project_monitor.py:1675-1778` (`check_escalated_review_cycles`)

## Human Feedback Loop (Separate Service)

### When It's Used

The `HumanFeedbackLoopExecutor` is used for workflow columns configured as `type: conversational`:

```yaml
- name: "Idea Research"
  type: conversational
  agent: idea_researcher
  feedback_timeout: 86400  # 24 hours
```

### Feedback Flow

```
┌─────────────────────────────────────────────────────────────┐
│  HumanFeedbackLoopExecutor (services/human_feedback_loop.py)│
│                                                              │
│  1. Agent produces initial output                           │
│     ↓                                                        │
│  2. _conversational_loop() POLLING:                         │
│     - Polls discussion for human comments                   │
│     - Much longer timeout (24 hours default)                │
│     - No concept of "escalation" - just waiting             │
│     ↓                                                        │
│  3. Human posts feedback/questions                          │
│     ↓                                                        │
│  4. Polling detects feedback                                │
│     ↓                                                        │
│  5. Re-invokes SAME agent with:                             │
│     - Previous output in context                            │
│     - Human feedback text                                   │
│     ↓                                                        │
│  6. Agent responds to human feedback                        │
│     ↓                                                        │
│  7. Loop back to step 2 (continue monitoring)               │
│     ↓                                                        │
│  8. Timeout reached → Consider complete                     │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

### Key Methods

- `start_loop()` - Start new conversational loop
- `_conversational_loop()` - **Separate polling loop** (configurable timeout)
- `_get_human_feedback_since_last_agent()` - Poll for new comments

### Code Location

**File**: `services/human_feedback_loop.py`
**Lines**:
- Main loop: 118-195 (`_conversational_loop`)
- Polling: 266-340 (`_get_human_feedback_since_last_agent`)

## Comparison: Two Independent Polling Mechanisms

| Aspect | ReviewCycle Escalation | HumanFeedbackLoop |
|--------|------------------------|-------------------|
| **Polling Location** | `project_monitor.check_escalated_review_cycles()` | `_conversational_loop()` (built-in) |
| **Poll Interval** | 30 seconds (project_monitor poll) | 30 seconds (built-in loop) |
| **Timeout** | **Never** - polls indefinitely | 1 hour per round (configurable) |
| **Blocking** | **Non-blocking** - returns immediately | **Non-blocking** - runs in thread |
| **State Persistence** | Yes (saves to disk) | No (in-memory) |
| **Resume After Restart** | Yes | No |
| **What Triggers** | BLOCKED or max iterations | Column type = conversational |
| **Who Responds** | Reviewer agent (with human input) | Same agent (conversational) |
| **GraphQL Query** | Direct query in `check_escalated_review_cycles` | Direct query in `_get_human_feedback_since_last_agent` |

## Why Two Separate Mechanisms?

1. **Different Use Cases**:
   - Review escalation is an **exception case** (something went wrong)
   - Conversational loops are the **normal flow** (expected collaboration)

2. **Different Requirements**:
   - Review escalation needs **crash recovery** (state persistence)
   - Conversational loops are **ephemeral** (restart = new session)

3. **Different Timeout Semantics**:
   - Review escalation **never times out** - waits forever for human intervention
   - Conversational loops **timeout and complete** - silence means conversation done

4. **Different Context**:
   - Review escalation passes feedback to **reviewer agent** to update review
   - Conversational loops pass feedback to **same agent** to continue conversation

## No Cross-Service Communication

**Important**: When a review cycle escalates, it does **NOT**:
- Call `HumanFeedbackLoopExecutor`
- Start a conversational loop
- Hand off to the other service

It saves state and returns. ProjectMonitor handles detection and resumption.

## Future Consolidation?

While both services poll for human comments, consolidation is **not recommended** because:

1. **State management differs** - Review cycles need persistence, conversational loops don't
2. **Recovery semantics differ** - Review cycles resume mid-cycle, conversational loops start fresh
3. **Agent invocation differs** - Review cycles call reviewer with feedback, conversational loops call same agent
4. **Timeout semantics differ** - Review cycles are urgent, conversational loops are exploratory

The current separation provides clarity and maintainability.

## Configuration Examples

### Review Column with Escalation

```yaml
columns:
  - name: "Requirements Review"
    type: review
    maker_agent: business_analyst
    reviewer_agent: requirements_reviewer
    max_iterations: 3
    escalate_on_blocked: true  # Enable escalation
    workspace: discussions
```

When this escalates, `ReviewCycleManager._wait_for_human_feedback()` handles it.

### Conversational Column (No Escalation)

```yaml
columns:
  - name: "Idea Research"
    type: conversational
    agent: idea_researcher
    workspace: discussions
    feedback_timeout: 86400  # 24 hours
```

This uses `HumanFeedbackLoopExecutor._conversational_loop()` from the start - no escalation concept.

## Debugging Tips

### To Debug Review Escalation

1. Check cycle state: `state/projects/<project>/review_cycles/active_cycles.yaml`
2. Look for `status: awaiting_human_feedback`
3. Check logs for:
   - `"Review cycle escalated..."` - Escalation occurred
   - `"Checking for human feedback on escalated cycle..."` - ProjectMonitor polling
   - `"Human feedback detected..."` - Feedback found, resuming
4. **No timeout** - Polls indefinitely until feedback received

### To Debug Conversational Loop

1. Check logs for: `"Starting human feedback loop..."`, `"Polling for feedback..."`
2. No state file (in-memory only)
3. Timeout: Configured in column `feedback_timeout` (default 24 hours)

## Summary

- **ReviewCycleManager** escalates and saves state, returns immediately (non-blocking)
- **ProjectMonitor** polls for feedback every 30s via `check_escalated_review_cycles()`
- **No timeout** - escalated cycles wait indefinitely for human intervention
- **HumanFeedbackLoopExecutor** has built-in polling with 1-hour timeout per round
- **They do NOT interact** - each handles human feedback independently
- **Different use cases** - escalation (exception) vs collaboration (normal flow)
- **Different semantics** - indefinite wait vs timeout-and-complete

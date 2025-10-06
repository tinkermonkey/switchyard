# Work Execution State - Integration Analysis

## Architecture Overview

The Work Execution State Tracker integrates with multiple execution control services through a **centralized execution funnel** design.

```
┌─────────────────────────────────────────────────────────┐
│                  Execution Entry Points                 │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌──────────────────┐  ┌──────────────────┐            │
│  │ Project Monitor  │  │ Review Cycle     │            │
│  │ (Status Changes) │  │ Executor         │            │
│  └────────┬─────────┘  └────────┬─────────┘            │
│           │                      │                      │
│           │  ┌──────────────────┐│                      │
│           │  │ Human Feedback   ││                      │
│           │  │ Loop Executor    ││                      │
│           │  └────────┬─────────┘│                      │
│           │           │          │                      │
│           ▼           ▼          ▼                      │
│      ┌────────────────────────────────┐                 │
│      │   Work Execution State         │                 │
│      │   Tracker (Deduplication)      │                 │
│      └────────────┬───────────────────┘                 │
│                   │                                     │
│                   ▼                                     │
│      ┌────────────────────────────┐                     │
│      │   Centralized Agent        │                     │
│      │   Executor                 │                     │
│      └────────────┬───────────────┘                     │
│                   │                                     │
│                   ▼                                     │
│      ┌────────────────────────────┐                     │
│      │   Work Execution State     │                     │
│      │   Tracker (Outcomes)       │                     │
│      └────────────────────────────┘                     │
│                                                         │
└─────────────────────────────────────────────────────────┘
```

## Execution Control Services

### 1. Project Monitor (`services/project_monitor.py`)

**Role**: Detects GitHub status changes and routes work to appropriate handlers

**Integration with Work State Tracker**:

```python
# Entry Point: trigger_agent_for_status() - Line 563-594
# BEFORE routing to column-specific handlers:

should_execute, reason = work_execution_tracker.should_execute_work(
    issue_number=issue_number,
    column=status,
    agent=agent,
    trigger_source='manual',
    project_name=project_name
)

if not should_execute:
    return None  # Skip execution

# Records status changes - Line 1553-1561
work_execution_tracker.record_status_change(
    issue_number=change['issue_number'],
    from_status=change['old_status'],
    to_status=change['new_status'],
    trigger='manual',
    project_name=project_name
)

# Records execution start - Line 895-903
work_execution_tracker.record_execution_start(
    issue_number=issue_number,
    column=status,
    agent=agent,
    trigger_source='manual',
    project_name=project_name
)
```

**Flow Control**:
- ✅ Checks `should_execute_work()` BEFORE routing to column handlers
- ✅ Records status changes immediately when detected
- ✅ Records execution start when task is queued
- ✅ Routes to ReviewCycleExecutor, HumanFeedbackLoopExecutor, or standard task queue

**Special Cases**:
- **Review columns** (`type='review'`): Bypasses simple deduplication, uses ReviewCycleExecutor
- **Conversational columns** (`type='conversational'`): Uses HumanFeedbackLoopExecutor
- **Standard columns**: Uses task queue → AgentExecutor

### 2. Review Cycle Executor (`services/review_cycle.py`)

**Role**: Manages synchronous maker-checker review loops with multiple iterations

**Integration with Work State Tracker**:

```python
# Uses centralized AgentExecutor - Line 1762-1779
async def _execute_agent_directly(self, agent_name, task_context, project_name):
    from services.agent_executor import get_agent_executor

    executor = get_agent_executor()
    return await executor.execute_agent(
        agent_name=agent_name,
        project_name=project_name,
        task_context=task_context,
        task_id_prefix="review_cycle"  # Distinguishes review cycle executions
    )
```

**State Management**:
- **Own State**: Tracks review iterations, maker/reviewer outputs, escalation in `ReviewCycleState`
- **Work State Integration**: Automatically inherits outcome tracking from AgentExecutor

**Flow**:
1. Review cycle starts → Project Monitor checks `should_execute_work()`
2. If allowed, ReviewCycleExecutor takes over
3. Executes maker → AgentExecutor records outcome
4. Executes reviewer → AgentExecutor records outcome
5. Repeats until approved or max iterations
6. Escalates to human or progresses

**Key Point**: Review cycles have their own iteration logic (ReviewCycleState) but still record final outcomes through AgentExecutor.

### 3. Human Feedback Loop Executor (`services/human_feedback_loop.py`)

**Role**: Manages conversational loops with human feedback monitoring

**Integration with Work State Tracker**:

```python
# Uses centralized AgentExecutor - Line 297-305
async def _execute_agent(self, state, column, issue_data, ...):
    from services.agent_executor import get_agent_executor

    executor = get_agent_executor()
    result = await executor.execute_agent(
        agent_name=state.agent,
        project_name=state.project_name,
        task_context=context,
        task_id_prefix="conversational"  # Distinguishes conversational executions
    )
```

**State Management**:
- **Own State**: Tracks conversation history, Claude session IDs, iteration count in `HumanFeedbackState`
- **Persistence**: Uses `ConversationalSessionStateManager` for session continuity
- **Work State Integration**: Automatically inherits outcome tracking from AgentExecutor

**Flow**:
1. Conversational column detected → Project Monitor checks `should_execute_work()`
2. If allowed, HumanFeedbackLoopExecutor takes over
3. Executes agent → AgentExecutor records outcome
4. Monitors for feedback → Re-executes if needed → AgentExecutor records each outcome
5. Continues until card moves or timeout

**Key Point**: Each feedback iteration is a separate execution, each recorded with outcome.

### 4. Centralized Agent Executor (`services/agent_executor.py`)

**Role**: Single point of execution for ALL agents with guaranteed observability

**Integration with Work State Tracker**:

```python
# Records SUCCESS - Line 92-101
if 'issue_number' in task_context and 'column' in task_context:
    work_execution_tracker.record_execution_outcome(
        issue_number=task_context['issue_number'],
        column=task_context['column'],
        agent=agent_name,
        outcome='success',
        project_name=project_name
    )

# Records FAILURE - Line 110-120
if 'issue_number' in task_context and 'column' in task_context:
    work_execution_tracker.record_execution_outcome(
        issue_number=task_context['issue_number'],
        column=task_context['column'],
        agent=agent_name,
        outcome='failure',
        project_name=project_name,
        error=str(e)
    )
```

**Execution Path**:
1. Receives execution request (from any source)
2. Emits observability events
3. Executes agent
4. **Records outcome** (success or failure)
5. Posts to GitHub
6. Returns result

**Key Architecture**: ALL agent executions flow through this service, ensuring:
- Consistent observability
- Guaranteed outcome recording
- Centralized error handling

### 5. Pipeline Progression (`services/pipeline_progression.py`)

**Role**: Automatically advances issues to next column after work completion

**Integration with Work State Tracker**:

```python
# Records auto status change - Line 172-181
work_execution_tracker.record_status_change(
    issue_number=issue_number,
    from_status=None,
    to_status=target_column,
    trigger='auto',  # Distinguishes automatic from manual
    project_name=project_name
)

# Records execution start with auto trigger - Line 253-261
work_execution_tracker.record_execution_start(
    issue_number=issue_number,
    column=next_column,
    agent=next_agent,
    trigger_source='pipeline_progression',  # Key: Identifies auto-progression
    project_name=project_name
)
```

**Flow**:
1. Agent completes successfully
2. Pipeline progression moves issue to next column
3. Records status change with `trigger='auto'`
4. Records execution start with `trigger_source='pipeline_progression'`
5. Work State Tracker sees auto-progression and **skips** if work was already successful

**Key Point**: The `trigger_source='pipeline_progression'` enables the tracker to distinguish:
- Manual moves (should execute for rework)
- Auto-progression (should skip to prevent double-triggering)

## Conflict Resolution & Coordination

### No Conflicts Due to Centralized Design

**Q**: What if ReviewCycleExecutor and HumanFeedbackLoopExecutor both try to control the same issue?

**A**: Cannot happen - routing is deterministic based on column type:
```python
# project_monitor.py - Line 820-835
if column.type == 'conversational':
    return self._start_conversational_loop_for_issue(...)
elif column.type == 'review':
    return self._start_review_cycle_for_issue(...)
else:
    # Standard task queue
```

**Q**: What if work is already in progress?

**A**: Work State Tracker prevents duplicate execution:
```python
# Case 5: Work already in progress
if last_execution.outcome == 'in_progress':
    return False, "work_already_in_progress"
```

**Q**: How does it handle session resumption after restart?

**A**: Each service has its own recovery mechanism:
- **ReviewCycleExecutor**: Resumes active cycles from `state/projects/{project}/review_cycles/active_cycles.yaml`
- **HumanFeedbackLoopExecutor**: Restores Claude sessions from `state/conversational_sessions/{project}_issue_{num}.yaml`
- **Work State Tracker**: Loads execution history from `state/execution_history/{project}_issue_{num}.yaml`

All three can coexist because they track different aspects:
- Review cycles track maker-checker iterations
- Feedback loops track conversation continuity
- Work state tracks execution outcomes for deduplication

## Data Flow Example: Issue 101 with Review Cycle

### Scenario: Testing column is a review column

```
1. User moves issue to Testing (manual)
   └─> ProjectMonitor.trigger_agent_for_status()
       └─> work_execution_tracker.should_execute_work()
           ├─> trigger_source='manual'
           ├─> No previous execution
           └─> Returns: True, "first_execution" ✓

       └─> ProjectMonitor._start_review_cycle_for_issue()
           └─> ReviewCycleExecutor.start_review_cycle()

               ├─> Execute reviewer (iteration 1)
               │   └─> AgentExecutor.execute_agent()
               │       └─> work_execution_tracker.record_execution_outcome(
               │               outcome='success',
               │               agent='code_reviewer'
               │           )

               ├─> Reviewer requests changes
               │   └─> Execute maker (iteration 1)
               │       └─> AgentExecutor.execute_agent()
               │           └─> work_execution_tracker.record_execution_outcome(
               │                   outcome='success',
               │                   agent='senior_software_engineer'
               │               )

               ├─> Execute reviewer (iteration 2)
               │   └─> Reviewer approves

               └─> PipelineProgression.progress_to_next_stage()
                   ├─> work_execution_tracker.record_status_change(
                   │       from_status='Testing',
                   │       to_status='Deployment',
                   │       trigger='auto'
                   │   )
                   └─> work_execution_tracker.record_execution_start(
                           trigger_source='pipeline_progression'
                       )

2. ProjectMonitor detects auto-progression to Deployment
   └─> work_execution_tracker.should_execute_work()
       ├─> trigger_source='pipeline_progression'
       ├─> last_execution.outcome='success'
       └─> Returns: False, "skip_auto_progression_after_success" ✓
```

## State Isolation & Responsibilities

| Service | State Type | Storage | Purpose |
|---------|-----------|---------|---------|
| **Work Execution Tracker** | Execution outcomes, status changes | `state/execution_history/` | Deduplication logic |
| **Review Cycle Executor** | Iteration count, maker/reviewer outputs | `state/projects/{name}/review_cycles/` | Maker-checker loop control |
| **Human Feedback Loop** | Conversation history, Claude session | `state/conversational_sessions/` | Session continuity |
| **Conversational Session State** | Claude session IDs | `state/conversational_sessions/` | Claude SDK session persistence |
| **Project Monitor** | Last seen project items | In-memory (ephemeral) | Change detection |

**No Overlap**: Each service tracks different aspects of execution, enabling composition without conflicts.

## Summary: How They Work Together

1. **Entry Control**: Project Monitor uses Work State Tracker to decide if work should execute
2. **Routing**: Based on column type, routes to specialized executors OR task queue
3. **Execution**: ALL paths funnel through centralized AgentExecutor
4. **Outcome Recording**: AgentExecutor automatically records success/failure to Work State Tracker
5. **Progression**: Pipeline progression uses Work State Tracker to prevent double-triggering

**Key Insight**: The Work Execution State Tracker acts as a **coordination layer** that:
- Sits at entry points (deduplication)
- Sits at exit points (outcome recording)
- Enables all execution services to coexist without conflicts
- Provides the "memory" needed for intelligent restart decisions

The integration is **non-invasive** - existing services continue their specialized logic while the tracker provides cross-cutting deduplication and outcome tracking.

# Agent Execution Architecture

## Overview

There is **ONE centralized way** to execute agents in the orchestrator, ensuring observability is **always** enabled.

## Centralized Service: `AgentExecutor`

**Location**: `services/agent_executor.py`

This service provides the **ONLY** way to execute agents. All agent executions MUST go through this service.

### What It Guarantees

1. **Observability events** are always emitted:
   - `task_received` - When agent execution starts
   - `agent_initialized` - When agent is created
   - `claude_call_started` - When Claude API call begins
   - `claude_call_completed` - When Claude API call finishes
   - `agent_completed` - When agent execution completes

2. **Claude logs** are always streamed to Redis for:
   - Real-time websocket delivery to UI
   - Historical log storage (500 entries, 2-hour TTL)

3. **Consistent context structure** across all execution paths:
   - `observability` - Always present
   - `stream_callback` - Always present
   - `task_id` - Always present
   - `agent` - Always present
   - `project` - Always present
   - `use_docker` - Always present

### Usage

```python
from services.agent_executor import get_agent_executor

executor = get_agent_executor()
result = await executor.execute_agent(
    agent_name="business_analyst",
    project_name="context-studio",
    task_context={...},
    task_id_prefix="review_cycle"  # Optional prefix for task ID
)
```

## Execution Paths

### 1. Task Queue Execution

**File**: `agents/orchestrator_integration.py`
**Function**: `process_task_integrated()`

Used when agents are executed from the Redis task queue. Includes:
- Feature branch management for dev/full-sdlc pipelines
- Task validation (dev container requirements)
- Auto-commit for agents that make code changes

```python
# Uses centralized executor internally
executor = get_agent_executor()
result = await executor.execute_agent(...)

# Then handles auto-commit if needed
if makes_code_changes:
    await auto_commit_service.commit_agent_changes(...)
```

### 2. Review Cycle Execution (Maker-Checker Loops)

**File**: `services/review_cycle.py`
**Method**: `ReviewCycleManager._execute_agent_directly()`

Used for maker-checker review cycles where agents review each other's work.

```python
executor = get_agent_executor()
return await executor.execute_agent(
    agent_name=agent_name,
    project_name=project_name,
    task_context=task_context,
    task_id_prefix="review_cycle"
)
```

### 3. Conversational Loop Execution (Discussion Threads)

**File**: `services/conversational_loop.py`
**Method**: `ConversationalLoop._execute_agent()`

Used for multi-turn conversational agents in GitHub Discussions.

```python
executor = get_agent_executor()
result = await executor.execute_agent(
    agent_name=state.agent,
    project_name=state.project_name,
    task_context=context,
    task_id_prefix="conversational"
)
```

## Code Eliminated

### Removed Duplication

Previously, observability setup code was duplicated across 3 files:
- `services/review_cycle.py` - ~113 lines removed
- `services/conversational_loop.py` - ~60 lines removed
- `agents/orchestrator_integration.py` - ~80 lines removed

**Total**: ~253 lines of duplicated code eliminated

### Removed "Legacy" Functions

Deleted truly legacy functions that were only used in tests:
- `business_analyst_agent()` - Replaced by `AgentExecutor`
- `code_reviewer_agent()` - Replaced by `AgentExecutor`

Tests should use `AgentExecutor` directly or mock agent execution.

## Design Principles

### 1. Single Responsibility

`AgentExecutor` has ONE job: Execute agents with full observability.

Additional concerns (branch management, auto-commit, validation) are handled by the calling code.

### 2. No Bypassing Observability

There is NO way to execute an agent without observability. The architecture enforces this at the design level.

### 3. Consistent Context

All agents receive the same standardized context structure, eliminating confusion and bugs from inconsistent context shapes.

### 4. Centralized Changes

If observability requirements change (e.g., new events, different Redis keys), there is ONE place to update: `AgentExecutor`.

## Testing

Tests should use `AgentExecutor` directly:

```python
from services.agent_executor import get_agent_executor

async def test_agent_execution():
    executor = get_agent_executor()
    result = await executor.execute_agent(
        agent_name="business_analyst",
        project_name="test-project",
        task_context={"issue": {...}},
        task_id_prefix="test"
    )
```

## Benefits

1. **Observability is guaranteed** - No code path can bypass it
2. **Less code** - ~250 lines eliminated
3. **Single source of truth** - One place to update observability logic
4. **Easier to test** - One service to mock instead of three
5. **Clearer architecture** - Obvious entry point for all agent execution
6. **No "legacy" confusion** - No ambiguous comments about what's actually legacy

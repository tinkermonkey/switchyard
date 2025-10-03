# Observability Consistency Proposal

## Problem
Currently, there are multiple code paths for executing agents:
1. `process_task_integrated()` - Main orchestrator loop
2. `_execute_agent_directly()` - Review cycle
3. Potentially future: feedback loops, ad-hoc executions, etc.

Each path must replicate observability setup:
- Task received events
- Stream callbacks for live logging
- Agent initialized events
- Agent completed events
- Error handling with failure events

**Risk**: Inconsistency leads to missing observability in some execution paths.

## Solution: Single Agent Execution Function

### Option A: Refactor to Shared Function

Create `agents/executor.py`:

```python
async def execute_agent_with_observability(
    agent_name: str,
    task_context: Dict[str, Any],
    project_name: str,
    task_id: Optional[str] = None,
    logger: Optional[Any] = None
) -> Dict[str, Any]:
    """
    Execute an agent with full observability and proper context structure.

    This is the canonical way to execute agents, ensuring:
    - Consistent observability events
    - Proper context nesting
    - Stream callback for live logs
    - Error handling with failure events
    - Agent configuration via PipelineFactory

    Args:
        agent_name: Name of agent to execute
        task_context: Task-specific context dict
        project_name: Project name
        task_id: Optional task ID (generated if not provided)
        logger: Optional logger instance

    Returns:
        Result dict from agent execution
    """
    from config.manager import config_manager
    from state_management.manager import StateManager
    from pipeline.factory import PipelineFactory
    from pipeline.orchestrator import SequentialPipeline
    from monitoring.observability import get_observability_manager
    from pathlib import Path
    import time
    import json
    import logging

    if logger is None:
        logger = logging.getLogger(__name__)

    # Get observability manager
    obs = get_observability_manager()

    # Generate task ID if not provided
    if task_id is None:
        from datetime import datetime
        task_id = f"{agent_name}_{int(datetime.now().timestamp())}"

    # Emit task received event
    obs.emit_task_received(agent_name, task_id, project_name, task_context)

    # Create stream callback for live Claude Code output
    def stream_callback(event):
        """Publish Claude stream events to Redis for websocket forwarding"""
        try:
            if obs and obs.enabled:
                event_data = {
                    'agent': agent_name,
                    'task_id': task_id,
                    'project': project_name,
                    'timestamp': event.get('timestamp') or time.time(),
                    'event': event
                }
                event_json = json.dumps(event_data)

                # Publish to pub/sub for real-time delivery
                obs.redis.publish('orchestrator:claude_stream', event_json)

                # Also add to Redis Stream for history
                claude_stream_key = "orchestrator:claude_logs_stream"
                obs.redis.xadd(
                    claude_stream_key,
                    {'log': event_json},
                    maxlen=500,
                    approximate=True
                )

                # Set 2-hour TTL
                obs.redis.expire(claude_stream_key, 7200)
        except Exception as e:
            logger.error(f"Error publishing stream event: {e}")

    # Create pipeline context
    state_manager = StateManager(Path("orchestrator_data/state"))

    pipeline_context = {
        'pipeline_id': f"pipeline_{task_id}_{time.time()}",
        'task_id': task_id,
        'agent': agent_name,
        'project': project_name,
        'context': task_context,  # Nest task context here
        'work_dir': f"./projects/{project_name}",
        'completed_work': [],
        'decisions': [],
        'metrics': {},
        'validation': {},
        'state_manager': state_manager,
        'observability': obs,
        'stream_callback': stream_callback,
        'use_docker': task_context.get('use_docker', True)
    }

    # Use PipelineFactory to create agent
    factory = PipelineFactory(config_manager)
    agent_stage = factory.create_agent(agent_name, project_name)

    # Get agent config and emit initialized event
    agent_config = agent_stage.agent_config or {}
    obs.emit_agent_initialized(agent_name, task_id, project_name, agent_config)

    # Add claude_model to context if configured
    if 'claude_model' in agent_config:
        pipeline_context['claude_model'] = agent_config['claude_model']

    # Create single-stage pipeline
    pipeline = SequentialPipeline([agent_stage], state_manager)

    # Execute with full observability
    start_time = time.time()
    try:
        logger.info(f"Executing {agent_name} for task {task_id}")
        result = await pipeline.execute(pipeline_context)

        # Emit success event
        duration_ms = (time.time() - start_time) * 1000
        obs.emit_agent_completed(agent_name, task_id, project_name, duration_ms, True)

        logger.info(f"Agent {agent_name} completed successfully")
        return result

    except Exception as e:
        # Emit failure event
        duration_ms = (time.time() - start_time) * 1000
        obs.emit_agent_completed(agent_name, task_id, project_name, duration_ms, False, str(e))

        logger.error(f"Agent {agent_name} failed: {e}")
        raise
```

### Usage

**In main.py / orchestrator_integration.py:**
```python
from agents.executor import execute_agent_with_observability

async def process_task_integrated(task, state_manager, logger):
    # Branch management, validation, etc.
    # ...

    # Execute agent with observability
    result = await execute_agent_with_observability(
        agent_name=task.agent,
        task_context=task.context,
        project_name=task.project,
        task_id=task.id,
        logger=logger
    )

    # Post-processing (auto-commit, etc.)
    # ...

    return result
```

**In review_cycle.py:**
```python
from agents.executor import execute_agent_with_observability

async def _execute_review_loop(...):
    # Execute reviewer
    await execute_agent_with_observability(
        agent_name=cycle_state.reviewer_agent,
        task_context=review_task_context,
        project_name=cycle_state.project_name,
        task_id=f"review_cycle_reviewer_{iteration}_{issue_number}"
    )

    # Execute maker
    await execute_agent_with_observability(
        agent_name=cycle_state.maker_agent,
        task_context=maker_task_context,
        project_name=cycle_state.project_name,
        task_id=f"review_cycle_maker_{iteration}_{issue_number}"
    )
```

### Benefits

1. **Single source of truth**: One place to maintain observability logic
2. **Guaranteed consistency**: All agent executions use same path
3. **Easier testing**: Test observability once
4. **Easier updates**: Add new observability features in one place
5. **Clear contract**: Function signature documents requirements
6. **No duplication**: DRY principle

### Implementation Steps

1. Create `agents/executor.py` with the shared function
2. Update `process_task_integrated()` to use it (keeping branch management, auto-commit)
3. Update `review_cycle.py` to use it
4. Add unit tests for observability consistency
5. Document as the canonical agent execution path

### Alternative: Decorator Pattern

Could also use a decorator:

```python
@with_observability
async def execute_agent(agent_name, task_context, project_name):
    # Core execution logic
    pass
```

But the function approach is more explicit and easier to test.

## Recommendation

Implement **Option A** in the next iteration. For now, the review cycle has full observability parity with the main loop, but we should consolidate to prevent future drift.

### Testing Strategy

1. Add observability assertions to review cycle tests
2. Verify all events are emitted:
   - task_received
   - agent_initialized
   - stream events (via mock)
   - agent_completed (success/failure)
3. Compare event structure between main loop and review cycle
4. Add integration test that executes agent both ways and verifies identical events

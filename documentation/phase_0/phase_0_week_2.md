# Phase 0 - Week 2: End-to-End Orchestration

## Objective
Connect all components from Week 1 into a working end-to-end orchestration system with monitoring.

## Prerequisites from Week 1
- [x] All import errors resolved
- [x] `run_claude_code()` function implemented
- [x] Business Analyst agent working
- [x] Basic pipeline configuration created
- [x] Task queue functional

---

## Day 1-2: Connect Pipeline to Task Queue

### Task 1.1: Fix Main Loop Integration
**Priority**: Critical
**Files**: `main.py`

**Problem**: Line 67 calls undefined `process_task(task)` function

```python
# ADD these imports at top of main.py:
from agents.agent_stages import create_sdlc_pipeline

# REPLACE the undefined process_task call with:
async def process_task_integrated(task, state_manager, logger):
    """Process task using sequential pipeline with proper integration"""

    # Convert Task object to pipeline context
    pipeline_context = {
        'pipeline_id': f"pipeline_{task.id}_{datetime.now().timestamp()}",
        'task_id': task.id,
        'agent': task.agent,
        'project': task.project,
        'context': task.context,
        'work_dir': f"./projects/{task.project}",
        'completed_work': [],
        'decisions': [],
        'metrics': {},
        'validation': {}
    }

    # Create pipeline with single agent for now
    from agents.01_business_analyst import BusinessAnalystAgent
    from pipeline.orchestrator import SequentialPipeline

    stages = [BusinessAnalystAgent()]
    pipeline = SequentialPipeline(stages, state_manager)

    try:
        logger.log_info(f"Starting pipeline execution for task {task.id}")
        result = await pipeline.execute(pipeline_context)
        logger.log_info(f"Pipeline completed for task {task.id}")
        return result

    except Exception as e:
        logger.log_error(f"Pipeline execution failed for task {task.id}: {e}")
        raise

# UPDATE the main loop process_task call:
result = await process_task_integrated(task, state_manager, logger)
```

### Task 1.2: Fix Remaining Import Issues
**Priority**: Critical
**Files**: `pipeline/resilient_pipeline.py`, `handoff/protocol.py`

```python
# In pipeline/resilient_pipeline.py - FIX imports:
# CHANGE FROM:
from ..resilience.circuit_breaker import CircuitBreaker
from ..resilience.retry_manager import RetryManager

# CHANGE TO:
from resilience.circuit_breaker import CircuitBreaker
from resilience.retry_manager import RetryManager

# In handoff/protocol.py - FIX import:
# CHANGE FROM:
from ..state.manager import StateManager

# CHANGE TO:
from state_management.manager import StateManager
```

### Task 1.3: Test Basic Pipeline Execution
**Priority**: High
**Action**: Create integration test script

```python
# Create: tests/integration/test_pipeline_integration.py
import asyncio
from datetime import datetime
from task_queue.task_manager import Task, TaskPriority
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger

async def test_pipeline_integration():
    """Test pipeline execution with real Business Analyst"""

    # Setup
    state_manager = StateManager()
    logger = OrchestratorLogger("test")

    # Create test task
    test_task = Task(
        id="integration_test_001",
        agent="business_analyst",
        project="test_project",
        priority=TaskPriority.HIGH,
        context={
            "issue": {
                "title": "User Authentication System",
                "body": "Need secure login/logout functionality with password reset",
                "labels": ["security", "authentication", "feature"]
            }
        },
        created_at=datetime.now().isoformat()
    )

    # Import the process_task function
    from main import process_task_integrated

    # Execute
    try:
        result = await process_task_integrated(test_task, state_manager, logger)
        print(f" Pipeline integration test passed")
        print(f"=� Result keys: {result.keys()}")
        return True
    except Exception as e:
        print(f"L Integration test failed: {e}")
        return False

if __name__ == "__main__":
    asyncio.run(test_pipeline_integration())
```

---

## Day 3-4: State Persistence & Handoff Testing

### Task 2.1: Verify Checkpoint/Recovery Works
**Priority**: Critical
**Action**: Test state persistence across pipeline execution

```python
# Create: tests/integration/test_pipeline_integration.py
import asyncio
from datetime import datetime
from main import process_task_integrated
from state_management.manager import StateManager
from monitoring.logging import OrchestratorLogger

async def test_checkpoint_recovery():
    """Test pipeline checkpoint and recovery functionality"""

    state_manager = StateManager()
    logger = OrchestratorLogger("checkpoint_test")

    # Create task that will be checkpointed
    test_task = Task(
        id="checkpoint_test_001",
        agent="business_analyst",
        project="checkpoint_test",
        priority=TaskPriority.MEDIUM,
        context={"issue": {"title": "Test checkpoint", "body": "Testing state persistence"}}
    )

    # Execute and verify checkpoint creation
    result = await process_task_integrated(test_task, state_manager, logger)

    # Check if checkpoint files were created
    checkpoints = list(Path("orchestrator_data/state/checkpoints").glob("*.json"))
    assert len(checkpoints) > 0, "No checkpoint files created"

    # Test recovery by loading latest checkpoint
    pipeline_id = result['pipeline_id']
    checkpoint = await state_manager.get_latest_checkpoint(pipeline_id)

    assert checkpoint is not None, "Failed to retrieve checkpoint"
    assert checkpoint['pipeline_id'] == pipeline_id, "Checkpoint data corrupted"

    print(" Checkpoint/recovery test passed")

if __name__ == "__main__":
    asyncio.run(test_checkpoint_recovery())
```

### Task 2.2: Test Handoff Package Creation
**Priority**: High
**Action**: Verify handoff packages are created even with single agent

```python
# Add to agents/01_business_analyst.py at end of execute() method:

# Create handoff package for next stage (even if no next stage)
from handoff.protocol import HandoffManager
from handoff.quality_gate import QualityGate

handoff_manager = HandoffManager(state_manager)
handoff = await handoff_manager.create_handoff(
    source_agent="business_analyst",
    target_agent="end_of_pipeline",
    context=context,
    artifacts={
        "requirements_document": context.get('requirements_analysis', {}),
        "user_stories": context.get('requirements_analysis', {}).get('user_stories', [])
    }
)

# Validate handoff package
quality_gate = QualityGate({
    "completeness_score": 0.7,
    "clarity_score": 0.7
})

passed, issues = quality_gate.evaluate(handoff)
if not passed:
    context['warnings'] = issues
    logger.log_warning(f"Quality gate issues: {issues}")

context['handoff_id'] = handoff.handoff_id
logger.log_info(f"Handoff package created: {handoff.handoff_id}")
```

### Task 2.3: Test Complete Pipeline Flow
**Priority**: Critical
**Action**: Manual end-to-end test

```bash
# Manual test sequence:

# 1. Start Redis
redis-server --daemonize yes

# 2. Clear previous state
rm -rf orchestrator_data/state/*
rm -rf orchestrator_data/handoffs/*

# 3. Run pipeline integration test
python tests/integration/test_pipeline_integration.py

# 4. Verify artifacts created:
ls -la orchestrator_data/state/checkpoints/     # Should have checkpoint files
ls -la orchestrator_data/state/*.log           # Should have execution logs
ls -la orchestrator_data/handoffs/             # Should have handoff packages

# 5. Verify handoff package structure
python -c "
import json
from pathlib import Path
handoffs = list(Path('orchestrator_data/handoffs').glob('*.json'))
if handoffs:
    with open(handoffs[0]) as f:
        handoff = json.load(f)
        print('Handoff keys:', handoff.keys())
        print('Artifacts:', handoff['artifacts'].keys())
else:
    print('No handoff packages found')
"
```

---

## Day 5-6: Monitoring & Health Integration

### Task 3.1: Fix Health Monitor Integration
**Priority**: High
**Files**: `monitoring/health_monitor.py`, `main.py`

```python
# Fix health_monitor.py import issues:
# CHANGE FROM:
from config import env

# CHANGE TO:
from config.environment import Environment

# UPDATE HealthMonitor.__init__ to handle orchestrator properly:
def __init__(self, orchestrator=None):
    self.orchestrator = orchestrator
    self.env = Environment()  # Load environment config
    # ... rest of init

# In main.py, UPDATE health monitor initialization:
health_monitor = HealthMonitor(orchestrator=pipeline)  # Pass pipeline as orchestrator
```

### Task 3.2: Verify Logging Integration
**Priority**: Medium
**Action**: Test all components log properly

```python
# Create: tests/integration/test_logging_integration.py
import asyncio
from datetime import datetime
from pathlib import Path

async def test_logging_integration():
    """Verify all components create proper logs"""

    # Clear existing logs
    log_files = Path("orchestrator_data/state").glob("*.log")
    for log_file in log_files:
        log_file.unlink()

    # Run a full pipeline execution
    from scripts.test_pipeline_integration import test_pipeline_integration
    await test_pipeline_integration()

    # Check log files were created
    log_files = list(Path("orchestrator_data/state").glob("*.log"))
    assert len(log_files) > 0, "No log files created"

    # Check log content has expected entries
    for log_file in log_files:
        with open(log_file) as f:
            content = f.read()
            assert "pipeline_id" in content, f"Missing pipeline_id in {log_file}"
            assert "business_analyst" in content, f"Missing agent name in {log_file}"

    print(" Logging integration test passed")

if __name__ == "__main__":
    asyncio.run(test_logging_integration())
```

### Task 3.3: Test Metrics Collection
**Priority**: Medium
**Action**: Verify metrics are collected during task execution

```python
# Add to main.py after successful task completion:

# Record additional metrics
metrics.record_task_complete(
    task.agent,
    duration,
    success=True
)

# Record pipeline-specific metrics
if hasattr(result, 'get') and result.get('quality_metrics'):
    quality_scores = result['quality_metrics']
    for metric_name, score in quality_scores.items():
        # You'll need to add this method to MetricsCollector
        metrics.record_quality_metric(task.agent, metric_name, score)
```

---

## Day 7: Integration Testing & Validation

### Task 4.1: Complete End-to-End Test
**Priority**: Critical
**Action**: Test full orchestration flow from task queue to completion

```python
# Create: tests/integration/test_complete_orchestration.py
import asyncio
import time
from datetime import datetime
from task_queue.task_manager import TaskQueue, Task, TaskPriority

async def test_complete_orchestration():
    """Test complete flow: enqueue -> pipeline -> state -> handoff -> logging"""

    print("=� Starting complete orchestration test...")

    # 1. Setup task queue
    task_queue = TaskQueue()

    # 2. Create test task
    test_task = Task(
        id="complete_test_001",
        agent="business_analyst",
        project="end_to_end_test",
        priority=TaskPriority.HIGH,
        context={
            "issue": {
                "title": "E-commerce Checkout Flow",
                "body": "Users need ability to add items to cart, review order, and complete purchase with payment processing",
                "labels": ["feature", "e-commerce", "payment", "high-priority"]
            }
        },
        created_at=datetime.now().isoformat()
    )

    # 3. Enqueue task
    task_queue.enqueue(test_task)
    print(" Task enqueued")

    # 4. Verify task can be dequeued
    dequeued_task = task_queue.dequeue()
    assert dequeued_task.id == test_task.id
    print(" Task dequeued successfully")

    # 5. Process through pipeline (simulating main.py behavior)
    from main import process_task_integrated
    from state_management.manager import StateManager
    from monitoring.logging import OrchestratorLogger

    state_manager = StateManager()
    logger = OrchestratorLogger("complete_test")

    start_time = time.time()
    result = await process_task_integrated(dequeued_task, state_manager, logger)
    duration = time.time() - start_time

    print(f" Pipeline execution completed in {duration:.2f}s")

    # 6. Verify all expected artifacts
    from pathlib import Path

    # Check state files
    checkpoints = list(Path("orchestrator_data/state/checkpoints").glob("*.json"))
    assert len(checkpoints) > 0, "No checkpoint files created"
    print(f" Found {len(checkpoints)} checkpoint files")

    # Check handoff files
    handoffs = list(Path("orchestrator_data/handoffs").glob("*.json"))
    assert len(handoffs) > 0, "No handoff packages created"
    print(f" Found {len(handoffs)} handoff packages")

    # Check log files
    logs = list(Path("orchestrator_data/state").glob("*.log"))
    assert len(logs) > 0, "No log files created"
    print(f" Found {len(logs)} log files")

    # 7. Validate result structure
    assert 'pipeline_id' in result
    assert 'requirements_analysis' in result
    assert 'quality_metrics' in result
    print(" Result structure validated")

    print("<� Complete orchestration test PASSED!")
    return True

if __name__ == "__main__":
    asyncio.run(test_complete_orchestration())
```

### Task 4.2: Performance & Resilience Testing
**Priority**: Medium
**Action**: Test circuit breaker and error handling

```python
# Create: tests/integration/test_resilience.py
import asyncio
from datetime import datetime

async def test_circuit_breaker():
    """Test circuit breaker functionality with simulated failures"""

    # Create task that will cause failures
    failing_task = Task(
        id="failing_test_001",
        agent="business_analyst",
        project="failure_test",
        context={
            "issue": {
                "title": "Invalid Requirements",
                "body": "",  # Empty body should cause analysis failure
                "labels": []
            }
        }
    )

    # Attempt multiple executions to trigger circuit breaker
    from main import process_task_integrated
    from state_management.manager import StateManager
    from monitoring.logging import OrchestratorLogger

    state_manager = StateManager()
    logger = OrchestratorLogger("resilience_test")

    failures = 0
    for i in range(5):  # Try 5 times
        try:
            await process_task_integrated(failing_task, state_manager, logger)
        except Exception as e:
            failures += 1
            print(f"Attempt {i+1} failed: {e}")

    assert failures > 0, "Expected some failures for circuit breaker testing"
    print(f" Circuit breaker test: {failures}/5 attempts failed as expected")

if __name__ == "__main__":
    asyncio.run(test_circuit_breaker())
```

---

## Success Criteria

### Must Complete
- [ ] Main loop executes tasks through SequentialPipeline successfully
- [ ] Task queue integration works (enqueue � dequeue � process � complete)
- [ ] State persistence works (checkpoints created and recoverable)
- [ ] Handoff packages created with proper structure and validation
- [ ] Logging integration works across all components
- [ ] Complete end-to-end test passes

### Should Complete
- [ ] Health monitoring functional and reporting correctly
- [ ] Metrics collection working for task completion
- [ ] Basic error handling and circuit breaker functionality
- [ ] Performance acceptable (task completion under 60 seconds)

### Nice to Have
- [ ] Advanced resilience testing (circuit breaker validation)
- [ ] Performance metrics and optimization
- [ ] Detailed health reporting dashboard

## File Changes Required

### New Files
- `tests/integration/test_pipeline_integration.py`
- `tests/integration/test_state_persistence.py`
- `tests/integration/test_logging_integration.py`
- `tests/integration/test_complete_orchestration.py`
- `tests/integration/test_resilience.py`

### Modified Files
- `main.py` - Add process_task_integrated function and fix integration
- `pipeline/resilient_pipeline.py` - Fix import paths
- `handoff/protocol.py` - Fix import paths
- `agents/01_business_analyst.py` - Add handoff package creation
- `monitoring/health_monitor.py` - Fix environment import

## Environment Requirements

```bash
# Ensure Redis is running
redis-server --daemonize yes

# Ensure Claude Code CLI is authenticated
claude --version

# Clear state directories for clean testing
rm -rf orchestrator_data/state/*
rm -rf orchestrator_data/handoffs/*
```

## Week 2 Success Target

**End-to-End Flow Working**: Task enqueued � processed through pipeline � Business Analyst executes � state checkpointed � handoff created � logs generated � metrics recorded � task marked complete.

This establishes the orchestration foundation needed for adding multiple agents in future phases.
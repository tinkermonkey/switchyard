# Multi-Threaded Per-Project Task Execution Design

## Current Architecture Analysis

### Single-Threaded Bottleneck
**Problem**: The main orchestrator loop (`main.py:395-489`) processes ONE task at a time globally:
```python
while True:
    task = task_queue.dequeue()  # Dequeues from ALL projects
    if task:
        result = await process_task_integrated(task, state_manager, logger)
        # Blocks until task completes
    await asyncio.sleep(5)
```

**Impact**:
- codetoreum #32 executing → context-studio #178 waits
- No parallelism across independent projects
- Long-running agents block all other work
- Doesn't scale with number of projects

### Thread-Safe Components ✅
1. **Redis Task Queue**
   - `rpop` is atomic - multiple consumers safe
   - Each worker can dequeue independently
   - Already supports multi-consumer pattern

2. **Pipeline Locks (Redis)**
   - Stored in Redis with atomic operations
   - Already per-project, per-pipeline
   - Multiple workers can check/acquire independently

3. **Docker Agent Execution**
   - Each agent runs in isolated container
   - subprocess.Popen is thread-safe
   - Containers have unique names (includes timestamp)

4. **GitHubIntegration**
   - Already has `threading.Lock()` for rate limiting
   - Multiple workers can use GitHub API safely

### NOT Thread-Safe ❌
1. **StateManager** (`state_management/manager.py`)
   - Uses `aiofiles` for async I/O
   - No locking for concurrent file access
   - Multiple workers could corrupt checkpoint files

2. **YAML Lock Files** (`services/pipeline_lock_manager.py:242`)
   - `_save_lock_to_yaml()` does direct file write
   - Race conditions if multiple workers write
   - Used as fallback when Redis unavailable

3. **Work Execution State** (`services/work_execution_state.py`)
   - Writes to YAML files per issue
   - No file locking mechanism
   - Could have race conditions

---

## Proposed Design: Per-Project Worker Pool

### Architecture Overview

```
┌──────────────────────────────────────────────────────┐
│                Main Orchestrator                       │
│  - Startup/initialization                             │
│  - Health monitoring                                  │
│  - ProjectMonitor (background thread)                 │
└────────────────────┬─────────────────────────────────┘
                     │
                     │ spawns
                     ▼
┌──────────────────────────────────────────────────────┐
│           Worker Pool Manager                         │
│  - Creates N worker threads                           │
│  - Monitors worker health                             │
│  - Handles worker crashes/restarts                    │
└────────────────────┬─────────────────────────────────┘
                     │
        ┌────────────┼────────────┬──────────────┐
        ▼            ▼            ▼              ▼
   ┌─────────┐  ┌─────────┐  ┌─────────┐   ┌─────────┐
   │ Worker  │  │ Worker  │  │ Worker  │...│ Worker  │
   │   #1    │  │   #2    │  │   #3    │   │   #N    │
   └─────────┘  └─────────┘  └─────────┘   └─────────┘
        │            │            │              │
        │            │            │              │
        └────────────┴────────────┴──────────────┘
                     │
                     ▼
            ┌────────────────┐
            │  Redis Queue   │
            │  (Shared)      │
            └────────────────┘
```

### Worker Thread Implementation

```python
class TaskWorker:
    """Worker thread that processes tasks from Redis queue"""

    def __init__(self, worker_id: int, task_queue: TaskQueue, logger):
        self.worker_id = worker_id
        self.task_queue = task_queue
        self.logger = logger
        self.running = True
        self.current_task = None

        # Each worker gets its own StateManager instance
        # (file operations need coordination via file locks)
        self.state_manager = StateManager(
            Path(f"orchestrator_data/state/worker_{worker_id}")
        )

    async def run(self):
        """Main worker loop"""
        self.logger.info(f"Worker {self.worker_id} started")

        while self.running:
            try:
                # Atomic dequeue from Redis
                task = self.task_queue.dequeue()

                if task:
                    self.current_task = task
                    self.logger.info(
                        f"Worker {self.worker_id} processing task {task.id} "
                        f"for project {task.project}"
                    )

                    # Execute task (same as current logic)
                    result = await process_task_integrated(
                        task, self.state_manager, self.logger
                    )

                    self.current_task = None
                    self.logger.info(
                        f"Worker {self.worker_id} completed task {task.id}"
                    )
                else:
                    # No task available, sleep briefly
                    await asyncio.sleep(2)

            except Exception as e:
                self.logger.error(
                    f"Worker {self.worker_id} error: {e}"
                )
                if self.current_task:
                    self.logger.error(
                        f"Failed task: {self.current_task.id}"
                    )
                self.current_task = None
                await asyncio.sleep(5)  # Backoff on error
```

### Worker Pool Manager

```python
class WorkerPoolManager:
    """Manages pool of worker threads"""

    def __init__(self, num_workers: int, task_queue: TaskQueue, logger):
        self.num_workers = num_workers
        self.task_queue = task_queue
        self.logger = logger
        self.workers = []
        self.worker_threads = []

    def start(self):
        """Start all worker threads"""
        for i in range(self.num_workers):
            worker = TaskWorker(i, self.task_queue, self.logger)
            self.workers.append(worker)

            # Create async task for worker
            thread = threading.Thread(
                target=self._run_worker_loop,
                args=(worker,),
                daemon=True
            )
            thread.start()
            self.worker_threads.append(thread)

        self.logger.info(f"Started {self.num_workers} worker threads")

    def _run_worker_loop(self, worker: TaskWorker):
        """Run worker in its own event loop"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(worker.run())

    def get_active_tasks(self):
        """Get list of currently executing tasks"""
        return [
            {
                'worker_id': w.worker_id,
                'task': w.current_task
            }
            for w in self.workers
            if w.current_task is not None
        ]
```

---

## Required Code Changes

### 1. Thread-Safe File Locking

**File**: `utils/file_lock.py` (NEW)
```python
import fcntl
import contextlib

@contextlib.contextmanager
def file_lock(file_path):
    """Context manager for file locking"""
    with open(file_path, 'a') as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
```

### 2. Update PipelineLockManager

**File**: `services/pipeline_lock_manager.py:242`
```python
def _save_lock_to_yaml(self, lock: PipelineLock):
    """Save lock to YAML with file locking"""
    from utils.file_lock import file_lock

    state_file = self._get_state_file(lock.project, lock.board)
    lock_file = state_file.with_suffix('.lock')

    with file_lock(lock_file):
        with open(state_file, 'w') as f:
            yaml.dump(asdict(lock), f)
```

### 3. Update WorkExecutionState

**File**: `services/work_execution_state.py`
Add file locking around YAML read/write operations.

### 4. Update main.py

**File**: `main.py:370-491`
```python
# Replace single-threaded loop with worker pool
from services.worker_pool import WorkerPoolManager

# Determine optimal number of workers
num_workers = int(os.environ.get('ORCHESTRATOR_WORKERS', '3'))
logger.info(f"Starting worker pool with {num_workers} workers")

# Start worker pool
worker_pool = WorkerPoolManager(num_workers, task_queue, logger)
worker_pool.start()

# Main loop now just monitors health and workers
while True:
    try:
        # Health check
        if current_time - last_health_check >= health_check_backoff:
            health = await health_monitor.check_health()
            # ... health check logic ...

        # Monitor workers (check for crashes)
        active_tasks = worker_pool.get_active_tasks()
        logger.debug(f"Active tasks: {len(active_tasks)}")

        await asyncio.sleep(10)

    except KeyboardInterrupt:
        logger.info("Shutting down orchestrator")
        worker_pool.stop()
        break
```

---

## Configuration

### Environment Variables
```bash
# Number of worker threads
ORCHESTRATOR_WORKERS=3

# Recommended: 1 per 2-3 active projects
# Example: 5 projects → 2-3 workers
```

### Scaling Guidelines
- **Small deployments** (1-3 projects): 2 workers
- **Medium deployments** (4-10 projects): 3-5 workers
- **Large deployments** (10+ projects): 5-8 workers

**Why not more?**
- Each worker consumes memory
- Docker containers are the actual parallelism
- More workers = more contention for GitHub API
- Diminishing returns beyond 8 workers

---

## Benefits

1. **Parallel Execution**
   - Multiple projects can run agents simultaneously
   - context-studio and codetoreum work independently

2. **Better Resource Utilization**
   - CPU idle time reduced
   - Docker host can run multiple containers

3. **Improved Throughput**
   - 3 workers can handle 3x tasks in same time period
   - Shorter queue wait times

4. **Graceful Degradation**
   - If one worker crashes, others continue
   - Failed tasks can be retried by another worker

---

## Risks & Mitigation

### Risk: File Lock Contention
**Mitigation**:
- Use Redis as primary for locks (already done)
- YAML only as fallback
- File locks only matter during Redis outage

### Risk: Worker Crashes
**Mitigation**:
- Each worker in try/catch
- WorkerPoolManager monitors and restarts crashed workers
- Tasks stay in Redis until successfully processed

### Risk: Race Conditions
**Mitigation**:
- Pipeline locks use Redis (atomic)
- File operations use fcntl locks
- Each worker has isolated state directory

### Risk: Increased Memory Usage
**Mitigation**:
- Configurable worker count
- Start with 2-3 workers
- Monitor memory and adjust

---

## Implementation Plan

### Phase 1: Foundation (Low Risk)
1. Add file locking utilities
2. Update YAML write operations with locks
3. Add WorkerPoolManager (disabled by default)
4. Add `ORCHESTRATOR_WORKERS=1` to maintain current behavior

### Phase 2: Testing (Medium Risk)
1. Set `ORCHESTRATOR_WORKERS=2` in test environment
2. Run parallel workload tests
3. Monitor for race conditions
4. Validate pipeline locks work correctly

### Phase 3: Production Rollout (Low Risk)
1. Deploy with `ORCHESTRATOR_WORKERS=3`
2. Monitor for 24-48 hours
3. Gradually increase if needed
4. Document optimal worker counts

---

## Compatibility

**Backward Compatible**: YES ✅
- Default `ORCHESTRATOR_WORKERS=1` maintains current behavior
- Opt-in via environment variable
- No breaking changes to existing code
- Can roll back by setting workers=1

**Redis Required**: YES ⚠️
- Multi-worker requires Redis
- In-memory queue fallback not recommended
- Redis is already required for production


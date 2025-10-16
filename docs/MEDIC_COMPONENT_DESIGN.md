# Medic Component Design Document

**Status:** Design Phase  
**Created:** 2025-10-15  
**Author:** System Architecture  
**Version:** 1.0

---

## Executive Summary

The **Medic** is a self-healing component that continuously monitors Docker container logs for errors and warnings in the Clauditoreum orchestrator, automatically diagnoses issues using Claude Code, applies fixes, and restarts containers when safe to do so. It operates as a separate containerized service with its own repair cycle, implementing a mini-SDLC for error detection, analysis, fixing, and verification.

### Key Capabilities

- **Autonomous Error Detection**: Continuously monitors core container logs (orchestrator, observability-server, pattern-ingestion, etc.)
- **Intelligent Fix Cycles**: Uses Claude Code to analyze and fix detected errors
- **Safe Restart Strategy**: Waits for pipeline inactivity before restarting containers
- **Iterative Verification**: Monitors post-restart to verify fixes are effective
- **Full Observability**: Leverages existing event system with new Medic-specific events
- **Git Integration**: Automatically commits verified fixes to the Clauditoreum repository

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Component Structure](#component-structure)
3. [Error Detection & Accumulation](#error-detection--accumulation)
4. [Fix Cycle Workflow](#fix-cycle-workflow)
5. [Observability Integration](#observability-integration)
6. [Data Models](#data-models)
7. [Configuration](#configuration)
8. [Deployment](#deployment)
9. [Safety & Constraints](#safety--constraints)
10. [Future Enhancements](#future-enhancements)

---

## Architecture Overview

### High-Level Design

```
┌─────────────────────────────────────────────────────────────────┐
│                        Medic Container                           │
│                                                                  │
│  ┌────────────────┐         ┌─────────────────┐                │
│  │                │         │                 │                │
│  │  Log Monitor   │────────▶│  Error Queue    │                │
│  │  (every 1 min) │         │  (Elasticsearch)│                │
│  │                │         │                 │                │
│  └────────────────┘         └─────────────────┘                │
│         │                            │                          │
│         │                            ▼                          │
│         │                   ┌─────────────────┐                │
│         │                   │                 │                │
│         │                   │  Fix Cycle      │                │
│         │                   │  Orchestrator   │                │
│         │                   │  (every 1 hour) │                │
│         │                   │                 │                │
│         │                   └─────────────────┘                │
│         │                            │                          │
│         │                            ▼                          │
│         │                   ┌─────────────────┐                │
│         │                   │                 │                │
│         │                   │  Claude Code    │                │
│         │                   │  Executor       │                │
│         │                   │                 │                │
│         │                   └─────────────────┘                │
│         │                            │                          │
│         │                            ▼                          │
│         │                   ┌─────────────────┐                │
│         │                   │                 │                │
│         └──────────────────▶│  Restart        │                │
│                             │  Controller     │                │
│                             │                 │                │
│                             └─────────────────┘                │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
         │                            │
         │                            │
         ▼                            ▼
┌────────────────┐          ┌─────────────────┐
│                │          │                 │
│  Docker Socket │          │  Orchestrator   │
│  (log access)  │          │  Containers     │
│                │          │                 │
└────────────────┘          └─────────────────┘
```

### Key Design Principles

1. **Separation of Concerns**: Log monitoring runs continuously; fix cycles run periodically
2. **Eventual Consistency**: Errors accumulate in queue; fixes are applied in batches
3. **Safety First**: Never restart containers during active pipeline runs
4. **Iterative Improvement**: Post-restart monitoring verifies fix effectiveness
5. **Observability Native**: Full event instrumentation using existing patterns

---

## Component Structure

### Directory Layout

```
clauditoreum/
├── medic/
│   ├── __init__.py
│   ├── log_monitor.py          # Continuous log monitoring
│   ├── error_signature.py      # Error detection and fingerprinting
│   ├── error_queue.py          # Elasticsearch-backed work queue
│   ├── fix_cycle.py            # Periodic fix execution
│   ├── restart_controller.py   # Safe container restart logic
│   ├── verification.py         # Post-fix verification
│   └── config.py               # Medic configuration
├── services/
│   └── medic_service.py        # Main service entry point
└── config/
    └── medic.yaml              # Container list, schedules, thresholds
```

### Service Components

#### 1. Log Monitor (`medic/log_monitor.py`)

**Purpose**: Continuously monitors Docker logs for errors and warnings

**Responsibilities**:
- Poll Docker logs from monitored containers (every 1 minute)
- Parse log lines for error signatures (ERROR, CRITICAL, WARNING, exception traces)
- Extract error context (timestamp, container, log level, message, stack trace)
- Create error fingerprints for deduplication
- Enqueue new errors to Elasticsearch

**Key Methods**:
```python
class LogMonitor:
    async def monitor_loop(self):
        """Main monitoring loop (runs continuously)"""
        
    async def fetch_container_logs(self, container_name: str, since: datetime) -> List[LogLine]:
        """Fetch logs from a container since last check"""
        
    def extract_error_signature(self, log_line: LogLine) -> Optional[ErrorSignature]:
        """Extract error details and create signature"""
        
    async def enqueue_error(self, error: ErrorSignature):
        """Add error to Elasticsearch queue"""
```

#### 2. Error Queue (`medic/error_queue.py`)

**Purpose**: Elasticsearch-backed queue for error tracking and prioritization

**Responsibilities**:
- Store error signatures with status tracking
- Increment occurrence counts for recurring errors
- Support priority-based retrieval (by occurrence count)
- Track error lifecycle (new → in_analysis → fix_applied → verified/failed)

**Data Flow**:
```
LogMonitor → [enqueue] → ErrorQueue (ES) → [dequeue] → FixCycle
                                              ↓
                                         [update_status]
                                              ↓
                                         [verify] → verified/failed
```

#### 3. Fix Cycle Orchestrator (`medic/fix_cycle.py`)

**Purpose**: Periodic execution of fix cycles (similar to `repair_cycle.py`)

**Responsibilities**:
- Run every hour (configurable)
- Check for active pipeline runs (safety check)
- Dequeue errors from queue (prioritized by occurrence count)
- Execute Claude Code to diagnose and fix errors
- Track fix attempts and results
- Coordinate restart when fixes are ready

**Workflow**:
```python
class MedicFixCycle:
    async def execute_cycle(self):
        """Main fix cycle execution"""
        # 1. Safety check: wait for no active pipeline runs
        if not await self.is_safe_to_proceed():
            return
        
        # 2. Dequeue errors (prioritized)
        errors = await self.error_queue.get_pending_errors(limit=10)
        
        # 3. For each error, execute fix attempt
        for error in errors:
            result = await self.fix_error(error)
            await self.error_queue.update_status(error.id, result.status)
        
        # 4. If fixes applied, schedule restart
        if any(r.status == "fix_applied" for r in results):
            await self.schedule_restart()
        
        # 5. Post-restart: verify fixes
        if restart_occurred:
            await self.verify_fixes()
```

#### 4. Restart Controller (`medic/restart_controller.py`)

**Purpose**: Safely restart containers after fixes are applied

**Responsibilities**:
- Verify no active pipeline runs
- Gracefully stop containers
- Start containers in correct order (redis → orchestrator → etc.)
- Wait for health checks
- Monitor startup logs for errors

**Safety Guarantees**:
- Never restart during active pipeline runs
- Retry failed restarts with exponential backoff
- Emit observability events for restart lifecycle

#### 5. Verification Engine (`medic/verification.py`)

**Purpose**: Monitor post-restart logs to verify fix effectiveness

**Responsibilities**:
- Monitor logs for 10 minutes after restart
- Check if the same error signature reappears
- Update error status: `verified` (success) or `needs_retry` (failure)
- Track fix success rate metrics

---

## Error Detection & Accumulation

### Error Signature Model

Each detected error is fingerprinted for deduplication:

```python
@dataclass
class ErrorSignature:
    """Unique fingerprint of an error"""
    
    # Identification
    signature_hash: str          # MD5 hash of normalized error pattern
    container_name: str          # Source container
    log_level: str               # ERROR, CRITICAL, WARNING
    
    # Error details
    error_type: str              # Exception class or error pattern
    message: str                 # Error message (normalized)
    stack_trace: Optional[str]   # Full stack trace (if available)
    context_lines: List[str]     # Surrounding log lines
    
    # Occurrence tracking
    first_seen: str              # ISO timestamp
    last_seen: str               # ISO timestamp
    occurrence_count: int        # Number of times seen
    
    # Fix lifecycle
    status: ErrorStatus          # Enum: new, in_analysis, fix_applied, verified, failed
    fix_attempts: int            # Number of fix attempts
    last_fix_attempt: Optional[str]  # Timestamp of last fix
    
    # Metadata
    priority_score: float        # Calculated based on severity + occurrence
    related_files: List[str]     # Files likely involved (extracted from trace)
```

### Error Status Lifecycle

```
┌─────────┐
│   new   │  ← Error first detected
└────┬────┘
     │
     ▼
┌──────────────┐
│ in_analysis  │  ← Fix cycle picks up error
└──────┬───────┘
       │
       ▼
┌──────────────┐
│ fix_applied  │  ← Claude Code applied changes
└──────┬───────┘
       │
       ├─────────────┐
       │             │
       ▼             ▼
┌──────────┐   ┌─────────────┐
│ verified │   │ needs_retry │  ← Error still appears after restart
└──────────┘   └──────┬──────┘
                      │
                      └──────▶ (back to in_analysis if fix_attempts < max)
```

### Elasticsearch Index Structure

**Index**: `medic-error-queue-YYYY.MM.DD`

**Mapping**:
```json
{
  "mappings": {
    "properties": {
      "signature_hash": { "type": "keyword" },
      "container_name": { "type": "keyword" },
      "log_level": { "type": "keyword" },
      "error_type": { "type": "text" },
      "message": { "type": "text" },
      "stack_trace": { "type": "text" },
      "first_seen": { "type": "date" },
      "last_seen": { "type": "date" },
      "occurrence_count": { "type": "integer" },
      "status": { "type": "keyword" },
      "fix_attempts": { "type": "integer" },
      "priority_score": { "type": "float" },
      "related_files": { "type": "keyword" }
    }
  }
}
```

### Priority Calculation

```python
def calculate_priority(error: ErrorSignature) -> float:
    """
    Calculate priority score for error fixing order
    
    Higher score = higher priority
    """
    severity_weight = {
        "CRITICAL": 100,
        "ERROR": 50,
        "WARNING": 10
    }
    
    base_score = severity_weight.get(error.log_level, 1)
    occurrence_multiplier = min(error.occurrence_count, 100)  # Cap at 100x
    
    # Penalty for failed fix attempts
    failure_penalty = error.fix_attempts * 0.1
    
    return (base_score * occurrence_multiplier) - failure_penalty
```

---

## Fix Cycle Workflow

### Main Fix Cycle Algorithm

```python
async def execute_fix_cycle():
    """
    Main fix cycle that runs every hour
    
    Steps:
        1. Safety check (no active pipelines)
        2. Dequeue errors (prioritized)
        3. Analyze and fix each error
        4. Restart containers if fixes applied
        5. Verify fixes
        6. Commit to git if verified
    """
    
    # Emit cycle started event
    obs.emit(EventType.MEDIC_CYCLE_STARTED, ...)
    
    try:
        # Step 1: Wait for safety window
        if not await wait_for_safe_window(timeout=30*60):  # 30 min timeout
            logger.warning("Timed out waiting for safe window, skipping cycle")
            return
        
        # Step 2: Dequeue errors (top 10 by priority)
        errors = await error_queue.get_pending_errors(limit=10)
        
        if not errors:
            logger.info("No errors to fix")
            return
        
        # Step 3: Fix each error
        fix_results = []
        for error in errors:
            result = await fix_single_error(error)
            fix_results.append(result)
        
        # Step 4: Check if any fixes were applied
        fixes_applied = [r for r in fix_results if r.status == "fix_applied"]
        
        if not fixes_applied:
            logger.info("No fixes were applied this cycle")
            return
        
        # Step 5: Restart containers
        restart_result = await restart_containers()
        
        if not restart_result.success:
            logger.error(f"Container restart failed: {restart_result.error}")
            return
        
        # Step 6: Verification period (10 minutes)
        await asyncio.sleep(60)  # Wait 1 minute for startup
        verification_results = await verify_fixes(fixes_applied, duration=600)
        
        # Step 7: Commit verified fixes
        verified_fixes = [r for r in verification_results if r.verified]
        if verified_fixes:
            await commit_fixes_to_git(verified_fixes)
        
        # Emit cycle completed event
        obs.emit(EventType.MEDIC_CYCLE_COMPLETED, ...)
        
    except Exception as e:
        logger.error(f"Fix cycle failed: {e}")
        obs.emit(EventType.MEDIC_CYCLE_FAILED, ...)
```

### Error Fixing with Claude Code

```python
async def fix_single_error(error: ErrorSignature) -> FixResult:
    """
    Use Claude Code to analyze and fix a single error
    
    Steps:
        1. Build context (error details, related files, recent logs)
        2. Execute Claude Code in Clauditoreum workspace
        3. Parse result (success/failure, files changed)
        4. Update error status
    """
    
    # Emit fix started event
    obs.emit(EventType.MEDIC_FIX_STARTED, error_id=error.signature_hash, ...)
    
    # Step 1: Build prompt for Claude
    prompt = f"""
You are analyzing an error in the Clauditoreum orchestrator system.

**Error Details:**
- Container: {error.container_name}
- Type: {error.error_type}
- Message: {error.message}
- Occurrences: {error.occurrence_count}
- First seen: {error.first_seen}
- Last seen: {error.last_seen}

**Stack Trace:**
{error.stack_trace}

**Context (surrounding logs):**
{chr(10).join(error.context_lines)}

**Related Files (from stack trace):**
{chr(10).join(error.related_files)}

**Task:**
1. Analyze the root cause of this error
2. Identify the files that need to be modified
3. Apply a fix to prevent this error from occurring
4. Ensure the fix doesn't break existing functionality

Please analyze and fix this error. Make all necessary code changes.
"""
    
    # Step 2: Execute Claude Code (using existing AgentExecutor pattern)
    from services.agent_executor import get_agent_executor
    
    agent_executor = get_agent_executor()
    
    task_context = {
        "project": "clauditoreum",
        "task_description": f"Fix error: {error.error_type}",
        "direct_prompt": prompt,
        "timeout": 900,  # 15 minutes
        "workspace_path": "/workspace/clauditoreum",
    }
    
    try:
        result = await agent_executor.execute_agent(
            agent_name="senior_software_engineer",
            project_name="clauditoreum",
            task_context=task_context,
            task_id_prefix=f"medic_fix_{error.signature_hash[:8]}"
        )
        
        # Step 3: Update error status
        await error_queue.update_status(
            error.signature_hash,
            status="fix_applied",
            fix_attempts=error.fix_attempts + 1
        )
        
        # Emit fix completed event
        obs.emit(EventType.MEDIC_FIX_COMPLETED, ...)
        
        return FixResult(
            error_id=error.signature_hash,
            status="fix_applied",
            files_changed=extract_changed_files(result),
            success=True
        )
        
    except Exception as e:
        logger.error(f"Fix failed for {error.signature_hash}: {e}")
        
        await error_queue.update_status(
            error.signature_hash,
            status="needs_retry" if error.fix_attempts < 3 else "failed"
        )
        
        # Emit fix failed event
        obs.emit(EventType.MEDIC_FIX_FAILED, ...)
        
        return FixResult(
            error_id=error.signature_hash,
            status="failed",
            error=str(e),
            success=False
        )
```

### Safe Restart Strategy

```python
async def wait_for_safe_window(timeout: int = 1800) -> bool:
    """
    Wait for a safe window to restart containers
    
    Safe window conditions:
        - No active pipeline runs
        - No agents currently executing
        - At least 5 minutes since last task completion
    
    Args:
        timeout: Maximum seconds to wait (default: 30 minutes)
    
    Returns:
        True if safe window achieved, False if timeout
    """
    
    start_time = utc_now()
    check_interval = 30  # seconds
    
    while (utc_now() - start_time).total_seconds() < timeout:
        # Check 1: Active pipeline runs
        active_runs = await get_active_pipeline_runs()
        if active_runs:
            logger.info(f"Waiting for {len(active_runs)} pipeline runs to complete")
            await asyncio.sleep(check_interval)
            continue
        
        # Check 2: Active agents
        active_agents = await get_active_agents()
        if active_agents:
            logger.info(f"Waiting for {len(active_agents)} agents to complete")
            await asyncio.sleep(check_interval)
            continue
        
        # Check 3: Recent activity (5 min cooldown)
        last_task_time = await get_last_task_completion_time()
        if last_task_time and (utc_now() - last_task_time).total_seconds() < 300:
            logger.info("Waiting for cooldown period after last task")
            await asyncio.sleep(check_interval)
            continue
        
        # All checks passed - safe to proceed
        logger.info("Safe window achieved for container restart")
        return True
    
    # Timeout reached
    logger.warning(f"Safe window timeout after {timeout} seconds")
    return False


async def restart_containers() -> RestartResult:
    """
    Restart core orchestrator containers
    
    Order:
        1. Stop: orchestrator → observability-server → pattern-ingestion
        2. Start: observability-server → pattern-ingestion → orchestrator
    
    Returns:
        RestartResult with success status and details
    """
    
    # Emit restart started event
    obs.emit(EventType.MEDIC_RESTART_STARTED, ...)
    
    containers_to_restart = [
        "orchestrator",
        "observability-server",
        "pattern-ingestion"
    ]
    
    try:
        # Step 1: Stop containers (reverse order)
        for container in reversed(containers_to_restart):
            logger.info(f"Stopping {container}")
            subprocess.run(
                ["docker", "stop", f"clauditoreum_{container}_1"],
                check=True,
                timeout=30
            )
        
        # Step 2: Start containers (forward order)
        for container in containers_to_restart:
            logger.info(f"Starting {container}")
            subprocess.run(
                ["docker", "start", f"clauditoreum_{container}_1"],
                check=True,
                timeout=30
            )
        
        # Step 3: Wait for health checks
        await wait_for_health_checks(containers_to_restart, timeout=120)
        
        # Emit restart completed event
        obs.emit(EventType.MEDIC_RESTART_COMPLETED, ...)
        
        return RestartResult(success=True)
        
    except Exception as e:
        logger.error(f"Container restart failed: {e}")
        
        # Emit restart failed event
        obs.emit(EventType.MEDIC_RESTART_FAILED, error=str(e))
        
        return RestartResult(success=False, error=str(e))
```

### Post-Restart Verification

```python
async def verify_fixes(
    fixes_applied: List[FixResult],
    duration: int = 600
) -> List[VerificationResult]:
    """
    Monitor logs after restart to verify fixes are effective
    
    Args:
        fixes_applied: List of fixes that were applied
        duration: Verification window in seconds (default: 10 minutes)
    
    Returns:
        List of verification results for each fix
    """
    
    # Emit verification started event
    obs.emit(EventType.MEDIC_VERIFICATION_STARTED, ...)
    
    results = []
    
    # Create error signature monitors
    error_monitors = {
        fix.error_id: ErrorMonitor(fix.error_id)
        for fix in fixes_applied
    }
    
    # Monitor logs for duration
    start_time = utc_now()
    while (utc_now() - start_time).total_seconds() < duration:
        # Fetch logs from all containers
        logs = await fetch_recent_logs(since=start_time)
        
        # Check each log line against error signatures
        for log_line in logs:
            for error_id, monitor in error_monitors.items():
                if monitor.matches(log_line):
                    logger.warning(f"Error {error_id[:8]} reappeared after fix")
                    monitor.record_occurrence()
        
        await asyncio.sleep(10)  # Check every 10 seconds
    
    # Generate verification results
    for fix in fixes_applied:
        monitor = error_monitors[fix.error_id]
        
        if monitor.occurrence_count == 0:
            # Success - error did not reappear
            await error_queue.update_status(fix.error_id, status="verified")
            
            result = VerificationResult(
                error_id=fix.error_id,
                verified=True,
                occurrence_count=0
            )
        else:
            # Failure - error still occurring
            await error_queue.update_status(fix.error_id, status="needs_retry")
            
            result = VerificationResult(
                error_id=fix.error_id,
                verified=False,
                occurrence_count=monitor.occurrence_count
            )
        
        results.append(result)
        
        # Emit individual verification event
        obs.emit(
            EventType.MEDIC_FIX_VERIFIED if result.verified else EventType.MEDIC_FIX_FAILED_VERIFICATION,
            error_id=fix.error_id,
            ...
        )
    
    # Emit verification completed event
    obs.emit(EventType.MEDIC_VERIFICATION_COMPLETED, ...)
    
    return results
```

---

## Observability Integration

### New Event Types

Add to `monitoring/observability.py`:

```python
class EventType(Enum):
    # ... existing events ...
    
    # Medic Cycle Events
    MEDIC_CYCLE_STARTED = "medic_cycle_started"
    MEDIC_CYCLE_COMPLETED = "medic_cycle_completed"
    MEDIC_CYCLE_FAILED = "medic_cycle_failed"
    
    # Error Detection Events
    MEDIC_ERROR_DETECTED = "medic_error_detected"
    MEDIC_ERROR_QUEUED = "medic_error_queued"
    MEDIC_ERROR_DEDUPLICATED = "medic_error_deduplicated"
    
    # Fix Events
    MEDIC_FIX_STARTED = "medic_fix_started"
    MEDIC_FIX_COMPLETED = "medic_fix_completed"
    MEDIC_FIX_FAILED = "medic_fix_failed"
    
    # Restart Events
    MEDIC_RESTART_STARTED = "medic_restart_started"
    MEDIC_RESTART_COMPLETED = "medic_restart_completed"
    MEDIC_RESTART_FAILED = "medic_restart_failed"
    MEDIC_RESTART_DELAYED = "medic_restart_delayed"
    
    # Verification Events
    MEDIC_VERIFICATION_STARTED = "medic_verification_started"
    MEDIC_VERIFICATION_COMPLETED = "medic_verification_completed"
    MEDIC_FIX_VERIFIED = "medic_fix_verified"
    MEDIC_FIX_FAILED_VERIFICATION = "medic_fix_failed_verification"
    
    # Safety Events
    MEDIC_WAITING_FOR_SAFE_WINDOW = "medic_waiting_for_safe_window"
    MEDIC_SAFE_WINDOW_ACHIEVED = "medic_safe_window_achieved"
    MEDIC_SAFE_WINDOW_TIMEOUT = "medic_safe_window_timeout"
```

### Event Emission Examples

```python
# Cycle started
obs.emit(
    EventType.MEDIC_CYCLE_STARTED,
    agent="medic",
    task_id=f"medic_cycle_{cycle_id}",
    project="clauditoreum",
    data={
        "cycle_id": cycle_id,
        "pending_errors": len(errors),
        "schedule": "hourly",
        "timestamp": utc_isoformat()
    }
)

# Error detected
obs.emit(
    EventType.MEDIC_ERROR_DETECTED,
    agent="medic_monitor",
    task_id=f"monitor_{container_name}",
    project="clauditoreum",
    data={
        "container_name": container_name,
        "error_type": error.error_type,
        "log_level": error.log_level,
        "signature_hash": error.signature_hash,
        "message": error.message[:200]  # Truncate
    }
)

# Fix completed
obs.emit(
    EventType.MEDIC_FIX_COMPLETED,
    agent="medic",
    task_id=f"medic_fix_{error_id[:8]}",
    project="clauditoreum",
    data={
        "error_id": error_id,
        "error_type": error.error_type,
        "files_changed": result.files_changed,
        "fix_attempt": error.fix_attempts,
        "duration_ms": duration_ms
    }
)

# Restart delayed (safety)
obs.emit(
    EventType.MEDIC_RESTART_DELAYED,
    agent="medic",
    task_id=f"medic_cycle_{cycle_id}",
    project="clauditoreum",
    data={
        "reason": "active_pipeline_runs",
        "active_runs": len(active_runs),
        "retry_in_seconds": 300
    }
)
```

### Integration with Pipeline Run System

Medic fix cycles should create pipeline runs for tracking:

```python
from services.pipeline_run import PipelineRunManager, PipelineRun

async def execute_fix_cycle():
    """Execute fix cycle with pipeline run tracking"""
    
    # Create pipeline run for this medic cycle
    pipeline_run_manager = PipelineRunManager()
    
    pipeline_run = PipelineRun(
        id=f"medic_{cycle_id}",
        issue_number=0,  # N/A for medic
        issue_title=f"Medic Fix Cycle {cycle_id}",
        issue_url="",
        project="clauditoreum",
        board="medic",
        started_at=utc_isoformat(),
        status="active"
    )
    
    await pipeline_run_manager.create_pipeline_run(pipeline_run)
    
    try:
        # Execute cycle...
        # (all events will be tagged with pipeline_run_id)
        
        # Complete pipeline run
        await pipeline_run_manager.complete_pipeline_run(pipeline_run.id)
        
    except Exception as e:
        # Mark as failed
        await pipeline_run_manager.fail_pipeline_run(pipeline_run.id, str(e))
```

---

## Data Models

### Core Data Classes

```python
from dataclasses import dataclass
from typing import List, Optional
from enum import Enum
from datetime import datetime


class ErrorStatus(Enum):
    """Error lifecycle status"""
    NEW = "new"
    IN_ANALYSIS = "in_analysis"
    FIX_APPLIED = "fix_applied"
    VERIFIED = "verified"
    NEEDS_RETRY = "needs_retry"
    FAILED = "failed"
    IGNORED = "ignored"  # User-marked to ignore


@dataclass
class ErrorSignature:
    """Unique fingerprint of an error (see earlier definition)"""
    signature_hash: str
    container_name: str
    log_level: str
    error_type: str
    message: str
    stack_trace: Optional[str]
    context_lines: List[str]
    first_seen: str
    last_seen: str
    occurrence_count: int
    status: ErrorStatus
    fix_attempts: int
    last_fix_attempt: Optional[str]
    priority_score: float
    related_files: List[str]


@dataclass
class FixResult:
    """Result of a single error fix attempt"""
    error_id: str
    status: str  # "fix_applied", "failed", "skipped"
    files_changed: List[str]
    success: bool
    error: Optional[str] = None
    duration_ms: Optional[float] = None


@dataclass
class VerificationResult:
    """Result of post-restart verification"""
    error_id: str
    verified: bool
    occurrence_count: int  # How many times error appeared during verification
    verification_duration: int  # Seconds monitored


@dataclass
class RestartResult:
    """Result of container restart operation"""
    success: bool
    containers_restarted: List[str]
    error: Optional[str] = None
    duration_seconds: Optional[float] = None


@dataclass
class MedicCycleResult:
    """Result of complete medic fix cycle"""
    cycle_id: str
    started_at: str
    ended_at: str
    errors_analyzed: int
    fixes_applied: int
    fixes_verified: int
    fixes_failed: int
    restart_performed: bool
    success: bool
    error: Optional[str] = None
```

---

## Configuration

### Configuration File: `config/medic.yaml`

```yaml
medic:
  # Monitoring configuration
  monitoring:
    enabled: true
    interval_seconds: 60  # Check logs every minute
    containers:
      - orchestrator
      - observability-server
      - pattern-ingestion
      - elasticsearch
      - redis
    # Log levels to monitor
    log_levels:
      - ERROR
      - CRITICAL
      - WARNING  # Optional, can be disabled
    # Patterns to ignore (false positives)
    ignore_patterns:
      - "Connection pool is full"  # Expected under load
      - "Temporary network error"   # Self-recovering
  
  # Fix cycle configuration
  fix_cycle:
    enabled: true
    schedule: "0 * * * *"  # Cron: every hour at :00
    max_errors_per_cycle: 10
    max_fix_attempts: 3
    timeout_seconds: 900  # 15 minutes per fix
  
  # Restart configuration
  restart:
    enabled: true
    safe_window_timeout: 1800  # 30 minutes
    cooldown_after_task: 300   # 5 minutes
    health_check_timeout: 120  # 2 minutes
    containers_order:
      stop:
        - orchestrator
        - observability-server
        - pattern-ingestion
      start:
        - observability-server
        - pattern-ingestion
        - orchestrator
  
  # Verification configuration
  verification:
    enabled: true
    duration_seconds: 600  # 10 minutes
    check_interval: 10     # Check logs every 10 seconds
  
  # Git integration
  git:
    enabled: true
    auto_commit: true
    branch: "main"  # Commit directly to main (self-healing)
    commit_message_template: "🔧 Medic: Auto-fix {error_type} in {container}"
  
  # Elasticsearch configuration
  elasticsearch:
    index_prefix: "medic-error-queue"
    retention_days: 30
```

### Environment Variables

Add to `.env`:

```bash
# Medic configuration
MEDIC_ENABLED=true
MEDIC_MONITORING_INTERVAL=60
MEDIC_FIX_SCHEDULE="0 * * * *"
MEDIC_MAX_ERRORS_PER_CYCLE=10
MEDIC_AUTO_COMMIT=true
```

---

## Deployment

### Docker Compose Service

Add to `docker-compose.yml`:

```yaml
services:
  # ... existing services ...
  
  medic:
    build: .
    container_name: clauditoreum_medic
    volumes:
      - ./:/app
      - ..:/workspace
      - /var/run/docker.sock:/var/run/docker.sock  # Docker access
      - ~/.ssh/id_ed25519:/home/orchestrator/.ssh/id_ed25519:ro
      - ~/.gitconfig:/home/orchestrator/.gitconfig:ro
    environment:
      - REDIS_URL=redis://redis:6379
      - ELASTICSEARCH_HOSTS=http://elasticsearch:9200
      - MEDIC_ENABLED=${MEDIC_ENABLED:-true}
      - MEDIC_MONITORING_INTERVAL=${MEDIC_MONITORING_INTERVAL:-60}
      - MEDIC_FIX_SCHEDULE=${MEDIC_FIX_SCHEDULE:-0 * * * *}
      - LOG_LEVEL=${LOG_LEVEL:-INFO}
    depends_on:
      - redis
      - elasticsearch
    networks:
      - orchestrator-net
    working_dir: /app
    command: ["python", "-m", "services.medic_service"]
    restart: unless-stopped
```

### Service Entry Point: `services/medic_service.py`

```python
"""
Medic Service - Self-healing orchestrator monitor

Runs two concurrent tasks:
    1. Log monitor (continuous)
    2. Fix cycle (periodic - every hour)
"""

import asyncio
import logging
from medic.log_monitor import LogMonitor
from medic.fix_cycle import MedicFixCycle
from medic.config import load_medic_config

logger = logging.getLogger(__name__)


async def main():
    """Main entry point for Medic service"""
    
    logger.info("Starting Medic service...")
    
    # Load configuration
    config = load_medic_config()
    
    if not config.enabled:
        logger.warning("Medic is disabled in configuration")
        return
    
    # Initialize components
    log_monitor = LogMonitor(config.monitoring)
    fix_cycle = MedicFixCycle(config.fix_cycle)
    
    # Run both tasks concurrently
    try:
        await asyncio.gather(
            log_monitor.run(),      # Continuous monitoring
            fix_cycle.run(),        # Periodic fix cycles
            return_exceptions=True
        )
    except Exception as e:
        logger.error(f"Medic service failed: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Safety & Constraints

### Critical Safety Rules

1. **Never Restart During Active Work**
   - Check for active pipeline runs
   - Check for active agent containers
   - Enforce cooldown period after last task

2. **Max Fix Attempts**
   - Limit to 3 fix attempts per error
   - Mark as "failed" after max attempts
   - Require manual intervention for failed fixes

3. **Verification Required**
   - All fixes must be verified after restart
   - Failed verification → requeue for retry
   - Only commit verified fixes to git

4. **Graceful Degradation**
   - If Elasticsearch is down, buffer errors in Redis
   - If restart fails, retry with exponential backoff
   - If verification fails, revert changes (optional)

### Resource Limits

```python
# In medic/config.py

class MedicLimits:
    """Resource and safety limits"""
    
    # Fix cycle limits
    MAX_ERRORS_PER_CYCLE = 10
    MAX_FIX_ATTEMPTS_PER_ERROR = 3
    MAX_CYCLE_DURATION = 3600  # 1 hour
    
    # Restart limits
    MAX_RESTART_ATTEMPTS = 3
    RESTART_BACKOFF_BASE = 60  # Exponential backoff base
    SAFE_WINDOW_TIMEOUT = 1800  # 30 minutes
    
    # Monitoring limits
    MAX_LOG_LINES_PER_FETCH = 1000
    LOG_FETCH_TIMEOUT = 30  # seconds
    
    # Queue limits
    MAX_QUEUE_SIZE = 10000
    ERROR_RETENTION_DAYS = 30
```

### Error Handling

```python
# Retry decorator for critical operations
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=60),
    reraise=True
)
async def critical_operation():
    """Operations that should be retried"""
    pass
```

---

## Future Enhancements

### Phase 2 Features

1. **Pattern Learning**
   - Analyze fix success rates
   - Learn common error patterns
   - Auto-generate prevention rules

2. **Predictive Monitoring**
   - Detect error trends before they become critical
   - Alert on increasing error rates
   - Proactive fix suggestions

3. **Multi-Repository Support**
   - Monitor managed project containers
   - Fix errors in managed projects
   - Coordinate fixes across repositories

4. **Human Feedback Loop**
   - Slack/GitHub notifications for critical errors
   - Manual approval for high-risk fixes
   - Learn from manual interventions

5. **Rollback Capability**
   - Git rollback for failed fixes
   - Container snapshot/restore
   - Automatic rollback on verification failure

### Phase 3 Features

1. **Advanced Analytics**
   - Error correlation analysis
   - Root cause clustering
   - Cost analysis (tokens, time, resources)

2. **Intelligent Scheduling**
   - Dynamic fix cycle timing based on system load
   - Priority-based queue management
   - Adaptive verification windows

3. **Self-Optimization**
   - Tune monitoring thresholds automatically
   - Optimize fix cycle parameters
   - Learn optimal restart windows

---

## Implementation Checklist

### Phase 1: MVP (Core Functionality)

- [ ] Create `medic/` module structure
- [ ] Implement `LogMonitor` with Docker logs API
- [ ] Implement `ErrorQueue` with Elasticsearch
- [ ] Implement `MedicFixCycle` orchestrator
- [ ] Implement `RestartController`
- [ ] Implement `VerificationEngine`
- [ ] Add Medic event types to `monitoring/observability.py`
- [ ] Create `config/medic.yaml` configuration
- [ ] Add Medic service to `docker-compose.yml`
- [ ] Write unit tests for core components
- [ ] Write integration tests for fix cycle
- [ ] Update documentation

### Phase 2: Integration

- [ ] Integrate with `AgentExecutor` for Claude Code execution
- [ ] Integrate with `PipelineRunManager` for tracking
- [ ] Add Medic dashboard to web UI
- [ ] Implement git commit functionality
- [ ] Add observability server endpoints for Medic status

### Phase 3: Validation

- [ ] Test in development environment
- [ ] Simulate errors and verify fixes
- [ ] Test restart safety mechanisms
- [ ] Verify observability event flow
- [ ] Performance testing (resource usage)

---

## Conclusion

The **Medic** component represents a sophisticated self-healing capability for the Clauditoreum orchestrator. By continuously monitoring container logs, intelligently applying fixes using Claude Code, and carefully coordinating restarts only when safe, the Medic ensures the orchestrator maintains high availability and reliability with minimal human intervention.

Key design strengths:

1. **Separation of Concerns**: Monitoring and fixing are decoupled
2. **Safety First**: Multiple layers of safety checks prevent disruption
3. **Observability Native**: Full integration with existing event system
4. **Iterative Verification**: Post-fix monitoring ensures effectiveness
5. **Git Integration**: Verified fixes are automatically committed

This design provides a solid foundation for autonomous system maintenance while maintaining the flexibility for future enhancements like pattern learning, predictive monitoring, and multi-repository support.

---

**Next Steps**: Review this design with the team, gather feedback, and proceed with Phase 1 implementation.

# Root Cause Diagnosis

**Failure Signature:** `sha256:da1465c55e2e607be5f17f4ba799b52c6fff72fc525b4466ed9daf7c8ac16f09`
**Investigation Date:** 2025-11-28

## Error Summary
The project monitor thread crashes with a fatal error when it encounters a newly added project configuration file that hasn't been reconciled yet, causing the orchestrator to exit.

## Root Cause Analysis

This is a **race condition** between the project monitor thread and the main orchestrator thread during runtime configuration changes.

**Sequence of Events:**
1. At 19:11:37 - New project config file created: `config/projects/utterance_emitter.yaml`
2. At 19:11:42 (5 seconds later) - Project monitor iteration detects the new project via `list_visible_projects()`
3. Project monitor attempts to load GitHub state for `utterance_emitter`
4. State file doesn't exist (hasn't been reconciled yet)
5. Monitor treats missing state as FATAL and calls `exit(1)`, crashing the orchestrator
6. At 19:18:27 - Main orchestrator thread would have reconciled the project (if it hadn't crashed)

**Code Location (services/project_monitor.py:3520-3530):**
```python
for project_name in self.config_manager.list_visible_projects():
    project_config = self.config_manager.get_project_config(project_name)

    # Get project state to find actual GitHub project numbers
    project_state = state_manager.load_project_state(project_name)
    if not project_state:
        logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
        logger.error("This indicates GitHub project management failed during reconciliation")
        logger.error("Project monitoring cannot function without GitHub project state")
        logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
        exit(1)  # Fatal error - stop immediately
```

**Why This Happens:**
- The project monitor runs in a daemon thread (main.py:416-422) and continuously polls projects
- Configuration changes are detected by `list_visible_projects()` which scans filesystem
- New project configs appear immediately but GitHub state reconciliation happens asynchronously
- The monitor assumes missing state = reconciliation failure, but it's actually just "not yet reconciled"
- The fatal `exit(1)` was designed for persistent reconciliation failures, not transient race conditions

## Evidence

### Log Analysis
```
2025-11-28 19:11:37 - Config file created (filesystem timestamp)
2025-11-28 19:11:42 - ERROR - FATAL: No GitHub state found for project 'utterance_emitter'
2025-11-28 19:11:42 - ERROR - Project monitoring cannot function without GitHub project state
2025-11-28 19:11:42 - ERROR - STOPPING PROJECT MONITOR: Core functionality is broken
2025-11-28 19:18:27 - INFO - Reconciling project configuration: utterance_emitter (config changed)
2025-11-28 19:18:40 - INFO - Successfully reconciled project: utterance_emitter
```

The 7-minute gap (19:11:42 to 19:18:27) represents the orchestrator restart time.

### Code Analysis
**services/project_monitor.py:3524-3530**
- Uses `exit(1)` which terminates the entire process, not just the thread
- No distinction between "state not found" vs "reconciliation failed"
- No grace period or retry logic for newly added projects

**main.py:379-422**
- Reconciliation loop at lines 379-414 (main thread)
- Monitor starts at lines 416-422 (daemon thread)
- Both iterate `list_visible_projects()` but no synchronization

## Impact Assessment
- **Severity:** High (causes orchestrator crash and restart)
- **Frequency:** Occurs every time a new project config is added at runtime
- **Affected Components:**
  - Project Monitor (crashes)
  - All active agents (interrupted during restart)
  - Pipeline locks (require recovery)
  - Active repair cycles (require recovery)

## System State

**Project Config File:**
- Created: 2025-11-28 19:11:37
- Contains valid configuration for `utterance_emitter` project

**GitHub State File:**
- Created: 2025-11-28 19:18:29 (after restart and reconciliation)
- Contains valid board mappings for 3 pipelines

**Container State:**
- Orchestrator restarted after crash
- Successfully reconciled project after restart
- Project now functioning normally

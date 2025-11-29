# Root Cause Diagnosis

**Failure Signature:** `sha256:0c9036180c057ccc500503e5d299d7e8dd783e1ce79193485d7096d7724406eb`
**Investigation Date:** 2025-11-28

## Error Summary
Orchestrator startup fails with "FATAL: No GitHub state found for project 'utterance_emitter'" during pipeline lock recovery, causing the orchestrator to exit with code 1.

## Root Cause Analysis

The error is caused by a **startup sequence race condition** in `main.py`. The orchestrator performs these operations in the following order:

1. **Line 196-230**: Creates a temporary `ProjectMonitor` instance for pipeline lock recovery
2. **Line 382-406**: Performs GitHub project reconciliation (which creates the state files)

The temporary `ProjectMonitor` at line 196 attempts to access GitHub state for all visible projects during lock recovery, **before** those projects have been reconciled and their state files created.

### Code Flow

**main.py:196-199**
```python
# Create a temporary ProjectMonitor instance for checking issue columns
temp_monitor = ProjectMonitor(task_queue, config_manager)

for project_name in config_manager.list_visible_projects():
```

This temporary monitor is used to check if locked issues are still in active columns:

**main.py:225-230**
```python
lock_holder_column = temp_monitor._get_issue_column_name(
    project_name, lock.locked_by_issue
)
```

The `_get_issue_column_name()` method in `ProjectMonitor` requires GitHub state to be loaded:

**services/project_monitor.py:3520-3530**
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

## Evidence

### Log Analysis

**Timeline from orchestrator logs:**

```
2025-11-28 19:11:42 - ERROR - FATAL: No GitHub state found for project 'utterance_emitter'
2025-11-28 19:11:42 - ERROR - This indicates GitHub project management failed during reconciliation

[7 minutes later...]

2025-11-28 19:18:27 - INFO - Reconciling project configuration: utterance_emitter (config changed)
2025-11-28 19:18:40 - INFO - Successfully reconciled project: utterance_emitter
```

The error occurs at 19:11:42, but reconciliation doesn't happen until 19:18:27 (after a restart).

### Code Analysis

The startup sequence in main.py shows the problematic order:

1. Line 196: `temp_monitor = ProjectMonitor(task_queue, config_manager)` - Creates monitor
2. Line 199-230: Uses temp_monitor to check issue columns (requires GitHub state)
3. Line 382-406: **LATER** - Performs GitHub reconciliation that creates state files

### System State

The `utterance_emitter` project exists in config but had no state file at startup:
- Config file: `/workspace/clauditoreum/config/projects/utterance_emitter.yaml` (exists)
- State file: `/workspace/clauditoreum/state/projects/utterance_emitter/github_state.yaml` (didn't exist until reconciliation)

This scenario occurs when:
- A new project is added to config
- The orchestrator hasn't reconciled it yet
- The orchestrator is restarted before first reconciliation

## Impact Assessment

- **Severity:** Medium
- **Frequency:** Once per new project addition (transient issue)
- **Affected Components:**
  - Orchestrator startup sequence
  - Pipeline lock recovery
  - New project onboarding
- **Actual Impact:**
  - Orchestrator exits with fatal error on startup
  - Requires restart to recover (which then succeeds after reconciliation)
  - One-time issue per new project, self-correcting on second startup

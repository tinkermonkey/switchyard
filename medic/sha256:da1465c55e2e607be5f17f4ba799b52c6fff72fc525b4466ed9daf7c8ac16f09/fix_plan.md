# Fix Plan

**Failure Signature:** `sha256:da1465c55e2e607be5f17f4ba799b52c6fff72fc525b4466ed9daf7c8ac16f09`

## Proposed Solution

Replace the fatal error handling with graceful skipping of projects without GitHub state, allowing them to be reconciled on the next iteration.

**High-level approach:**
1. Change missing state from FATAL error to WARNING
2. Skip monitoring for projects without state (don't crash)
3. Trigger reconciliation signal for main thread to handle
4. Continue monitoring other projects normally

## Implementation Steps

### Step 1: Modify Error Handling in Project Monitor

**File:** `services/project_monitor.py` (lines 3524-3530)

Change from fatal exit to graceful skip:

```python
# Before (FATAL):
project_state = state_manager.load_project_state(project_name)
if not project_state:
    logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
    logger.error("This indicates GitHub project management failed during reconciliation")
    logger.error("Project monitoring cannot function without GitHub project state")
    logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
    exit(1)  # Fatal error - stop immediately

# After (SKIP):
project_state = state_manager.load_project_state(project_name)
if not project_state:
    # New project detected without state - needs reconciliation
    if not hasattr(self, '_missing_state_warnings'):
        self._missing_state_warnings = {}

    # Warn once per project, then periodically
    now = time.time()
    last_warning = self._missing_state_warnings.get(project_name, 0)
    if now - last_warning > 60:  # Warn every 60 seconds
        logger.warning(
            f"Project '{project_name}' has no GitHub state yet - skipping until reconciliation completes. "
            f"This is expected for newly added projects."
        )
        self._missing_state_warnings[project_name] = now

    continue  # Skip this project and continue monitoring others
```

### Step 2: Similar Fix for Board State

**File:** `services/project_monitor.py` (lines 3538-3543)

Apply same pattern for missing board state:

```python
# Before (FATAL):
board_state = project_state.boards.get(pipeline.board_name)
if not board_state:
    logger.error(f"FATAL: No GitHub state found for board '{pipeline.board_name}' in project '{project_name}'")
    logger.error("This indicates GitHub project board creation failed during reconciliation")
    logger.error("STOPPING PROJECT MONITOR: GitHub board management is broken")
    exit(1)  # Fatal error - stop immediately

# After (SKIP):
board_state = project_state.boards.get(pipeline.board_name)
if not board_state:
    # Board not in state yet - reconciliation in progress
    if not hasattr(self, '_missing_board_warnings'):
        self._missing_board_warnings = {}

    board_key = f"{project_name}:{pipeline.board_name}"
    now = time.time()
    last_warning = self._missing_board_warnings.get(board_key, 0)
    if now - last_warning > 60:
        logger.warning(
            f"Board '{pipeline.board_name}' for project '{project_name}' has no GitHub state yet - "
            f"skipping until reconciliation completes."
        )
        self._missing_board_warnings[board_key] = now

    continue  # Skip this board and continue with others
```

### Step 3: Add Import Statement

**File:** `services/project_monitor.py` (top of file)

Ensure `time` module is imported:

```python
import time
```

## Code Changes Required

### File: services/project_monitor.py

**Location 1 (around line 3524):**
```python
# Current problematic code
project_state = state_manager.load_project_state(project_name)
if not project_state:
    logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
    logger.error("This indicates GitHub project management failed during reconciliation")
    logger.error("Project monitoring cannot function without GitHub project state")
    logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
    exit(1)

# Proposed fix
project_state = state_manager.load_project_state(project_name)
if not project_state:
    if not hasattr(self, '_missing_state_warnings'):
        self._missing_state_warnings = {}

    now = time.time()
    last_warning = self._missing_state_warnings.get(project_name, 0)
    if now - last_warning > 60:
        logger.warning(
            f"Project '{project_name}' has no GitHub state yet - skipping until reconciliation completes. "
            f"This is expected for newly added projects."
        )
        self._missing_state_warnings[project_name] = now

    continue
```

**Location 2 (around line 3538):**
```python
# Current problematic code
board_state = project_state.boards.get(pipeline.board_name)
if not board_state:
    logger.error(f"FATAL: No GitHub state found for board '{pipeline.board_name}' in project '{project_name}'")
    logger.error("This indicates GitHub project board creation failed during reconciliation")
    logger.error("STOPPING PROJECT MONITOR: GitHub board management is broken")
    exit(1)

# Proposed fix
board_state = project_state.boards.get(pipeline.board_name)
if not board_state:
    if not hasattr(self, '_missing_board_warnings'):
        self._missing_board_warnings = {}

    board_key = f"{project_name}:{pipeline.board_name}"
    now = time.time()
    last_warning = self._missing_board_warnings.get(board_key, 0)
    if now - last_warning > 60:
        logger.warning(
            f"Board '{pipeline.board_name}' for project '{project_name}' has no GitHub state yet - "
            f"skipping until reconciliation completes."
        )
        self._missing_board_warnings[board_key] = now

    continue
```

## Testing Strategy

### Unit Test
Create test case that simulates the race condition:

```python
def test_monitor_handles_missing_state_gracefully():
    """Test that monitor skips projects without state instead of crashing"""
    # Setup: Create project config without state file
    # Execute: Run one monitor iteration
    # Assert: Monitor continues (doesn't exit)
    # Assert: Warning logged
    # Assert: Other projects still monitored
```

### Integration Test
1. Start orchestrator with existing projects
2. Add new project config file at runtime
3. Verify orchestrator continues running
4. Verify project is skipped with warning
5. Wait for reconciliation
6. Verify project starts being monitored

### Manual Test
1. Start orchestrator
2. Copy a new project YAML to `config/projects/`
3. Watch logs - should see warnings, not crash
4. Verify new project appears in monitoring after reconciliation

## Risks and Considerations

**Low Risk:**
- Change only affects error handling path that currently crashes
- Graceful degradation is safer than hard crash
- No changes to reconciliation logic itself

**Potential Side Effects:**
- Projects genuinely failing reconciliation will be silently skipped
  - **Mitigation:** Keep warning logs with periodic reminders
  - **Future Enhancement:** Add metric for projects stuck in "no state" for >5 minutes

**Backwards Compatibility:**
- Fully compatible - only changes error handling behavior
- No API or data format changes

## Deployment Plan

1. **Pre-deployment:**
   - Review changes with project owner
   - Run unit and integration tests

2. **Deployment:**
   - Apply changes to `services/project_monitor.py`
   - Restart orchestrator (graceful shutdown)
   - Monitor logs for first 10 minutes

3. **Verification:**
   - Check orchestrator stays running
   - Test adding new project config
   - Verify warning logs appear
   - Confirm project becomes active after reconciliation

4. **Rollback Plan:**
   - If issues detected, revert `services/project_monitor.py`
   - Restart orchestrator
   - Original behavior restored (fatal exit on missing state)

## Additional Recommendations

### Future Enhancement (Optional)
Add a metric/health check for projects stuck without state:

```python
# Track how long projects have been missing state
if project_name not in self._missing_state_timestamps:
    self._missing_state_timestamps[project_name] = now

time_without_state = now - self._missing_state_timestamps[project_name]
if time_without_state > 300:  # 5 minutes
    logger.error(
        f"Project '{project_name}' has been missing GitHub state for {time_without_state:.0f}s. "
        f"This may indicate a reconciliation failure."
    )
```

This would distinguish between "not yet reconciled" (expected) vs "reconciliation permanently failing" (actual problem).

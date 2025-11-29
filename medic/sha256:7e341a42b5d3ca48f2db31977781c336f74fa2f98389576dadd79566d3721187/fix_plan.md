# Fix Plan

**Failure Signature:** `sha256:7e341a42b5d3ca48f2db31977781c336f74fa2f98389576dadd79566d3721187`

## Proposed Solution

Replace the fatal error handling with graceful degradation that matches the initialization code path. Instead of crashing the entire orchestrator, the monitor should log a warning and skip monitoring for that project until reconciliation completes.

## Implementation Steps

1. **Modify the error handling in the main monitoring loop** (`services/project_monitor.py:3524-3530`)
   - Change from `logger.error()` + `exit(1)` to `logger.warning()` + `continue`
   - Match the pattern used in initialization (lines 3413-3417)

2. **Apply same fix to board state check** (`services/project_monitor.py:3539-3543`)
   - Currently also has fatal error path with `exit(1)`
   - Should use same graceful degradation approach

3. **Add retry mechanism** (optional enhancement)
   - The monitor polls every ~30 seconds
   - Missing projects will automatically appear once reconciliation completes
   - No special retry logic needed - the normal polling loop handles this

4. **Update log messages** to clarify this is expected during new project addition

## Code Changes Required

### File: `services/project_monitor.py`

**Location 1: Lines 3524-3530** (main issue)
```python
# Before
project_state = state_manager.load_project_state(project_name)
if not project_state:
    logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
    logger.error("This indicates GitHub project management failed during reconciliation")
    logger.error("Project monitoring cannot function without GitHub project state")
    logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
    exit(1)  # Fatal error - stop immediately

# After
project_state = state_manager.load_project_state(project_name)
if not project_state:
    logger.warning(f"No GitHub state found for project '{project_name}' - reconciliation may be in progress, skipping")
    continue
```

**Location 2: Lines 3538-3543** (same pattern for board state)
```python
# Before
board_state = project_state.boards.get(pipeline.board_name)
if not board_state:
    logger.error(f"FATAL: No GitHub state found for board '{pipeline.board_name}' in project '{project_name}'")
    logger.error("This indicates GitHub project board creation failed during reconciliation")
    logger.error("STOPPING PROJECT MONITOR: GitHub board management is broken")
    exit(1)  # Fatal error - stop immediately

# After
board_state = project_state.boards.get(pipeline.board_name)
if not board_state:
    logger.warning(f"No board state for '{pipeline.board_name}' in project '{project_name}' - reconciliation may be in progress, skipping")
    continue
```

## Testing Strategy

### Unit Tests
1. **Test case**: Monitor encounters project with no state
   - Mock `state_manager.load_project_state()` to return `None`
   - Verify monitor logs warning and continues (does not crash)
   - Verify other projects continue to be monitored

2. **Test case**: Monitor encounters project with missing board state
   - Mock project state with empty `boards` dict
   - Verify monitor logs warning and continues
   - Verify other pipelines/projects continue to be monitored

### Integration Tests
1. **Scenario**: Add new project config while orchestrator running
   - Start orchestrator with existing projects
   - Add new project config file
   - Verify monitor logs warnings but continues running
   - Verify reconciliation creates state
   - Verify monitor picks up new project in next poll cycle

2. **Scenario**: Corrupted/deleted state file
   - Delete state file for existing project
   - Verify monitor handles gracefully
   - Verify reconciliation can recover

### Manual Testing
1. Add a test project configuration while orchestrator is running
2. Monitor logs to verify warning messages instead of fatal errors
3. Verify reconciliation creates state
4. Verify monitoring resumes for new project automatically

## Risks and Considerations

### Potential Risks
- **None identified** - This change makes the system more resilient
- The new behavior already exists in the initialization code path and works correctly

### Edge Cases Handled
1. **Reconciliation in progress**: Monitor will skip and retry on next poll
2. **Reconciliation fails**: Monitor will continue warning but won't crash
3. **Manual state file deletion**: System can recover via reconciliation
4. **Multiple projects**: Fix ensures one misconfigured project doesn't affect others

### Performance Impact
- **Minimal**: Only adds one `continue` statement per missing project
- Missing projects are naturally infrequent
- No performance degradation expected

## Deployment Plan

### Pre-deployment
1. Run existing test suite to ensure no regressions
2. Add new unit tests for graceful degradation behavior
3. Code review focusing on error handling consistency

### Deployment
1. **Low risk change** - can deploy during normal operations
2. No database migrations required
3. No configuration changes required
4. Docker restart will pick up new code

### Post-deployment Validation
1. Monitor logs for any instances of the warning message
2. Verify no `exit(1)` calls from project monitor
3. Add new project config and verify graceful handling
4. Check uptime metrics to confirm no unexpected restarts

### Rollback Plan
- Simple git revert if issues discovered
- No data migration needed
- Restart orchestrator container

## Additional Recommendations

1. **Audit other `exit()` calls**: Search codebase for other instances of `exit(1)` that might cause similar issues
   ```bash
   grep -r "exit(1)" services/
   ```

2. **Consider process supervision**: While Docker restart policy works, consider:
   - Proper service orchestration (systemd, supervisor)
   - Health check endpoints
   - Graceful shutdown handlers

3. **Monitoring improvements**:
   - Alert on reconciliation failures
   - Track time between config addition and state creation
   - Monitor for repeated warnings (indicates reconciliation stuck)

4. **Documentation**:
   - Update operator guide with expected behavior when adding projects
   - Document that brief warnings are normal during project addition

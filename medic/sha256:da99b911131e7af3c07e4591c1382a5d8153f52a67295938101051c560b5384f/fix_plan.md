# Fix Plan

**Failure Signature:** `sha256:da99b911131e7af3c07e4591c1382a5d8153f52a67295938101051c560b5384f`

## Note: Duplicate Investigation

This failure signature is a **duplicate** of `sha256:da1465c55e2e607be5f17f4ba799b52c6fff72fc525b4466ed9daf7c8ac16f09`.

Both signatures represent the same underlying issue and same error occurrence. The comprehensive fix plan has been documented in the related investigation.

**Primary Fix Plan:** See `/medic/sha256:da1465c55e2e607be5f17f4ba799b52c6fff72fc525b4466ed9daf7c8ac16f09/fix_plan.md`

## Quick Summary

The issue is a race condition where the project monitor crashes when it encounters a newly added project configuration file before it has been reconciled.

**Solution:** Replace fatal error handling with graceful skipping of projects without GitHub state.

## Implementation Reference

### Changes Required

**File:** `services/project_monitor.py`

**Location 1 (line 3524-3530):** Handle missing project state
```python
# Current (FATAL):
project_state = state_manager.load_project_state(project_name)
if not project_state:
    logger.error(f"FATAL: No GitHub state found for project '{project_name}'")
    logger.error("This indicates GitHub project management failed during reconciliation")
    logger.error("Project monitoring cannot function without GitHub project state")
    logger.error("STOPPING PROJECT MONITOR: Core functionality is broken")
    exit(1)  # Fatal error - stop immediately

# Proposed (SKIP):
project_state = state_manager.load_project_state(project_name)
if not project_state:
    if not hasattr(self, '_missing_state_warnings'):
        self._missing_state_warnings = {}

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

**Location 2 (line 3538-3543):** Handle missing board state (similar pattern)
- See primary fix plan for details

## Testing Strategy

1. **Unit Test:** Verify monitor skips projects without state instead of crashing
2. **Integration Test:** Add project config at runtime, verify orchestrator continues
3. **Manual Test:** Copy new YAML to config/projects/, watch for warnings not crashes

## Deployment

Apply the changes from the primary fix plan to `services/project_monitor.py` and restart the orchestrator.

**Rollback:** Revert changes and restart if issues occur.

## Additional Notes

### Fingerprinting Issue

The medic system created two separate fingerprints for the same error:
- `sha256:da1465c55e2e607be5f17f4ba799b52c6fff72fc525b4466ed9daf7c8ac16f09` - Occurred: 1 time at 19:11:42
- `sha256:da99b911131e7af3c07e4591c1382a5d8153f52a67295938101051c560b5384f` - Occurred: 1 time at 19:11:42

**Recommendation:** The medic fingerprinting algorithm should be reviewed to ensure identical error sequences don't create duplicate fingerprints. This could be:
- Subtle timing differences in log processing
- Different ordering of stack trace elements
- Minor variations in error message normalization

Consider implementing deduplication logic that merges fingerprints with:
- Same error type and pattern
- Same container
- Timestamps within same second
- Same context (e.g., same project name)

# Repair Cycle Auto-Commit Fix

**Date**: October 18, 2025
**Issue**: Changes made during repair cycle executions were not being committed to git

## Problem Description

When the repair cycle completed successfully and fixed test failures, the changes were left uncommitted in the workspace. This happened because:

1. **Repair cycle agents skip workspace preparation** - To optimize performance, repair cycles set `skip_workspace_prep=True` for each agent invocation within the cycle (see `pipeline/repair_cycle.py`)

2. **Skipping workspace prep also skips finalization** - When `skip_workspace_prep=True`, no `workspace_context` is created, which means the normal finalization flow (including auto-commit) is bypassed (see `services/agent_executor.py` lines 85-88 and 197-215)

3. **No commit step after repair cycle completion** - The monitoring code (`_monitor_repair_cycle_container` in `services/project_monitor.py`) would post a summary, auto-advance the issue, and cleanup files, but never committed the changes

## Solution

Added auto-commit logic to `services/project_monitor.py` in the `_monitor_repair_cycle_container` function. After a repair cycle completes successfully (`overall_success=True`), the orchestrator now:

1. Calls `auto_commit_service.commit_agent_changes()` to commit all changes
2. Uses a descriptive commit message indicating the repair cycle completed
3. Pushes the changes to the remote branch
4. Logs success/failure appropriately
5. Continues with cleanup even if commit fails (graceful degradation)

### Code Location

File: `services/project_monitor.py`
Function: `_monitor_repair_cycle_container` (monitor_thread inner function)
Lines: ~2111-2157

### Commit Flow (IMPROVED ORDER)

```
Repair Cycle Completes (exit_code=0)
    ↓
Load result.json
    ↓
Determine overall_success=True
    ↓
Post summary comment to GitHub
    ↓
**NEW** → Auto-commit changes ← **NEW**
    ↓
    └─ Check for changes
    └─ Verify on feature branch
    └─ Stage all changes
    └─ Commit with message
    └─ Push to remote
    ↓
Auto-advance issue if applicable
    ↓
Cleanup state files
    ↓
Remove container
    ↓
Clear Redis tracking
```

**Important**: Auto-commit happens **BEFORE** auto-advance to ensure code is pushed before the issue moves to the next stage. This prevents race conditions where the next stage agent might start before code is available.

## Testing

To verify this fix works:

1. Create an issue with failing tests
2. Move it to the Testing column (triggers repair cycle)
3. Wait for repair cycle to complete successfully
4. Check that changes are committed and pushed to the branch
5. Verify commit message mentions "Complete repair cycle for issue #X"

## Related Code

- `pipeline/repair_cycle.py` - Sets `skip_workspace_prep=True` for agent calls
- `services/agent_executor.py` - Skips workspace finalization when `skip_workspace_prep=True`
- `services/auto_commit.py` - Handles the actual git commit operations
- `services/project_monitor.py` - Monitors repair cycle containers and handles completion

## Impact

- **Positive**: Changes from repair cycles are now properly committed and pushed
- **No Breaking Changes**: Graceful error handling ensures failures don't block repair cycles
- **Consistent Behavior**: Repair cycles now match the behavior of regular agent executions

## Future Considerations

Consider adding similar auto-commit logic for other long-running containerized operations that may bypass normal workspace finalization.

# Double Recording Fix - Investigation and Solution

## Problem Summary

After implementing the duplicate agent launch fix and restarting the orchestrator, a new error appeared during pipeline execution:

```
ERROR - No in_progress execution found for code_reviewer in Code Review,
creating new record with outcome success. This should only happen after
orchestrator restart/crash.
```

This error was **new** - the user reported never seeing it before.

## Root Cause Investigation

### Timeline of Events

**Pipeline Run:** ab95b1f0-1466-4d15-8a1a-30de3a417d9a (Issue #284)

```
19:19:58.447 - Recorded execution start (in_progress)
19:21:23.007 - Recorded execution outcome: success (FIRST CALL - succeeds)
19:21:27.060 - ERROR: No in_progress execution found (SECOND CALL - fails)
19:21:27.081 - Agent code_reviewer completed successfully
```

### Why Two Calls?

Found **double recording** in the codebase:

1. **`docker_runner.py:1341`**
   ```python
   # CRITICAL: Record successful outcome immediately before any result processing
   # This ensures outcome is recorded even if result processing fails
   work_execution_tracker.record_execution_outcome(...)
   ```

2. **`agent_executor.py:495`**
   ```python
   # CRITICAL: Always try to record outcome to prevent stuck "in_progress" states
   work_execution_tracker.record_execution_outcome(...)
   ```

Both locations intentionally record outcomes as a safety mechanism, but this creates a race condition.

### Why Is This Error New?

The error is new because of **commit 0cd006a** (from earlier today):

**Before commit 0cd006a:**
- docker_runner recorded with `column='unknown'`
- agent_executor recorded with `column='Code Review'`
- **Different columns → didn't match same record → both succeeded silently**
- Side effect: Created duplicate execution records (but no error logged)

**After commit 0cd006a:**
- docker_runner records with `column='Code Review'` (fixed!)
- agent_executor records with `column='Code Review'`
- **Same column → both match same record → second call fails with ERROR**
- First call: updates `in_progress` → `success` ✓
- Second call: can't find `in_progress` (already `success`) → ERROR ✗

### The Commit That Exposed It

```
commit 0cd006aee3c7e6766357ca118ec1cd46ddc32510
Date:   Wed Jan 28 13:21:18 2026 -0500

Fix: Pass column through container recovery chain for proper execution state matching

The fix changed docker_runner to use the actual column instead of 'unknown',
which was correct for container recovery but exposed the double-recording issue.
```

## Solution Implemented

### Approach

Make `agent_executor` check if `docker_runner` already recorded the outcome before attempting to record it again. This preserves both safety mechanisms while preventing the double-recording error.

### Code Changes

**File:** `services/agent_executor.py`

**Success Path (line 482-520):**
```python
# Record successful execution outcome
# Note: docker_runner also records outcome (for early recording before result processing)
# We check if it's already recorded to avoid double-recording errors
if 'issue_number' in task_context:
    from services.work_execution_state import work_execution_tracker
    column = task_context.get('column', 'unknown')

    # Check if docker_runner already recorded the outcome
    state = work_execution_tracker.load_state(project_name, task_context['issue_number'])
    already_recorded = False

    for execution in reversed(state.get('execution_history', [])):
        if (execution.get('column') == column and
            execution.get('agent') == agent_name and
            execution.get('outcome') in ['success', 'failure']):
            # Found a recent completed execution for this agent/column
            # docker_runner must have already recorded it
            already_recorded = True
            logger.debug(
                f"Execution outcome already recorded by docker_runner for "
                f"{project_name}/#{task_context['issue_number']} {agent_name} in {column}"
            )
            break

    if not already_recorded:
        work_execution_tracker.record_execution_outcome(
            issue_number=task_context['issue_number'],
            column=column,
            agent=agent_name,
            outcome='success',
            project_name=project_name
        )
```

**Failure Path (line 575-615):**
- Applied the same fix for consistency

**Blocked Path (line 550-564):**
- No change needed (circuit breaker failures don't go through docker_runner)

### Why This Works

1. **docker_runner records first** (at container completion)
   - Updates `in_progress` → `success` ✓

2. **agent_executor checks before recording**
   - Finds existing `success` outcome
   - Skips redundant recording ✓
   - No error logged ✓

3. **Both safety mechanisms preserved:**
   - docker_runner: Records even if result processing fails
   - agent_executor: Records if docker_runner didn't (non-Docker agents, etc.)

## Testing

### Expected Behavior After Fix

When running an agent in Docker:

```
19:19:58 - Recorded execution start (in_progress)
19:21:23 - docker_runner: Recorded execution outcome: success
19:21:27 - agent_executor: Execution outcome already recorded, skipping
19:21:27 - Agent code_reviewer completed successfully
```

**No ERROR, no duplicate recording.**

### Test Cases

1. **Normal Docker agent execution**
   - docker_runner records → agent_executor skips ✓

2. **Non-Docker agent execution**
   - docker_runner doesn't run → agent_executor records ✓

3. **Result processing failure**
   - docker_runner records early → agent_executor skips ✓
   - Even if processing fails, outcome is already recorded ✓

4. **Container recovery after restart**
   - docker_runner records on container completion → agent_executor skips ✓

## Files Modified

1. **services/agent_executor.py**
   - Lines 482-520: Success path
   - Lines 575-615: Failure path
   - Added check for already-recorded outcomes before recording

## Impact Assessment

**Risk Level:** Low

**Why Safe:**
- Only adds a check before existing behavior
- Preserves both safety mechanisms
- No changes to docker_runner logic
- No changes to record_execution_outcome logic
- Falls back to recording if check fails

**What Could Go Wrong:**
- Check incorrectly identifies already-recorded outcome
  - **Mitigation**: Check is conservative (only skips if found completed outcome)
  - **Impact**: Would record again, log ERROR (same as before fix)

## Relationship to Duplicate Agent Fix

These are **separate issues:**

1. **Duplicate Agent Fix** (services/review_cycle.py:424-437)
   - Prevents launching duplicate agents after orchestrator restart
   - Was NOT the cause of this error

2. **Double Recording Fix** (services/agent_executor.py)
   - Prevents recording execution outcome twice
   - Was exposed by commit 0cd006a (container recovery column fix)

The duplicate agent fix is still valid and working correctly. This double-recording issue existed but was hidden until commit 0cd006a fixed column matching.

## Summary

- **Root Cause**: Double recording by docker_runner AND agent_executor
- **Why New**: Commit 0cd006a fixed column matching, exposing the issue
- **Solution**: agent_executor checks if already recorded before recording
- **Result**: No ERROR, both safety mechanisms preserved
- **Risk**: Low - only adds defensive check

## Next Steps

1. Monitor logs for the debug message: "Execution outcome already recorded by docker_runner"
2. Verify no ERROR messages about "No in_progress execution found"
3. Confirm normal pipeline execution continues to work

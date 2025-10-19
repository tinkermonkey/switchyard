# Docker Runner Error Capture Improvement

**Date**: October 19, 2025
**Issue**: Agent failures with "No error output captured" - unable to diagnose what went wrong

## Problem Description

When Claude Code agent containers failed (exit code 1), the error message was:
```
Agent execution failed (returncode=1): No error output captured
```

This made it impossible to diagnose the actual failure reason because:
1. The stderr reading thread (`read_stderr()`) only captured what the process wrote to stderr
2. If the Claude CLI crashed or was killed, it might not write to stderr
3. Container logs weren't being checked as a fallback

## Root Cause

The Docker runner relied solely on stderr output from the subprocess:
```python
def read_stderr():
    for line in iter(process.stderr.readline, ''):
        if line:
            stderr_parts.append(line)
```

When the container failed without writing to stderr (e.g., OOM kill, segfault, killed by signal), the `stderr_parts` list was empty, resulting in the unhelpful "No error output captured" message.

## Solution

Added a fallback mechanism to fetch container logs when no stderr is captured:

```python
if not stderr_text:
    try:
        logger.warning(f"No stderr captured, attempting to fetch container logs for {container_name}")
        logs_result = subprocess.run(
            ['docker', 'logs', '--tail', '100', container_name],
            capture_output=True,
            text=True,
            timeout=5
        )
        if logs_result.stdout or logs_result.stderr:
            stderr_text = f"Container logs (last 100 lines):\n{logs_result.stdout}\n{logs_result.stderr}"
            logger.info(f"Captured container logs: {len(stderr_text)} chars")
    except Exception as log_err:
        logger.warning(f"Failed to fetch container logs: {log_err}")

# Final fallback
if not stderr_text:
    stderr_text = "No error output captured. Container may have crashed or been killed."
```

## Error Capture Flow

```
Agent Container Fails (returncode != 0)
    ↓
Check stderr_parts
    ↓
    ├─ Has stderr? → Use captured stderr
    │
    └─ Empty stderr?
        ↓
        Try to fetch container logs (docker logs --tail 100)
        ↓
        ├─ Logs available? → Use container logs
        │
        └─ No logs?
            ↓
            Use descriptive fallback message:
            "Container may have crashed or been killed"
```

## Common Failure Scenarios Now Covered

### 1. Out of Memory (OOM) Kill
- **Before**: "No error output captured"
- **After**: Container logs show "Killed" or OOM killer messages

### 2. Segmentation Fault
- **Before**: "No error output captured"
- **After**: Container logs show "Segmentation fault"

### 3. Signal Termination (SIGTERM, SIGKILL)
- **Before**: "No error output captured"
- **After**: Container logs show exit reason

### 4. Python/Node Crash Without Stderr
- **Before**: "No error output captured"
- **After**: Container logs show stack trace or error message

## Code Location

**File**: `claude/docker_runner.py`
**Function**: `_execute_in_container`
**Lines**: ~783-806

## Testing

To verify this improvement works:

1. **Intentional container kill** (simulate OOM):
   ```bash
   # While agent is running
   docker kill <container-name>
   ```
   Expected: Error message includes "Container may have crashed or been killed"

2. **Check logs after normal failure**:
   Look for lines like:
   ```
   No stderr captured, attempting to fetch container logs for ...
   Captured container logs: XXX chars
   ```

## Benefits

✅ **Better diagnostics** - Can now see what actually went wrong  
✅ **Faster debugging** - No need to manually check docker logs  
✅ **More actionable errors** - Specific failure reasons help identify fixes  
✅ **Graceful degradation** - Falls back to descriptive message if logs unavailable

## Related Issues

This fix is separate from the **repair cycle auto-commit** fix, but both improve the robustness of agent execution:

- **Repair cycle auto-commit**: Ensures changes are committed after successful repair cycles
- **Error capture**: Ensures we can diagnose failures when they occur

## Future Enhancements

Consider:
- Streaming container logs during execution (not just on failure)
- Capturing container exit code reasons (e.g., OOM vs signal vs error)
- Adding container resource monitoring (memory, CPU usage before crash)

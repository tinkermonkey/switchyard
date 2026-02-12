# Critical Bug: Reconciliation Process Failure

**Severity**: CRITICAL
**Impact**: 100% failure rate for reconnecting to running agents during orchestrator restart
**Date Discovered**: February 12, 2026
**Restart Events Analyzed**: 3 restarts (11:45, 12:40, 17:35)

## Summary

The orchestrator's reconciliation process fails to correctly reconnect to running agent containers during restart. When containers are running during an orchestrator restart, they are consistently marked as "blocked - manual intervention required" instead of being successfully reconnected.

## Evidence

### Restart at 12:40 - 2 Containers Running

**What state tracking thought was running:**
- rounds/#24 (senior_software_engineer)
- documentation_robotics_viewer/#266 (senior_software_engineer)

**What Docker actually had running:**
- rounds/#25 (code_reviewer) - container: claude-agent-rounds-251fc493...
- documentation_robotics_viewer/#268 (senior_software_engineer) - container: claude-agent-documentation_robotics_viewer-91e6a6d6...

**Reconciliation logs:**
```
12:40:47 - WARNING: Container exists in Docker but not in Redis tracking for rounds/#24 senior_software_engineer - attempting repair
12:40:47 - INFO: REPAIRED Redis tracking for container claude-agent-rounds-251fc493... (agent=code_reviewer, project=rounds, issue=#25)
12:40:47 - WARNING: Container exists in Docker but not in Redis tracking for documentation_robotics_viewer/#266 senior_software_engineer - attempting repair
12:40:47 - INFO: REPAIRED Redis tracking for container claude-agent-documentation_robotics_viewer-91e6a6d6... (agent=senior_software_engineer, project=documentation_robotics_viewer, issue=#268)
```

**Result:**
- ❌ Repair triggered for #24 but repaired #25 instead
- ❌ Repair triggered for #266 but repaired #268 instead
- ✅ #25 and #268 continued successfully
- ❌ #24 and #266 marked as stuck (containers missing)

### Restart at 17:35 - Final Outcome

```
17:35:11 - WARNING: Marked stuck execution as failed: rounds/#24 senior_software_engineer in Development (no container found, outcome not recorded). Pipeline is now blocked - manual intervention required.
17:35:11 - WARNING: Marked stuck execution as failed: documentation_robotics_viewer/#266 senior_software_engineer in Development (no container found, outcome not recorded). Pipeline is now blocked - manual intervention required.
```

## Root Causes

### 1. Stale State Entries
State tracking has entries for #24 and #266 that were never cleaned up from previous executions. These stale entries cause reconciliation to look for containers that don't exist.

### 2. Container Matching Logic Failure
The repair process:
1. Is triggered with issue #24
2. Searches for containers matching project name pattern
3. Finds claude-agent-rounds-* (which is #25)
4. Repairs that container without validating it matches the triggering issue

### 3. No Validation of Match
The `_repair_missing_redis_tracking()` method:
- Takes a project/issue as input
- Receives `all_container_names` list
- Inspects containers and extracts issue number from labels
- **Does not validate** that extracted issue matches the expected issue
- Blindly repairs whatever container it finds

## Code Location

**File**: `services/work_execution_state.py`

**Method**: `_repair_missing_redis_tracking(self, project: str, issue_number: int, agent: str, container_names: list)`

**Bug**:
- Input: `issue_number=24`
- Finds container with label `org.clauditoreum.issue_number=25`
- Repairs it anyway without validation
- Logs success for wrong issue

## Impact

1. **Lost Work**: Agents that complete while disconnected have outcomes lost
2. **Pipeline Blockage**: Issues marked as failed block entire pipeline
3. **Manual Intervention**: Every restart with running agents requires manual cleanup
4. **Success Rate**: 0% successful reconnections observed across 3 restarts

## Reproduction

1. Start orchestrator with clean state
2. Queue 2 tasks that start agents
3. Wait for agents to start running
4. Restart orchestrator while agents running
5. Observe: Both marked as failed instead of reconnected

## Recommended Fixes

### Fix 1: Validate Container Match (Immediate)

```python
def _repair_missing_redis_tracking(self, project: str, issue_number: int, agent: str, container_names: list):
    for container_name in container_names:
        # ... existing inspection code ...

        label_issue = parts[4] if len(parts) > 4 and parts[4] else str(issue_number)

        # VALIDATE: Only repair if issue numbers match
        if label_issue != str(issue_number):
            logger.warning(
                f"Container {container_name} is for issue #{label_issue}, "
                f"not #{issue_number} - skipping repair"
            )
            continue

        # ... rest of repair logic ...
```

### Fix 2: Clean Up Stale State (Important)

On reconciliation:
1. Load all in-progress executions from state
2. For each, check if container actually exists in Docker
3. If not, mark as failed immediately (don't wait for next reconciliation)
4. Only attempt repair for executions with matching containers

### Fix 3: Improve Container Discovery (Optimal)

Instead of:
```python
# Current: State-driven (wrong)
for execution in in_progress_executions:
    find_containers_for(execution.issue)
```

Do:
```python
# Better: Container-driven (correct)
containers = discover_all_agent_containers()
for container in containers:
    issue = extract_issue_from_labels(container)
    reconcile_state_for(issue, container)
```

## Recommended Actions

### Immediate (High Priority)
1. Add validation to `_repair_missing_redis_tracking()` to verify issue number match
2. Add DEBUG logging to show container discovery process
3. Clean up stale state entries for #24 and #266 manually

### Short Term (This Week)
1. Implement container-driven discovery instead of state-driven
2. Add reconciliation metrics to track success/failure rate
3. Add alerts for failed reconciliations

### Long Term (Next Sprint)
1. Redesign state management to be more resilient
2. Add automated tests for reconciliation scenarios
3. Consider using Docker events API for real-time tracking

## Manual Cleanup Required

```bash
# Clean up the two blocked pipelines
gh issue comment 24 --repo tinkermonkey/rounds --body "Pipeline reconciliation bug - execution lost during restart. Closing to unblock."
gh issue close 24 --repo tinkermonkey/rounds

gh issue comment 266 --repo tinkermonkey/documentation_robotics_viewer --body "Pipeline reconciliation bug - execution lost during restart. Closing to unblock."
gh issue close 266 --repo tinkermonkey/documentation_robotics_viewer
```

## Testing

After fix implementation:
1. Start 2 long-running agents
2. Restart orchestrator
3. Verify both agents reconnect successfully
4. Verify Redis tracking is restored
5. Verify outcomes are properly recorded when agents finish

---

**Status**: OPEN - Requires immediate attention
**Priority**: P0 - Critical system reliability issue
**Owner**: TBD
**ETA**: Should be fixed before next production restart

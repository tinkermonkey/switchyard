# Root Cause Diagnosis

**Failure Signature:** `sha256:e59d7185876ea7c5545723fff16b46ba1bfb4eea283433c7e7f8a6b474ee671e`
**Investigation Date:** 2025-11-29

## Error Summary

The human feedback monitoring loop continues to poll for a deleted GitHub discussion, generating "No result from GraphQL query" warnings every 30 seconds indefinitely.

## Root Cause Analysis

The `HumanFeedbackLoopExecutor._conversational_loop()` method monitors a GitHub discussion for human feedback by polling every 30 seconds. However, when a discussion is deleted from GitHub:

1. The `project_monitor.py` detects the deletion and removes it from state (line 4371-4372)
2. The monitoring loop in `human_feedback_loop.py` continues running
3. The GraphQL query at line 719 returns `None` because the discussion no longer exists
4. The code logs a warning at line 724: "No result from GraphQL query for discussion {discussion_id}"
5. The loop continues polling because it only exits on column changes (line 369), not on GraphQL failures

The monitoring loop has no mechanism to detect that the discussion it's monitoring has been deleted, so it continues indefinitely.

## Evidence

### Log Analysis

From orchestrator logs at 2025-11-29 13:03:03:
```
2025-11-29 13:03:03,692 - services.project_monitor - INFO - Discussion D_kwDON0IZfM4AjD4P for issue #38 no longer exists, removing from state
```

Followed by 20 occurrences of the warning over ~10 minutes (every 30 seconds):
```
2025-11-29 13:03:26,039 - services.human_feedback_loop - WARNING - No result from GraphQL query for discussion D_kwDON0IZfM4AjD4P
2025-11-29 13:03:56,250 - services.human_feedback_loop - WARNING - No result from GraphQL query for discussion D_kwDON0IZfM4AjD4P
2025-11-29 13:04:26,491 - services.human_feedback_loop - WARNING - No result from GraphQL query for discussion D_kwDON0IZfM4AjD4P
...
```

### Code Analysis

**services/human_feedback_loop.py:719-726**
```python
result = github_app.graphql_request(query, {'discussionId': state.discussion_id})

logger.debug(f"GraphQL result: {result is not None}")

if not result:
    logger.warning(f"No result from GraphQL query for discussion {state.discussion_id}")
    return None  # Returns None but loop continues
```

**services/human_feedback_loop.py:281-408** (monitoring loop)
- Loop exits only when card moves to different column (line 369)
- Loop exits when card moves to Backlog (line 388)
- No exit condition for deleted discussions
- A `None` return from `_get_human_feedback_since_last_agent()` is treated as "no new feedback" and the loop continues

### System State

- **Affected Component**: Human feedback loop executor
- **Trigger Condition**: Discussion deleted while monitoring loop is active
- **Current Behavior**: Infinite polling with warnings until orchestrator restart
- **Expected Behavior**: Monitoring loop should detect deletion and exit gracefully

## Impact Assessment

- **Severity**: Low-Medium
  - No functional breakage (the monitoring loop is meant to run until the card moves)
  - Creates log noise (20+ warnings per 10 minutes)
  - Wastes GitHub API rate limit (1 query every 30 seconds)
  - Could mask other important warnings in logs

- **Frequency**: Occasional
  - Only occurs when discussions are manually deleted during active monitoring
  - 20 occurrences detected in ~10 minute window (13:16-13:26)
  - Pattern: Issue #38 discussion deleted, monitoring loop orphaned

- **Affected Components**:
  - `services/human_feedback_loop.py` - HumanFeedbackLoopExecutor
  - Elasticsearch decision events (may have stale listening events)
  - GitHub API rate limit budget
  - Log clarity and observability

# Fix Plan

**Failure Signature:** `sha256:e59d7185876ea7c5545723fff16b46ba1bfb4eea283433c7e7f8a6b474ee671e`

## Proposed Solution

Add deletion detection to the human feedback monitoring loop. When the GraphQL query for a discussion returns `None`, check if the discussion has been deleted and exit the monitoring loop gracefully.

## Implementation Steps

1. **Track consecutive GraphQL failures in the monitoring loop**
   - Add a counter to track consecutive `None` results from `_get_human_feedback_since_last_agent()`
   - After 2-3 consecutive failures, verify the discussion still exists

2. **Add discussion existence check method**
   - Create a lightweight GraphQL query to check if a discussion exists
   - Call this when we detect repeated `None` results

3. **Update monitoring loop to exit on deletion**
   - When deletion is confirmed, log an info message (not warning)
   - Emit a `feedback_listening_stopped` decision event with reason "discussion_deleted"
   - Exit the monitoring loop gracefully (return `(None, True)`)

4. **Differentiate between "no feedback" and "query failed"**
   - Update `_get_human_feedback_since_last_agent()` to return a tuple: `(feedback, query_success)`
   - Or raise a specific exception for deleted discussions
   - This allows the monitoring loop to distinguish between "no new comments" vs "discussion deleted"

## Code Changes Required

### File: services/human_feedback_loop.py

#### Change 1: Update `_get_human_feedback_since_last_agent()` to detect deletion

```python
# Before (line 719-726)
result = github_app.graphql_request(query, {'discussionId': state.discussion_id})

logger.debug(f"GraphQL result: {result is not None}")

if not result:
    logger.warning(f"No result from GraphQL query for discussion {state.discussion_id}")
    return None

# After
result = github_app.graphql_request(query, {'discussionId': state.discussion_id})

logger.debug(f"GraphQL result: {result is not None}")

if not result:
    # Check if this is a transient error or permanent deletion
    # Try a lightweight existence check
    existence_query = """
    query($discussionId: ID!) {
      node(id: $discussionId) {
        id
      }
    }
    """
    existence_result = github_app.graphql_request(existence_query, {'discussionId': state.discussion_id})

    if not existence_result or not existence_result.get('node'):
        # Discussion has been deleted
        logger.info(f"Discussion {state.discussion_id} has been deleted from GitHub")
        # Raise a custom exception to signal deletion to the monitoring loop
        raise DiscussionDeletedException(state.discussion_id)
    else:
        # Transient error - log warning and continue
        logger.warning(f"Transient error querying discussion {state.discussion_id}, will retry")
        return None
```

#### Change 2: Add custom exception class

```python
# Add near top of file (after imports)
class DiscussionDeletedException(Exception):
    """Raised when a discussion being monitored has been deleted"""
    def __init__(self, discussion_id: str):
        self.discussion_id = discussion_id
        super().__init__(f"Discussion {discussion_id} has been deleted")
```

#### Change 3: Update monitoring loop to handle deletion

```python
# Before (line 286-295)
try:
    human_feedback = await self._get_human_feedback_since_last_agent(
        state,
        org
    )
except Exception as e:
    logger.error(f"Error checking for feedback: {e}")
    import traceback
    logger.error(traceback.format_exc())
    human_feedback = None

# After (line 286-295)
try:
    human_feedback = await self._get_human_feedback_since_last_agent(
        state,
        org
    )
except DiscussionDeletedException as e:
    # Discussion has been deleted - exit monitoring loop gracefully
    logger.info(
        f"Discussion {state.discussion_id} for issue #{state.issue_number} "
        f"has been deleted. Stopping feedback monitoring."
    )

    # Emit feedback listening stopped event
    decision_events.emit_feedback_listening_stopped(
        issue_number=state.issue_number,
        project=state.project_name,
        board=state.board_name,
        agent=state.agent,
        reason="Discussion deleted from GitHub",
        feedback_received=state.current_iteration > 0
    )

    return (None, True)  # Exit the loop
except Exception as e:
    logger.error(f"Error checking for feedback: {e}")
    import traceback
    logger.error(traceback.format_exc())
    human_feedback = None
```

## Testing Strategy

### Unit Tests
1. Test `_get_human_feedback_since_last_agent()` with mocked GraphQL responses:
   - Mock scenario: GraphQL returns `None` but existence check confirms discussion exists (transient error)
   - Mock scenario: GraphQL returns `None` and existence check returns `None` (deleted discussion)
   - Verify `DiscussionDeletedException` is raised in the second case

2. Test monitoring loop with `DiscussionDeletedException`:
   - Verify loop exits gracefully
   - Verify decision event is emitted with correct reason
   - Verify return value is `(None, True)`

### Integration Tests
1. Create a test discussion
2. Start a monitoring loop for that discussion
3. Delete the discussion via GitHub API
4. Verify the monitoring loop exits within 1 minute (2 polling cycles)
5. Verify no warnings are logged after exit
6. Verify `feedback_listening_stopped` event is indexed in Elasticsearch

### Manual Testing
1. Start orchestrator with a test project
2. Create an issue and move it to a conversational column
3. Wait for discussion creation and monitoring to start
4. Manually delete the discussion via GitHub UI
5. Monitor orchestrator logs - should see "Discussion has been deleted" message
6. Verify no recurring warnings
7. Verify monitoring loop exits cleanly

## Risks and Considerations

### Performance Impact
- Additional GraphQL query on deletion detection (lightweight, only runs when `result=None`)
- This is acceptable since it only happens when a discussion is deleted or has transient errors
- The extra query prevents 100+ wasted queries over the lifetime of the orphaned loop

### Race Conditions
- Discussion could be deleted between the main query and existence check
- This is acceptable - we'll correctly identify it as deleted on the existence check
- No data loss since deletion already happened

### False Positives
- Transient GitHub API errors could temporarily return `None`
- The existence check mitigates this by distinguishing between errors and deletions
- If the existence check also fails, it's likely truly deleted or a sustained API outage
- In sustained API outage, exiting the loop is acceptable (column changes will restart monitoring)

### Backward Compatibility
- No breaking changes to public APIs
- Existing monitoring loops will benefit from the fix automatically
- Decision events remain compatible (using existing `feedback_listening_stopped` event type)

## Deployment Plan

1. **Development**
   - Implement changes in feature branch
   - Add unit tests for new exception and handling
   - Run full test suite

2. **Testing**
   - Deploy to staging environment
   - Manually test deletion detection
   - Monitor logs for 24 hours to ensure no regressions

3. **Rollout**
   - Merge to main branch
   - Deploy to production during low-activity period
   - Monitor for 1 hour after deployment
   - Check Elasticsearch for any `feedback_listening_stopped` events with `reason="Discussion deleted"`

4. **Validation**
   - Review orchestrator logs for reduced warning noise
   - Verify GitHub API call volume decreases (fewer wasted queries)
   - Confirm no unexpected monitoring loop exits

5. **Rollback Plan**
   - If issues detected, revert to previous commit
   - The change is isolated to `human_feedback_loop.py`, making rollback safe
   - No data migration required

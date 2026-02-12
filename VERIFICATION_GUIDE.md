# Duplicate Logs Fix - Verification Guide

## What Was Fixed

**Problem**: Claude Code live logs appeared duplicated (100% duplication rate) in both the web UI and Elasticsearch.

**Root Cause**: Two independent code paths were writing the SAME Claude events to Redis simultaneously:
1. **agent_executor.py** - Created stream callback that published to Redis
2. **docker-claude-wrapper.py** - Also published the same events to Redis (inside container)

**Solution**: Removed the redundant stream callback from agent_executor.py, keeping only docker-claude-wrapper.py as the single source of Claude logs.

## Verification Steps

### 1. Quick Automated Check

Run the verification script:

```bash
./scripts/verify_no_duplicate_logs.sh
```

**Expected Output**:
```
✅ SUCCESS: No duplicate message IDs found!
   The fix is working correctly.
```

**If duplicates are still found**, the script will show:
- Count of duplicate message IDs
- List of duplicate IDs
- Sample entries with timestamps

### 2. Manual Elasticsearch Verification

Query recent events and check for duplicates:

```bash
# Fetch recent events
curl -s "http://localhost:9200/claude-streams-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 200,
  "sort": [{"timestamp": "desc"}],
  "query": {"term": {"event_category": "claude_stream"}}
}' | jq -r '.hits.hits[]._source.message_id' | sort | uniq -d
```

**Expected Output**: Empty (no duplicate message IDs)

### 3. Live Web UI Testing

1. **Start the orchestrator**:
   ```bash
   docker-compose up -d
   docker-compose logs -f orchestrator
   ```

2. **Trigger an agent execution**:
   - Move an issue to the "Development" column on your GitHub project board
   - Or manually trigger an agent via the API

3. **Watch live logs in the web UI**:
   - Open http://localhost:3000
   - Navigate to the live logs view
   - **Verify**: Each log entry appears exactly ONCE (no duplicates)

4. **Check container logs**:
   ```bash
   # Find the agent container
   docker ps | grep claude-agent

   # View its logs
   docker logs <agent-container-name>
   ```

   You should see docker-claude-wrapper.py publishing events to Redis.

### 4. Redis Stream Verification

Check the Redis Stream directly:

```bash
# Connect to Redis
docker-compose exec redis redis-cli

# View recent stream entries
XREVRANGE orchestrator:claude_logs_stream + - COUNT 50
```

**What to verify**:
- Each unique message ID appears only once
- Stream entries have incrementing IDs (no gaps or duplicates)

### 5. Regression Testing

Verify core functionality still works:

#### A. Agent Execution Completes Successfully
```bash
# Watch orchestrator logs
docker-compose logs -f orchestrator

# Trigger an agent and verify it completes without errors
```

#### B. Final Results Persist Correctly
```bash
# Check Redis for agent result
docker-compose exec redis redis-cli

# List agent result keys
KEYS claude:result:*

# Get a specific result
GET claude:result:<task_id>
```

#### C. Container Result Recovery Works
```bash
# Restart orchestrator while agent is running
docker-compose restart orchestrator

# Verify orchestrator recovers the agent's result from Redis
docker-compose logs orchestrator | grep "Recovered result"
```

#### D. Both Successful and Failed Executions
- Test with an agent that succeeds
- Test with an agent that intentionally fails
- Verify logs appear correctly in both cases

## What to Look For

### ✅ Success Indicators
- No duplicate message IDs in Elasticsearch
- Each log entry appears once in the web UI
- Agent results still persist to Redis
- Container result recovery still works
- docker-claude-wrapper.py logs show event publishing

### ❌ Failure Indicators
- Duplicate message IDs in Elasticsearch
- Log entries appear twice in web UI
- Agent results fail to persist
- Result recovery fails after orchestrator restart
- Errors in docker-claude-wrapper.py

## Rollback Plan

If issues are discovered:

1. **Revert the commit**:
   ```bash
   git revert HEAD
   git push
   ```

2. **Restart services**:
   ```bash
   docker-compose restart orchestrator
   ```

3. **Verify rollback**:
   - Run verification script again
   - Should show duplicates (expected after rollback)

## Expected Behavior After Fix

### Before Fix (OLD Behavior)
- **Duplication Rate**: 100%
- **Elasticsearch**: Every message ID appears exactly twice
- **Timing**: Duplicates within 0.2-0.3ms of each other
- **Redis Stream IDs**: Different IDs for same message
- **Web UI**: Each log line appears twice

### After Fix (NEW Behavior)
- **Duplication Rate**: 0%
- **Elasticsearch**: Each message ID appears exactly once
- **Timing**: Single timestamp per message
- **Redis Stream IDs**: One ID per message
- **Web UI**: Each log line appears once

## Technical Details

### What Changed

**File**: `services/agent_executor.py`

**Removed**:
- `_create_stream_callback()` method (lines 709-742)
- `stream_callback` parameter from `_build_execution_context()`
- `stream_callback` field from execution context dict

**Kept Unchanged**:
- `scripts/docker-claude-wrapper.py` - Primary logging path
- `claude/docker_runner.py` - Still accepts optional stream_callback for backward compatibility
- Integration tests - Test docker_runner directly, not affected

### Why This Is Safe

1. **docker-claude-wrapper.py is more robust**:
   - Already production-tested
   - Has fallback storage to `/tmp/agent_result_{task_id}.json`
   - Implements retry logic for transient Redis failures
   - Essential for result persistence with `--rm` containers

2. **Cleaner separation of concerns**:
   - Containers handle their own logging
   - Orchestrator doesn't duplicate container work

3. **No functionality lost**:
   - All events still published to Redis (via wrapper)
   - Result persistence still works
   - Live logs still stream to web UI
   - History still maintained in Redis Stream

## Monitoring

After deploying the fix, monitor:

1. **Elasticsearch duplication rate**: Should drop from 100% to 0%
2. **Web UI user reports**: Should stop complaining about duplicate logs
3. **Redis Stream size**: Should be ~50% smaller (no more duplicates)
4. **Agent execution success rate**: Should remain unchanged

## Questions?

If verification fails or you encounter issues:
1. Check orchestrator logs: `docker-compose logs orchestrator`
2. Check agent container logs: `docker logs <agent-container>`
3. Verify Redis connectivity: `docker-compose exec redis redis-cli ping`
4. Verify Elasticsearch: `curl http://localhost:9200/_cluster/health`

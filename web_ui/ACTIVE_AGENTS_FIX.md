# Active Agents Fix - Hybrid Approach

## Problem
After implementing the pipeline-centric approach, active agents were not showing up because:

1. **Repair cycle agents** run outside of pipeline runs (no `pipeline_run_id`)
2. **Pipeline status timing issue**: Agents can start after a pipeline is marked "completed"
3. **Two agent types exist**:
   - Pipeline-associated agents (tracked in Elasticsearch)
   - Standalone agents (repair cycles, tracked in Redis only)

## Solution: Hybrid Data Source

Modified `/api/active-agents` endpoint to combine **both Elasticsearch and Redis**:

### Query Strategy

1. **Elasticsearch**: Query active + recently completed (last 2 hours) pipeline runs
   - Finds agents with `agent_initialized` events but no completion
   - Handles timing edge cases where agents start after pipeline completes

2. **Redis Fallback**: Query `agent:container:*` keys for standalone agents
   - Catches repair cycles and ad-hoc tasks
   - Filters out duplicates already found in Elasticsearch

### Implementation

```python
# services/observability_server.py:841-966

# 1. Query Elasticsearch for pipeline-associated agents
pipeline_runs_query = {
    "query": {
        "bool": {
            "should": [
                {"term": {"status": "active"}},
                {
                    "bool": {
                        "must": [
                            {"term": {"status": "completed"}},
                            {"range": {"ended_at": {"gte": two_hours_ago}}}
                        ]
                    }
                }
            ]
        }
    }
}

# 2. Query Redis for standalone agents
redis_client = redis.Redis(host='redis', port=6379, decode_responses=True)
agent_keys = redis_client.keys('agent:container:*')

# 3. Merge both sources, avoiding duplicates
```

## Testing

### Backend Endpoint
```bash
curl http://localhost:5001/api/active-agents | jq '.'
```

Expected output when agents are running:
```json
{
  "success": true,
  "count": 3,
  "agents": [
    {
      "agent": "senior_software_engineer",
      "project": "codetoreum",
      "container_name": "claude-agent-...",
      "source": "redis",
      "pipeline_run_id": null,
      ...
    }
  ]
}
```

### Logs
```bash
docker compose logs -f observability-server | grep "Active agents"
```

Expected log format:
```
Active agents endpoint returning 3 agents (0 from pipelines, 3 from Redis)
```

## Frontend Integration

The web UI automatically polls this endpoint every 5 seconds via `useActivePipelineAgents` hook:
- **File**: `web_ui/src/hooks/useActivePipelineAgents.js`
- **Polling interval**: 5000ms
- **Context**: `web_ui/src/contexts/AgentStateContext.jsx`

## Data Flow

```
User opens Web UI
  ↓
useActivePipelineAgents hook mounts
  ↓
Polls /api/active-agents every 5s
  ↓
Backend queries:
  1. Elasticsearch (pipeline-associated agents)
  2. Redis (standalone agents)
  ↓
Merged results returned to UI
  ↓
AgentStateContext updates
  ↓
ActiveAgents component re-renders
```

## Benefits

✅ **Complete Coverage**: Captures both pipeline and standalone agents
✅ **Backward Compatible**: Existing UI code works without changes
✅ **Resilient**: Handles timing edge cases and data inconsistencies
✅ **Debuggable**: `source` field indicates data origin (elasticsearch/redis)
✅ **No Data Loss**: Falls back to Redis when Elasticsearch has gaps

## Known Scenarios

### Scenario 1: Pipeline-Associated Agent
- **Status**: Agent runs within an active pipeline run
- **Data Source**: Elasticsearch
- **Has**: `pipeline_run_id`, `issue_number`, `branch_name`, `board`

### Scenario 2: Standalone Agent (Repair Cycle)
- **Status**: Agent runs for test repair, no pipeline context
- **Data Source**: Redis
- **Has**: `container_name`, `project`, `started_at`
- **Missing**: `pipeline_run_id` (null), limited metadata

### Scenario 3: Late-Starting Agent
- **Status**: Agent starts after pipeline marked "completed"
- **Data Source**: Elasticsearch (caught by 2-hour window)
- **Has**: Full pipeline context

## Future Improvements

1. **Unified Tracking**: Ensure all agents get pipeline_run_ids
2. **Real-time Updates**: Add WebSocket push instead of polling
3. **Agent Metadata**: Enrich Redis data with more context
4. **Cleanup Logic**: Remove completed agent keys from Redis promptly

## Verification Steps

After deploying this fix:

1. Check backend returns agents:
   ```bash
   curl http://localhost:5001/api/active-agents | jq '.count'
   ```

2. Check web UI polling (browser console):
   ```javascript
   // Should see periodic requests to /api/active-agents
   ```

3. Verify Active Agents component shows data:
   - Navigate to http://localhost:3000
   - Check "Active Agents" section
   - Agents should appear with project names and runtime

4. Check logs show proper counts:
   ```bash
   docker compose logs observability-server | grep "Active agents endpoint"
   ```

## Summary

The hybrid approach successfully combines the **reliability of pipeline-centric tracking** with the **completeness of Redis-based detection**, ensuring all active agents are visible in the UI regardless of their execution context.

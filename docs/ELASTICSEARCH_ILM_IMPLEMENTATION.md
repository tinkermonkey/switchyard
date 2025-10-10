# Elasticsearch ILM (Index Lifecycle Management) Implementation

**Date:** October 10, 2025  
**Status:** ✅ Complete

## Overview

Implemented automatic retention policies (ILM) for all time-series Elasticsearch indices in the orchestrator. Indices now automatically delete after their retention period to prevent unbounded storage growth.

## ILM Policies Created

### 1. orchestrator-metrics-policy (90-day retention)
**Applies to:**
- `orchestrator-task-metrics-*` - Task execution metrics
- `orchestrator-quality-metrics-*` - Quality scores

**Lifecycle:**
- **Hot phase** (0-7 days): Daily rollover, priority 100
- **Warm phase** (7-90 days): Lower priority (50)
- **Delete phase** (90+ days): Automatic deletion

**Created by:** `monitoring/metrics.py` on startup

### 2. agent-logs-ilm-policy (90-day retention)
**Applies to:**
- `agent-logs-*` - Legacy agent logs
- `agent-events-*` - Agent lifecycle events
- `claude-streams-*` - Claude streaming logs
- `pipeline-runs-*` - Pipeline execution tracking

**Lifecycle:**
- **Hot phase** (0-7 days): Daily rollover, max 5GB, priority 100
- **Warm phase** (7-90 days): Lower priority (50)
- **Delete phase** (90+ days): Automatic deletion

**Created by:** `services/log_collector.py` on startup

### 3. review-outcomes-lifecycle (12-month retention)
**Applies to:**
- `review-outcomes-*` - Review learning outcomes

**Lifecycle:**
- **Hot phase** (0-30 days): Monthly rollover, max 5GB, priority 100
- **Warm phase** (30-365 days): Lower priority (50)
- **Delete phase** (365+ days): Automatic deletion

**Rationale:** Review outcomes are valuable for long-term learning, kept for 1 year

**Created by:** `services/review_learning_schema.py` on setup

## Files Modified

### 1. monitoring/metrics.py
**Changes:**
- Added `_create_index_templates()` method now creates ILM policy
- Added `ilm.put_lifecycle()` call for `orchestrator-metrics-policy`
- Updated index templates to reference ILM policy in settings
- Added `@timestamp` field to mappings for time-series queries

**Code:**
```python
self.es.ilm.put_lifecycle(
    name="orchestrator-metrics-policy",
    body=ilm_policy
)
```

### 2. services/log_collector.py
**Changes:**
- Added `ilm.put_lifecycle()` call in `setup_elasticsearch()`
- Creates `agent-logs-ilm-policy` on startup
- All log indices now reference this policy

**Code:**
```python
self.es.ilm.put_lifecycle(
    name="agent-logs-ilm-policy",
    body=AGENT_LOGS_ILM_POLICY
)
```

### 3. services/pattern_detection_schema.py
**Changes:**
- Added `index.lifecycle.name` to settings for:
  - `AGENT_LOGS_MAPPING`
  - `AGENT_EVENTS_MAPPING`
  - `CLAUDE_STREAMS_MAPPING`
  - `PIPELINE_RUNS_MAPPING`

**Code:**
```python
"settings": {
    "index": {
        "lifecycle": {
            "name": "agent-logs-ilm-policy"
        }
    }
}
```

### 4. services/review_learning_schema.py
**Changes:**
- Added `REVIEW_OUTCOMES_ILM_POLICY` constant (12-month retention)
- Updated `setup_review_learning_indices()` to create ILM policy
- Review outcomes template already had ILM reference, now policy is actually created

**Code:**
```python
es_client.ilm.put_lifecycle(
    name="review-outcomes-lifecycle",
    body=REVIEW_OUTCOMES_ILM_POLICY
)
```

## Indices WITHOUT ILM (By Design)

The following indices are **not** time-series and don't need automatic deletion:

### Pattern Detection Indices (Persistent State)
- `pattern-occurrences` - Pattern occurrence tracking
- `pattern-github-tracking` - GitHub issue/discussion tracking
- `pattern-llm-analysis` - LLM analysis results
- `pattern-insights` - Aggregated insights
- `pattern-claude-md-changes` - Claude.md change tracking
- `pattern-similarity` - Pattern similarity grouping

**Rationale:** These are operational state, not time-series logs. They grow slowly and have built-in deduplication.

### Review Learning Indices (Manually Managed)
- `review-filters` - Learned review filters (persistent configuration)
- `agent-performance` - Agent performance summaries (periodically updated)

**Rationale:** Small operational indices that are updated in-place, not appended.

## Verification

### Check ILM Policies Exist
```bash
# List all ILM policies
curl http://localhost:9200/_ilm/policy?pretty

# View specific policy
curl http://localhost:9200/_ilm/policy/orchestrator-metrics-policy?pretty
curl http://localhost:9200/_ilm/policy/agent-logs-ilm-policy?pretty
curl http://localhost:9200/_ilm/policy/review-outcomes-lifecycle?pretty
```

### Check Indices Have ILM Applied
```bash
# Check index settings
curl http://localhost:9200/orchestrator-task-metrics-*/_settings?pretty | grep lifecycle
curl http://localhost:9200/agent-events-*/_settings?pretty | grep lifecycle
curl http://localhost:9200/review-outcomes-*/_settings?pretty | grep lifecycle
```

### Monitor ILM Execution
```bash
# See which phase each index is in
curl http://localhost:9200/_cat/indices/orchestrator-*?v&h=index,health,status,pri,rep,docs.count,store.size,creation.date

# Detailed ILM status
curl http://localhost:9200/orchestrator-task-metrics-*/_ilm/explain?pretty
```

### Manual ILM Actions (if needed)
```bash
# Force rollover for testing
curl -X POST "http://localhost:9200/orchestrator-task-metrics-*/_rollover?pretty"

# Manually move to next phase
curl -X POST "http://localhost:9200/orchestrator-task-metrics-2025.10.10/_ilm/move/warm?pretty"

# Retry failed ILM actions
curl -X POST "http://localhost:9200/orchestrator-task-metrics-*/_ilm/retry?pretty"
```

## Retention Periods Summary

| Index Pattern | Retention | Rollover | Rationale |
|--------------|-----------|----------|-----------|
| `orchestrator-task-metrics-*` | 90 days | Daily | Recent performance data |
| `orchestrator-quality-metrics-*` | 90 days | Daily | Recent quality trends |
| `agent-logs-*` | 90 days | Daily/5GB | Legacy logs |
| `agent-events-*` | 90 days | Daily/5GB | Agent execution logs |
| `claude-streams-*` | 90 days | Daily/5GB | Claude streaming logs |
| `pipeline-runs-*` | 90 days | Daily/5GB | Pipeline tracking |
| `review-outcomes-*` | 365 days | Monthly/5GB | Long-term learning data |

## Configuration

### Change Retention Period

Edit the ILM policy in the respective file:

**For metrics (90 days):** `monitoring/metrics.py`
```python
"delete": {
    "min_age": "90d",  # Change to "180d" for 6 months
    "actions": {"delete": {}}
}
```

**For logs (90 days):** `services/pattern_detection_schema.py`
```python
"delete": {
    "min_age": "90d",  # Change to "30d" for 1 month
    "actions": {"delete": {}}
}
```

**For review outcomes (365 days):** `services/review_learning_schema.py`
```python
"delete": {
    "min_age": "365d",  # Change to "730d" for 2 years
    "actions": {"delete": {}}
}
```

After changing, restart the service to recreate the ILM policy.

### Override ILM Policy

To update an existing policy without restart:

```bash
curl -X PUT "http://localhost:9200/_ilm/policy/orchestrator-metrics-policy" -H 'Content-Type: application/json' -d '{
  "policy": {
    "phases": {
      "hot": {
        "actions": {
          "rollover": {
            "max_age": "1d"
          }
        }
      },
      "delete": {
        "min_age": "30d",
        "actions": {
          "delete": {}
        }
      }
    }
  }
}'
```

## Storage Estimation

### Before ILM (Unbounded Growth)
- **Task metrics**: ~1000 tasks/day × 200 bytes = 200KB/day = 73MB/year
- **Quality metrics**: ~3000 metrics/day × 150 bytes = 450KB/day = 164MB/year
- **Agent logs**: ~5000 events/day × 2KB = 10MB/day = 3.65GB/year
- **Claude streams**: ~10000 lines/day × 1KB = 10MB/day = 3.65GB/year
- **Total**: ~20MB/day = **7.3GB/year** (unbounded)

### After ILM (With Retention)
- **Metrics (90 days)**: 20MB/day × 90 = 1.8GB max
- **Logs (90 days)**: 20MB/day × 90 = 1.8GB max
- **Review outcomes (365 days)**: 1MB/day × 365 = 365MB max
- **Total steady state**: **~4GB** (bounded)

**Storage savings**: ILM prevents unbounded growth, caps at ~4GB instead of growing indefinitely.

## Troubleshooting

### ILM Not Deleting Old Indices

1. **Check ILM is enabled:**
   ```bash
   curl http://localhost:9200/_cluster/settings?pretty | grep ilm
   ```

2. **Check ILM status:**
   ```bash
   curl http://localhost:9200/_ilm/status?pretty
   ```

3. **If stopped, start it:**
   ```bash
   curl -X POST "http://localhost:9200/_ilm/start?pretty"
   ```

4. **Check for errors:**
   ```bash
   curl http://localhost:9200/orchestrator-*/_ilm/explain?pretty
   ```

### Indices Not Using ILM Policy

If new indices don't have ILM applied:

1. **Check index template:**
   ```bash
   curl http://localhost:9200/_index_template/orchestrator-task-metrics?pretty
   ```

2. **Verify lifecycle.name is set in template**

3. **Delete and recreate index** (data will be lost):
   ```bash
   curl -X DELETE "http://localhost:9200/orchestrator-task-metrics-2025.10.10"
   # Let it be auto-created on next write
   ```

### Manual Cleanup of Old Indices

If ILM wasn't working and old indices accumulated:

```bash
# List all indices with dates
curl http://localhost:9200/_cat/indices/orchestrator-*?v&s=index

# Delete indices older than 90 days (example for September)
curl -X DELETE "http://localhost:9200/orchestrator-task-metrics-2025.07.*"
curl -X DELETE "http://localhost:9200/agent-events-2025.07.*"
```

## Benefits

1. ✅ **Automatic cleanup** - No manual maintenance needed
2. ✅ **Predictable storage** - Capped at ~4GB total
3. ✅ **Performance** - Smaller indices = faster queries
4. ✅ **Cost savings** - Less storage = lower cloud costs
5. ✅ **Compliance** - Automatic data retention enforcement
6. ✅ **Tiered storage** - Hot data prioritized, warm data deprioritized

## Next Steps After Deployment

1. **Monitor storage:**
   ```bash
   watch -n 300 'curl -s http://localhost:9200/_cat/indices/orchestrator-*?v&s=store.size:desc'
   ```

2. **Verify first deletion** (after 90 days):
   ```bash
   # Check that indices older than 90 days are gone
   curl http://localhost:9200/_cat/indices/orchestrator-*?v
   ```

3. **Alert on ILM failures:**
   Set up monitoring for `_ilm/explain` errors

4. **(Optional) Adjust retention** based on actual needs

## Related Documentation

- [Elasticsearch Metrics Integration](./ELASTICSEARCH_METRICS_INTEGRATION.md)
- [Prometheus Removal](./PROMETHEUS_REMOVAL.md)
- [Pattern Detection](../config/pattern_detection.yaml)

---

**Impact:** High - Prevents unbounded storage growth  
**Risk:** Low - ILM is a standard Elasticsearch feature  
**Testing:** Monitor ILM status after deployment, verify first deletion in 90 days

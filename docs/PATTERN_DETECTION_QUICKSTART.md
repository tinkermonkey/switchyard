# Pattern Detection System - Quick Start Guide

## Overview

The Pattern Detection System monitors agent behavior through live logs, identifies inefficiency patterns, and will propose improvements to CLAUDE.md configuration files.

**Phase 1 Status:** ✅ Complete - Data collection pipeline is ready
**Current Capabilities:**
- Collects all agent events and Claude Code logs from Redis
- Stores in Elasticsearch for 90 days
- Searchable via Kibana web UI
- Ready for pattern detection (Phase 2)

## Quick Start

### 1. Start the System

```bash
# Navigate to orchestrator directory
cd ~/workspace/orchestrator/clauditoreum

# Start all services (includes Elasticsearch, Kibana, and log collector)
docker-compose up -d

# Check all services are running
docker-compose ps

# Expected services:
# - redis
# - orchestrator
# - observability-server
# - web-ui
# - elasticsearch (NEW)
# - kibana (NEW)
# - log-collector (NEW)
```

### 2. Verify Log Collection

```bash
# Check log collector is running and processing events
docker-compose logs -f log-collector

# You should see output like:
# "LogCollector initialized (consumer: log_collector_1728139381)"
# "Elasticsearch is ready"
# "Redis is ready"
# "Indexed 50 events to agent-logs-2025-10-05 (total: 150)"
```

### 3. Access Kibana

1. Open browser to **http://localhost:5601**
2. Wait for Kibana to initialize (30-60 seconds on first start)
3. Click "Explore on my own" if prompted

### 4. Create Index Pattern

1. Navigate to **Management → Stack Management → Index Patterns**
2. Click **"Create index pattern"**
3. Enter pattern: `agent-logs-*`
4. Click **"Next step"**
5. Select time field: `timestamp`
6. Click **"Create index pattern"**

### 5. Explore Agent Logs

1. Navigate to **Analytics → Discover**
2. Select index pattern: `agent-logs-*`
3. Set time range (top-right): "Last 24 hours"
4. Explore logs with filters and search

## Useful Queries

### Find Recent Errors

In Kibana Discover, use KQL (Kibana Query Language):

```
success: false AND timestamp >= now-1d
```

### Find Tool Execution Failures

```
event_category: "tool_result" AND success: false
```

### Find Specific Agent Activity

```
agent_name: "senior-software-engineer" AND project: "context-studio"
```

### Find Git-Related Errors

```
tool_name: "Bash" AND tool_params_text: "git" AND success: false
```

## Command-Line Queries

### Count Total Events

```bash
curl http://localhost:9200/agent-logs-*/_count
```

### Get Recent Errors

```bash
curl -X POST "localhost:9200/agent-logs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "query": {
      "bool": {
        "must": [
          { "term": { "success": false } },
          { "range": { "timestamp": { "gte": "now-1h" } } }
        ]
      }
    },
    "size": 10,
    "sort": [{ "timestamp": "desc" }]
  }'
```

### Count Events by Agent

```bash
curl -X POST "localhost:9200/agent-logs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "aggs": {
      "by_agent": {
        "terms": { "field": "agent_name", "size": 20 }
      }
    }
  }'
```

### Find Most Common Errors

```bash
curl -X POST "localhost:9200/agent-logs-*/_search" \
  -H 'Content-Type: application/json' \
  -d '{
    "size": 0,
    "query": { "term": { "success": false } },
    "aggs": {
      "common_errors": {
        "terms": { "field": "error_message.keyword", "size": 10 }
      }
    }
  }'
```

## Monitoring the System

### Check Log Collector Health

```bash
# View recent logs
docker-compose logs --tail 50 log-collector

# Follow logs in real-time
docker-compose logs -f log-collector
```

### Check Elasticsearch Health

```bash
# Cluster health
curl localhost:9200/_cluster/health?pretty

# Index statistics
curl localhost:9200/_cat/indices/agent-logs-*?v

# Document count per index
curl localhost:9200/_cat/count/agent-logs-*?v
```

### Check Redis Stream Status

```bash
# Enter Redis CLI
docker-compose exec redis redis-cli

# Check stream lengths
> XLEN orchestrator:event_stream
> XLEN orchestrator:claude_logs_stream

# Check consumer group status
> XINFO GROUPS orchestrator:event_stream
> XINFO GROUPS orchestrator:claude_logs_stream
```

## Typical Data Flow

```
1. Agent executes task
   ↓
2. Observability events published to Redis
   - Pub/Sub: orchestrator:agent_events
   - Stream: orchestrator:event_stream (TTL: 2 hours)
   ↓
3. Claude Code logs published to Redis
   - Pub/Sub: orchestrator:claude_stream
   - Stream: orchestrator:claude_logs_stream (TTL: 2 hours)
   ↓
4. Log Collector consumes from both streams
   - Uses consumer groups for reliability
   - Enriches events with metadata
   - Batches for efficiency (50 events or 5 seconds)
   ↓
5. Bulk indexed to Elasticsearch
   - Daily indices: agent-logs-2025-10-05
   - 90-day retention
   - Optimized for search and aggregation
   ↓
6. Queryable via Kibana or Elasticsearch API
   - Real-time search
   - Aggregations and visualizations
   - Ready for pattern detection
```

## Troubleshooting

### Elasticsearch won't start

```bash
# Check if port is already in use
lsof -i :9200

# Check container logs
docker-compose logs elasticsearch

# Common fix: Increase Docker memory limit to 4GB minimum
# Docker Desktop → Settings → Resources → Memory
```

### Kibana shows "Kibana server is not ready yet"

This is normal on first start. Wait 1-2 minutes for Elasticsearch to be fully ready.

```bash
# Check Elasticsearch health
curl localhost:9200/_cluster/health

# Wait for status: "yellow" or "green"
```

### No data in Elasticsearch

```bash
# 1. Check if orchestrator is running and generating events
docker-compose logs orchestrator

# 2. Check if events are in Redis
docker-compose exec redis redis-cli
> XLEN orchestrator:event_stream

# 3. Check log collector is consuming
docker-compose logs log-collector | grep "Indexed"

# 4. Restart log collector if needed
docker-compose restart log-collector
```

### Log collector keeps restarting

```bash
# Check error logs
docker-compose logs log-collector

# Common issues:
# - Elasticsearch not ready: Wait for health check
# - Python import errors: Rebuild container
docker-compose build log-collector
docker-compose up -d log-collector
```

## Configuration

### Adjust Batch Size

Edit `config/pattern_detection.yaml`:

```yaml
elasticsearch:
  batch_size: 100  # Increase for higher throughput
  batch_timeout_seconds: 10.0  # Wait longer before flushing
```

### Adjust Retention

Edit `services/pattern_detection_schema.py`:

```python
AGENT_LOGS_ILM_POLICY = {
    "policy": {
        "phases": {
            "delete": {
                "min_age": "180d",  # Keep for 180 days instead of 90
                ...
            }
        }
    }
}
```

Then restart and recreate indices.

### Adjust Elasticsearch Memory

Edit `docker-compose.yml`:

```yaml
elasticsearch:
  environment:
    - "ES_JAVA_OPTS=-Xms1g -Xmx1g"  # Increase to 1GB
```

## Next Phase: Pattern Detection

Phase 2 will implement:

1. **Rule-Based Detection Engine**
   - Automatically detect patterns defined in `config/patterns/*.yaml`
   - Track pattern occurrences in PostgreSQL
   - Real-time alerting for critical patterns

2. **Pattern Dashboard**
   - Kibana visualizations showing pattern trends
   - Top patterns by frequency and impact
   - Pattern occurrence timeline

3. **Testing Infrastructure**
   - Synthetic pattern injection for testing
   - Pattern detection validation

See `docs/SELF_IMPROVEMENT_PATTERN_DETECTION.md` for full roadmap.

## Useful Resources

- **Elasticsearch Query DSL:** https://www.elastic.co/guide/en/elasticsearch/reference/9.0/query-dsl.html
- **Kibana Discover:** https://www.elastic.co/guide/en/kibana/9.0/discover.html
- **KQL Syntax:** https://www.elastic.co/guide/en/kibana/9.0/kuery-query.html
- **Full Design Doc:** `docs/SELF_IMPROVEMENT_PATTERN_DETECTION.md`
- **Phase 1 Details:** `docs/PATTERN_DETECTION_PHASE1_COMPLETE.md`

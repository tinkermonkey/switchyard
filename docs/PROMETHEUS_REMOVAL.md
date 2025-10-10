# Prometheus Removal & Elasticsearch Integration

**Date:** October 10, 2025  
**Status:** ✅ Complete

## Overview

Removed Prometheus metrics integration and replaced it with Elasticsearch-based metrics storage. Metrics are written to daily Elasticsearch indices with JSON file backup.

## What Was Removed

### Dependencies
- `prometheus-client>=0.17.1` from `requirements.txt`

### Configuration
- `metrics_port: int = 8000` from `config/environment.py`
- `METRICS_PORT=8000` from `.env.example`

### Prometheus Metrics (in-memory)
- `tasks_total` - Counter for total tasks by agent and status
- `task_duration_seconds` - Histogram for task duration by agent
- `active_tasks` - Gauge for currently active tasks by agent
- `pipeline_health` - Gauge for overall pipeline health (0-100)

### HTTP Server
- Port 8000 metrics endpoint (never exposed in docker-compose.yml)

## What Was Added

### Elasticsearch Integration

Metrics are now written to **Elasticsearch indices** (daily rotation):

1. **Task Execution Metrics** (`orchestrator-task-metrics-YYYY.MM.DD`):
   ```json
   {
     "@timestamp": "2025-10-10T12:00:00",
     "agent": "business_analyst",
     "duration": 45.2,
     "success": true
   }
   ```

2. **Quality Metrics** (`orchestrator-quality-metrics-YYYY.MM.DD`):
   ```json
   {
     "@timestamp": "2025-10-10T12:00:00",
     "agent": "business_analyst",
     "metric_name": "completeness",
     "score": 0.95
   }
   ```

### Index Templates

Automatically creates Elasticsearch index templates with proper mappings:
- `@timestamp`: date field for time-series queries
- `agent`: keyword field for aggregations
- `duration`: float field for task metrics
- `success`: boolean field for task metrics
- `metric_name`: keyword field for quality metrics
- `score`: float field for quality metrics

## What Was Preserved

### JSON Metrics Logging (Backup)
Metrics are still written to JSON files as a backup:

1. **Task Execution Metrics** (`orchestrator_data/metrics/task_metrics_<date>.jsonl`)
2. **Quality Metrics** (`orchestrator_data/metrics/quality_metrics_<date>.jsonl`)

### MetricsCollector API
The `MetricsCollector` class interface remains unchanged for backward compatibility:
- `record_task_start(agent)` - No-op (kept for compatibility)
- `record_task_complete(agent, duration, success)` - Writes to Elasticsearch + JSON
- `update_pipeline_health(score)` - No-op (kept for compatibility)
- `record_quality_metric(agent, metric_name, score)` - Writes to Elasticsearch + JSON

## Data Flow

### Before Removal
```
main.py
  └─> MetricsCollector
        ├─> Prometheus (in-memory metrics on port 8000)
        └─> JSON files (quality_metrics_*.jsonl)
```

### After Migration
```
main.py
  └─> MetricsCollector
        ├─> Elasticsearch (orchestrator-*-metrics-* indices)
        └─> JSON files (backup: task_metrics_*.jsonl, quality_metrics_*.jsonl)
```

## Changes Made

### Code Files
1. **requirements.txt** - Removed `prometheus-client` dependency (elasticsearch already present)
2. **monitoring/metrics.py** - Added Elasticsearch integration with fallback to JSON logging
3. **config/environment.py** - Removed `metrics_port` field
4. **main.py** - Removed `port` parameter from `MetricsCollector()` instantiation
5. **.env.example** - Removed `METRICS_PORT` configuration

### Documentation Files
1. **.github/copilot-instructions.md** - Updated metrics section
2. **.claude/CLAUDE.md** - Updated metrics section  
3. **SETUP.md** - Updated health checks and monitoring sections
4. **docs/PROMETHEUS_REMOVAL.md** - This file (updated with Elasticsearch info)

## Querying Metrics in Elasticsearch

### View Recent Task Metrics
```bash
# Get last 100 task executions
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search?size=100&sort=@timestamp:desc" | jq '.hits.hits[]._source'

# Count tasks by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {
        "field": "agent"
      }
    }
  }
}' | jq '.aggregations.by_agent.buckets'

# Average duration by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {
        "field": "agent"
      },
      "aggs": {
        "avg_duration": {
          "avg": {
            "field": "duration"
          }
        }
      }
    }
  }
}' | jq '.aggregations.by_agent.buckets'

# Success rate by agent
curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_agent": {
      "terms": {
        "field": "agent"
      },
      "aggs": {
        "success_rate": {
          "avg": {
            "field": "success"
          }
        }
      }
    }
  }
}' | jq '.aggregations.by_agent.buckets'
```

### View Quality Metrics
```bash
# Get recent quality metrics
curl -s "http://localhost:9200/orchestrator-quality-metrics-*/_search?size=100&sort=@timestamp:desc" | jq '.hits.hits[]._source'

# Average scores by metric type
curl -s "http://localhost:9200/orchestrator-quality-metrics-*/_search" -H 'Content-Type: application/json' -d '{
  "size": 0,
  "aggs": {
    "by_metric": {
      "terms": {
        "field": "metric_name"
      },
      "aggs": {
        "avg_score": {
          "avg": {
            "field": "score"
          }
        }
      }
    }
  }
}' | jq '.aggregations.by_metric.buckets'
```

### Create Kibana Visualizations

1. **Access Kibana**: If you add Kibana to docker-compose.yml, it will auto-discover the indices
2. **Index Patterns**: Create patterns for `orchestrator-task-metrics-*` and `orchestrator-quality-metrics-*`
3. **Dashboards**: Build visualizations for:
   - Task duration trends by agent
   - Success/failure rates
   - Quality score trends
   - Agent execution counts

## Migration Notes

### Elasticsearch Configuration

The orchestrator automatically connects to Elasticsearch using the `ELASTICSEARCH_HOSTS` environment variable (defaults to `http://elasticsearch:9200`).

**Fallback Behavior**: If Elasticsearch is unavailable, metrics will still be written to JSON files without errors.

### Index Management

- **Daily Indices**: Metrics are written to daily indices (e.g., `orchestrator-task-metrics-2025.10.10`)
- **Index Templates**: Automatically created on startup with proper field mappings
- **Retention**: Configure ILM policies in Elasticsearch to manage retention (not configured by default)

### JSON Backup Files

JSON files are still written as a backup in case of Elasticsearch issues:

```bash
# View task execution metrics
cat orchestrator_data/metrics/task_metrics_$(date +%Y-%m-%d).jsonl

# View quality metrics
cat orchestrator_data/metrics/quality_metrics_$(date +%Y-%m-%d).jsonl

# Parse with jq
cat orchestrator_data/metrics/task_metrics_*.jsonl | jq -s 'group_by(.agent) | map({agent: .[0].agent, count: length})'
```

## No Breaking Changes

The `MetricsCollector` API remains identical, so no changes are needed to calling code in `main.py` or agent integration code. Methods that updated Prometheus metrics are now no-ops or write to JSON files.

## Next Steps

After pulling these changes:

1. **Rebuild containers**: `docker compose build`
2. **Restart services**: `docker compose up -d`
3. **Verify Elasticsearch connection**: Check orchestrator logs for "MetricsCollector initialized with Elasticsearch"
4. **Check indices are created**: 
   ```bash
   curl http://localhost:9200/_cat/indices/orchestrator-*?v
   ```
5. **Run a test task**: Verify metrics appear in Elasticsearch:
   ```bash
   curl -s "http://localhost:9200/orchestrator-task-metrics-*/_search?size=1&sort=@timestamp:desc" | jq '.hits.hits[]._source'
   ```
6. **(Optional) Add Kibana**: For visualization and dashboard building

## Files Changed

- `requirements.txt` (removed prometheus-client)
- `monitoring/metrics.py` (added Elasticsearch integration)
- `config/environment.py` (removed metrics_port)
- `main.py` (removed port parameter)
- `.env.example` (removed METRICS_PORT)
- `.github/copilot-instructions.md` (updated docs)
- `.claude/CLAUDE.md` (updated docs)
- `SETUP.md` (updated docs)
- `docs/PROMETHEUS_REMOVAL.md` (this file - updated with Elasticsearch info)

---

**Impact:** Low - Elasticsearch already running, dual-write to JSON for safety  
**Risk:** None - Fallback to JSON if Elasticsearch unavailable  
**Testing:** Verify Elasticsearch indices created and metrics written after task execution

## Benefits of Elasticsearch vs Prometheus

1. **Already Running**: Elasticsearch is already part of the stack (used for pattern detection)
2. **Better Querying**: Full-text search and complex aggregations
3. **Time-Series Data**: Native support for time-series data with daily indices
4. **Visualization**: Easy integration with Kibana for dashboards
5. **No Additional Services**: No need to run separate Prometheus + Grafana containers
6. **Backup**: JSON files still written as backup
7. **Flexible Retention**: ILM policies for automated data lifecycle management
